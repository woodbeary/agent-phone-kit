"""A human-first gate on your own Twilio number, with the agent as the fallback.

Use this instead of ElevenLabs' native Twilio integration when your own Twilio
voice webhook has to run first. The flow, on the number's voice webhook:

1. ``POST /twilio/voice``: "To reach a person, press 1." Callers who never press 1
   (robocalls, mostly) hear goodbye after two tries.
2. ``POST /twilio/gate``: on 1, a disclosure, then ``<Dial>`` to a person.
3. ``POST /twilio/dial-result``: if nobody answered, ElevenLabs' register-call API
   returns TwiML that connects the call to the agent; the kit returns it as is. The
   parent and human call SIDs go along as dynamic variables, so the post-call
   webhook folds all the legs onto one call record.

Every request must carry a valid ``X-Twilio-Signature``: HMAC-SHA1 of the exact URL
Twilio called plus the sorted POST fields, keyed with the account's auth token.
Behind a proxy the URL the app sees is often not the URL Twilio signed, so the
check tries each URL the request could have been signed with: as received, as
forwarded (``X-Forwarded-Proto`` and ``X-Forwarded-Host``), under a configured
public base URL, with a ``+`` restored where a query value decoded to a space,
with and without a trailing slash, and with and without the default port. It
fails closed: there is no switch to skip it, a missing auth token stops the app
from starting, and a missing or wrong signature is a 403.

Phone numbers are normalized to E.164 where they enter, so ``" 12025550143"`` (a
``+`` decoded to a space) and ``"(202) 555-0143"`` both become ``+12025550143``.
"""

import base64
import hashlib
import hmac
import logging
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from agent_phone_kit.calls import HUMAN_CALL_SID_VARIABLE, PARENT_CALL_SID_VARIABLE

logger = logging.getLogger(__name__)

TWILIO_SIGNATURE_HEADER = "X-Twilio-Signature"
REGISTER_CALL_PATH = "/v1/convai/twilio/register-call"
ELEVENLABS_API = "https://api.elevenlabs.io"

AUTH_TOKEN_ENV = "TWILIO_AUTH_TOKEN"  # noqa: S105 (a variable name, not a secret)
PUBLIC_BASE_URL_ENV = "TWILIO_PUBLIC_BASE_URL"
HUMAN_NUMBER_ENV = "GATE_HUMAN_NUMBER"
API_KEY_ENV = "ELEVENLABS_API_KEY"
AGENT_ID_ENV = "ELEVENLABS_AGENT_ID"
_GATE_ENVS = (AUTH_TOKEN_ENV, HUMAN_NUMBER_ENV, API_KEY_ENV, AGENT_ID_ENV)
_GATE_SWITCHES = (HUMAN_NUMBER_ENV, PUBLIC_BASE_URL_ENV)
"""Only the gate reads these, so either one turns it on. The ElevenLabs and Twilio
tools read the other three too, so having them in the environment doesn't."""

PRESS_ONE = "To reach a person, press 1."
PRESS_ONE_AGAIN = "Sorry, I didn't get that. To reach a person, press 1."
DISCLOSURE = (
    "This call may be recorded and transcribed. "
    "If nobody can answer, an AI assistant will take your call."
)
GOODBYE = "Goodbye."
UNAVAILABLE = "Sorry, we can't take your call right now. Please call back later."
ANSWERED = frozenset({"completed", "answered"})


# --- phone numbers -------------------------------------------------------------------

_SEPARATORS = re.compile(r"[\s().\-]")


def restore_plus(value: str) -> str:
    """Put back a ``+`` that a query string decoded to a space: ``" 1202..."`` -> ``"+1202..."``."""
    return "+" + value[1:] if re.match(r" [0-9]", value) else value


def to_e164(raw: str | None) -> str | None:
    """Normalize a phone number to E.164, or None if it isn't one.

    Ten digits are taken as a North American number. ``anonymous``, ``client:...``
    and ``sip:...`` callers are not phone numbers.
    """
    if raw is None:
        return None
    text = _SEPARATORS.sub("", restore_plus(raw))
    if text.startswith("+"):
        digits = text[1:]
        return text if digits.isdigit() and digits.isascii() and 8 <= len(digits) <= 15 else None
    if not (text.isdigit() and text.isascii()):
        return None
    if len(text) == 10 and text[0] in "23456789":
        return "+1" + text
    if len(text) == 11 and text[0] == "1" and text[1] in "23456789":
        return "+" + text
    return None


# --- signatures ----------------------------------------------------------------------


def twilio_signature(auth_token: str, url: str, params: Iterable[tuple[str, str]]) -> str:
    """The ``X-Twilio-Signature`` Twilio sends for a POST to ``url`` with ``params``."""
    payload = url + "".join(key + value for key, value in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def _with_and_without_port(url: str) -> list[str]:
    parts = urlsplit(url)
    default = {"https": 443, "http": 80}.get(parts.scheme)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    bare = urlunsplit(parts._replace(netloc=host))
    if default is None:
        return [url, bare]
    with_port = urlunsplit(parts._replace(netloc=f"{host}:{parts.port or default}"))
    return [url, bare, with_port]


def _query_variants(raw_query: str) -> list[str]:
    pairs = parse_qsl(raw_query, keep_blank_values=True)
    restored = [(key, restore_plus(value)) for key, value in pairs]
    literal_plus = "&".join(
        f"{quote(key, safe='')}={quote(value, safe='+')}" for key, value in restored
    )
    return [raw_query, literal_plus, urlencode(restored)]


def _first(header: str | None) -> str | None:
    value = (header or "").split(",")[0].strip()
    return value or None


def candidate_urls(
    url: str, headers: Mapping[str, str], public_base_url: str | None = None
) -> list[str]:
    """Every URL Twilio could have signed for a request the app received at ``url``."""
    parts = urlsplit(url)
    bases = [f"{parts.scheme}://{parts.netloc}"]
    forwarded_proto = _first(headers.get("x-forwarded-proto"))
    forwarded_host = _first(headers.get("x-forwarded-host"))
    if forwarded_proto or forwarded_host:
        bases.append(f"{forwarded_proto or parts.scheme}://{forwarded_host or parts.netloc}")
    if public_base_url:
        bases.append(public_base_url.rstrip("/"))

    path = parts.path or "/"
    paths = [path, path.rstrip("/") if path.endswith("/") and path != "/" else path + "/"]
    queries = _query_variants(parts.query)

    seen: dict[str, None] = {}
    for base in bases:
        for each_path in paths:
            for query in queries:
                full = base + each_path + (f"?{query}" if query else "")
                for variant in _with_and_without_port(full):
                    seen.setdefault(variant, None)
    return list(seen)


def is_valid_twilio_request(
    auth_token: str,
    signature: str | None,
    candidates: Iterable[str],
    params: Iterable[tuple[str, str]],
) -> bool:
    if not auth_token:
        raise ValueError("Twilio auth token is not configured")
    if not signature:
        return False
    fields = list(params)
    expected = signature.encode()
    return any(
        hmac.compare_digest(twilio_signature(auth_token, url, fields).encode(), expected)
        for url in candidates
    )


# --- register-call -------------------------------------------------------------------


class RegisterCallError(Exception):
    """ElevenLabs didn't return TwiML for the call."""


@dataclass(frozen=True)
class RegisterCallRequest:
    agent_id: str
    from_number: str
    to_number: str
    dynamic_variables: Mapping[str, Any] = field(default_factory=dict)

    def body(self) -> dict[str, Any]:
        """The JSON body of ``POST /v1/convai/twilio/register-call``."""
        return {
            "agent_id": self.agent_id,
            "from_number": self.from_number,
            "to_number": self.to_number,
            "direction": "inbound",
            "conversation_initiation_client_data": {
                "dynamic_variables": dict(self.dynamic_variables)
            },
        }


class RegisterCall(Protocol):
    def __call__(self, request: RegisterCallRequest) -> str:
        """Return TwiML that connects the call to the agent, or raise RegisterCallError."""
        ...


class ElevenLabsRegisterCall:
    """Calls ElevenLabs' register-call API. ``base_url`` is there for tests and proxies."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = ELEVENLABS_API,
        timeout: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("ElevenLabs API key is not configured")
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={"xi-api-key": api_key},
            transport=transport,
        )

    def __call__(self, request: RegisterCallRequest) -> str:
        try:
            response = self._client.post(REGISTER_CALL_PATH, json=request.body())
        except httpx.HTTPError as exc:
            raise RegisterCallError(f"register-call failed: {exc}") from exc
        if response.status_code >= 300:
            raise RegisterCallError(f"register-call answered HTTP {response.status_code}")
        twiml = response.text
        if "<Response" not in twiml:
            raise RegisterCallError("register-call did not return TwiML")
        return twiml

    def close(self) -> None:
        self._client.close()


# --- settings ------------------------------------------------------------------------


@dataclass(frozen=True)
class GateSettings:
    twilio_auth_token: str = field(repr=False)
    human_number: str
    agent_id: str
    elevenlabs_api_key: str = field(repr=False)
    public_base_url: str | None = None
    ring_seconds: int = 18
    attempts: int = 2

    def __post_init__(self) -> None:
        if not self.twilio_auth_token:
            raise ValueError("the gate needs the Twilio auth token to check signatures")
        if to_e164(self.human_number) != self.human_number:
            raise ValueError("human_number must be an E.164 number, like +12025550100")
        if not self.agent_id:
            raise ValueError("the gate needs the ElevenLabs agent ID")
        if self.public_base_url and not re.match(r"https?://[^/?#]+", self.public_base_url):
            raise ValueError("public_base_url must look like https://voice.example.com")
        if self.ring_seconds < 5 or self.attempts < 1:
            raise ValueError("ring_seconds must be at least 5 and attempts at least 1")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "GateSettings | None":
        """The gate's settings, or None when the gate isn't turned on.

        ``GATE_HUMAN_NUMBER`` (or ``TWILIO_PUBLIC_BASE_URL``) turns it on, and then all
        four of its variables must be set: some but not all is an error, not a gate that
        skips checks. An ``ELEVENLABS_API_KEY`` or ``TWILIO_AUTH_TOKEN`` exported for
        other tools doesn't turn it on.
        """
        env = os.environ if environ is None else environ
        if not any(env.get(name) for name in _GATE_SWITCHES):
            return None
        missing = [name for name in _GATE_ENVS if not env.get(name)]
        if missing:
            raise RuntimeError(f"the Twilio gate also needs: {', '.join(missing)}")
        return cls(
            twilio_auth_token=env[AUTH_TOKEN_ENV],
            human_number=to_e164(env[HUMAN_NUMBER_ENV]) or env[HUMAN_NUMBER_ENV],
            agent_id=env[AGENT_ID_ENV],
            elevenlabs_api_key=env[API_KEY_ENV],
            public_base_url=env.get(PUBLIC_BASE_URL_ENV) or None,
        )


# --- the routes ----------------------------------------------------------------------


def _twiml(*children: ET.Element) -> Response:
    root = ET.Element("Response")
    root.extend(children)
    body = ET.tostring(root, encoding="unicode", xml_declaration=True)
    return Response(body, media_type="text/xml")


def _element(tag: str, text: str | None = None, **attributes: str) -> ET.Element:
    element = ET.Element(tag, attributes)
    element.text = text
    return element


def gate_router(settings: GateSettings, register_call: RegisterCall) -> APIRouter:
    router = APIRouter(prefix="/twilio", tags=["twilio"])

    def action(path: str, **query: str) -> str:
        url = (settings.public_base_url or "").rstrip("/") + "/twilio/" + path
        return url + (f"?{urlencode(query)}" if query else "")

    async def verified_form(request: Request) -> dict[str, str] | None:
        body = (await request.body()).decode("utf-8", errors="replace")
        params = parse_qsl(body, keep_blank_values=True)
        candidates = candidate_urls(str(request.url), request.headers, settings.public_base_url)
        signature = request.headers.get(TWILIO_SIGNATURE_HEADER)
        if not is_valid_twilio_request(settings.twilio_auth_token, signature, candidates, params):
            logger.warning(
                "rejected Twilio webhook %s: %s",
                request.url.path,
                "no signature" if not signature else "signature matches no candidate URL",
            )
            return None
        return dict(params)

    def forbidden() -> Response:
        return PlainTextResponse("invalid Twilio signature", status_code=403)

    def line_of(request: Request) -> str | None:
        return to_e164(request.query_params.get("line"))

    def press_one(prompt: str, attempt: int, line: str | None) -> Response:
        query = {"attempt": str(attempt), **({"line": line} if line else {})}
        target = action("gate", **query)
        gather = _element(
            "Gather", input="dtmf", numDigits="1", timeout="5", action=target, method="POST"
        )
        gather.append(_element("Say", prompt))
        return _twiml(gather, _element("Redirect", target, method="POST"))

    @router.post("/voice")
    async def voice(request: Request) -> Response:
        if await verified_form(request) is None:
            return forbidden()
        return press_one(PRESS_ONE, 1, line_of(request))

    @router.post("/gate")
    async def gate(request: Request) -> Response:
        form = await verified_form(request)
        if form is None:
            return forbidden()
        line = line_of(request)
        try:
            attempt = int(request.query_params.get("attempt", "1"))
        except ValueError:
            attempt = settings.attempts
        if form.get("Digits", "").strip() != "1":
            if attempt >= settings.attempts:
                return _twiml(_element("Say", GOODBYE), _element("Hangup"))
            return press_one(PRESS_ONE_AGAIN, attempt + 1, line)
        dial = _element(
            "Dial",
            action=action("dial-result", **({"line": line} if line else {})),
            method="POST",
            timeout=str(settings.ring_seconds),
            answerOnBridge="true",
        )
        dial.append(_element("Number", settings.human_number))
        return _twiml(_element("Say", DISCLOSURE), dial)

    @router.post("/dial-result")
    async def dial_result(request: Request) -> Response:
        form = await verified_form(request)
        if form is None:
            return forbidden()
        status = form.get("DialCallStatus", "").lower()
        if status in ANSWERED:
            return _twiml(_element("Hangup"))
        caller = form.get("From", "")
        called = form.get("To", "")
        variables = {
            PARENT_CALL_SID_VARIABLE: form.get("CallSid") or None,
            HUMAN_CALL_SID_VARIABLE: form.get("DialCallSid") or None,
            "human_dial_status": status or None,
            "line": line_of(request),
        }
        call = RegisterCallRequest(
            agent_id=settings.agent_id,
            from_number=to_e164(caller) or caller,
            to_number=to_e164(called) or called,
            dynamic_variables={k: v for k, v in variables.items() if v is not None},
        )
        try:
            twiml = await run_in_threadpool(register_call, call)
        except Exception:
            logger.exception("register-call failed for %s", form.get("CallSid"))
            return _twiml(_element("Say", UNAVAILABLE), _element("Hangup"))
        return Response(twiml, media_type="text/xml")

    return router
