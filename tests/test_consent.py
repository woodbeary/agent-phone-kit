"""The consent lint and the agent-phone-kit command."""

import copy
import json
import subprocess
import sys
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from agent_phone_kit.cli import main
from agent_phone_kit.consent import (
    FIRST_MESSAGE,
    NO_INTERRUPTIONS,
    discloses,
    lint_agent_config,
)

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "examples" / "elevenlabs" / "agent.json"

GOOD = {
    "conversation_config": {
        "agent": {
            "first_message": "Thanks for calling. This call is recorded. How can I help?",
            "disable_first_message_interruptions": True,
        }
    },
    "platform_settings": {"privacy": {"record_voice": True}},
}


def config(
    *, first_message: Any = ..., no_interruptions: Any = ..., record_voice: Any = ...
) -> dict[str, Any]:
    """GOOD with fields changed; ``None`` removes a field."""
    built = copy.deepcopy(GOOD)
    agent = built["conversation_config"]["agent"]
    privacy = built["platform_settings"]["privacy"]
    for target, key, value in (
        (agent, "first_message", first_message),
        (agent, "disable_first_message_interruptions", no_interruptions),
        (privacy, "record_voice", record_voice),
    ):
        if value is None:
            target.pop(key)
        elif value is not ...:
            target[key] = value
    return built


def paths(problems: list[Any]) -> list[str]:
    return [problem.path for problem in problems]


def test_a_recorded_call_that_says_so_passes() -> None:
    assert lint_agent_config(GOOD) == []


def test_the_example_agent_passes() -> None:
    assert lint_agent_config(json.loads(EXAMPLE.read_text(encoding="utf-8"))) == []


def test_recorded_without_a_disclosure_fails() -> None:
    problems = lint_agent_config(config(first_message="Hi! How can I help you today?"))
    assert paths(problems) == [FIRST_MESSAGE]
    assert "doesn't say so" in problems[0].message


@pytest.mark.parametrize("first_message", [None, "", "   "])
def test_recorded_with_no_first_message_fails(first_message: str | None) -> None:
    problems = lint_agent_config(config(first_message=first_message))
    assert paths(problems) == [FIRST_MESSAGE]
    assert "caller speaks first" in problems[0].message


def test_record_voice_not_set_is_treated_as_recorded() -> None:
    problems = lint_agent_config(config(first_message="Hello!", record_voice=None))
    assert paths(problems) == [FIRST_MESSAGE]
    assert "not set, so assumed on" in problems[0].message


def test_not_recorded_needs_no_recording_disclosure() -> None:
    assert lint_agent_config(config(first_message="Hello!", record_voice=False)) == []


@pytest.mark.parametrize("no_interruptions", [False, None, "true", 1])
def test_the_disclosure_must_not_be_interruptible(no_interruptions: Any) -> None:
    for record_voice in (True, False):
        problems = lint_agent_config(
            config(no_interruptions=no_interruptions, record_voice=record_voice)
        )
        assert paths(problems) == [NO_INTERRUPTIONS]


def test_both_problems_are_reported() -> None:
    problems = lint_agent_config(config(first_message="Hello!", no_interruptions=False))
    assert paths(problems) == [FIRST_MESSAGE, NO_INTERRUPTIONS]


def test_an_empty_config_fails_closed() -> None:
    assert paths(lint_agent_config({})) == [FIRST_MESSAGE, NO_INTERRUPTIONS]


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("This call is recorded.", True),
        ("Calls may be recorded for quality.", True),
        ("We're recording this call.", True),
        ("This call may be transcribed.", True),
        ("I'm an AI assistant for the claims line.", True),
        ("You're speaking with an A.I. agent.", True),
        ("I'm a virtual assistant.", True),
        ("I'm an automated agent.", True),
        ("I'm not a real person, but I can help.", True),
        ("This line is monitored.", True),
        ("Thanks for calling! How can I help?", False),
        ("Please say your claim number.", False),
        ("Hi, I'm Aiden from the claims team.", False),
        ("We have a record number of claims.", True),
        ("{{greeting}}", False),
    ],
)
def test_discloses(message: str, expected: bool) -> None:
    # The lint looks for words, not meaning; "record number" passes. Read your first
    # message yourself too.
    assert discloses(message) is expected


# --- the command ---------------------------------------------------------------------


def write(tmp_path: Path, name: str, value: Any) -> Path:
    path = tmp_path / name
    path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
    return path


def run(*argv: str) -> tuple[int, str]:
    out = StringIO()
    return main(list(argv), out=out), out.getvalue()


def test_lint_passes_a_good_config(tmp_path: Path) -> None:
    path = write(tmp_path, "agent.json", GOOD)
    assert run("lint", str(path)) == (0, f"{path}: ok\n")


def test_lint_fails_with_every_problem_named(tmp_path: Path) -> None:
    good = write(tmp_path, "good.json", GOOD)
    bad = write(tmp_path, "bad.json", config(first_message="Hello!", no_interruptions=False))
    status, output = run("lint", str(good), str(bad))
    assert status == 1
    lines = output.splitlines()
    assert lines[0] == f"{good}: ok"
    assert lines[1].startswith(f"{bad}: {FIRST_MESSAGE}: the call is recorded")
    assert lines[2].startswith(f"{bad}: {NO_INTERRUPTIONS}: must be true")


@pytest.mark.parametrize("content", ["{not json", "[1, 2]"])
def test_lint_of_something_that_isnt_a_config_is_exit_2(tmp_path: Path, content: str) -> None:
    status, output = run("lint", str(write(tmp_path, "agent.json", content)))
    assert status == 2
    assert "agent.json" in output


def test_lint_of_a_missing_file_is_exit_2(tmp_path: Path) -> None:
    assert run("lint", str(tmp_path / "missing.json"))[0] == 2


def test_the_installed_command_runs_as_a_module(tmp_path: Path) -> None:
    bad = write(tmp_path, "bad.json", config(first_message="Hello!"))
    result = subprocess.run(  # noqa: S603 (our own module, fixed arguments)
        [sys.executable, "-m", "agent_phone_kit", "lint", str(EXAMPLE), str(bad)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert f"{EXAMPLE}: ok" in result.stdout
    assert f"{bad}: {FIRST_MESSAGE}" in result.stdout


def test_ci_lints_the_example_agent() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "- run: agent-phone-kit lint examples/elevenlabs/agent.json" in workflow
