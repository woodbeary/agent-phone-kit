"""Configuration, read from environment variables."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from agent_phone_kit.signature import DEFAULT_TOLERANCE_SECONDS

TOOL_SECRET_ENV = "TOOL_BEARER_SECRET"  # noqa: S105 (a variable name, not a secret)
WEBHOOK_SECRET_ENV = "ELEVENLABS_WEBHOOK_SECRET"  # noqa: S105
TOLERANCE_ENV = "WEBHOOK_TOLERANCE_SECONDS"

MIN_TOOL_SECRET_LENGTH = 16


@dataclass(frozen=True)
class Settings:
    """Secrets the kit needs. Both are excluded from ``repr`` so they stay out of logs."""

    tool_secret: str = field(repr=False)
    """Bearer secret ElevenLabs sends to your tools and initiation webhook."""

    webhook_secret: str = field(repr=False)
    """HMAC secret ElevenLabs shows when you create the post-call webhook."""

    webhook_tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS

    def __post_init__(self) -> None:
        if len(self.tool_secret) < MIN_TOOL_SECRET_LENGTH:
            raise ValueError(f"tool secret must be at least {MIN_TOOL_SECRET_LENGTH} characters")
        if not self.webhook_secret:
            raise ValueError("webhook secret must not be empty")
        if self.webhook_tolerance_seconds <= 0:
            raise ValueError("webhook tolerance must be positive")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        missing = [name for name in (TOOL_SECRET_ENV, WEBHOOK_SECRET_ENV) if not env.get(name)]
        if missing:
            raise RuntimeError(f"set these environment variables: {', '.join(missing)}")
        return cls(
            tool_secret=env[TOOL_SECRET_ENV],
            webhook_secret=env[WEBHOOK_SECRET_ENV],
            webhook_tolerance_seconds=int(env.get(TOLERANCE_ENV) or DEFAULT_TOLERANCE_SECONDS),
        )
