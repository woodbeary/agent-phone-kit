"""pytest plugin, loaded automatically once agent-phone-kit is installed.

Fixtures:

* ``phone_kit_settings``: ``Settings`` with random throwaway secrets and a call store
  in the test's temporary directory.
* ``phone_kit_app``: the app under test. Override it in your conftest to test your own.
* ``phone_kit_client``: a ``TestClient`` for ``phone_kit_app``.
* ``replay_conversation``: replay a recorded conversation; fails the test with a diff.
* ``signed_post_call``: POST a payload to the post-call webhook, signed as ElevenLabs signs it.

Parametrization: a test that takes ``recorded_conversation`` runs once per
``*.json`` file in the directories named by the ``phone_kit_conversations`` ini option.
"""

import json
import secrets
from collections.abc import Callable, Collection, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from agent_phone_kit.replay import ReplayReport, replay
from agent_phone_kit.settings import Settings
from agent_phone_kit.signature import SIGNATURE_HEADER, sign_payload


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini(
        "phone_kit_conversations",
        type="paths",
        default=[],
        help="directories of recorded conversation JSON files, one test case per file",
    )


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "recorded_conversation" not in metafunc.fixturenames:
        return
    directories = metafunc.config.getini("phone_kit_conversations")
    files = sorted(path for directory in directories for path in Path(directory).glob("*.json"))
    metafunc.parametrize("recorded_conversation", files, ids=[path.stem for path in files])


@pytest.fixture
def phone_kit_settings(tmp_path: Path) -> Settings:
    return Settings(
        tool_secret=secrets.token_urlsafe(32),
        webhook_secret=secrets.token_urlsafe(32),
        call_store_path=str(tmp_path / "calls.sqlite3"),
    )


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
def replay_conversation(
    phone_kit_client: Any, phone_kit_settings: Settings
) -> Callable[..., ReplayReport]:
    def run(
        source: str | Path | Mapping[str, Any], *, ignore_fields: Collection[str] = ()
    ) -> ReplayReport:
        report = replay(
            phone_kit_client,
            source,
            headers={"Authorization": f"Bearer {phone_kit_settings.tool_secret}"},
            ignore_fields=ignore_fields,
        )
        if not report.ok:
            pytest.fail(report.explain(), pytrace=False)
        return report

    return run


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
