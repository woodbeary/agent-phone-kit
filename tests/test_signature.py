"""ElevenLabs-Signature verification, against vectors built from the scheme.

Scheme (ElevenLabs SDK ``construct_event``): header ``t=<unix>,v0=<hex>``,
digest = HMAC-SHA256(secret, f"{t}.{raw body}"), reject when older than 30 min.
"""

import pytest

from agent_phone_kit.signature import (
    DEFAULT_TOLERANCE_SECONDS,
    MAX_FUTURE_SKEW_SECONDS,
    SignatureError,
    compute_signature,
    sign_payload,
    verify_signature,
)

# Known-answer vector, computed independently with:
#   printf '%s' "$T.$BODY" | openssl dgst -sha256 -hmac "$KEY"
KAT_KEY = "kit-known-answer-key"
KAT_T = 1767225600
KAT_BODY = b'{"type":"post_call_transcription","data":{"conversation_id":"conv_kat"}}'
KAT_DIGEST = "770fc424bce4fdf835577a3288bf2173c0395c510a0aa563fbb340776f9e65a7"

KEY = "test-webhook-key"
BODY = b'{"type":"post_call_transcription","data":{"conversation_id":"conv_1"}}'
NOW = 1_800_000_000


def header_for(body: bytes = BODY, *, t: int = NOW, key: str = KEY) -> str:
    return f"t={t},v0={compute_signature(key, t, body)}"


def test_known_answer_vector() -> None:
    assert compute_signature(KAT_KEY, KAT_T, KAT_BODY) == KAT_DIGEST
    header = f"t={KAT_T},v0={KAT_DIGEST}"
    assert verify_signature(KAT_BODY, header, KAT_KEY, now=KAT_T + 1) == KAT_T


def test_sign_payload_round_trips() -> None:
    header = sign_payload(KEY, BODY, timestamp=NOW)
    assert header == header_for()
    assert verify_signature(BODY, header, KEY, now=NOW) == NOW


def test_sign_payload_defaults_to_current_time() -> None:
    assert verify_signature(BODY, sign_payload(KEY, BODY), KEY) > 0


def test_tampered_body_is_rejected() -> None:
    with pytest.raises(SignatureError, match="does not match"):
        verify_signature(BODY.replace(b"conv_1", b"conv_2"), header_for(), KEY, now=NOW)


def test_wrong_secret_is_rejected() -> None:
    with pytest.raises(SignatureError, match="does not match"):
        verify_signature(BODY, header_for(key="another-key"), KEY, now=NOW)


def test_timestamp_is_part_of_the_signed_message() -> None:
    forged = f"t={NOW + 1},v0={compute_signature(KEY, NOW, BODY)}"
    with pytest.raises(SignatureError, match="does not match"):
        verify_signature(BODY, forged, KEY, now=NOW)


def test_stale_timestamp_is_rejected_even_with_a_valid_signature() -> None:
    old = NOW - DEFAULT_TOLERANCE_SECONDS - 1
    with pytest.raises(SignatureError, match="old"):
        verify_signature(BODY, header_for(t=old), KEY, now=NOW)


def test_timestamp_exactly_at_the_tolerance_is_accepted() -> None:
    edge = NOW - DEFAULT_TOLERANCE_SECONDS
    assert verify_signature(BODY, header_for(t=edge), KEY, now=NOW) == edge


def test_tolerance_is_configurable() -> None:
    with pytest.raises(SignatureError, match="old"):
        verify_signature(BODY, header_for(t=NOW - 61), KEY, now=NOW, tolerance_seconds=60)


def test_small_clock_skew_into_the_future_is_accepted() -> None:
    ahead = NOW + MAX_FUTURE_SKEW_SECONDS
    assert verify_signature(BODY, header_for(t=ahead), KEY, now=NOW) == ahead


def test_far_future_timestamp_is_rejected() -> None:
    with pytest.raises(SignatureError, match="future"):
        verify_signature(BODY, header_for(t=NOW + MAX_FUTURE_SKEW_SECONDS + 1), KEY, now=NOW)


@pytest.mark.parametrize("header", [None, ""])
def test_missing_header_is_rejected(header: str | None) -> None:
    with pytest.raises(SignatureError, match="missing"):
        verify_signature(BODY, header, KEY, now=NOW)


@pytest.mark.parametrize(
    "header",
    [
        f"v0={compute_signature(KEY, NOW, BODY)}",  # no timestamp
        f"t={NOW}",  # no signature
        f"t={NOW},v1={compute_signature(KEY, NOW, BODY)}",  # unknown scheme only
        "garbage",
    ],
)
def test_incomplete_header_is_rejected(header: str) -> None:
    with pytest.raises(SignatureError, match="needs both"):
        verify_signature(BODY, header, KEY, now=NOW)


@pytest.mark.parametrize("t", ["abc", "-5", "1.5", "\u0661\u0662\u0663", "1" * 13])
def test_non_integer_timestamp_is_rejected(t: str) -> None:
    with pytest.raises(SignatureError, match="unix time"):
        verify_signature(BODY, f"t={t},v0=00", KEY, now=NOW)


def test_spaces_and_extra_fields_are_tolerated() -> None:
    header = f"t={NOW}, v0={compute_signature(KEY, NOW, BODY)}, v1=ignored"
    assert verify_signature(BODY, header, KEY, now=NOW) == NOW


def test_any_matching_v0_is_accepted() -> None:
    header = f"t={NOW},v0={'0' * 64},v0={compute_signature(KEY, NOW, BODY)}"
    assert verify_signature(BODY, header, KEY, now=NOW) == NOW


def test_non_ascii_signature_is_rejected_not_crashing() -> None:
    with pytest.raises(SignatureError, match="does not match"):
        verify_signature(BODY, f"t={NOW},v0=é", KEY, now=NOW)


def test_empty_secret_is_a_configuration_error() -> None:
    with pytest.raises(ValueError, match="not configured"):
        verify_signature(BODY, header_for(), "", now=NOW)
