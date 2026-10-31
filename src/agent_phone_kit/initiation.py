"""Conversation initiation webhook for inbound Twilio and SIP calls.

When this webhook is enabled on the agent, ElevenLabs POSTs the caller's context
here before the agent speaks and starts the conversation with the dynamic
variables returned. A failed or slow response can stop the call from starting,
so a broken caller lookup is logged and the call goes ahead without it.

Caller ID can be spoofed. Use it to personalize the greeting, never to decide
what account data a caller may hear.
"""

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

# Fictional numbers: NANP reserves 555-0100 through 555-0199 for fiction.
SAMPLE_CALLERS = frozenset({"+12025550143", "+12025550178"})


class InitiationRequest(BaseModel):
    # SIP calls may also send call_id and sip_headers; keep whatever arrives.
    model_config = ConfigDict(extra="allow")

    caller_id: str | None = None
    called_number: str | None = None
    agent_id: str | None = None
    call_sid: str | None = None
    conversation_id: str | None = None


def initiation_router(
    is_known_caller: Callable[[str], bool], auth: Callable[..., None]
) -> APIRouter:
    router = APIRouter(prefix="/webhooks", tags=["webhooks"], dependencies=[Depends(auth)])

    @router.post("/conversation-init")
    def conversation_init(request: InitiationRequest) -> dict[str, Any]:
        on_file = False
        if request.caller_id:
            try:
                on_file = bool(is_known_caller(request.caller_id))
            except Exception:
                logger.exception(
                    "caller lookup failed (conversation %s); starting the call without it",
                    request.conversation_id,
                )
        return {
            "type": "conversation_initiation_client_data",
            "dynamic_variables": {"caller_on_file": on_file},
        }

    return router
