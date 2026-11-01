"""Decide whether a call was a conversation, before it is counted.

Hang-ups, "hello?" and test calls inflate call volume and wreck success rates.
A call counts as substantive when the caller said at least one thing that isn't
filler. A caller turn is filler when every word in it is a greeting or a
backchannel ("hello", "test", "mm-hmm"), or when it is a single word of four
characters or fewer ("yeah", "what").

Ported from a production TypeScript rule, with two bugs fixed:

* The old rule stripped hyphens before it looked words up in a filler list that
  still had hyphens in it, so "mm-hmm" became "mmhmm" and "uh-huh" became
  "uhhuh", which, at five characters, counted as conversation. Here the list and
  the message are normalized the same way.
* The old rule only matched the whole message against the list, so repeated
  filler ("hello hello", "test test", "Hello? Hello?") passed. Here every word is
  checked.

Punctuation no longer counts toward length, so "What?" is as short as "what"
and "hello??" is filler like "hello?". Change ``FILLER_WORDS`` for your callers
and their languages.
"""

import re
from collections.abc import Iterable, Mapping
from typing import Any

FILLER_WORDS = frozenset(
    {
        "hi",
        "hey",
        "hello",
        "test",
        "testing",
        "yes",
        "no",
        "okay",
        "ok",
        "mmhmm",
        "mhm",
        "uhhuh",
    }
)
"""Words that never make a turn substantive, normalized: lowercase, no punctuation."""

_JOINERS = re.compile(r"[-'\u2019]")
_NOT_WORD = re.compile(r"[^\w\s]|_")


def caller_words(message: str) -> list[str]:
    """Split a caller turn into lowercase words, without punctuation.

    Hyphens and apostrophes join, so "mm-hmm" is one word, ``mmhmm``.
    """
    joined = _JOINERS.sub("", message.lower())
    return _NOT_WORD.sub(" ", joined).split()


def is_substantive(message: str | None) -> bool:
    """True when a caller turn says something beyond greetings and backchannels."""
    words = caller_words(message or "")
    if not words or all(word in FILLER_WORDS for word in words):
        return False
    return len(words) >= 2 or len(words[0]) >= 5


def _turn_text(turn: Mapping[str, Any]) -> str:
    for key in ("message", "original_message"):
        value = turn.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def has_substantive_caller_speech(transcript: Iterable[Any] | None) -> bool:
    """True when any non-agent turn in an ElevenLabs transcript is substantive."""
    for turn in transcript or ():
        if not isinstance(turn, Mapping):
            continue
        if str(turn.get("role") or "").strip().lower() == "agent":
            continue
        if is_substantive(_turn_text(turn)):
            return True
    return False
