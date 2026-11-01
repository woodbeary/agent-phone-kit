"""One durable record per call, keyed on ``conversation_id``.

ElevenLabs sends several post-call events for one call (``post_call_transcription``,
``post_call_audio``, ``call_initiation_failure``). Each can be retried, retries
carry the identical payload, and a later event can carry less than an earlier one.
``CallStore`` keeps one row per ``conversation_id`` and merges every event into it:

* A field is filled or replaced, never nulled: the merge is ``COALESCE(new, old)``.
* ``event_timestamp`` guards the order. An event older than the newest one already
  merged only fills fields that are still empty; it never overwrites a newer value.
* Repeat deliveries are dropped on ``(type, conversation_id, event_timestamp)``, and
  the store keeps those keys too, so a restart doesn't forget them. A delivery whose
  handler crashed the process mid-way can be claimed again after
  ``stale_after_seconds``.

The merge is one ``INSERT ... ON CONFLICT (conversation_id) DO UPDATE`` statement
that runs unchanged on SQLite (the default, ``SQLiteCallStore``) and PostgreSQL
(``PostgresCallStore``, for more than one process). ``UPSERT_SQL`` and ``SCHEMA_SQL``
are the recipe; ``docs/postgres.md`` shows them with PostgreSQL placeholders.
"""

import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields
from enum import Enum
from os import PathLike
from typing import Any, Protocol

from agent_phone_kit.substantive import has_substantive_caller_speech

TRANSFER_TOOLS = frozenset({"transfer_to_number", "transfer_to_agent"})
DEFAULT_STALE_AFTER_SECONDS = 300


@dataclass(frozen=True)
class EventKey:
    """What makes a delivery a repeat: ElevenLabs retries carry the same three values."""

    event_type: str
    conversation_id: str
    event_timestamp: str

    @classmethod
    def of(cls, event: Mapping[str, Any]) -> "EventKey":
        data = _as_dict(event.get("data"))
        return cls(
            str(event.get("type")),
            str(data.get("conversation_id")),
            str(event.get("event_timestamp")),
        )


@dataclass(frozen=True)
class CallRecord:
    """Everything the store keeps about one call. ``None`` means "not known yet"."""

    conversation_id: str
    agent_id: str | None = None
    status: str | None = None
    started_at: int | None = None
    call_duration_secs: int | None = None
    termination_reason: str | None = None
    call_successful: str | None = None
    summary: str | None = None
    failure_reason: str | None = None
    tools_called: tuple[str, ...] | None = None
    tool_errors: int | None = None
    tool_latency_secs: tuple[float, ...] | None = None
    transferred: bool | None = None
    substantive: bool | None = None
    audio_received: bool | None = None
    first_event_at: int | None = None
    last_event_at: int | None = None
    event_types: tuple[str, ...] = ()
    """Read only: the event types merged so far."""


class Claim(Enum):
    NEW = "new"
    DUPLICATE = "duplicate"
    IN_PROGRESS = "in progress"


class CallStore(Protocol):
    def claim(self, key: EventKey) -> Claim:
        """Start handling a delivery, unless it was handled or is being handled."""
        ...

    def finish(self, key: EventKey) -> None:
        """Mark a claimed delivery as handled, so repeats are duplicates."""
        ...

    def release(self, key: EventKey) -> None:
        """Forget a claimed delivery, so its retry is handled."""
        ...

    def merge(self, record: CallRecord) -> None:
        """Merge one event's facts into the call's record."""
        ...

    def get(self, conversation_id: str) -> CallRecord | None: ...


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,18}", value.strip()):
        return int(value)
    return None


def _str_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def record_from_event(event: Mapping[str, Any]) -> CallRecord:
    """The facts one post-call event carries. Anything the event lacks is ``None``."""
    data = _as_dict(event.get("data"))
    metadata = _as_dict(data.get("metadata"))
    analysis = _as_dict(data.get("analysis"))
    event_type = str(event.get("type"))
    timestamp = _int_or_none(event.get("event_timestamp"))

    transcript = data.get("transcript")
    tools: dict[str, Any] = {}
    if isinstance(transcript, list):
        turns = [_as_dict(turn) for turn in transcript]
        calls = [_as_dict(c) for turn in turns for c in turn.get("tool_calls") or []]
        results = [_as_dict(r) for turn in turns for r in turn.get("tool_results") or []]
        names = tuple(str(call.get("tool_name", "")) for call in calls)
        tools = {
            "tools_called": names,
            "tool_errors": sum(1 for result in results if result.get("is_error")),
            "tool_latency_secs": tuple(
                float(r["tool_latency_secs"])
                for r in results
                if isinstance(r.get("tool_latency_secs"), int | float)
                and not isinstance(r.get("tool_latency_secs"), bool)
            ),
            "transferred": any(name in TRANSFER_TOOLS for name in names),
            "substantive": has_substantive_caller_speech(transcript),
        }

    return CallRecord(
        conversation_id=str(data.get("conversation_id")),
        agent_id=_str_or_none(data.get("agent_id")),
        status=_str_or_none(data.get("status")),
        started_at=_int_or_none(metadata.get("start_time_unix_secs")),
        call_duration_secs=_int_or_none(metadata.get("call_duration_secs")),
        termination_reason=_str_or_none(metadata.get("termination_reason")),
        call_successful=_str_or_none(analysis.get("call_successful")),
        summary=_str_or_none(analysis.get("transcript_summary")),
        failure_reason=_str_or_none(data.get("failure_reason")),
        audio_received=True if event_type == "post_call_audio" else None,
        first_event_at=timestamp,
        last_event_at=timestamp,
        **tools,
    )


# Columns merged with COALESCE, in table order.
_MERGED = (
    "agent_id",
    "status",
    "started_at",
    "call_duration_secs",
    "termination_reason",
    "call_successful",
    "summary",
    "failure_reason",
    "tools_called",
    "tool_errors",
    "tool_latency_secs",
    "transferred",
    "substantive",
    "audio_received",
)
_JSON_LISTS = frozenset({"tools_called", "tool_latency_secs"})
_BOOLEANS = frozenset({"transferred", "substantive", "audio_received"})

SCHEMA_SQL = (
    """CREATE TABLE IF NOT EXISTS calls (
    conversation_id TEXT PRIMARY KEY,
    agent_id TEXT,
    status TEXT,
    started_at BIGINT,
    call_duration_secs BIGINT,
    termination_reason TEXT,
    call_successful TEXT,
    summary TEXT,
    failure_reason TEXT,
    tools_called TEXT,
    tool_errors BIGINT,
    tool_latency_secs TEXT,
    transferred BOOLEAN,
    substantive BOOLEAN,
    audio_received BOOLEAN,
    first_event_at BIGINT,
    last_event_at BIGINT
)""",
    """CREATE TABLE IF NOT EXISTS call_events (
    event_type TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    event_timestamp TEXT NOT NULL,
    state TEXT NOT NULL,
    claimed_at BIGINT NOT NULL,
    PRIMARY KEY (event_type, conversation_id, event_timestamp)
)""",
)
"""The tables, in SQL that SQLite and PostgreSQL both accept."""

_NEWER = (
    "(excluded.last_event_at IS NOT NULL"
    " AND (calls.last_event_at IS NULL OR excluded.last_event_at >= calls.last_event_at))"
)


def _merge_column(column: str) -> str:
    return (
        f"    {column} = CASE WHEN {_NEWER}"
        f" THEN COALESCE(excluded.{column}, calls.{column})"
        f" ELSE COALESCE(calls.{column}, excluded.{column}) END"
    )


UPSERT_SQL = "\n".join(
    [
        "INSERT INTO calls (conversation_id, " + ", ".join(_MERGED) + ", first_event_at, "
        "last_event_at)",
        "VALUES (:conversation_id, "
        + ", ".join(f":{c}" for c in _MERGED)
        + ", :first_event_at, :last_event_at)",
        "ON CONFLICT (conversation_id) DO UPDATE SET",
        ",\n".join(
            [
                *(_merge_column(column) for column in _MERGED),
                "    first_event_at = CASE WHEN excluded.first_event_at IS NOT NULL"
                " AND (calls.first_event_at IS NULL"
                " OR excluded.first_event_at < calls.first_event_at)"
                " THEN excluded.first_event_at ELSE calls.first_event_at END",
                f"    last_event_at = CASE WHEN {_NEWER}"
                " THEN excluded.last_event_at ELSE calls.last_event_at END",
            ]
        ),
    ]
)
"""Merge one event into its call: ``COALESCE`` so nothing is nulled, newest event wins."""

CLAIM_SQL = """INSERT INTO call_events
    (event_type, conversation_id, event_timestamp, state, claimed_at)
VALUES (:event_type, :conversation_id, :event_timestamp, 'processing', :now)
ON CONFLICT (event_type, conversation_id, event_timestamp) DO UPDATE
    SET claimed_at = excluded.claimed_at
    WHERE call_events.state = 'processing' AND call_events.claimed_at < :stale_before"""

_STATE_SQL = """SELECT state FROM call_events
WHERE event_type = :event_type AND conversation_id = :conversation_id
    AND event_timestamp = :event_timestamp"""

_FINISH_SQL = """UPDATE call_events SET state = 'done'
WHERE event_type = :event_type AND conversation_id = :conversation_id
    AND event_timestamp = :event_timestamp"""

_RELEASE_SQL = """DELETE FROM call_events
WHERE event_type = :event_type AND conversation_id = :conversation_id
    AND event_timestamp = :event_timestamp"""

_SELECT_SQL = """SELECT conversation_id, agent_id, status, started_at, call_duration_secs,
    termination_reason, call_successful, summary, failure_reason, tools_called, tool_errors,
    tool_latency_secs, transferred, substantive, audio_received, first_event_at, last_event_at
FROM calls WHERE conversation_id = :conversation_id"""

_EVENT_TYPES_SQL = """SELECT DISTINCT event_type FROM call_events
WHERE conversation_id = :conversation_id AND state = 'done' ORDER BY event_type"""

_NAMED = re.compile(r":([a-z_]+)")


def pyformat(sql: str) -> str:
    """Rewrite ``:name`` placeholders as ``%(name)s``, the style psycopg uses."""
    return _NAMED.sub(r"%(\1)s", sql)


def _to_params(record: CallRecord) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for field in fields(CallRecord):
        if field.name == "event_types":
            continue
        value = getattr(record, field.name)
        if field.name in _JSON_LISTS and value is not None:
            value = json.dumps(list(value))
        params[field.name] = value
    return params


def _from_row(row: Sequence[Any], event_types: tuple[str, ...]) -> CallRecord:
    names = ("conversation_id", *_MERGED, "first_event_at", "last_event_at")
    values: dict[str, Any] = dict(zip(names, row, strict=True))
    for name in _JSON_LISTS:
        if values[name] is not None:
            values[name] = tuple(json.loads(values[name]))
    for name in _BOOLEANS:
        if values[name] is not None:
            values[name] = bool(values[name])
    return CallRecord(**values, event_types=event_types)


class SqlCallStore:
    """The ``CallStore`` logic, for any DB-API connection in autocommit mode."""

    def __init__(
        self,
        connection: Any,
        *,
        rewrite: Callable[[str], str] = str,
        stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._connection = connection
        self._rewrite = rewrite
        self._stale_after = stale_after_seconds
        self._clock = clock
        self._lock = threading.Lock()
        for statement in SCHEMA_SQL:
            self._execute(statement, {})

    def _execute(self, sql: str, params: Mapping[str, Any]) -> int:
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute(self._rewrite(sql), params)
                return cursor.rowcount
            finally:
                cursor.close()

    def _query(self, sql: str, params: Mapping[str, Any]) -> list[Sequence[Any]]:
        with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute(self._rewrite(sql), params)
                return list(cursor.fetchall())
            finally:
                cursor.close()

    @staticmethod
    def _key(key: EventKey) -> dict[str, str]:
        return {
            "event_type": key.event_type,
            "conversation_id": key.conversation_id,
            "event_timestamp": key.event_timestamp,
        }

    def claim(self, key: EventKey) -> Claim:
        now = int(self._clock())
        params = {**self._key(key), "now": now, "stale_before": now - self._stale_after}
        if self._execute(CLAIM_SQL, params) == 1:
            return Claim.NEW
        rows = self._query(_STATE_SQL, self._key(key))
        if rows and rows[0][0] == "processing":
            return Claim.IN_PROGRESS
        return Claim.DUPLICATE

    def finish(self, key: EventKey) -> None:
        self._execute(_FINISH_SQL, self._key(key))

    def release(self, key: EventKey) -> None:
        self._execute(_RELEASE_SQL, self._key(key))

    def merge(self, record: CallRecord) -> None:
        self._execute(UPSERT_SQL, _to_params(record))

    def get(self, conversation_id: str) -> CallRecord | None:
        params = {"conversation_id": conversation_id}
        rows = self._query(_SELECT_SQL, params)
        if not rows:
            return None
        types = tuple(str(row[0]) for row in self._query(_EVENT_TYPES_SQL, params))
        return _from_row(rows[0], types)

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class SQLiteCallStore(SqlCallStore):
    """The default store: one SQLite file, safe to share between threads."""

    def __init__(self, path: str | PathLike[str] = "calls.sqlite3", **options: Any) -> None:
        connection = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None, timeout=5.0
        )
        if str(path) != ":memory:":
            connection.execute("PRAGMA journal_mode=WAL")
        super().__init__(connection, **options)


class PostgresCallStore(SqlCallStore):
    """The same store on PostgreSQL, for more than one process.

    Pass a psycopg 3 connection in autocommit mode::

        import psycopg
        store = PostgresCallStore(psycopg.connect(dsn, autocommit=True))
        app = create_app(store=store)

    The statements are the SQLite ones with ``%(name)s`` placeholders.
    """

    def __init__(self, connection: Any, **options: Any) -> None:
        super().__init__(connection, rewrite=pyformat, **options)
