"""Cross-check the verifier against ElevenLabs' own Python SDK.

Skipped unless the ``elevenlabs`` package is installed; CI's ``sdk-conformance``
job installs the latest release, so a change to the SDK's scheme shows up there.
"""

import json
import time
from pathlib import Path

import pytest

elevenlabs_client = pytest.importorskip("elevenlabs.client")
from elevenlabs.errors import BadRequestError  # noqa: E402

from agent_phone_kit.signature import (  # noqa: E402
    MAX_FUTURE_SKEW_SECONDS,
    SignatureError,
    compute_signature,
    verify_signature,
)

KEY = "sdk-conformance-key"
BODY = '{"type":"post_call_transcription","data":{"conversation_id":"conv_sdk"}}'
UNICODE_BODY = '{"type":"post_call_transcription","data":{"conversation_id":"c","note":"¿Dónde?"}}'


def header(body: str, *, age: int = 0, key: str = KEY) -> str:
    t = int(time.time()) - age
    return f"t={t},v0={compute_signature(key, t, body.encode())}"


def sdk_accepts(body: str, sig_header: str) -> bool:
    client = elevenlabs_client.ElevenLabs(api_key="unused-offline")
    try:
        client.webhooks.construct_event(rawBody=body, sig_header=sig_header, secret=KEY)
    except BadRequestError:
        return False
    return True


def kit_accepts(body: str, sig_header: str) -> bool:
    try:
        verify_signature(body.encode(), sig_header, KEY)
    except SignatureError:
        return False
    return True


# name: (build the request, should it be accepted?)
CASES = {
    "valid": (lambda: (BODY, header(BODY)), True),
    "valid, non-ASCII body": (lambda: (UNICODE_BODY, header(UNICODE_BODY)), True),
    "29 minutes old": (lambda: (BODY, header(BODY, age=29 * 60)), True),
    "31 minutes old": (lambda: (BODY, header(BODY, age=31 * 60)), False),
    "body changed": (lambda: (BODY.replace("conv_sdk", "conv_x"), header(BODY)), False),
    "wrong key": (lambda: (BODY, header(BODY, key="other-key")), False),
    "no signature": (lambda: (BODY, header(BODY).split(",")[0]), False),
    "no timestamp": (lambda: (BODY, header(BODY).split(",")[1]), False),
}


@pytest.mark.parametrize("case", CASES)
def test_kit_and_sdk_agree(case: str) -> None:
    build, accepted = CASES[case]
    body, sig_header = build()
    assert sdk_accepts(body, sig_header) is accepted
    assert kit_accepts(body, sig_header) is accepted


def test_documented_difference_far_future_timestamps() -> None:
    # The SDK accepts any future timestamp; the kit allows five minutes of skew.
    future = int(time.time()) + MAX_FUTURE_SKEW_SECONDS + 60
    sig_header = f"t={future},v0={compute_signature(KEY, future, BODY.encode())}"
    assert sdk_accepts(BODY, sig_header)
    assert not kit_accepts(BODY, sig_header)


EXAMPLES = Path(__file__).parent.parent / "examples" / "elevenlabs"


def example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def test_tool_example_parses_as_an_sdk_webhook_tool() -> None:
    from elevenlabs.types import WebhookToolConfigInput

    config = example("claim_status_tool.json")
    assert config.pop("type") == "webhook"
    tool = WebhookToolConfigInput(**config)
    assert not tool.model_extra
    assert not tool.api_schema.model_extra


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("twilio_phone_number.json", "PhoneNumbersCreateRequestBody_Twilio"),
        ("sip_trunk_phone_number.json", "PhoneNumbersCreateRequestBody_SipTrunk"),
    ],
)
def test_phone_number_examples_parse_as_sdk_requests(name: str, model: str) -> None:
    from elevenlabs.conversational_ai.phone_numbers.types import phone_numbers_create_request_body

    request = getattr(phone_numbers_create_request_body, model)(**example(name))
    assert not request.model_extra
    for part in ("inbound_trunk_config", "outbound_trunk_config"):
        if getattr(request, part, None) is not None:
            assert not getattr(request, part).model_extra


def test_register_call_body_matches_the_sdk() -> None:
    import inspect
    import typing

    from elevenlabs.conversational_ai.twilio.client import TwilioClient
    from elevenlabs.types import ConversationInitiationClientDataRequestInput
    from elevenlabs.types.telephony_direction import TelephonyDirection

    from agent_phone_kit.twilio_gate import RegisterCallRequest

    body = RegisterCallRequest(
        "agent_synthetic", "+12025550143", "+12025550100", {"parent_call_sid": "CA0"}
    ).body()
    params = set(inspect.signature(TwilioClient.register_call).parameters)
    assert set(body) == params - {"self", "request_options"}
    assert body["direction"] in typing.get_args(typing.get_args(TelephonyDirection)[0])
    data = ConversationInitiationClientDataRequestInput(
        **body["conversation_initiation_client_data"]
    )
    assert not data.model_extra


def test_agent_example_parses_as_sdk_agent_settings() -> None:
    from elevenlabs.types import AgentPlatformSettingsRequestModel, ConversationalConfig

    agent = example("agent.json")
    assert set(agent) == {"name", "conversation_config", "platform_settings"}
    config = ConversationalConfig(**agent["conversation_config"])
    assert not config.model_extra
    for part in ("agent", "asr", "tts"):
        assert not getattr(config, part).model_extra, part
    platform = AgentPlatformSettingsRequestModel(**agent["platform_settings"])
    assert not platform.model_extra
    assert platform.privacy is not None
    assert not platform.privacy.model_extra
