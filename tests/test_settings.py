import pytest

from agent_phone_kit.settings import Settings

TOOL = "t" * 32
HOOK = "w" * 32


def test_from_env() -> None:
    settings = Settings.from_env(
        {
            "TOOL_BEARER_SECRET": TOOL,
            "ELEVENLABS_WEBHOOK_SECRET": HOOK,
            "WEBHOOK_TOLERANCE_SECONDS": "600",
            "TOOL_BUDGET_SECONDS": "1.5",
        }
    )
    assert settings == Settings(
        tool_secret=TOOL,
        webhook_secret=HOOK,
        webhook_tolerance_seconds=600,
        tool_budget_seconds=1.5,
    )


def test_tool_budget_defaults_to_two_seconds() -> None:
    env = {"TOOL_BEARER_SECRET": TOOL, "ELEVENLABS_WEBHOOK_SECRET": HOOK}
    assert Settings.from_env(env).tool_budget_seconds == 2.0


def test_from_env_names_what_is_missing() -> None:
    with pytest.raises(RuntimeError, match="TOOL_BEARER_SECRET, ELEVENLABS_WEBHOOK_SECRET"):
        Settings.from_env({})


@pytest.mark.parametrize("tolerance", [None, ""])
def test_tolerance_defaults_to_the_sdk_value(tolerance: str | None) -> None:
    env = {"TOOL_BEARER_SECRET": TOOL, "ELEVENLABS_WEBHOOK_SECRET": HOOK}
    if tolerance is not None:
        env["WEBHOOK_TOLERANCE_SECONDS"] = tolerance
    assert Settings.from_env(env).webhook_tolerance_seconds == 1800


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tool_secret": "short", "webhook_secret": HOOK},
        {"tool_secret": TOOL, "webhook_secret": ""},
        {"tool_secret": TOOL, "webhook_secret": HOOK, "webhook_tolerance_seconds": 0},
        {"tool_secret": TOOL, "webhook_secret": HOOK, "tool_budget_seconds": 0},
        {"tool_secret": TOOL, "webhook_secret": HOOK, "tool_budget_seconds": float("nan")},
    ],
)
def test_weak_or_empty_settings_are_refused(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        Settings(**kwargs)


def test_repr_does_not_leak_secrets() -> None:
    text = repr(Settings(tool_secret=TOOL, webhook_secret=HOOK))
    assert TOOL not in text
    assert HOOK not in text
