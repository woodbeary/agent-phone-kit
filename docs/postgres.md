# Call records on PostgreSQL

The default call store is one SQLite file, which is right for one process on one
host. With more than one process or host, put the records in PostgreSQL. The
statements are the same ones the SQLite store runs; only the placeholders change.

```python
import psycopg
from agent_phone_kit.app import create_app
from agent_phone_kit.calls import PostgresCallStore

store = PostgresCallStore(psycopg.connect(dsn, autocommit=True))
app = create_app(store=store)
```

Install the driver with the `postgres` extra (`pip install
"agent-phone-kit[postgres]"`, which adds `psycopg[binary]`). `PostgresCallStore` creates its tables if they don't exist
and serializes its own calls on one connection. For more throughput, run more
processes; each holds its own connection and the database does the rest.

## Tables

The `calls` table holds one row per `conversation_id`. `call_legs` holds every
call SID seen for a conversation. `call_events` holds one row per delivery,
`(type, conversation_id, event_timestamp)`, so repeats are dropped across
processes and restarts.

```sql
CREATE TABLE IF NOT EXISTS calls (
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
    transfer_conference TEXT,
    first_event_at BIGINT,
    last_event_at BIGINT
);

CREATE TABLE IF NOT EXISTS call_events (
    event_type TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    event_timestamp TEXT NOT NULL,
    state TEXT NOT NULL,
    claimed_at BIGINT NOT NULL,
    PRIMARY KEY (event_type, conversation_id, event_timestamp)
);

CREATE TABLE IF NOT EXISTS call_legs (
    conversation_id TEXT NOT NULL,
    call_sid TEXT NOT NULL,
    leg TEXT NOT NULL,
    PRIMARY KEY (conversation_id, call_sid)
);
```

## Merging an event

Every column is `COALESCE(new, old)`, so a field is filled or replaced but never
nulled. An event at least as new as the newest one merged (by `event_timestamp`)
takes precedence; an older one only fills fields that are still empty.

```sql
INSERT INTO calls (conversation_id, agent_id, status, started_at, call_duration_secs, termination_reason, call_successful, summary, failure_reason, tools_called, tool_errors, tool_latency_secs, transferred, substantive, audio_received, transfer_conference, first_event_at, last_event_at)
VALUES (%(conversation_id)s, %(agent_id)s, %(status)s, %(started_at)s, %(call_duration_secs)s, %(termination_reason)s, %(call_successful)s, %(summary)s, %(failure_reason)s, %(tools_called)s, %(tool_errors)s, %(tool_latency_secs)s, %(transferred)s, %(substantive)s, %(audio_received)s, %(transfer_conference)s, %(first_event_at)s, %(last_event_at)s)
ON CONFLICT (conversation_id) DO UPDATE SET
    agent_id = CASE WHEN (excluded.last_event_at IS NOT NULL AND (calls.last_event_at IS NULL OR excluded.last_event_at >= calls.last_event_at)) THEN COALESCE(excluded.agent_id, calls.agent_id) ELSE COALESCE(calls.agent_id, excluded.agent_id) END,
    status = CASE WHEN (excluded.last_event_at IS NOT NULL AND (calls.last_event_at IS NULL OR excluded.last_event_at >= calls.last_event_at)) THEN COALESCE(excluded.status, calls.status) ELSE COALESCE(calls.status, excluded.status) END,
    started_at = CASE WHEN (excluded.last_event_at IS NOT NULL AND (calls.last_event_at IS NULL OR excluded.last_event_at >= calls.last_event_at)) THEN COALESCE(excluded.started_at, calls.started_at) ELSE COALESCE(calls.started_at, excluded.started_at) END,
    call_duration_secs = CASE WHEN (excluded.last_event_at IS NOT NULL AND (calls.last_event_at IS NULL OR excluded.last_event_at >= calls.last_event_at)) THEN COALESCE(excluded.call_duration_secs, calls.call_duration_secs) ELSE COALESCE(calls.call_duration_secs, excluded.call_duration_secs) END,
    termination_reason = CASE WHEN (excluded.last_event_at IS NOT NULL AND (calls.last_event_at IS NULL OR excluded.last_event_at >= calls.last_event_at)) THEN COALESCE(excluded.termination_reason, calls.termination_reason) ELSE COALESCE(calls.termination_reason, excluded.termination_reason) END,
    call_successful = CASE WHEN (excluded.last_event_at IS NOT NULL AND (calls.last_event_at IS NULL OR excluded.last_event_at >= calls.last_event_at)) THEN COALESCE(excluded.call_successful, calls.call_successful) ELSE COALESCE(calls.call_successful, excluded.call_successful) END,
    summary = CASE WHEN (excluded.last_event_at IS NOT NULL AND (calls.last_event_at IS NULL OR excluded.last_event_at >= calls.last_event_at)) THEN COALESCE(excluded.summary, calls.summary) ELSE COALESCE(calls.summary, excluded.summary) END,
    failure_reason = CASE WHEN (excluded.last_event_at IS NOT NULL AND (calls.last_event_at IS NULL OR excluded.last_event_at >= calls.last_event_at)) THEN COALESCE(excluded.failure_reason, calls.failure_reason) ELSE COALESCE(calls.failure_reason, excluded.failure_reason) END,
    tools_called = CASE WHEN (excluded.last_event_at IS NOT NULL AND (calls.last_event_at IS NULL OR excluded.last_event_at >= calls.last_event_at)) THEN COALESCE(excluded.tools_called, calls.tools_called) ELSE COALESCE(calls.tools_called, excluded.tools_called) END,
    tool_errors = CASE WHEN (excluded.last_event_at IS NOT NULL AND (calls.last_event_at IS NULL OR excluded.last_event_at >= calls.last_event_at)) THEN COALESCE(excluded.tool_errors, calls.tool_errors) ELSE COALESCE(calls.tool_errors, excluded.tool_errors) END,
    tool_latency_secs = CASE WHEN (excluded.last_event_at IS NOT NULL AND (calls.last_event_at IS NULL OR excluded.last_event_at >= calls.last_event_at)) THEN COALESCE(excluded.tool_latency_secs, calls.tool_latency_secs) ELSE COALESCE(calls.tool_latency_secs, excluded.tool_latency_secs) END,
    transferred = CASE WHEN (excluded.last_event_at IS NOT NULL AND (calls.last_event_at IS NULL OR excluded.last_event_at >= calls.last_event_at)) THEN COALESCE(excluded.transferred, calls.transferred) ELSE COALESCE(calls.transferred, excluded.transferred) END,
    substantive = CASE WHEN (excluded.last_event_at IS NOT NULL AND (calls.last_event_at IS NULL OR excluded.last_event_at >= calls.last_event_at)) THEN COALESCE(excluded.substantive, calls.substantive) ELSE COALESCE(calls.substantive, excluded.substantive) END,
    audio_received = CASE WHEN (excluded.last_event_at IS NOT NULL AND (calls.last_event_at IS NULL OR excluded.last_event_at >= calls.last_event_at)) THEN COALESCE(excluded.audio_received, calls.audio_received) ELSE COALESCE(calls.audio_received, excluded.audio_received) END,
    transfer_conference = CASE WHEN (excluded.last_event_at IS NOT NULL AND (calls.last_event_at IS NULL OR excluded.last_event_at >= calls.last_event_at)) THEN COALESCE(excluded.transfer_conference, calls.transfer_conference) ELSE COALESCE(calls.transfer_conference, excluded.transfer_conference) END,
    first_event_at = CASE WHEN excluded.first_event_at IS NOT NULL AND (calls.first_event_at IS NULL OR excluded.first_event_at < calls.first_event_at) THEN excluded.first_event_at ELSE calls.first_event_at END,
    last_event_at = CASE WHEN (excluded.last_event_at IS NOT NULL AND (calls.last_event_at IS NULL OR excluded.last_event_at >= calls.last_event_at)) THEN excluded.last_event_at ELSE calls.last_event_at END;
```

Then each call SID the event names is added to `call_legs`; a SID already there
keeps its first label.

```sql
INSERT INTO call_legs (conversation_id, call_sid, leg)
VALUES (%(conversation_id)s, %(call_sid)s, %(leg)s)
ON CONFLICT (conversation_id, call_sid) DO NOTHING;
```

## Claiming a delivery

A delivery is claimed before it is handled and marked `done` after. A claim left
in `processing` by a process that died is taken over once it is older than the
stale window (5 minutes by default), so a retry isn't dropped forever.

```sql
INSERT INTO call_events
    (event_type, conversation_id, event_timestamp, state, claimed_at)
VALUES (%(event_type)s, %(conversation_id)s, %(event_timestamp)s, 'processing', %(now)s)
ON CONFLICT (event_type, conversation_id, event_timestamp) DO UPDATE
    SET claimed_at = excluded.claimed_at
    WHERE call_events.state = 'processing' AND call_events.claimed_at < %(stale_before)s;
```

One row inserted or updated means the claim is yours. Zero means another
delivery of the event is done (`duplicate`) or still being handled (answer 503 so
ElevenLabs retries).

## Tested

`tests/test_calls.py` runs every store test against PostgreSQL when
`AGENT_PHONE_KIT_TEST_POSTGRES` holds the DSN of a throwaway database. CI's
`postgres` job starts PostgreSQL 16 as a service and sets it. `tests/test_docs.py`
checks that the SQL on this page is the SQL the code runs.
