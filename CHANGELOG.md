# Changelog

## 0.2.0

- The claim-status tool answers within a 2 s budget (`TOOL_BUDGET_SECONDS`). A
  slow, failing or saturated backend gets a 200 with a fallback the agent can say,
  instead of a 503. The example tool config sets `response_timeout_secs: 5` and
  `pre_tool_speech: "auto"`.
- `CallStore`: one durable record per `conversation_id`. Each post-call event is
  merged with `COALESCE`, so a later, thinner event never nulls a field; an older
  event (by `event_timestamp`) only fills empty fields; transcription and audio
  events fold together. SQLite by default (`CALL_STORE_PATH`); `PostgresCallStore`
  runs the same SQL on PostgreSQL ([docs/postgres.md](docs/postgres.md)).
  Duplicate keys are stored too, so they survive a restart. **Breaking:**
  `RecentEvents` is gone, and `post_call_router` takes `store=` instead of
  `recent=`.
- Call legs: every call SID a call used (parent, human, agent, transfer) is kept on
  its record; `add_leg()` adds ones your own callbacks see.
- `is_substantive()` and `CallSummary.substantive`, ported from a production
  TypeScript rule with two bugs fixed ("mm-hmm"/"uh-huh" and repeated filler like
  "hello hello" used to count). `store.volume()` reports the substantive call rate
  next to raw volume, with tool latency p50 and p95.
- A press-1 gate on your own Twilio number (`/twilio/voice`, `/twilio/gate`,
  `/twilio/dial-result`) with ElevenLabs' register-call as the fallback. It checks
  `X-Twilio-Signature` against every URL a proxy could have rewritten, fails
  closed, and normalizes numbers to E.164. `GATE_HUMAN_NUMBER` turns it on; an
  `ELEVENLABS_API_KEY` or `TWILIO_AUTH_TOKEN` exported for other tools doesn't.
- `agent-phone-kit` command: `lint` (consent in the agent's first message, run in
  CI), `post-call` (sign and send a saved webhook body) and `stats`.
- `docs/demo.md`, a two-minute demo on the claim-status tool.
- Fixtures: a native Twilio call with `transfer_to_number`, and a widget
  conversation.

## 0.1.0

First release.

- `POST /tools/claim-status`: an example ElevenLabs server tool with bearer-secret
  auth and claim-number normalization, backed by synthetic data.
- `POST /webhooks/post-call`: verifies `ElevenLabs-Signature` (HMAC-SHA256 over
  `t.body`), rejects timestamps older than 30 minutes or more than 5 minutes ahead,
  drops repeat deliveries, and passes a `CallSummary` to a pluggable handler.
- `POST /webhooks/conversation-init`: the conversation initiation webhook for
  inbound Twilio and SIP calls; it answers even when the caller lookup fails.
- `agent_phone_kit.replay` and a pytest plugin (`pytest11` entry point) that
  replay recorded conversations against your tools, one test case per file.
- Twilio and SIP trunking guides, with an ElevenLabs tool config and phone-number
  request bodies checked against the ElevenLabs Python SDK in CI.
