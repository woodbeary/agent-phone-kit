"""The press-1 gate on your own Twilio number, with register-call as the AI fallback.

Twilio is never called. Requests are signed here the way Twilio signs them (checked
against a vector computed with openssl), and register-call goes to a fake ElevenLabs
API served on 127.0.0.1. All numbers are in the 555-01XX range reserved for fiction.
"""

import json
import socket
from collections.abc import Iterator
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit
from xml.etree import ElementTree as ET

import pytest
from conftest import serve_on_loopback
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.testclient import TestClient

from agent_phone_kit.app import create_app
from agent_phone_kit.calls import LEG_HUMAN, LEG_PARENT, CallLeg, SQLiteCallStore
from agent_phone_kit.settings import Settings
from agent_phone_kit.signature import SIGNATURE_HEADER, sign_payload
from agent_phone_kit.twilio_gate import (
    DISCLOSURE,
    GOODBYE,
    PRESS_ONE,
    PRESS_ONE_AGAIN,
    REGISTER_CALL_PATH,
    TWILIO_SIGNATURE_HEADER,
    UNAVAILABLE,
    ElevenLabsRegisterCall,
    GateSettings,
    RegisterCallError,
    RegisterCallRequest,
    candidate_urls,
    is_valid_twilio_request,
    to_e164,
    twilio_signature,
)

TOKEN = "fake-twilio-auth-token-for-tests"
API_KEY = "fake-elevenlabs-key-for-tests"
PUBLIC = "https://voice.example.com"
INTERNAL = "http://kit.internal:8000"  # the host the app sees behind the proxy
LINE = "+12025550100"
CALLER = "+12025550143"
HUMAN = "+12025550199"
PARENT_SID = "CA00000000000000000000000000000020"
DIAL_SID = "CA00000000000000000000000000000021"
FAKE_TWIML = (
    '<?xml version="1.0" encoding="UTF-8"?><Response><Connect>'
    '<Stream url="wss://register-call.fake.invalid/stream"/></Connect></Response>'
)

# --- signatures ----------------------------------------------------------------------

# Known-answer vector, computed independently with:
#   printf '%s' "${URL}AccountSidAC0...0CallSidCA0...20From+12025550143To+12025550100" \
#     | openssl dgst -sha1 -hmac "$TOKEN" -binary | base64
KAT_TOKEN = "kit-twilio-known-answer-token"
KAT_URL = "https://voice.example.com/twilio/voice?line=+12025550100"
KAT_PARAMS = [
    ("To", LINE),
    ("From", CALLER),
    ("CallSid", PARENT_SID),
    ("AccountSid", "AC00000000000000000000000000000000"),
]
KAT_SIGNATURE = "miVU+ftR+9i06GdwUtvPYePozAg="


def test_known_answer_vector() -> None:
    assert twilio_signature(KAT_TOKEN, KAT_URL, KAT_PARAMS) == KAT_SIGNATURE


def test_fields_are_sorted_and_the_url_is_exact() -> None:
    assert twilio_signature(KAT_TOKEN, KAT_URL, reversed(KAT_PARAMS)) == KAT_SIGNATURE
    assert twilio_signature(KAT_TOKEN, KAT_URL + "&x=1", KAT_PARAMS) != KAT_SIGNATURE
    assert twilio_signature(KAT_TOKEN, KAT_URL.replace("+", "%2B"), KAT_PARAMS) != KAT_SIGNATURE


def test_no_token_is_an_error_not_a_pass() -> None:
    with pytest.raises(ValueError):
        is_valid_twilio_request("", KAT_SIGNATURE, [KAT_URL], KAT_PARAMS)


def test_no_signature_fails() -> None:
    assert not is_valid_twilio_request(KAT_TOKEN, None, [KAT_URL], KAT_PARAMS)
    assert not is_valid_twilio_request(KAT_TOKEN, "", [KAT_URL], KAT_PARAMS)


def test_candidates_cover_proxy_rewrites() -> None:
    received = "http://kit.internal:8000/twilio/voice?line=%2012025550100"
    headers = {"x-forwarded-proto": "https", "x-forwarded-host": "voice.example.com, cdn"}
    candidates = candidate_urls(received, headers, "https://public.example.com/")
    for expected in (
        received,
        "https://voice.example.com/twilio/voice?line=+12025550100",
        "https://voice.example.com/twilio/voice/?line=+12025550100",
        "https://voice.example.com:443/twilio/voice?line=%2B12025550100",
        "https://public.example.com/twilio/voice?line=+12025550100",
        "http://kit.internal/twilio/voice?line=%2012025550100",
    ):
        assert expected in candidates, expected
    assert len(candidates) == len(set(candidates))


# --- phone numbers -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+12025550143", "+12025550143"),
        (" 12025550143", "+12025550143"),  # a "+" that a query string decoded to a space
        ("12025550143", "+12025550143"),
        ("2025550143", "+12025550143"),
        ("(202) 555-0143", "+12025550143"),
        ("+1 202.555.0143", "+12025550143"),
        ("+442079460143", "+442079460143"),
        (" 442079460143", "+442079460143"),
        ("anonymous", None),
        ("client:alice", None),
        ("sip:alice@example.com", None),
        ("+1202555", None),
        ("1202555014", None),
        ("0205550143", None),
        ("٢٠٢٥٥٥٠١٤٣", None),
        ("", None),
        (None, None),
    ],
)
def test_to_e164(raw: str | None, expected: str | None) -> None:
    assert to_e164(raw) == expected


# --- a fake ElevenLabs on loopback ---------------------------------------------------


class FakeElevenLabs:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.body = FAKE_TWIML
        self.base_url = ""

    def app(self) -> FastAPI:
        app = FastAPI()

        @app.post(REGISTER_CALL_PATH)
        async def register(request: Request) -> Response:
            self.requests.append(
                {"api_key": request.headers.get("xi-api-key"), "json": await request.json()}
            )
            if self.status != 200:
                return PlainTextResponse("fake failure", status_code=self.status)
            return Response(self.body, media_type="application/xml")

        return app


@pytest.fixture(scope="module")
def fake_elevenlabs() -> Iterator[FakeElevenLabs]:
    fake = FakeElevenLabs()
    with serve_on_loopback(fake.app()) as base_url:
        fake.base_url = base_url
        yield fake


@pytest.fixture
def elevenlabs(fake_elevenlabs: FakeElevenLabs) -> FakeElevenLabs:
    fake_elevenlabs.requests.clear()
    fake_elevenlabs.status = 200
    fake_elevenlabs.body = FAKE_TWIML
    return fake_elevenlabs


def gate_settings(**overrides: Any) -> GateSettings:
    values: dict[str, Any] = {
        "twilio_auth_token": TOKEN,
        "human_number": HUMAN,
        "agent_id": "agent_synthetic_claims_line",
        "elevenlabs_api_key": API_KEY,
    }
    values.update(overrides)
    return GateSettings(**values)


@pytest.fixture
def make_client(
    phone_kit_settings: Any, elevenlabs: FakeElevenLabs, tmp_path: Any
) -> Iterator[Any]:
    clients: list[ElevenLabsRegisterCall] = []
    stores: list[SQLiteCallStore] = []

    def make(**overrides: Any) -> tuple[TestClient, SQLiteCallStore]:
        register = ElevenLabsRegisterCall(API_KEY, base_url=elevenlabs.base_url, timeout=2)
        store = SQLiteCallStore(tmp_path / f"gate-{len(stores)}.sqlite3")
        clients.append(register)
        stores.append(store)
        settings = replace(phone_kit_settings, gate=gate_settings(**overrides))
        app = create_app(settings, store=store, register_call=register)
        return TestClient(app, base_url=INTERNAL), store

    yield make
    for register in clients:
        register.close()
    for store in stores:
        store.close()


def twilio_post(
    client: TestClient,
    path: str,
    params: dict[str, str],
    *,
    signed_url: str,
    token: str = TOKEN,
    headers: dict[str, str] | None = None,
    signature: str | None = None,
) -> Any:
    """POST as Twilio would, signed for the URL Twilio called (``signed_url``)."""
    if signature is None:
        signature = twilio_signature(token, signed_url, params.items())
    return client.post(
        path,
        content=urlencode(params),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            TWILIO_SIGNATURE_HEADER: signature,
            **(headers or {}),
        },
    )


FORWARDED = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "voice.example.com"}
CALL = {"CallSid": PARENT_SID, "From": CALLER, "To": LINE, "AccountSid": "AC" + "0" * 32}


def twiml(response: Any) -> ET.Element:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/xml")
    return ET.fromstring(response.text)


def action_query(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(url).query))


# --- signature checks through the routes ---------------------------------------------


def test_a_plus_one_number_in_the_query_behind_a_rewritten_host(make_client: Any) -> None:
    client, _ = make_client()
    # Twilio was configured with, and signed, the public URL with a literal "+".
    signed = f"{PUBLIC}/twilio/voice?line=+12025550100"
    response = twilio_post(
        client, "/twilio/voice?line=+12025550100", CALL, signed_url=signed, headers=FORWARDED
    )
    root = twiml(response)
    gather = root.find("Gather")
    assert gather is not None
    assert gather.findtext("Say") == PRESS_ONE
    assert action_query(gather.attrib["action"]) == {"attempt": "1", "line": LINE}


def test_the_configured_public_base_url_works_without_forwarded_headers(make_client: Any) -> None:
    client, _ = make_client(public_base_url=PUBLIC)
    signed = f"{PUBLIC}/twilio/voice?line=+12025550100"
    response = twilio_post(client, "/twilio/voice?line=+12025550100", CALL, signed_url=signed)
    gather = twiml(response).find("Gather")
    assert gather is not None
    assert gather.attrib["action"].startswith(f"{PUBLIC}/twilio/gate?")


def test_a_plus_the_proxy_decoded_to_a_space_is_restored(make_client: Any) -> None:
    client, _ = make_client()
    signed = f"{PUBLIC}/twilio/voice?line=+12025550100"
    response = twilio_post(
        client, "/twilio/voice?line=%2012025550100", CALL, signed_url=signed, headers=FORWARDED
    )
    assert response.status_code == 200


def test_a_plus_the_proxy_unescaped_is_restored(make_client: Any) -> None:
    # The kit's own action URLs encode "+" as %2B, and Twilio signs them that way. A
    # proxy that unescapes %2B hands the app a literal "+", which reads as a space.
    client, _ = make_client(public_base_url=PUBLIC)
    signed = f"{PUBLIC}/twilio/gate?attempt=1&line=%2B12025550100"
    response = twilio_post(
        client,
        "/twilio/gate?attempt=1&line=+12025550100",
        {**CALL, "Digits": "1"},
        signed_url=signed,
    )
    dial = twiml(response).find("Dial")
    assert dial is not None
    assert action_query(dial.attrib["action"]) == {"line": LINE}


def test_a_trailing_slash_the_proxy_dropped(make_client: Any) -> None:
    client, _ = make_client(public_base_url=PUBLIC)
    response = twilio_post(client, "/twilio/voice", CALL, signed_url=f"{PUBLIC}/twilio/voice/")
    assert response.status_code == 200


def test_a_rewritten_host_with_no_way_to_recover_it_fails_closed(make_client: Any) -> None:
    client, _ = make_client()
    signed = f"{PUBLIC}/twilio/voice?line=+12025550100"
    response = twilio_post(client, "/twilio/voice?line=+12025550100", CALL, signed_url=signed)
    assert response.status_code == 403


@pytest.mark.parametrize("path", ["/twilio/voice", "/twilio/gate", "/twilio/dial-result"])
def test_every_route_rejects_unsigned_and_wrongly_signed_requests(
    make_client: Any, elevenlabs: FakeElevenLabs, path: str
) -> None:
    client, _ = make_client(public_base_url=PUBLIC)
    params = {**CALL, "Digits": "1", "DialCallStatus": "no-answer"}
    signed = f"{PUBLIC}{path}"
    assert twilio_post(client, path, params, signed_url=signed, signature="").status_code == 403
    wrong = twilio_post(client, path, params, signed_url=signed, token="another-account-token")
    assert wrong.status_code == 403
    tampered = twilio_signature(TOKEN, signed, {**params, "Digits": "2"}.items())
    response = twilio_post(client, path, params, signed_url=signed, signature=tampered)
    assert response.status_code == 403
    assert elevenlabs.requests == []


# --- the gate ------------------------------------------------------------------------


def test_pressing_1_plays_the_disclosure_and_rings_the_person(make_client: Any) -> None:
    client, _ = make_client(public_base_url=PUBLIC)
    path = f"/twilio/gate?{urlencode({'attempt': '1', 'line': LINE})}"
    response = twilio_post(client, path, {**CALL, "Digits": "1"}, signed_url=PUBLIC + path)
    root = twiml(response)
    assert root.findtext("Say") == DISCLOSURE
    dial = root.find("Dial")
    assert dial is not None
    assert dial.findtext("Number") == HUMAN
    assert dial.attrib["timeout"] == "18"
    assert dial.attrib["answerOnBridge"] == "true"
    assert action_query(dial.attrib["action"]) == {"line": LINE}


def test_no_digit_asks_again_then_hangs_up(make_client: Any) -> None:
    client, _ = make_client(public_base_url=PUBLIC)
    first = "/twilio/gate?attempt=1"
    root = twiml(twilio_post(client, first, {**CALL, "Digits": ""}, signed_url=PUBLIC + first))
    gather = root.find("Gather")
    assert gather is not None
    assert gather.findtext("Say") == PRESS_ONE_AGAIN
    assert action_query(gather.attrib["action"]) == {"attempt": "2"}

    second = "/twilio/gate?attempt=2"
    root = twiml(twilio_post(client, second, {**CALL, "Digits": "9"}, signed_url=PUBLIC + second))
    assert root.findtext("Say") == GOODBYE
    assert root.find("Hangup") is not None
    assert root.find("Dial") is None


def test_an_answered_transfer_ends_without_the_agent(
    make_client: Any, elevenlabs: FakeElevenLabs
) -> None:
    client, _ = make_client(public_base_url=PUBLIC)
    path = "/twilio/dial-result"
    params = {**CALL, "DialCallSid": DIAL_SID, "DialCallStatus": "completed"}
    root = twiml(twilio_post(client, path, params, signed_url=PUBLIC + path))
    assert root.find("Hangup") is not None
    assert elevenlabs.requests == []


def test_no_answer_hands_the_call_to_the_agent_through_register_call(
    make_client: Any, elevenlabs: FakeElevenLabs
) -> None:
    client, _ = make_client()
    path = "/twilio/dial-result?line=+12025550100"
    params = {
        **CALL,
        "From": "(202) 555-0143",
        "DialCallSid": DIAL_SID,
        "DialCallStatus": "no-answer",
    }
    response = twilio_post(client, path, params, signed_url=PUBLIC + path, headers=FORWARDED)
    assert response.status_code == 200
    assert response.text == FAKE_TWIML
    assert response.headers["content-type"].startswith("text/xml")

    [sent] = elevenlabs.requests
    assert sent["api_key"] == API_KEY
    assert sent["json"] == {
        "agent_id": "agent_synthetic_claims_line",
        "from_number": CALLER,
        "to_number": LINE,
        "direction": "inbound",
        "conversation_initiation_client_data": {
            "dynamic_variables": {
                "parent_call_sid": PARENT_SID,
                "human_call_sid": DIAL_SID,
                "human_dial_status": "no-answer",
                "line": LINE,
            }
        },
    }


@pytest.mark.parametrize("status", [500, 401])
def test_a_failed_register_call_still_answers_the_caller(
    make_client: Any, elevenlabs: FakeElevenLabs, status: int
) -> None:
    elevenlabs.status = status
    client, _ = make_client(public_base_url=PUBLIC)
    path = "/twilio/dial-result"
    params = {**CALL, "DialCallSid": DIAL_SID, "DialCallStatus": "busy"}
    root = twiml(twilio_post(client, path, params, signed_url=PUBLIC + path))
    assert root.findtext("Say") == UNAVAILABLE
    assert root.find("Hangup") is not None


def test_register_call_with_nothing_listening_is_an_error(unused_port: int) -> None:
    register = ElevenLabsRegisterCall(API_KEY, base_url=f"http://127.0.0.1:{unused_port}")
    request = RegisterCallRequest("agent", CALLER, LINE)
    with pytest.raises(RegisterCallError):
        register(request)
    register.close()


def test_register_call_that_returns_no_twiml_is_an_error(elevenlabs: FakeElevenLabs) -> None:
    elevenlabs.body = '{"detail": "not twiml"}'
    register = ElevenLabsRegisterCall(API_KEY, base_url=elevenlabs.base_url)
    with pytest.raises(RegisterCallError, match="TwiML"):
        register(RegisterCallRequest("agent", CALLER, LINE))
    register.close()


@pytest.fixture
def unused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_the_legs_the_gate_passes_fold_onto_the_call_record(
    make_client: Any, elevenlabs: FakeElevenLabs, phone_kit_settings: Any, conversation_payload: Any
) -> None:
    client, store = make_client(public_base_url=PUBLIC)
    path = "/twilio/dial-result"
    params = {**CALL, "DialCallSid": DIAL_SID, "DialCallStatus": "no-answer"}
    twilio_post(client, path, params, signed_url=PUBLIC + path)
    [sent] = elevenlabs.requests

    # The post-call webhook for that conversation carries the variables back.
    event = conversation_payload("claim_approved.json")
    event["data"]["conversation_initiation_client_data"] = sent["json"][
        "conversation_initiation_client_data"
    ]
    event["data"]["metadata"]["phone_call"] = {"type": "twilio", "call_sid": PARENT_SID}
    body = json.dumps(event).encode()
    signature = sign_payload(phone_kit_settings.webhook_secret, body)
    assert client.post("/webhooks/post-call", content=body, headers={SIGNATURE_HEADER: signature})
    record = store.get("conv_synthetic_0001")
    assert record is not None
    assert record.legs == (CallLeg(PARENT_SID, LEG_PARENT), CallLeg(DIAL_SID, LEG_HUMAN))


# --- settings ------------------------------------------------------------------------

ENV = {
    "TWILIO_AUTH_TOKEN": TOKEN,
    "GATE_HUMAN_NUMBER": "(202) 555-0199",
    "ELEVENLABS_API_KEY": API_KEY,
    "ELEVENLABS_AGENT_ID": "agent_synthetic_claims_line",
}


def test_gate_settings_from_env() -> None:
    settings = GateSettings.from_env({**ENV, "TWILIO_PUBLIC_BASE_URL": PUBLIC})
    assert settings == gate_settings(public_base_url=PUBLIC)
    assert TOKEN not in repr(settings)
    assert API_KEY not in repr(settings)


def test_no_gate_variables_means_no_gate() -> None:
    assert GateSettings.from_env({}) is None


@pytest.mark.parametrize("missing", sorted(set(ENV) - {"GATE_HUMAN_NUMBER"}))
def test_a_partly_configured_gate_refuses_to_start(missing: str) -> None:
    env = {name: value for name, value in ENV.items() if name != missing}
    with pytest.raises(RuntimeError, match=missing):
        GateSettings.from_env(env)


def test_the_public_base_url_turns_the_gate_on_too() -> None:
    env = {"TWILIO_PUBLIC_BASE_URL": PUBLIC, "ELEVENLABS_API_KEY": API_KEY}
    with pytest.raises(RuntimeError, match="TWILIO_AUTH_TOKEN, GATE_HUMAN_NUMBER"):
        GateSettings.from_env(env)


@pytest.mark.parametrize(
    "exported",
    [
        {"ELEVENLABS_API_KEY": API_KEY},
        {"TWILIO_AUTH_TOKEN": TOKEN},
        {name: value for name, value in ENV.items() if name != "GATE_HUMAN_NUMBER"},
    ],
    ids=["elevenlabs-key", "twilio-token", "all-but-the-human-number"],
)
def test_keys_exported_for_other_tools_do_not_turn_the_gate_on(
    exported: dict[str, str], phone_kit_settings: Any
) -> None:
    # docs/twilio.md has you export ELEVENLABS_API_KEY to import a number with curl.
    assert GateSettings.from_env(exported) is None
    settings = Settings.from_env(
        {
            "TOOL_BEARER_SECRET": phone_kit_settings.tool_secret,
            "ELEVENLABS_WEBHOOK_SECRET": phone_kit_settings.webhook_secret,
            "CALL_STORE_PATH": phone_kit_settings.call_store_path,
            **exported,
        }
    )
    assert settings.gate is None
    assert TestClient(create_app(settings)).post("/twilio/voice").status_code == 404


@pytest.mark.parametrize(
    "overrides",
    [
        {"twilio_auth_token": ""},
        {"human_number": "202-555-0199"},
        {"agent_id": ""},
        {"public_base_url": "voice.example.com"},
        {"ring_seconds": 2},
    ],
)
def test_bad_gate_settings_are_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        gate_settings(**overrides)


def test_the_gate_is_only_mounted_when_configured(phone_kit_settings: Any) -> None:
    client = TestClient(create_app(phone_kit_settings))
    assert client.post("/twilio/voice").status_code == 404


def test_register_call_body_shape() -> None:
    body = RegisterCallRequest("agent", CALLER, LINE, {"parent_call_sid": PARENT_SID}).body()
    assert body["direction"] == "inbound"
    assert body["conversation_initiation_client_data"] == {
        "dynamic_variables": {"parent_call_sid": PARENT_SID}
    }
