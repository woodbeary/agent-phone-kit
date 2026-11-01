"""Lint an ElevenLabs agent config for consent in the call.

The person on the phone never accepted your terms, so the agent has to say what is
happening before anything else: that the call is recorded, or that they are talking
to an AI. The lint fails when:

* the call is recorded (``platform_settings.privacy.record_voice`` is true, or not
  set, which the lint treats as recorded) and the first message
  (``conversation_config.agent.first_message``) is empty or says nothing about
  recording or AI;
* ``conversation_config.agent.disable_first_message_interruptions`` is not true, so
  the caller can talk over the disclosure and never hear it.

It reads the body you send to create or update an agent, or the agent as
``GET /v1/convai/agents/{agent_id}`` returns it. It checks the default first
message in English; language presets and text in dynamic variables aren't checked.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

FIRST_MESSAGE = "conversation_config.agent.first_message"
NO_INTERRUPTIONS = "conversation_config.agent.disable_first_message_interruptions"
RECORD_VOICE = "platform_settings.privacy.record_voice"

DISCLOSURE = re.compile(
    r"\brecord(?:ed|ing|s)?\b"
    r"|\btranscri(?:bed|bes|be|ption|pts?)\b"
    r"|\bmonitored\b"
    r"|\bA\.?I\.?(?=\W|$)"
    r"|\bartificial intelligence\b"
    r"|\b(?:automated|virtual|digital) (?:assistant|agent)\b"
    r"|\bnot a (?:real )?(?:person|human)\b",
    re.IGNORECASE,
)
"""Words that tell the caller the call is recorded or that they're talking to an AI."""


@dataclass(frozen=True)
class Problem:
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


def _get(config: Mapping[str, Any], dotted: str) -> Any:
    value: Any = config
    for key in dotted.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def discloses(message: str) -> bool:
    """True when the message says the call is recorded or that an AI is speaking."""
    return bool(DISCLOSURE.search(message))


def lint_agent_config(config: Mapping[str, Any]) -> list[Problem]:
    """The consent problems in an agent config; empty when it passes."""
    problems: list[Problem] = []
    record_voice = _get(config, RECORD_VOICE)
    recorded = record_voice is not False
    first = _get(config, FIRST_MESSAGE)
    first_message = first.strip() if isinstance(first, str) else ""

    if recorded:
        assumed = "" if record_voice is True else f" ({RECORD_VOICE} is not set, so assumed on)"
        if not first_message:
            problems.append(
                Problem(
                    FIRST_MESSAGE,
                    f"the call is recorded{assumed} but there is no first message, so the "
                    "caller speaks first and hears no disclosure",
                )
            )
        elif not discloses(first_message):
            problems.append(
                Problem(
                    FIRST_MESSAGE,
                    f"the call is recorded{assumed} but the first message doesn't say so or "
                    "that an AI is speaking",
                )
            )
    if _get(config, NO_INTERRUPTIONS) is not True:
        problems.append(
            Problem(
                NO_INTERRUPTIONS,
                "must be true, or the caller can talk over the disclosure and never hear it",
            )
        )
    return problems
