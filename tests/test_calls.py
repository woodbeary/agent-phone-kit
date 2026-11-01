"""The durable call record: one per conversation_id, merged without ever nulling a field."""

import copy
import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_phone_kit.app import create_app
from agent_phone_kit.calls import (
    LEG_AGENT,
    LEG_HUMAN,
    LEG_PARENT,
    LEG_TRANSFER,
    CallLeg,
    CallRecord,
    Claim,
    EventKey,
    PostgresCallStore,
    SqlCallStore,
    SQLiteCallStore,
    legs_from_event,
    nearest_rank,
    record_from_event,
)
from agent_phone_kit.signature import SIGNATURE_HEADER, sign_payload

URL = "/webhooks/post-call"


class Clock:
    def __init__(self, now: float = 1_800_000_000) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


StoreFactory = Callable[..., SqlCallStore]


POSTGRES_ENV = "AGENT_PHONE_KIT_TEST_POSTGRES"


@pytest.fixture(params=["sqlite-memory", "sqlite-file", "postgres"])
def make_store(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[StoreFactory]:
    """Build stores on a fresh database; ``shared=True`` opens another one on the same
    database, as a second process would.

    The PostgreSQL case runs when ``AGENT_PHONE_KIT_TEST_POSTGRES`` holds a DSN for a
    throwaway database (CI starts one); its tables are dropped first.
    """
    backend = request.param
    stores: list[SqlCallStore] = []
    if backend == "postgres":
        dsn = os.environ.get(POSTGRES_ENV)
        if not dsn:
            pytest.skip(f"set {POSTGRES_ENV} to a throwaway PostgreSQL DSN")
        import psycopg  # with the DSN set, a missing driver is an error, not a skip
    counter = iter(range(1000))
    path = tmp_path / "calls-0.sqlite3"

    def make(*, shared: bool = False, **options: Any) -> SqlCallStore:
        nonlocal path
        if backend == "sqlite-memory":
            if shared:
                pytest.skip("an in-memory database can't be shared")
            store: SqlCallStore = SQLiteCallStore(":memory:", **options)
        elif backend == "sqlite-file":
            if not shared:
                path = tmp_path / f"calls-{next(counter)}.sqlite3"
            store = SQLiteCallStore(path, **options)
        else:
            connection = psycopg.connect(dsn, autocommit=True)
            if not shared:
                connection.execute("DROP TABLE IF EXISTS calls, call_events, call_legs")
            store = PostgresCallStore(connection, **options)
        stores.append(store)
        return store

    yield make
    for store in stores:
        store.close()


@pytest.fixture
def transcription(conversation_payload: Any) -> dict[str, Any]:
    return conversation_payload("claim_approved.json")


def thinner(event: dict[str, Any], *, later_by: int = 60) -> dict[str, Any]:
    """The same event delivered again later with less in it: no transcript, metadata
    or analysis."""
    thin = copy.deepcopy(event)
    thin["event_timestamp"] = event["event_timestamp"] + later_by
    for field in ("transcript", "metadata", "analysis", "agent_id"):
        thin["data"].pop(field, None)
    return thin


def audio_event(event: dict[str, Any], *, later_by: int = 5) -> dict[str, Any]:
    return {
        "type": "post_call_audio",
        "event_timestamp": event["event_timestamp"] + later_by,
        "data": {
            "agent_id": event["data"]["agent_id"],
            "conversation_id": event["data"]["conversation_id"],
            "full_audio": "AAAA",
        },
    }


def full_record(store: SqlCallStore, conversation_id: str) -> CallRecord:
    record = store.get(conversation_id)
    assert record is not None
    return record


# --- record_from_event ---------------------------------------------------------------


def test_record_from_a_full_transcription(transcription: dict[str, Any]) -> None:
    record = record_from_event(transcription)
    assert record.conversation_id == "conv_synthetic_0001"
    assert record.status == "done"
    assert record.call_duration_secs is not None
    assert record.summary
    assert record.tools_called == ("claim_status", "end_call")
    assert record.substantive is True
    assert record.audio_received is None
    assert record.first_event_at == record.last_event_at == transcription["event_timestamp"]


def test_a_thin_event_leaves_unknown_fields_none_not_empty(
    transcription: dict[str, Any],
) -> None:
    record = record_from_event(thinner(transcription))
    assert record.status == "done"
    for name in ("agent_id", "summary", "call_duration_secs", "tools_called", "tool_errors"):
        assert getattr(record, name) is None, name
    assert record.transferred is None
    assert record.substantive is None


# --- the merge -----------------------------------------------------------------------


def test_twice_then_a_thinner_late_copy_gives_one_record_with_nothing_nulled(
    make_store: StoreFactory, transcription: dict[str, Any]
) -> None:
    store = make_store()
    store.merge(record_from_event(transcription))
    first = full_record(store, "conv_synthetic_0001")
    store.merge(record_from_event(transcription))
    store.merge(record_from_event(thinner(transcription)))
    merged = full_record(store, "conv_synthetic_0001")

    for name in ("agent_id", "summary", "call_duration_secs", "tools_called", "substantive"):
        assert getattr(merged, name) == getattr(first, name), name
        assert getattr(merged, name) is not None, name
    assert merged.last_event_at == transcription["event_timestamp"] + 60
    assert merged.first_event_at == transcription["event_timestamp"]


def test_a_newer_event_replaces_values(
    make_store: StoreFactory, transcription: dict[str, Any]
) -> None:
    store = make_store()
    store.merge(record_from_event(transcription))
    newer = thinner(transcription)
    newer["data"]["status"] = "failed"
    store.merge(record_from_event(newer))
    assert full_record(store, "conv_synthetic_0001").status == "failed"


def test_an_older_event_only_fills_gaps(
    make_store: StoreFactory, transcription: dict[str, Any]
) -> None:
    store = make_store()
    current = copy.deepcopy(transcription)
    del current["data"]["analysis"]
    store.merge(record_from_event(current))

    older = copy.deepcopy(transcription)
    older["event_timestamp"] -= 600
    older["data"]["status"] = "processing"
    store.merge(record_from_event(older))

    record = full_record(store, "conv_synthetic_0001")
    assert record.status == "done"  # the older event did not overwrite it
    assert record.summary == transcription["data"]["analysis"]["transcript_summary"]  # filled
    assert record.first_event_at == older["event_timestamp"]
    assert record.last_event_at == transcription["event_timestamp"]


def test_an_event_without_a_timestamp_only_fills_gaps(
    make_store: StoreFactory, transcription: dict[str, Any]
) -> None:
    store = make_store()
    store.merge(record_from_event(transcription))
    undated = copy.deepcopy(transcription)
    del undated["event_timestamp"]
    undated["data"]["status"] = "processing"
    store.merge(record_from_event(undated))
    record = full_record(store, "conv_synthetic_0001")
    assert record.status == "done"
    assert record.last_event_at == transcription["event_timestamp"]


def test_transcription_and_audio_fold_into_one_record_in_either_order(
    make_store: StoreFactory, transcription: dict[str, Any]
) -> None:
    for order in ((transcription, audio_event(transcription)), (audio_event(transcription),)):
        store = make_store()
        for event in (*order, transcription):
            store.merge(record_from_event(event))
        record = full_record(store, "conv_synthetic_0001")
        assert record.summary
        assert record.tools_called == ("claim_status", "end_call")
        assert record.status == "done"
        store.close()


def test_audio_marks_the_record_and_keeps_everything_else(
    make_store: StoreFactory, transcription: dict[str, Any]
) -> None:
    store = make_store()
    store.merge(record_from_event(transcription))
    store.merge(record_from_event(audio_event(transcription)))
    record = full_record(store, "conv_synthetic_0001")
    assert record.audio_received is True
    assert record.summary
    assert record.status == "done"


def test_unknown_conversation_is_none(make_store: StoreFactory) -> None:
    assert make_store().get("conv_never_seen") is None


# --- call legs ----------------------------------------------------------------------

PARENT = "CA00000000000000000000000000000010"
HUMAN = "CA00000000000000000000000000000011"
TRANSFERRED = "CA00000000000000000000000000000012"


@pytest.fixture
def transfer_call(conversation_payload: Any) -> dict[str, Any]:
    return conversation_payload("twilio_transfer_to_number.json")


def gate_event(transcription: dict[str, Any]) -> dict[str, Any]:
    """A call the register-call gate handed to the agent after a person didn't answer."""
    event = copy.deepcopy(transcription)
    event["data"]["conversation_initiation_client_data"]["dynamic_variables"].update(
        {"parent_call_sid": PARENT, "human_call_sid": HUMAN}
    )
    # On register-call the agent streams on the caller's own call: the parent SID.
    event["data"]["metadata"]["phone_call"] = {"type": "twilio", "call_sid": PARENT}
    return event


def test_a_native_twilio_transfer_names_its_leg_and_conference(
    transfer_call: dict[str, Any],
) -> None:
    record = record_from_event(transfer_call)
    assert record.legs == (CallLeg("CA00000000000000000000000000000004", LEG_AGENT),)
    assert record.transferred is True
    assert record.transfer_conference == "transfer_conf_synthetic_0004"
    assert record.tool_latency_secs == (0.31,)


def test_the_gate_legs_come_from_dynamic_variables(transcription: dict[str, Any]) -> None:
    assert legs_from_event(gate_event(transcription)) == (
        CallLeg(PARENT, LEG_PARENT),
        CallLeg(HUMAN, LEG_HUMAN),
    )


def test_an_event_without_call_sids_has_no_legs(transcription: dict[str, Any]) -> None:
    assert legs_from_event(transcription) == ()
    assert (
        legs_from_event({"type": "x", "data": {"metadata": {"phone_call": {"call_sid": " "}}}})
        == ()
    )


def test_legs_fold_onto_one_record_and_later_events_never_drop_them(
    make_store: StoreFactory, transcription: dict[str, Any]
) -> None:
    store = make_store()
    store.merge(record_from_event(gate_event(transcription)))
    store.add_leg("conv_synthetic_0001", TRANSFERRED)
    store.merge(record_from_event(thinner(transcription)))
    store.merge(record_from_event(audio_event(transcription)))
    store.merge(record_from_event(gate_event(transcription)))
    assert full_record(store, "conv_synthetic_0001").legs == (
        CallLeg(PARENT, LEG_PARENT),
        CallLeg(HUMAN, LEG_HUMAN),
        CallLeg(TRANSFERRED, LEG_TRANSFER),
    )


def test_a_leg_reported_before_the_webhook_is_on_the_record(
    make_store: StoreFactory, transfer_call: dict[str, Any]
) -> None:
    store = make_store()
    store.add_leg("conv_synthetic_0004", TRANSFERRED, LEG_TRANSFER)
    assert store.get("conv_synthetic_0004") is None
    store.merge(record_from_event(transfer_call))
    assert [leg.call_sid for leg in full_record(store, "conv_synthetic_0004").legs] == [
        "CA00000000000000000000000000000004",
        TRANSFERRED,
    ]


def test_an_empty_call_sid_is_refused(make_store: StoreFactory) -> None:
    with pytest.raises(ValueError):
        make_store().add_leg("conv_synthetic_0001", "  ")


# --- claims --------------------------------------------------------------------------


KEY = EventKey("post_call_transcription", "conv_synthetic_0001", "1767225662")


def test_a_finished_delivery_is_a_duplicate(make_store: StoreFactory) -> None:
    store = make_store()
    assert store.claim(KEY) is Claim.NEW
    store.finish(KEY)
    assert store.claim(KEY) is Claim.DUPLICATE


def test_a_delivery_being_handled_is_in_progress(make_store: StoreFactory) -> None:
    store = make_store()
    assert store.claim(KEY) is Claim.NEW
    assert store.claim(KEY) is Claim.IN_PROGRESS


def test_a_released_delivery_can_be_claimed_again(make_store: StoreFactory) -> None:
    store = make_store()
    assert store.claim(KEY) is Claim.NEW
    store.release(KEY)
    assert store.claim(KEY) is Claim.NEW


def test_a_claim_abandoned_by_a_crash_is_reclaimed_after_the_stale_window(
    make_store: StoreFactory,
) -> None:
    clock = Clock()
    store = make_store(clock=clock, stale_after_seconds=300)
    assert store.claim(KEY) is Claim.NEW  # and then the process died
    clock.now += 299
    assert store.claim(KEY) is Claim.IN_PROGRESS
    clock.now += 2
    assert store.claim(KEY) is Claim.NEW


def test_a_finished_delivery_is_never_reclaimed(make_store: StoreFactory) -> None:
    clock = Clock()
    store = make_store(clock=clock)
    store.claim(KEY)
    store.finish(KEY)
    clock.now += 86_400
    assert store.claim(KEY) is Claim.DUPLICATE


def test_other_event_types_for_the_call_are_separate_deliveries(make_store: StoreFactory) -> None:
    store = make_store()
    assert store.claim(KEY) is Claim.NEW
    audio = EventKey("post_call_audio", KEY.conversation_id, KEY.event_timestamp)
    assert store.claim(audio) is Claim.NEW


def test_two_processes_fold_their_events_into_one_record(
    make_store: StoreFactory, transcription: dict[str, Any]
) -> None:
    first = make_store()
    second = make_store(shared=True)
    second.merge(record_from_event(audio_event(transcription)))
    first.merge(record_from_event(transcription))
    second.merge(record_from_event(thinner(transcription)))
    for store in (first, second):
        record = full_record(store, "conv_synthetic_0001")
        assert record.audio_received is True
        assert record.summary
        assert record.tools_called == ("claim_status", "end_call")


def test_only_one_process_claims_a_delivery(make_store: StoreFactory) -> None:
    first = make_store()
    second = make_store(shared=True)
    assert first.claim(KEY) is Claim.NEW
    assert second.claim(KEY) is Claim.IN_PROGRESS
    first.finish(KEY)
    assert second.claim(KEY) is Claim.DUPLICATE


def test_sqlite_file_survives_a_restart(tmp_path: Path, transcription: dict[str, Any]) -> None:
    path = tmp_path / "calls.sqlite3"
    first = SQLiteCallStore(path)
    first.claim(KEY)
    first.finish(KEY)
    first.merge(record_from_event(transcription))
    first.close()

    second = SQLiteCallStore(path)
    assert second.claim(KEY) is Claim.DUPLICATE
    assert full_record(second, "conv_synthetic_0001").summary
    second.close()


# --- through the webhook -------------------------------------------------------------


class Handler:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.fail_next = False

    def __call__(self, event: dict[str, Any], summary: Any) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("downstream is down")
        self.events.append(event["type"])


def post(client: TestClient, secret: str, event: dict[str, Any]) -> Any:
    body = json.dumps(event).encode()
    headers = {SIGNATURE_HEADER: sign_payload(secret, body), "Content-Type": "application/json"}
    return client.post(URL, content=body, headers=headers)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteCallStore]:
    store = SQLiteCallStore(tmp_path / "webhook.sqlite3")
    yield store
    store.close()


def test_webhook_twice_then_thinner_late_copy(
    phone_kit_settings: Any, store: SQLiteCallStore, transcription: dict[str, Any]
) -> None:
    handler = Handler()
    client = TestClient(create_app(phone_kit_settings, store=store, on_post_call=handler))
    secret = phone_kit_settings.webhook_secret

    assert post(client, secret, transcription).json() == {"status": "received"}
    before = full_record(store, "conv_synthetic_0001")
    assert post(client, secret, transcription).json() == {"status": "duplicate"}
    assert post(client, secret, thinner(transcription)).json() == {"status": "received"}
    assert post(client, secret, audio_event(transcription)).json() == {"status": "received"}

    after = full_record(store, "conv_synthetic_0001")
    for name in (
        "agent_id",
        "status",
        "started_at",
        "call_duration_secs",
        "call_successful",
        "summary",
        "tools_called",
        "tool_errors",
        "transferred",
        "substantive",
    ):
        assert getattr(after, name) == getattr(before, name), name
        assert getattr(after, name) is not None, name
    assert after.audio_received is True
    assert after.event_types == ("post_call_audio", "post_call_transcription")
    assert handler.events == [
        "post_call_transcription",
        "post_call_transcription",
        "post_call_audio",
    ]


def test_a_repeat_after_a_restart_is_still_a_duplicate(
    phone_kit_settings: Any, transcription: dict[str, Any]
) -> None:
    # Two apps on the same CALL_STORE_PATH, as before and after a deploy.
    secret = phone_kit_settings.webhook_secret
    assert post(TestClient(create_app(phone_kit_settings)), secret, transcription).json() == {
        "status": "received"
    }
    restarted = TestClient(create_app(phone_kit_settings))
    assert post(restarted, secret, transcription).json() == {"status": "duplicate"}


def test_a_delivery_still_being_handled_gets_a_retryable_503(
    phone_kit_settings: Any, store: SQLiteCallStore, transcription: dict[str, Any]
) -> None:
    client = TestClient(create_app(phone_kit_settings, store=store, on_post_call=Handler()))
    assert store.claim(EventKey.of(transcription)) is Claim.NEW  # another worker has it
    response = post(client, phone_kit_settings.webhook_secret, transcription)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"


def test_a_store_failure_is_a_500_and_the_retry_is_processed(
    phone_kit_settings: Any, store: SQLiteCallStore, transcription: dict[str, Any]
) -> None:
    class FlakyStore(SQLiteCallStore):
        fail_next = True

        def merge(self, record: CallRecord) -> None:
            if self.fail_next:
                self.fail_next = False
                raise OSError("disk full")
            super().merge(record)

    flaky = FlakyStore(":memory:")
    handler = Handler()
    client = TestClient(create_app(phone_kit_settings, store=flaky, on_post_call=handler))
    secret = phone_kit_settings.webhook_secret
    assert post(client, secret, transcription).status_code == 500
    assert handler.events == []
    assert post(client, secret, transcription).json() == {"status": "received"}
    assert full_record(flaky, "conv_synthetic_0001").summary
    flaky.close()


def test_a_handler_failure_keeps_the_record_and_the_retry_is_processed(
    phone_kit_settings: Any, store: SQLiteCallStore, transcription: dict[str, Any]
) -> None:
    handler = Handler()
    handler.fail_next = True
    client = TestClient(create_app(phone_kit_settings, store=store, on_post_call=handler))
    secret = phone_kit_settings.webhook_secret
    assert post(client, secret, transcription).status_code == 500
    assert post(client, secret, transcription).json() == {"status": "received"}
    assert handler.events == ["post_call_transcription"]
    assert full_record(store, "conv_synthetic_0001").event_types == ("post_call_transcription",)


def test_the_transfer_fixture_through_the_webhook(
    phone_kit_settings: Any, store: SQLiteCallStore, transfer_call: dict[str, Any]
) -> None:
    client = TestClient(create_app(phone_kit_settings, store=store, on_post_call=Handler()))
    assert post(client, phone_kit_settings.webhook_secret, transfer_call).status_code == 200
    store.add_leg("conv_synthetic_0004", TRANSFERRED)  # from your own transfer callback
    record = full_record(store, "conv_synthetic_0004")
    assert [leg.leg for leg in record.legs] == [LEG_AGENT, LEG_TRANSFER]
    assert record.transfer_conference == "transfer_conf_synthetic_0004"
    assert record.termination_reason == "Call transferred to +12025550199"


def test_the_default_store_is_a_sqlite_file_at_the_configured_path(
    phone_kit_settings: Any, transcription: dict[str, Any]
) -> None:
    client = TestClient(create_app(phone_kit_settings))
    post(client, phone_kit_settings.webhook_secret, transcription)
    store = SQLiteCallStore(phone_kit_settings.call_store_path)
    assert full_record(store, "conv_synthetic_0001").summary
    store.close()


# --- volume --------------------------------------------------------------------------


def call(conversation_id: str, *caller_lines: str, latency: tuple[float, ...] = ()) -> dict:
    turns: list[dict[str, Any]] = [{"role": "agent", "message": "Claims line. How can I help?"}]
    turns += [{"role": "user", "message": line} for line in caller_lines]
    if latency:
        turns.append(
            {
                "role": "agent",
                "tool_results": [
                    {"tool_name": "claim_status", "is_error": False, "tool_latency_secs": value}
                    for value in latency
                ],
            }
        )
    return {
        "type": "post_call_transcription",
        "event_timestamp": 1767230000,
        "data": {
            "conversation_id": conversation_id,
            "transcript": turns,
            "metadata": {"start_time_unix_secs": 1767229000},
        },
    }


def test_volume_reports_the_substantive_rate_next_to_raw_volume(
    make_store: StoreFactory,
) -> None:
    store = make_store()
    for event in (
        call("conv_a", "Is claim one zero zero one paid?", latency=(0.2, 0.4)),
        call("conv_b", "hello?", "hello hello"),
        call("conv_c", "mm-hmm"),
        call("conv_d", "test test"),
        call("conv_e", "I need an adjuster for my claim", latency=(1.9,)),
    ):
        store.merge(record_from_event(event))
    audio_only = {"type": "post_call_audio", "event_timestamp": 1767230005, "data": {}}
    audio_only["data"]["conversation_id"] = "conv_f"
    store.merge(record_from_event(audio_only))

    volume = store.volume()
    assert (volume.calls, volume.substantive_calls, volume.unknown_calls) == (6, 2, 1)
    assert volume.substantive_call_rate == pytest.approx(2 / 6)
    assert volume.tool_results == 3
    assert (volume.tool_latency_p50, volume.tool_latency_p95) == (0.4, 1.9)
    assert volume.as_dict()["substantive_call_rate"] == 0.3333


def test_volume_since_counts_only_later_calls(make_store: StoreFactory) -> None:
    store = make_store()
    store.merge(record_from_event(call("conv_old", "where is my claim")))
    later = call("conv_new", "where is my claim", latency=(0.5,))
    later["data"]["metadata"]["start_time_unix_secs"] = 1767300000
    store.merge(record_from_event(later))
    volume = store.volume(since=1767250000)
    assert (volume.calls, volume.substantive_calls, volume.tool_results) == (1, 1, 1)


def test_volume_of_an_empty_store(make_store: StoreFactory) -> None:
    volume = make_store().volume()
    assert (volume.calls, volume.substantive_call_rate, volume.tool_latency_p50) == (0, None, None)


@pytest.mark.parametrize(
    ("values", "percent", "expected"),
    [
        ([], 50, None),
        ([3.0], 95, 3.0),
        ([1.0, 2.0, 3.0, 4.0], 50, 2.0),
        ([1.0, 2.0, 3.0, 4.0], 95, 4.0),
        ([float(n) for n in range(1, 21)], 95, 19.0),
        ([float(n) for n in range(1, 21)], 50, 10.0),
    ],
)
def test_nearest_rank(values: list[float], percent: float, expected: float | None) -> None:
    assert nearest_rank(values, percent) == expected
