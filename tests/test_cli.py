"""The demo steps, one command each, against the kit served on 127.0.0.1."""

import json
import logging
import re
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from conftest import serve_on_loopback

from agent_phone_kit.app import create_app
from agent_phone_kit.calls import SQLiteCallStore
from agent_phone_kit.cli import main

CONVERSATIONS = Path(__file__).parent / "conversations"
APPROVED = CONVERSATIONS / "claim_approved.json"


def run(*argv: str) -> tuple[int, str]:
    out = StringIO()
    return main(list(argv), out=out), out.getvalue().strip()


@pytest.fixture
def kit(phone_kit_settings: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("ELEVENLABS_WEBHOOK_SECRET", phone_kit_settings.webhook_secret)
    with serve_on_loopback(create_app(phone_kit_settings)) as base_url:
        yield f"{base_url}/webhooks/post-call"


def record(settings: Any) -> Any:
    store = SQLiteCallStore(settings.call_store_path)
    try:
        return store.get("conv_synthetic_0001")
    finally:
        store.close()


def test_the_demo_steps(
    kit: str, phone_kit_settings: Any, caplog: pytest.LogCaptureFixture
) -> None:
    body = str(APPROVED)
    assert run("post-call", body, "--url", kit) == (0, 'HTTP 200 {"status":"received"}')
    assert run("post-call", body, "--url", kit) == (0, 'HTTP 200 {"status":"duplicate"}')

    summary = record(phone_kit_settings).summary
    thinner = ("--later", "60", "--drop", "data.analysis", "--drop", "data.metadata")
    assert run("post-call", body, "--url", kit, *thinner) == (0, 'HTTP 200 {"status":"received"}')
    assert record(phone_kit_settings).summary == summary  # one record, nothing nulled

    forged = run("post-call", body, "--url", kit, "--forge")
    assert forged == (1, 'HTTP 401 {"error":"invalid signature"}')
    with caplog.at_level(logging.WARNING, logger="agent_phone_kit.post_call"):
        stale = run("post-call", body, "--url", kit, "--stale", "3600")
    assert stale == (1, 'HTTP 401 {"error":"invalid signature"}')
    # 3600 s, or 3601 if a second ticked over between signing and checking.
    assert re.search(r"timestamp is 360[01] s old", caplog.text)


def test_post_call_needs_the_secret_in_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ELEVENLABS_WEBHOOK_SECRET", raising=False)
    status, output = run("post-call", str(APPROVED))
    assert status == 2
    assert "ELEVENLABS_WEBHOOK_SECRET" in output


def test_post_call_refuses_to_drop_a_field_that_isnt_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ELEVENLABS_WEBHOOK_SECRET", "x" * 32)
    status, output = run("post-call", str(APPROVED), "--drop", "data.nope")
    assert status == 2
    assert "data.nope" in output


def test_post_call_with_nothing_listening(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELEVENLABS_WEBHOOK_SECRET", "x" * 32)
    status, output = run("post-call", str(APPROVED), "--url", "http://127.0.0.1:9/x")
    assert status == 2
    assert "failed" in output


def test_stats_reads_the_call_store(kit: str, phone_kit_settings: Any) -> None:
    for name in ("claim_approved.json", "twilio_transfer_to_number.json"):
        assert run("post-call", str(CONVERSATIONS / name), "--url", kit)[0] == 0
    status, output = run("stats", "--db", phone_kit_settings.call_store_path)
    assert status == 0
    assert json.loads(output) == {
        "calls": 2,
        "substantive_calls": 2,
        "substantive_call_rate": 1.0,
        "unknown_calls": 0,
        "tool_results": 1,
        "tool_latency_p50_secs": 0.31,
        "tool_latency_p95_secs": 0.31,
    }


def test_stats_without_a_store(tmp_path: Path) -> None:
    status, output = run("stats", "--db", str(tmp_path / "none.sqlite3"))
    assert status == 2
    assert "no call store" in output
    assert not (tmp_path / "none.sqlite3").exists()
