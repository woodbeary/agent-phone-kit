"""The pytest plugin, run inside throwaway projects with pytester."""

import json
import shutil
from pathlib import Path

import pytest

CONVERSATIONS = Path(__file__).parent / "conversations"

INNER_TEST = """
def test_replay(recorded_conversation, replay_conversation):
    replay_conversation(recorded_conversation)
"""


def test_plugin_is_registered_through_its_entry_point(request: pytest.FixtureRequest) -> None:
    assert request.config.pluginmanager.get_plugin("agent_phone_kit") is not None


def test_each_conversation_file_becomes_a_test(pytester: pytest.Pytester) -> None:
    pytester.makeini("[pytest]\nphone_kit_conversations = calls\n")
    calls = pytester.mkdir("calls")
    for path in sorted(CONVERSATIONS.glob("*.json")):
        shutil.copy(path, calls / path.name)
    pytester.makepyfile(test_inner=INNER_TEST)
    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=len(list(CONVERSATIONS.glob("*.json"))))
    result.stdout.fnmatch_lines(["*test_replay?claim_approved? PASSED*"])


def test_a_broken_tool_fails_with_a_readable_diff(pytester: pytest.Pytester) -> None:
    payload = json.loads((CONVERSATIONS / "claim_approved.json").read_text(encoding="utf-8"))
    for turn in payload["data"]["transcript"]:
        for result in turn.get("tool_results") or []:
            result["result_value"] = result["result_value"].replace("approved", "denied")
    pytester.makeini("[pytest]\nphone_kit_conversations = calls\n")
    pytester.mkdir("calls")
    (pytester.path / "calls" / "edited.json").write_text(json.dumps(payload), encoding="utf-8")
    pytester.makepyfile(test_inner=INNER_TEST)
    result = pytester.runpytest()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(
        [
            "*edited.json: 1 of 1 tool call(s) differ*",
            "*status: expected 'denied', got 'approved'*",
        ]
    )


def test_no_conversations_configured_means_the_test_is_skipped(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_inner=INNER_TEST)
    pytester.runpytest().assert_outcomes(skipped=1)


def test_projects_can_override_the_app(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(
        """
import pytest
from agent_phone_kit.app import create_app
from agent_phone_kit.claims import Claim, InMemoryClaimStore

@pytest.fixture
def phone_kit_app(phone_kit_settings):
    store = InMemoryClaimStore([Claim("CLM-5555", "paid", "Nothing left to do.")])
    return create_app(phone_kit_settings, claims=store)
"""
    )
    pytester.makepyfile(
        test_inner="""
def test_own_store(phone_kit_client, phone_kit_settings):
    headers = {"Authorization": f"Bearer {phone_kit_settings.tool_secret}"}
    body = phone_kit_client.post(
        "/tools/claim-status", json={"claim_number": "5555"}, headers=headers
    ).json()
    assert body["status"] == "paid"
"""
    )
    pytester.runpytest().assert_outcomes(passed=1)
