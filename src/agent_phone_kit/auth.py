"""Bearer-secret auth for the endpoints an ElevenLabs agent calls.

In the agent's tool config, add an ``Authorization`` header whose value is a
workspace secret holding ``Bearer <your secret>``.
"""

import hmac
from collections.abc import Callable
from typing import Annotated

from fastapi import Header, HTTPException, status


def bearer_auth(secret: str) -> Callable[..., None]:
    """Return a FastAPI dependency that accepts only ``Authorization: Bearer <secret>``."""
    expected = secret.encode()

    def require_bearer(authorization: Annotated[str | None, Header()] = None) -> None:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip().encode(), expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing or invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return require_bearer
