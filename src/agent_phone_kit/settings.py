"""Configuration, read from environment variables."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from agent_phone_kit.claims import DEFAULT_BUDGET_SECONDS
from agent_phone_kit.signature import DEFAULT_TOLERANCE_SECONDS
from agent_phone_kit.twilio_gate import GateSettings

TOOL_SECRET_ENV = "TOOL_BEARER_SECRET"  # noqa: S105 (a variable name, not a secret)
WEBHOOK_SECRET_ENV = "ELEVENLABS_WEBHOOK_SECRET"  # noqa: S105
TOLERANCE_ENV = "WEBHOOK_TOLERANCE_SECONDS"
BUDGET_ENV = "TOOL_BUDGET_SECONDS"
CALL_STORE_ENV = "CALL_STORE_PATH"
DEFAULT_CALL_STORE_PATH = "calls.sqlite3"

MIN_TOOL_SECRET_LENGTH = 16


@dataclass(frozen=True)
class Settings:
    """Secrets the kit needs. Both are excluded from ``repr`` so they stay out of logs."""

    tool_secret: str = field(repr=False)
    """Bearer secret ElevenLabs sends to your tools and initiation webhook."""

    webhook_secret: str = field(repr=False)
    """HMAC secret ElevenLabs shows when you create the post-call webhook."""

    webhook_tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS

    tool_budget_seconds: float = DEFAULT_BUDGET_SECONDS
    """How long a tool waits on its backend before it answers with a fallback message."""

    call_store_path: str = DEFAULT_CALL_STORE_PATH
    """The SQLite file that holds one record per call (``:memory:`` for none)."""

    gate: GateSettings | None = field(default=None, repr=False)
    """The register-call gate on your own Twilio number, if you use one."""

    def __post_init__(self) -> None:
        if len(self.tool_secret) < MIN_TOOL_SECRET_LENGTH:
            raise ValueError(f"tool secret must be at least {MIN_TOOL_SECRET_LENGTH} characters")
        if not self.webhook_secret:
            raise ValueError("webhook secret must not be empty")
        if self.webhook_tolerance_seconds <= 0:
            raise ValueError("webhook tolerance must be positive")
        if not 0 < self.tool_budget_seconds < float("inf"):
            raise ValueError("tool budget must be a positive number of seconds")

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
            tool_budget_seconds=float(env.get(BUDGET_ENV) or DEFAULT_BUDGET_SECONDS),
            call_store_path=env.get(CALL_STORE_ENV) or DEFAULT_CALL_STORE_PATH,
            gate=GateSettings.from_env(env),
        )
