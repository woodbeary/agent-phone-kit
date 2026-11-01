# The two-minute demo

What the kit does, in the order a call happens. Every step uses the code in this
repo: the claim-status tool and the synthetic claims in `agent_phone_kit/claims.py`.
Steps 2 and 3 need a real ElevenLabs agent and phone number; everything else runs
on a laptop with no accounts.

## Before

- Run the kit somewhere ElevenLabs can reach, for example behind
  `cloudflared tunnel --url http://localhost:8000`.
- In ElevenLabs, add the tool from `examples/elevenlabs/claim_status_tool.json` and
  point the post-call webhook at `https://YOUR-HOST/webhooks/post-call`. Import the
  Twilio number ([twilio.md](twilio.md)).
- Do a dry run the day before.

## The steps

**0:00 Start it.**

```bash
pip install -e ".[dev]"
uvicorn agent_phone_kit.app:create_app --factory --port 8000
```

Show `examples/elevenlabs/claim_status_tool.json`: the URL, the `Authorization`
header from a workspace secret, `conversation_id` from `system__conversation_id`,
`response_timeout_secs: 5`.

**0:15 Call it** (needs the agent and number). Ask: "What's the status of claim
C L M one zero zero one?" The tool answers, and the agent says: "Claim CLM-1001 is
approved. Payment goes out within five business days." In the kit's log there is
one request for it, well inside the 2 s budget.

**0:50 Hang up** (needs the agent). The signed post-call webhook arrives, and the
log shows one `post-call {...}` line with `"tools_called": ["claim_status", ...]`
and `"substantive": true`.

**1:05 Replay it.** With the webhook's secret in `ELEVENLABS_WEBHOOK_SECRET` and the
saved body (or `tests/conversations/claim_approved.json`):

```console
$ agent-phone-kit post-call call.json
HTTP 200 {"status":"received"}
$ agent-phone-kit post-call call.json
HTTP 200 {"status":"duplicate"}
$ agent-phone-kit post-call call.json --later 60 --drop data.analysis --drop data.metadata
HTTP 200 {"status":"received"}
$ agent-phone-kit show conv_synthetic_0001
$ agent-phone-kit stats
```

The third one is the same event a minute later with less in it, so its own log
line shows `null` for the duration. `show` (with the `conversation_id` from your
body) prints the one merged record: `call_duration_secs` and `summary` are still
there, and `last_event_at` moved a minute on. `stats` still counts one call.

**1:20 Break the signature.**

```console
$ agent-phone-kit post-call call.json --forge
HTTP 401 {"error":"invalid signature"}
$ agent-phone-kit post-call call.json --stale 3600
HTTP 401 {"error":"invalid signature"}
```

`--forge` adds one byte after signing. The server log says why the second one
failed: `timestamp is 3600 s old`. ElevenLabs never retries a 401.

**1:35 Break a tool.** In `SAMPLE_CLAIMS`, change CLM-1001 to `denied` and run
`pytest`. The recorded call fails with a field-by-field diff:
`status: expected 'approved', got 'denied'`. That is what stops a deploy from
breaking a call that already happened.

**1:50 Lint consent.** Set `first_message` in a copy of
`examples/elevenlabs/agent.json` to "Hi! How can I help?" and run
`agent-phone-kit lint` on it: exit 1, naming the first message. CI runs the same
command on the example.
