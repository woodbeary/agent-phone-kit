"""Post-call webhook receiver.

ElevenLabs POSTs here after a call ends (``post_call_transcription``,
``post_call_audio``, ``call_initiation_failure``). The receiver:

1. verifies the ``ElevenLabs-Signature`` HMAC over the raw body and rejects
   stale timestamps (401, which ElevenLabs does not retry);
2. drops repeat deliveries of the same event (a retry carries the identical payload);
3. merges the event into the call's durable record in a ``CallStore`` (one record per
   ``conversation_id``; a later, thinner event never nulls a field);
4. hands the event and a ``CallSummary`` to your handler. If the store or the handler
   raises, it answers 500 and forgets the delivery, so a retry (when enabled on the
   webhook) is processed again. Merging the same event twice changes nothing.
"""

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from agent_phone_kit.calls import (
    TRANSFER_TOOLS,
    CallStore,
    Claim,
    EventKey,
    record_from_event,
)
from agent_phone_kit.signature import SIGNATURE_HEADER, SignatureError, verify_signature

logger = logging.getLogger(__name__)

__all__ = ["TRANSFER_TOOLS", "CallSummary", "PostCallHandler", "post_call_router", "summarize"]


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


def summarize(event: Mapping[str, Any]) -> CallSummary:
    record = record_from_event(event)
    return CallSummary(
        event_type=str(event.get("type")),
        conversation_id=record.conversation_id,
        agent_id=record.agent_id,
        status=record.status,
        call_duration_secs=record.call_duration_secs,
        call_successful=record.call_successful,
        tools_called=record.tools_called or (),
        tool_errors=record.tool_errors or 0,
        tool_latency_secs=record.tool_latency_secs or (),
        transferred=bool(record.transferred),
        failure_reason=record.failure_reason,
        substantive=record.substantive,
    )


def log_summary(event: dict[str, Any], summary: CallSummary) -> None:
    """Default handler: one JSON log line per event."""
    logger.info("post-call %s", json.dumps(asdict(summary), sort_keys=True))


def post_call_router(
    secret: str,
    *,
    tolerance_seconds: int,
    handler: PostCallHandler,
    store: CallStore,
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

        key = EventKey.of(event)
        claim = await run_in_threadpool(store.claim, key)
        if claim is Claim.DUPLICATE:
            return JSONResponse({"status": "duplicate"})
        if claim is Claim.IN_PROGRESS:
            # Another delivery of this event is still being handled. 503 is retried.
            return JSONResponse(
                {"status": "in progress"}, status_code=503, headers={"Retry-After": "30"}
            )
        try:
            await run_in_threadpool(store.merge, record_from_event(event))
            await run_in_threadpool(handler, event, summarize(event))
        except Exception:
            await run_in_threadpool(store.release, key)
            logger.exception("post-call handling failed for %s", data["conversation_id"])
            return JSONResponse({"error": "handler failed"}, status_code=500)
        await run_in_threadpool(store.finish, key)
        return JSONResponse({"status": "received"})

    return router
