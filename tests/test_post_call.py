"""Post-call webhook receiver: signature, freshness, duplicates, handler failures."""

import json
import logging
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_phone_kit.app import create_app
from agent_phone_kit.post_call import CallSummary, RecentEvents, summarize
from agent_phone_kit.signature import SIGNATURE_HEADER, sign_payload

URL = "/webhooks/post-call"


class Recorder:
    """A post-call handler that remembers what it got, and can fail once on demand."""

    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], CallSummary]] = []
        self.fail_next = False

    def __call__(self, event: dict[str, Any], summary: CallSummary) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("database is down")
        self.calls.append((event, summary))


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def phone_kit_app(phone_kit_settings: Any, recorder: Recorder) -> Any:
    # Overrides the plugin's fixture, the same way a project would plug in its own app.
    return create_app(phone_kit_settings, on_post_call=recorder)


@pytest.fixture
def payload(conversation_payload: Any) -> dict[str, Any]:
    return conversation_payload("claim_approved.json")


def test_signed_webhook_is_accepted(
    signed_post_call: Any, recorder: Recorder, payload: dict[str, Any]
) -> None:
    response = signed_post_call(payload)
    assert response.status_code == 200
    assert response.json() == {"status": "received"}
    [(event, summary)] = recorder.calls
    assert event == payload
    assert summary.conversation_id == "conv_synthetic_0001"
    assert summary.tools_called == ("claim_status", "end_call")


def test_redelivery_is_handled_once(
    signed_post_call: Any, recorder: Recorder, payload: dict[str, Any]
) -> None:
    assert signed_post_call(payload).json() == {"status": "received"}
    assert signed_post_call(payload).json() == {"status": "duplicate"}
    assert len(recorder.calls) == 1


def test_other_events_for_the_same_call_are_not_duplicates(
    signed_post_call: Any, recorder: Recorder, payload: dict[str, Any]
) -> None:
    audio = {
        "type": "post_call_audio",
        "event_timestamp": payload["event_timestamp"] + 5,
        "data": {"agent_id": "a", "conversation_id": "conv_synthetic_0001", "full_audio": "AAAA"},
    }
    assert signed_post_call(payload).status_code == 200
    assert signed_post_call(audio).json() == {"status": "received"}
    assert [s.event_type for _, s in recorder.calls] == [
        "post_call_transcription",
        "post_call_audio",
    ]


def test_handler_failure_returns_500_and_the_retry_is_processed(
    signed_post_call: Any, recorder: Recorder, payload: dict[str, Any]
) -> None:
    recorder.fail_next = True
    assert signed_post_call(payload).status_code == 500
    assert signed_post_call(payload).json() == {"status": "received"}
    assert len(recorder.calls) == 1


def test_replayed_old_webhook_is_rejected(
    signed_post_call: Any, recorder: Recorder, payload: dict[str, Any]
) -> None:
    # A real signature, captured 31 minutes ago and sent again.
    response = signed_post_call(payload, timestamp=int(time.time()) - 31 * 60)
    assert response.status_code == 401
    assert response.json() == {"error": "invalid signature"}
    assert recorder.calls == []


def test_wrong_secret_is_rejected(
    signed_post_call: Any, recorder: Recorder, payload: dict[str, Any]
) -> None:
    assert signed_post_call(payload, secret="not-the-webhook-secret").status_code == 401
    assert recorder.calls == []


def test_unsigned_request_is_rejected(phone_kit_client: Any, recorder: Recorder) -> None:
    assert phone_kit_client.post(URL, json={"type": "post_call_transcription"}).status_code == 401
    assert recorder.calls == []


def test_body_changed_after_signing_is_rejected(
    phone_kit_client: Any, phone_kit_settings: Any, recorder: Recorder, payload: dict[str, Any]
) -> None:
    body = json.dumps(payload).encode()
    header = sign_payload(phone_kit_settings.webhook_secret, body)
    tampered = body.replace(b"approved", b"denied")
    response = phone_kit_client.post(URL, content=tampered, headers={SIGNATURE_HEADER: header})
    assert response.status_code == 401
    assert recorder.calls == []


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[1, 2]", b'{"type": "post_call_transcription", "data": {}}', b"\xff\xfe"],
)
def test_signed_but_malformed_body_is_400(
    signed_post_call: Any, recorder: Recorder, body: bytes
) -> None:
    assert signed_post_call(body).status_code == 400
    assert recorder.calls == []


def test_default_handler_logs_a_summary_without_transcript_text(
    phone_kit_settings: Any, payload: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    client = TestClient(create_app(phone_kit_settings))
    body = json.dumps(payload).encode()
    headers = {SIGNATURE_HEADER: sign_payload(phone_kit_settings.webhook_secret, body)}
    with caplog.at_level(logging.INFO, logger="agent_phone_kit.post_call"):
        assert client.post(URL, content=body, headers=headers).status_code == 200
    assert "conv_synthetic_0001" in caplog.text
    assert "checking on a claim" not in caplog.text


def test_summary_of_a_transferred_call(conversation_payload: Any) -> None:
    summary = summarize(conversation_payload("claim_not_found_transfer.json"))
    assert summary == CallSummary(
        event_type="post_call_transcription",
        conversation_id="conv_synthetic_0003",
        agent_id="agent_synthetic_claims_line",
        status="done",
        call_duration_secs=52,
        call_successful="success",
        tools_called=("claim_status", "transfer_to_number"),
        tool_errors=0,
        tool_latency_secs=(),
        transferred=True,
    )


def test_summary_counts_tool_errors_and_latency() -> None:
    event = {
        "type": "post_call_transcription",
        "data": {
            "conversation_id": "c",
            "transcript": [
                {"role": "agent", "tool_calls": [{"tool_name": "claim_status"}]},
                {
                    "role": "agent",
                    "tool_results": [
                        {"tool_name": "claim_status", "is_error": True, "tool_latency_secs": 2.5},
                        {"tool_name": "claim_status", "is_error": False, "tool_latency_secs": 1},
                    ],
                },
            ],
        },
    }
    summary = summarize(event)
    assert summary.tool_errors == 1
    assert summary.tool_latency_secs == (2.5, 1.0)


def test_summary_of_a_call_initiation_failure() -> None:
    event = {
        "type": "call_initiation_failure",
        "event_timestamp": 1767225600,
        "data": {
            "agent_id": "a",
            "conversation_id": "c",
            "failure_reason": "busy",
            "metadata": {"type": "twilio", "body": {}},
        },
    }
    summary = summarize(event)
    assert summary.failure_reason == "busy"
    assert summary.tools_called == ()
    assert summary.call_duration_secs is None


def test_recent_events_forgets_the_oldest_key() -> None:
    recent = RecentEvents(max_size=2)
    assert all(recent.claim(key) for key in ("a", "b", "c"))
    assert recent.claim("a") is True
    assert recent.claim("c") is False
