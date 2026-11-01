"""The example server tool and its bearer-secret auth."""

import asyncio
import logging
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_phone_kit.app import create_app
from agent_phone_kit.auth import bearer_auth
from agent_phone_kit.claims import (
    DEFAULT_BUDGET_SECONDS,
    FALLBACK_MESSAGE,
    SAMPLE_CLAIMS,
    BudgetedLookups,
    Claim,
    ClaimLookupError,
    InMemoryClaimStore,
    claim_status_router,
    normalize_claim_number,
)

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


class SlowStore:
    """A claims backend that hangs until the test lets it go."""

    def __init__(self, delay: float = 10.0) -> None:
        self.delay = delay
        self.release = threading.Event()
        self.calls = 0

    def get(self, claim_number: str) -> Claim | None:
        self.calls += 1
        self.release.wait(self.delay)
        return InMemoryClaimStore(SAMPLE_CLAIMS).get(claim_number)


@pytest.fixture
def slow_store() -> Iterator[SlowStore]:
    store = SlowStore()
    yield store
    store.release.set()


def budgeted_client(settings: Any, store: Any, **overrides: Any) -> TestClient:
    return TestClient(create_app(replace(settings, **overrides), claims=store))


def test_slow_backend_gets_a_sayable_200_within_the_budget(
    phone_kit_settings: Any, tool_headers: dict[str, str], slow_store: SlowStore
) -> None:
    client = budgeted_client(phone_kit_settings, slow_store, tool_budget_seconds=0.2)
    started = time.monotonic()
    response = client.post(URL, json={"claim_number": "CLM-1001"}, headers=tool_headers)
    elapsed = time.monotonic() - started
    assert response.status_code == 200
    assert response.json() == {
        "found": False,
        "claim_number": "CLM-1001",
        "status": None,
        "next_step": None,
        "message": FALLBACK_MESSAGE,
    }
    assert elapsed < 1.5, f"answered after {elapsed:.2f} s; the backend was still running"


def test_backend_inside_the_budget_answers_normally(
    phone_kit_settings: Any, tool_headers: dict[str, str]
) -> None:
    store = SlowStore(delay=0.05)
    client = budgeted_client(phone_kit_settings, store, tool_budget_seconds=2.0)
    response = client.post(URL, json={"claim_number": "CLM-1001"}, headers=tool_headers)
    assert response.json()["status"] == "approved"


def test_the_default_budget_is_two_seconds(phone_kit_settings: Any) -> None:
    assert phone_kit_settings.tool_budget_seconds == DEFAULT_BUDGET_SECONDS == 2.0


@pytest.mark.parametrize("error", [ClaimLookupError("timeout"), KeyError("CLM-1001")])
def test_backend_failure_is_a_sayable_200_not_an_error(
    phone_kit_settings: Any,
    tool_headers: dict[str, str],
    error: Exception,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class DownStore:
        def get(self, claim_number: str) -> None:
            raise error

    client = TestClient(create_app(phone_kit_settings, claims=DownStore()))
    with caplog.at_level(logging.ERROR, logger="agent_phone_kit.claims"):
        body = {"claim_number": "CLM-1001", "conversation_id": "conv_x"}
        response = client.post(URL, json=body, headers=tool_headers)
    assert response.status_code == 200
    assert response.json()["message"] == FALLBACK_MESSAGE
    assert "conv_x" in caplog.text


def test_lookups_past_the_limit_are_not_started(
    tool_headers: dict[str, str], slow_store: SlowStore, phone_kit_settings: Any
) -> None:
    app = FastAPI()
    app.include_router(
        claim_status_router(
            slow_store,
            bearer_auth(phone_kit_settings.tool_secret),
            budget_seconds=0.1,
            lookups=BudgetedLookups(max_in_flight=1),
        )
    )
    client = TestClient(app)
    first = client.post(URL, json={"claim_number": "CLM-1001"}, headers=tool_headers)
    second = client.post(URL, json={"claim_number": "CLM-1002"}, headers=tool_headers)
    assert first.json()["message"] == second.json()["message"] == FALLBACK_MESSAGE
    assert slow_store.calls == 1


def test_a_late_answer_after_the_deadline_is_dropped() -> None:
    async def scenario() -> None:
        lookups = BudgetedLookups()
        gate = threading.Event()
        with pytest.raises(TimeoutError):
            await lookups.run(lambda _: gate.wait(5), "x", timeout=0.05)
        gate.set()
        await asyncio.sleep(0.05)  # the late result arrives and is ignored
        assert await lookups.run(str.upper, "ok", timeout=1) == "OK"

    asyncio.run(scenario())


def test_budget_must_be_positive() -> None:
    with pytest.raises(ValueError):
        claim_status_router(InMemoryClaimStore(SAMPLE_CLAIMS), lambda: None, budget_seconds=0)


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
