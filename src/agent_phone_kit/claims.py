"""Example server tool: look up an insurance claim's status during a call.

The agent calls ``POST /tools/claim-status`` when the caller asks about a claim.
The answer always comes back as 200 with a ``message`` the agent can read out,
so "not found" and "that isn't a claim number" are normal turns in the call, not
tool errors. Only a backend failure is an error (503).

The data is synthetic. To use your own claims system, implement ``ClaimStore``
and pass it to ``create_app(claims=...)``.
"""

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Claim:
    number: str
    status: str
    next_step: str


class ClaimLookupError(Exception):
    """The claims backend could not answer (timeout, outage)."""


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


def claim_status_router(store: ClaimStore, auth: Callable[..., None]) -> APIRouter:
    router = APIRouter(prefix="/tools", tags=["tools"], dependencies=[Depends(auth)])

    @router.post("/claim-status")
    def claim_status(request: ClaimStatusRequest) -> ClaimStatusResponse:
        number = normalize_claim_number(request.claim_number)
        if number is None:
            return ClaimStatusResponse(
                found=False,
                message="That doesn't sound like a claim number. "
                "Claim numbers are C L M followed by at least four digits.",
            )
        try:
            claim = store.get(number)
        except ClaimLookupError:
            logger.exception("claim lookup failed (conversation %s)", request.conversation_id)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="The claims system is not answering right now.",
            ) from None
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
