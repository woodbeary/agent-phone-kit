"""The example ElevenLabs configs stay in step with the app."""

import json
from pathlib import Path
from typing import Any

import pytest

from agent_phone_kit.app import create_app
from agent_phone_kit.claims import DEFAULT_BUDGET_SECONDS, ClaimStatusRequest

EXAMPLES = Path(__file__).parent.parent / "examples" / "elevenlabs"


def load(name: str) -> dict[str, Any]:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def test_tool_config_points_at_a_real_route(phone_kit_settings: Any) -> None:
    schema = load("claim_status_tool.json")["api_schema"]
    path = schema["url"].removeprefix("https://YOUR-HOST")
    paths = create_app(phone_kit_settings).openapi()["paths"]
    assert schema["method"].lower() in paths[path]


def test_tool_body_matches_the_request_model() -> None:
    body = load("claim_status_tool.json")["api_schema"]["request_body_schema"]
    assert set(body["properties"]) == set(ClaimStatusRequest.model_fields)
    assert body["properties"]["conversation_id"]["dynamic_variable"] == "system__conversation_id"


def test_tool_timeout_leaves_room_for_the_kit_budget() -> None:
    # ElevenLabs' response_timeout_secs can't go below 5 s, so the kit enforces the 2 s
    # budget itself; the platform timeout is the backstop.
    tool = load("claim_status_tool.json")
    assert tool["response_timeout_secs"] == 5
    assert tool["response_timeout_secs"] > DEFAULT_BUDGET_SECONDS
    assert tool["pre_tool_speech"] == "auto"


def test_tool_auth_comes_from_a_workspace_secret_not_a_literal() -> None:
    headers = load("claim_status_tool.json")["api_schema"]["request_headers"]
    assert headers == {"Authorization": {"secret_id": "YOUR_WORKSPACE_SECRET_ID"}}


@pytest.mark.parametrize("name", ["twilio_phone_number.json", "sip_trunk_phone_number.json"])
def test_phone_number_examples_hold_only_placeholders(name: str) -> None:
    def strings(value: Any) -> list[str]:
        if isinstance(value, dict):
            return [s for v in value.values() for s in strings(v)]
        if isinstance(value, list):
            return [s for v in value for s in strings(v)]
        return [value] if isinstance(value, str) else []

    config = load(name)
    assert config["phone_number"] == "+1XXXXXXXXXX"
    secrets_like = [s for s in strings(config) if "SECRET" in s or "PASSWORD" in s]
    assert secrets_like
    assert all(s.startswith("YOUR_") for s in secrets_like)


def test_sip_example_uses_encrypted_transport_and_supported_codecs() -> None:
    outbound = load("sip_trunk_phone_number.json")["outbound_trunk_config"]
    assert outbound["transport"] == "tls"
    assert outbound["media_encryption"] == "required"
    assert set(outbound["enabled_codecs"]) <= {"G722/8000", "PCMU/8000", "PCMA/8000"}
