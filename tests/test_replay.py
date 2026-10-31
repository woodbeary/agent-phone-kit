"""Replaying recorded conversations against the tools."""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_phone_kit.app import create_app
from agent_phone_kit.claims import SAMPLE_CLAIMS, Claim, InMemoryClaimStore
from agent_phone_kit.replay import load_conversation, recorded_tool_calls, replay

CONVERSATIONS = Path(__file__).parent / "conversations"


def test_every_recorded_conversation_still_replays(
    recorded_conversation: Path, replay_conversation: Any
) -> None:
    # One case per file in tests/conversations (the plugin's ini option in pyproject.toml).
    report = replay_conversation(recorded_conversation)
    assert report.calls, "each fixture should contain at least one webhook tool call"


def test_conversations_are_collected_from_the_ini_option(request: pytest.FixtureRequest) -> None:
    configured = request.config.getini("phone_kit_conversations")
    assert [Path(p).resolve() for p in configured] == [CONVERSATIONS.resolve()]


def test_a_changed_tool_answer_fails_the_replay_with_a_diff(
    phone_kit_settings: Any, tool_headers: dict[str, str]
) -> None:
    # Someone changes what CLM-1001 says; the recorded call no longer matches.
    changed = [
        Claim("CLM-1001", "denied", "A letter explains why.") if c.number == "CLM-1001" else c
        for c in SAMPLE_CLAIMS
    ]
    client = TestClient(create_app(phone_kit_settings, claims=InMemoryClaimStore(changed)))
    report = replay(client, CONVERSATIONS / "claim_approved.json", headers=tool_headers)
    assert not report.ok
    explanation = report.explain()
    assert "claim_approved.json: 1 of 1 tool call(s) differ" in explanation
    assert "status: expected 'approved', got 'denied'" in explanation
    assert "claim_status [req_0001_a] POST /tools/claim-status" in explanation


def test_ignored_fields_are_not_compared(
    phone_kit_settings: Any, tool_headers: dict[str, str]
) -> None:
    changed = [Claim("CLM-1001", "approved", "Payment goes out tomorrow.")]
    client = TestClient(create_app(phone_kit_settings, claims=InMemoryClaimStore(changed)))
    source = CONVERSATIONS / "claim_approved.json"
    assert not replay(client, source, headers=tool_headers).ok
    report = replay(client, source, headers=tool_headers, ignore_fields={"next_step", "message"})
    assert report.ok, report.explain()


def test_an_error_where_success_was_recorded_is_a_mismatch(phone_kit_client: Any) -> None:
    report = replay(phone_kit_client, CONVERSATIONS / "claim_approved.json")  # no auth header
    assert not report.ok
    assert "expected success, got HTTP 401" in report.explain()


def test_a_recorded_error_must_still_be_an_error(
    phone_kit_client: Any, tool_headers: dict[str, str]
) -> None:
    conversation = load_conversation(CONVERSATIONS / "claim_approved.json")
    for turn in conversation["transcript"]:
        for result in turn.get("tool_results") or []:
            result["is_error"] = True
    report = replay(phone_kit_client, conversation, headers=tool_headers)
    assert "expected an error, got HTTP 200" in report.explain()
    assert report.source == "conv_synthetic_0001"


def test_only_webhook_tools_are_replayed(conversation_payload: Any) -> None:
    calls = recorded_tool_calls(
        load_conversation(conversation_payload("claim_not_found_transfer.json"))
    )
    assert [(c.tool_name, c.method, c.path) for c in calls] == [
        ("claim_status", "POST", "/tools/claim-status")
    ]
    assert calls[0].result_is_error is False
    assert '"found":false' in calls[0].result


def test_path_and_query_params_are_rebuilt() -> None:
    conversation = {
        "transcript": [
            {
                "role": "agent",
                "tool_calls": [
                    {
                        "request_id": "r1",
                        "tool_name": "get_policy",
                        "tool_details": {
                            "type": "webhook",
                            "method": "get",
                            "url": "https://tools.example.com/policies/{policy_id}?verbose=1",
                            "path_params": {"policy_id": "P 7/1"},
                            "query_params": {"fields": "status"},
                            "body": None,
                        },
                    }
                ],
            }
        ]
    }
    [call] = recorded_tool_calls(conversation)
    assert call.method == "GET"
    assert call.path == "/policies/P%207%2F1"
    assert call.query == {"verbose": "1", "fields": "status"}
    assert call.result is None


def test_load_conversation_accepts_the_webhook_body_or_its_data(conversation_payload: Any) -> None:
    payload = conversation_payload("claim_approved.json")
    assert load_conversation(payload) == payload["data"]
    assert load_conversation(payload["data"]) == payload["data"]


@pytest.mark.parametrize("bad", [{}, {"data": {}}, {"transcript": "text"}])
def test_load_conversation_rejects_payloads_without_a_transcript(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="no transcript"):
        load_conversation(bad)
