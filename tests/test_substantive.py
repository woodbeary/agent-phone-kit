"""The substantive-call filter, as a table against the TypeScript rule it replaces.

``old`` is what the production TypeScript rule (``isSubstantiveCallerMessage``)
returned for the same message, run with Node on 2026-11-01. Rows where ``old``
differs from ``expected`` are the bugs fixed in the port.
"""

import pytest

from agent_phone_kit.post_call import summarize
from agent_phone_kit.substantive import (
    FILLER_WORDS,
    caller_words,
    has_substantive_caller_speech,
    is_substantive,
)

# (caller message, expected, what the TypeScript rule said)
TABLE = [
    # Bug 1: the hyphen was stripped before the lookup, so these became "mmhmm" and
    # "uhhuh" and passed on length.
    ("mm-hmm", False, True),
    ("Mm-hmm.", False, True),
    ("uh-huh", False, True),
    ("Uh-huh?", False, True),
    # Bug 2: repeated filler passed because only the whole message was looked up.
    ("hello hello", False, True),
    ("Hello? Hello?", False, True),
    ("test test", False, True),
    ("testing testing", False, True),
    ("yes yes", False, True),
    ("hi, hello, hey", False, True),
    ("um um", False, True),
    ("Um, um.", False, True),
    ("yeah yeah", False, True),
    ("bye bye", False, True),
    # Bug 1 again: the same backchannels spelled with a space.
    ("uh huh", False, True),
    ("Mm hmm.", False, True),
    # Punctuation counted toward length.
    ("hello??", False, True),
    ("What?", False, True),
    # Unchanged: filler, short and empty turns.
    ("hello", False, False),
    ("Hello?", False, False),
    ("hello.", False, False),
    ("Hi.", False, False),
    ("mhm", False, False),
    ("hmm", False, False),
    ("um", False, False),
    ("ok", False, False),
    ("okay.", False, False),
    ("Yeah.", False, False),
    ("...", False, False),
    ("", False, False),
    # Unchanged: real requests.
    ("I need to check my claim.", True, True),
    ("CLM 1001", True, True),
    ("thanks", True, True),
    ("no, thanks", True, True),
    ("¿Dónde está mi reclamo?", True, True),
    ("hello, I'm calling about a claim", True, True),
    ("uh-huh, the claim from March", True, True),
    ("yeah, the claim from March", True, True),
    ("help me", True, True),
]


@pytest.mark.parametrize(("message", "expected", "old"), TABLE, ids=[row[0] for row in TABLE])
def test_is_substantive(message: str, expected: bool, old: bool) -> None:
    assert is_substantive(message) is expected


def test_the_table_records_both_bugs() -> None:
    fixed = {message for message, expected, old in TABLE if expected != old}
    assert {"mm-hmm", "uh-huh", "uh huh", "hello hello", "test test", "um um"} <= fixed


def test_hyphens_and_apostrophes_join_words() -> None:
    assert caller_words("Mm-hmm, don\u2019t know") == ["mmhmm", "dont", "know"]


def test_filler_words_are_stored_normalized() -> None:
    assert all(caller_words(word) == [word] for word in FILLER_WORDS)


def test_none_is_not_substantive() -> None:
    assert is_substantive(None) is False


def transcript(*turns: tuple[str, str | None]) -> list[dict]:
    return [{"role": role, "message": message} for role, message in turns]


def test_only_the_caller_counts() -> None:
    turns = transcript(
        ("agent", "Thanks for calling the claims line. How can I help?"),
        ("user", "Hello?"),
        ("user", "hello hello"),
    )
    assert has_substantive_caller_speech(turns) is False


def test_one_real_caller_turn_is_enough() -> None:
    turns = transcript(("user", "mm-hmm"), ("user", "Is claim one zero zero one paid?"))
    assert has_substantive_caller_speech(turns) is True


def test_original_message_is_used_when_message_is_empty() -> None:
    turns = [{"role": "user", "message": None, "original_message": "Where is my claim?"}]
    assert has_substantive_caller_speech(turns) is True


@pytest.mark.parametrize("value", [None, [], [None, "x", 3]])
def test_missing_or_odd_transcripts_are_not_substantive(value: object) -> None:
    assert has_substantive_caller_speech(value) is False  # type: ignore[arg-type]


def test_summary_marks_a_hello_only_call_as_not_substantive() -> None:
    event = {
        "type": "post_call_transcription",
        "data": {
            "conversation_id": "conv_hello_only",
            "transcript": transcript(("agent", "Claims line, how can I help?"), ("user", "Hello?")),
        },
    }
    assert summarize(event).substantive is False


def test_summary_of_an_event_without_a_transcript_does_not_guess() -> None:
    event = {"type": "post_call_audio", "data": {"conversation_id": "c", "full_audio": "AAAA"}}
    assert summarize(event).substantive is None
