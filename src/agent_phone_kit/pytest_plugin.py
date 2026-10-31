"""pytest plugin, loaded automatically once agent-phone-kit is installed.

Fixtures:

* ``phone_kit_settings``: ``Settings`` with random throwaway secrets.
* ``phone_kit_app``: the app under test. Override it in your conftest to test your own.
* ``phone_kit_client``: a ``TestClient`` for ``phone_kit_app``.
* ``signed_post_call``: POST a payload to the post-call webhook, signed as ElevenLabs signs it.
"""

import json
import secrets
from collections.abc import Callable, Iterator, Mapping
from typing import Any

import pytest

from agent_phone_kit.settings import Settings
from agent_phone_kit.signature import SIGNATURE_HEADER, sign_payload


@pytest.fixture
def phone_kit_settings() -> Settings:
    return Settings(tool_secret=secrets.token_urlsafe(32), webhook_secret=secrets.token_urlsafe(32))


@pytest.fixture
def phone_kit_app(phone_kit_settings: Settings) -> Any:
    from agent_phone_kit.app import create_app

    return create_app(phone_kit_settings)


@pytest.fixture
def phone_kit_client(phone_kit_app: Any) -> Iterator[Any]:
    from fastapi.testclient import TestClient

    with TestClient(phone_kit_app) as client:
        yield client


@pytest.fixture
def signed_post_call(phone_kit_client: Any, phone_kit_settings: Settings) -> Callable[..., Any]:
    def post(
        payload: Mapping[str, Any] | bytes,
        *,
        timestamp: int | None = None,
        secret: str | None = None,
        path: str = "/webhooks/post-call",
    ) -> Any:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        signature = sign_payload(
            secret or phone_kit_settings.webhook_secret, body, timestamp=timestamp
        )
        return phone_kit_client.post(
            path,
            content=body,
            headers={SIGNATURE_HEADER: signature, "Content-Type": "application/json"},
        )

    return post
