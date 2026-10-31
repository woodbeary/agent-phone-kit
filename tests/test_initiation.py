"""Conversation initiation webhook (inbound Twilio and SIP calls)."""

from typing import Any

from fastapi.testclient import TestClient

from agent_phone_kit.app import create_app

URL = "/webhooks/conversation-init"

# Fields ElevenLabs sends, per its personalization docs. Numbers are fictional (555-01XX).
REQUEST = {
    "caller_id": "+12025550143",
    "called_number": "+12025550100",
    "agent_id": "agent_synthetic_claims_line",
    "call_sid": "CA00000000000000000000000000000000",
    "conversation_id": "conv_synthetic_init",
}


def test_requires_the_bearer_secret(phone_kit_client: Any) -> None:
    assert phone_kit_client.post(URL, json=REQUEST).status_code == 401


def test_known_caller(phone_kit_client: Any, tool_headers: dict[str, str]) -> None:
    response = phone_kit_client.post(URL, json=REQUEST, headers=tool_headers)
    assert response.status_code == 200
    assert response.json() == {
        "type": "conversation_initiation_client_data",
        "dynamic_variables": {"caller_on_file": True},
    }


def test_unknown_or_missing_caller(phone_kit_client: Any, tool_headers: dict[str, str]) -> None:
    for request in ({**REQUEST, "caller_id": "+12025550199"}, {"agent_id": "a"}, {}):
        response = phone_kit_client.post(URL, json=request, headers=tool_headers)
        assert response.status_code == 200
        assert response.json()["dynamic_variables"] == {"caller_on_file": False}


def test_sip_extras_are_accepted(phone_kit_client: Any, tool_headers: dict[str, str]) -> None:
    request = {**REQUEST, "call_id": "sip-call-id", "sip_headers": {"X-Campaign-ID": "c1"}}
    assert phone_kit_client.post(URL, json=request, headers=tool_headers).status_code == 200


def test_broken_lookup_still_lets_the_call_start(
    phone_kit_settings: Any, tool_headers: dict[str, str]
) -> None:
    def lookup(caller_id: str) -> bool:
        raise ConnectionError("directory is down")

    client = TestClient(create_app(phone_kit_settings, is_known_caller=lookup))
    response = client.post(URL, json=REQUEST, headers=tool_headers)
    assert response.status_code == 200
    assert response.json()["dynamic_variables"] == {"caller_on_file": False}
