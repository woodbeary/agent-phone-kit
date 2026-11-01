"""The FastAPI app: one example tool, the initiation webhook and the post-call receiver.

Run it with::

    uvicorn agent_phone_kit.app:create_app --factory
"""

import logging
from collections.abc import Callable

from fastapi import FastAPI

from agent_phone_kit import __version__
from agent_phone_kit.auth import bearer_auth
from agent_phone_kit.calls import CallStore, SQLiteCallStore
from agent_phone_kit.claims import (
    SAMPLE_CLAIMS,
    ClaimStore,
    InMemoryClaimStore,
    claim_status_router,
)
from agent_phone_kit.initiation import SAMPLE_CALLERS, initiation_router
from agent_phone_kit.post_call import PostCallHandler, log_summary, post_call_router
from agent_phone_kit.settings import Settings


def _show_kit_logs() -> None:
    """Make the kit's INFO lines visible under a bare ``uvicorn`` run.

    Does nothing when logging is already configured (by your app, or by pytest).
    """
    kit = logging.getLogger("agent_phone_kit")
    if kit.handlers or logging.getLogger().handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    kit.addHandler(handler)
    kit.setLevel(logging.INFO)


def create_app(
    settings: Settings | None = None,
    *,
    claims: ClaimStore | None = None,
    is_known_caller: Callable[[str], bool] | None = None,
    on_post_call: PostCallHandler | None = None,
    store: CallStore | None = None,
) -> FastAPI:
    """Build the app. Anything not passed in falls back to the synthetic samples.

    Call records go to ``store``, by default a SQLite file at ``settings.call_store_path``.
    """
    settings = settings or Settings.from_env()
    _show_kit_logs()
    auth = bearer_auth(settings.tool_secret)

    app = FastAPI(title="agent-phone-kit", version=__version__)
    app.include_router(
        claim_status_router(
            claims or InMemoryClaimStore(SAMPLE_CLAIMS),
            auth,
            budget_seconds=settings.tool_budget_seconds,
        )
    )
    app.include_router(initiation_router(is_known_caller or SAMPLE_CALLERS.__contains__, auth))
    app.include_router(
        post_call_router(
            settings.webhook_secret,
            tolerance_seconds=settings.webhook_tolerance_seconds,
            handler=on_post_call or log_summary,
            store=store or SQLiteCallStore(settings.call_store_path),
        )
    )

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
