"""Example server tool: look up an insurance claim's status during a call.

The agent calls ``POST /tools/claim-status`` when the caller asks about a claim.
The answer always comes back as 200 with a ``message`` the agent can read out,
so "not found" and "that isn't a claim number" are normal turns in the call, not
tool errors.

The lookup has a budget, 2 s by default. ElevenLabs' own tool timeout can't be
set below 5 s, and with ``tool_error_handling_mode: auto`` a webhook tool's error
is hidden from the agent, so a slow or failing backend would leave the caller in
silence and the agent with nothing to say. When the backend is down, raises, or
doesn't answer within the budget, the tool still answers 200 with a fallback
``message`` the agent can say.

The data is synthetic. To use your own claims system, implement ``ClaimStore``
and pass it to ``create_app(claims=...)``.
"""

import asyncio
import contextlib
import logging
import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Claim:
    number: str
    status: str
    next_step: str


DEFAULT_BUDGET_SECONDS = 2.0
DEFAULT_MAX_LOOKUPS = 16

FALLBACK_MESSAGE = (
    "I can't reach the claims system right now. "
    "I can take a callback number, or you can call back in a few minutes."
)


class ClaimLookupError(Exception):
    """The claims backend could not answer (timeout, outage)."""


class LookupBusyError(Exception):
    """Too many lookups are still running; this one was not started."""


class ClaimStore(Protocol):
    def get(self, claim_number: str) -> Claim | None:
        """Return the claim, ``None`` if it doesn't exist, or raise ``ClaimLookupError``."""
        ...


class InMemoryClaimStore:
    def __init__(self, claims: Iterable[Claim]) -> None:
        self._claims = {claim.number: claim for claim in claims}

    def get(self, claim_number: str) -> Claim | None:
        return self._claims.get(claim_number)


SAMPLE_CLAIMS = (
    Claim("CLM-1001", "approved", "Payment goes out within five business days."),
    Claim("CLM-1002", "in review", "An adjuster will call within two business days."),
    Claim("CLM-1003", "waiting on documents", "Upload the repair invoice to finish the review."),
)

_SEPARATORS = re.compile(r"[\s\-_.#]+")
_CLAIM_NUMBER = re.compile(r"(?:CLM)?([0-9]{4,10})")


def normalize_claim_number(raw: str) -> str | None:
    """Turn what the agent heard ("clm 1001", "CLM-1001", "1001") into ``CLM-1001``."""
    match = _CLAIM_NUMBER.fullmatch(_SEPARATORS.sub("", raw).upper())
    return f"CLM-{match.group(1)}" if match else None


class ClaimStatusRequest(BaseModel):
    claim_number: str = Field(min_length=1, max_length=64)
    conversation_id: str | None = Field(default=None, max_length=128)


class ClaimStatusResponse(BaseModel):
    found: bool
    claim_number: str | None = None
    status: str | None = None
    next_step: str | None = None
    message: str


class BudgetedLookups:
    """Run blocking lookups off the event loop, each with a deadline.

    Each lookup runs on its own daemon thread, at most ``max_in_flight`` at once.
    A lookup that outlives its deadline keeps running in the background, but it
    never holds up the answer, the event loop, other routes' threads or shutdown.
    When ``max_in_flight`` lookups are still running, a new one isn't started.
    """

    def __init__(self, max_in_flight: int = DEFAULT_MAX_LOOKUPS) -> None:
        if max_in_flight < 1:
            raise ValueError("max_in_flight must be at least 1")
        self._slots = threading.BoundedSemaphore(max_in_flight)

    async def run(self, func: Callable[[str], Any], arg: str, *, timeout: float) -> Any:
        """Return ``func(arg)``, or raise ``TimeoutError``, ``LookupBusyError`` or its error."""
        if not self._slots.acquire(blocking=False):
            raise LookupBusyError
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()

        def deliver(result: Any, error: BaseException | None) -> None:
            if future.done():
                return  # the deadline passed and the caller has moved on
            if error is None:
                future.set_result(result)
            else:
                future.set_exception(error)

        def work() -> None:
            result: Any = None
            error: BaseException | None = None
            try:
                result = func(arg)
            except BaseException as exc:
                error = exc
            finally:
                self._slots.release()
            with contextlib.suppress(RuntimeError):  # the loop has closed; nobody is waiting
                loop.call_soon_threadsafe(deliver, result, error)

        try:
            threading.Thread(target=work, name="claim-lookup", daemon=True).start()
        except BaseException:
            self._slots.release()
            raise
        return await asyncio.wait_for(future, timeout)


def _fallback(number: str) -> ClaimStatusResponse:
    return ClaimStatusResponse(found=False, claim_number=number, message=FALLBACK_MESSAGE)


def claim_status_router(
    store: ClaimStore,
    auth: Callable[..., None],
    *,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    lookups: BudgetedLookups | None = None,
) -> APIRouter:
    if budget_seconds <= 0:
        raise ValueError("budget_seconds must be positive")
    lookups = lookups or BudgetedLookups()
    router = APIRouter(prefix="/tools", tags=["tools"], dependencies=[Depends(auth)])

    @router.post("/claim-status")
    async def claim_status(request: ClaimStatusRequest) -> ClaimStatusResponse:
        number = normalize_claim_number(request.claim_number)
        if number is None:
            return ClaimStatusResponse(
                found=False,
                message="That doesn't sound like a claim number. "
                "Claim numbers are C L M followed by at least four digits.",
            )
        conversation = request.conversation_id
        try:
            claim = await lookups.run(store.get, number, timeout=budget_seconds)
        except TimeoutError:
            logger.warning(
                "claim lookup took longer than %.1f s (conversation %s); said the fallback",
                budget_seconds,
                conversation,
            )
            return _fallback(number)
        except LookupBusyError:
            logger.warning(
                "too many claim lookups still running (conversation %s); said the fallback",
                conversation,
            )
            return _fallback(number)
        except Exception:
            logger.exception(
                "claim lookup failed (conversation %s); said the fallback", conversation
            )
            return _fallback(number)
        if claim is None:
            return ClaimStatusResponse(
                found=False, claim_number=number, message=f"I couldn't find claim {number}."
            )
        return ClaimStatusResponse(
            found=True,
            claim_number=claim.number,
            status=claim.status,
            next_step=claim.next_step,
            message=f"Claim {claim.number} is {claim.status}. {claim.next_step}",
        )

    return router
