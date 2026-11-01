"""Post-call webhook receiver.

ElevenLabs POSTs here after a call ends (``post_call_transcription``,
``post_call_audio``, ``call_initiation_failure``). The receiver:

1. verifies the ``ElevenLabs-Signature`` HMAC over the raw body and rejects
   stale timestamps (401, which ElevenLabs does not retry);
2. drops repeat deliveries of the same event (a retry carries the identical payload);
3. hands the event and a ``CallSummary`` to your handler. If the handler raises,
   it answers 500 and forgets the event, so a retry (when enabled on the webhook)
   is processed again.
"""

import json
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from agent_phone_kit.signature import SIGNATURE_HEADER, SignatureError, verify_signature
from agent_phone_kit.substantive import has_substantive_caller_speech

logger = logging.getLogger(__name__)

TRANSFER_TOOLS = frozenset({"transfer_to_number", "transfer_to_agent"})


@dataclass(frozen=True)
class CallSummary:
    """Facts about one call, safe to log: no transcript text, no caller details."""

    event_type: str
    conversation_id: str
    agent_id: str | None = None
    status: str | None = None
    call_duration_secs: int | None = None
    call_successful: str | None = None
    tools_called: tuple[str, ...] = ()
    tool_errors: int = 0
    tool_latency_secs: tuple[float, ...] = ()
    transferred: bool = False
    failure_reason: str | None = None
    substantive: bool | None = None
    """Did the caller say anything beyond "hello?" or "test"? None when the event has no
    transcript (``post_call_audio``, ``call_initiation_failure``)."""


PostCallHandler = Callable[[dict[str, Any], CallSummary], None]


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def summarize(event: Mapping[str, Any]) -> CallSummary:
    data = _as_dict(event.get("data"))
    transcript = data.get("transcript")
    turns = [_as_dict(turn) for turn in transcript or []]
    calls = [_as_dict(c) for turn in turns for c in turn.get("tool_calls") or []]
    results = [_as_dict(r) for turn in turns for r in turn.get("tool_results") or []]
    names = tuple(str(call.get("tool_name", "")) for call in calls)
    metadata = _as_dict(data.get("metadata"))
    return CallSummary(
        event_type=str(event.get("type")),
        conversation_id=str(data.get("conversation_id")),
        agent_id=data.get("agent_id"),
        status=data.get("status"),
        call_duration_secs=metadata.get("call_duration_secs"),
        call_successful=_as_dict(data.get("analysis")).get("call_successful"),
        tools_called=names,
        tool_errors=sum(1 for result in results if result.get("is_error")),
        tool_latency_secs=tuple(
            float(r["tool_latency_secs"])
            for r in results
            if isinstance(r.get("tool_latency_secs"), int | float)
        ),
        transferred=any(name in TRANSFER_TOOLS for name in names),
        failure_reason=data.get("failure_reason"),
        substantive=(
            has_substantive_caller_speech(transcript) if isinstance(transcript, list) else None
        ),
    )


def log_summary(event: dict[str, Any], summary: CallSummary) -> None:
    """Default handler: one JSON log line per event."""
    logger.info("post-call %s", json.dumps(asdict(summary), sort_keys=True))


class RecentEvents:
    """Remembers recent deliveries so each event is handled once per process.

    Behind more than one process, replace this with a shared store, for example
    a unique index on (type, conversation_id, event_timestamp).
    """

    def __init__(self, max_size: int = 10_000) -> None:
        self._max_size = max_size
        self._keys: OrderedDict[Hashable, None] = OrderedDict()
        self._lock = threading.Lock()

    def claim(self, key: Hashable) -> bool:
        """Mark ``key`` as handled. Returns False if it already was."""
        with self._lock:
            if key in self._keys:
                return False
            self._keys[key] = None
            if len(self._keys) > self._max_size:
                self._keys.popitem(last=False)
            return True

    def release(self, key: Hashable) -> None:
        """Forget ``key`` so the next delivery of it is handled."""
        with self._lock:
            self._keys.pop(key, None)


def post_call_router(
    secret: str,
    *,
    tolerance_seconds: int,
    handler: PostCallHandler,
    recent: RecentEvents,
) -> APIRouter:
    router = APIRouter(prefix="/webhooks", tags=["webhooks"])

    @router.post("/post-call")
    async def post_call(request: Request) -> JSONResponse:
        body = await request.body()
        try:
            verify_signature(
                body,
                request.headers.get(SIGNATURE_HEADER),
                secret,
                tolerance_seconds=tolerance_seconds,
            )
        except SignatureError as exc:
            logger.warning("rejected post-call webhook: %s", exc)
            return JSONResponse({"error": "invalid signature"}, status_code=401)

        try:
            event = json.loads(body)
        except ValueError:
            return JSONResponse({"error": "body is not JSON"}, status_code=400)
        data = event.get("data") if isinstance(event, dict) else None
        if not isinstance(data, dict) or not data.get("conversation_id"):
            return JSONResponse({"error": "expected data.conversation_id"}, status_code=400)

        key = (
            str(event.get("type")),
            str(data["conversation_id"]),
            str(event.get("event_timestamp")),
        )
        if not recent.claim(key):
            return JSONResponse({"status": "duplicate"})
        try:
            await run_in_threadpool(handler, event, summarize(event))
        except Exception:
            recent.release(key)
            logger.exception("post-call handler failed for %s", data["conversation_id"])
            return JSONResponse({"error": "handler failed"}, status_code=500)
        return JSONResponse({"status": "received"})

    return router
