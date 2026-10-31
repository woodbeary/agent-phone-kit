"""The example server tool and its bearer-secret auth."""

from typing import Any

import pytest

from agent_phone_kit.app import create_app
from agent_phone_kit.claims import ClaimLookupError, normalize_claim_number

URL = "/tools/claim-status"


@pytest.mark.parametrize(
    "authorization",
    [None, "", "Bearer", "Bearer wrong-token", "Basic dXNlcjpwYXNz", "bearer"],
)
def test_requests_without_the_bearer_secret_are_rejected(
    phone_kit_client: Any, authorization: str | None
) -> None:
    headers = {} if authorization is None else {"Authorization": authorization}
    response = phone_kit_client.post(URL, json={"claim_number": "CLM-1001"}, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_scheme_is_case_insensitive(phone_kit_client: Any, phone_kit_settings: Any) -> None:
    headers = {"Authorization": f"bearer {phone_kit_settings.tool_secret}"}
    response = phone_kit_client.post(URL, json={"claim_number": "CLM-1001"}, headers=headers)
    assert response.status_code == 200


def test_found_claim(phone_kit_client: Any, tool_headers: dict[str, str]) -> None:
    response = phone_kit_client.post(
        URL, json={"claim_number": "CLM-1001", "conversation_id": "conv_x"}, headers=tool_headers
    )
    assert response.status_code == 200
    assert response.json() == {
        "found": True,
        "claim_number": "CLM-1001",
        "status": "approved",
        "next_step": "Payment goes out within five business days.",
        "message": "Claim CLM-1001 is approved. Payment goes out within five business days.",
    }


def test_unknown_claim_is_a_normal_answer_not_an_error(
    phone_kit_client: Any, tool_headers: dict[str, str]
) -> None:
    response = phone_kit_client.post(URL, json={"claim_number": "clm 4242"}, headers=tool_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["found"] is False
    assert body["claim_number"] == "CLM-4242"
    assert body["message"] == "I couldn't find claim CLM-4242."


def test_unparseable_claim_number_asks_again(
    phone_kit_client: Any, tool_headers: dict[str, str]
) -> None:
    response = phone_kit_client.post(URL, json={"claim_number": "my car"}, headers=tool_headers)
    assert response.status_code == 200
    assert response.json()["found"] is False
    assert response.json()["claim_number"] is None
    assert "C L M" in response.json()["message"]


@pytest.mark.parametrize("payload", [{}, {"claim_number": ""}, {"claim_number": "1" * 65}])
def test_invalid_request_body_is_422(
    phone_kit_client: Any, tool_headers: dict[str, str], payload: dict[str, str]
) -> None:
    assert phone_kit_client.post(URL, json=payload, headers=tool_headers).status_code == 422


def test_backend_outage_is_a_503(phone_kit_settings: Any, tool_headers: dict[str, str]) -> None:
    from fastapi.testclient import TestClient

    class DownStore:
        def get(self, claim_number: str) -> None:
            raise ClaimLookupError("timeout")

    client = TestClient(create_app(phone_kit_settings, claims=DownStore()))
    response = client.post(URL, json={"claim_number": "CLM-1001"}, headers=tool_headers)
    assert response.status_code == 503


@pytest.mark.parametrize(
    ("heard", "expected"),
    [
        ("CLM-1001", "CLM-1001"),
        ("clm 1001", "CLM-1001"),
        (" CLM_1001 ", "CLM-1001"),
        ("1001", "CLM-1001"),
        ("#1001.", "CLM-1001"),
        ("CLM-0012345678", "CLM-0012345678"),
        ("CLM-100", None),
        ("CLM-12345678901", None),
        ("ABC-1001", None),
        ("\u0661\u0660\u0660\u0661", None),  # Arabic-Indic digits
        ("", None),
    ],
)
def test_normalize_claim_number(heard: str, expected: str | None) -> None:
    assert normalize_claim_number(heard) == expected


def test_healthz(phone_kit_client: Any) -> None:
    assert phone_kit_client.get("/healthz").json() == {"status": "ok"}
