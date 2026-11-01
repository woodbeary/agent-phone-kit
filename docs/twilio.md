# Attach the agent to a Twilio number

This uses ElevenLabs' native Twilio integration: you import a Twilio number into
ElevenLabs, ElevenLabs configures the number's voice webhook, and inbound calls go
straight to your agent. The kit's endpoints are the same whichever way the call
arrives.

Source: [Twilio native integration](https://elevenlabs.io/docs/eleven-agents/phone-numbers/twilio-integration/native-integration),
[Twilio personalization](https://elevenlabs.io/docs/eleven-agents/customization/personalization/twilio-personalization)
(both read 2026-10-31).

## 1. Get a number and a scoped key in Twilio

- Buy a number in Twilio if the agent should take inbound calls. A verified caller
  ID works for outbound calls only and can't be assigned to an agent.
- Create a Twilio API key (its SID starts with `SK`) instead of handing over the
  account auth token.

## 2. Import the number into ElevenLabs

Dashboard: **Agents → Phone Numbers → Import number → Twilio**, then fill in the
label, number, API key SID and secret, and pick the agent.

API: fill in the placeholders in
[`examples/elevenlabs/twilio_phone_number.json`](../examples/elevenlabs/twilio_phone_number.json)
(keep the filled copy out of git) and send it:

```bash
curl -sS https://api.elevenlabs.io/v1/convai/phone-numbers \
  -H "xi-api-key: $ELEVENLABS_API_KEY" \
  -H "Content-Type: application/json" \
  -d @twilio_phone_number.filled.json
```

The JSON's field names are checked against the ElevenLabs Python SDK's request
model in CI (`tests/test_sdk_conformance.py`).

## 3. Point the agent at the kit

Run the kit somewhere ElevenLabs can reach over HTTPS, then wire up three things:

| What | Where in ElevenLabs | Kit endpoint | Auth |
| --- | --- | --- | --- |
| Server tool | Agent → Tools → add a webhook tool, or `elevenlabs tools add` with [`claim_status_tool.json`](../examples/elevenlabs/claim_status_tool.json) | `POST /tools/claim-status` | `Authorization` header from a workspace secret holding `Bearer <TOOL_BEARER_SECRET>` |
| Initiation webhook (optional) | Agents settings: webhook URL and header secret. Agent → Security: enable fetching initiation data | `POST /webhooks/conversation-init` | Same bearer secret |
| Post-call webhook | Create a webhook with HMAC auth in ElevenLabs' settings, copy its secret into `ELEVENLABS_WEBHOOK_SECRET`, select it as the agents' post-call webhook | `POST /webhooks/post-call` | `ElevenLabs-Signature` HMAC |

Notes:

- The initiation webhook's answer must include every custom dynamic variable the
  agent defines. The kit returns one, `caller_on_file`; add yours in
  `agent_phone_kit/initiation.py`. Previews started from the agent's dashboard
  page don't call this webhook, so give `caller_on_file` a placeholder value in
  the agent.
- If the initiation webhook fails or times out, the call may not start. The kit's
  endpoint answers even when the caller lookup throws.
- Webhook retries are off by default in ElevenLabs. Turn them on so a 500 from
  your post-call handler is retried.

## 4. Check it with a real call

These steps need your ElevenLabs and Twilio accounts; nothing here is exercised in CI.

1. Call the number and ask about claim `CLM-1001`. The agent should say it is approved.
2. In the kit's log, look for one `post-call {...}` line for the call, with
   `"tools_called": ["claim_status", ...]`.
3. Save that call's webhook body, replace anything personal with fake values, and
   add it to `tests/conversations/`. `pytest` now replays it.

## A person first, the agent if nobody answers: register-call

The native integration hands the number's voice webhook to ElevenLabs, so nothing
of yours runs before the agent. When a person should get the call first, keep the
number's voice webhook pointed at the kit and let ElevenLabs'
[register-call API](https://elevenlabs.io/docs/eleven-agents/phone-numbers/twilio-integration/register-call)
connect the call to the agent only if nobody picks up. The kit's gate does this:

1. `POST /twilio/voice` (the number's voice webhook): "To reach a person, press 1."
   A caller who doesn't press 1 hears it once more, then goodbye. Most robocalls
   stop here.
2. `POST /twilio/gate`: on 1, a disclosure ("This call may be recorded and
   transcribed. If nobody can answer, an AI assistant will take your call."), then
   `<Dial>` to `GATE_HUMAN_NUMBER` for 18 seconds.
3. `POST /twilio/dial-result`: if the person answered, hang up when they're done.
   If not, call register-call and return its TwiML, which connects the call to the
   agent over a WebSocket. The parent call SID, the SID of the `<Dial>` that
   wasn't answered and its status go along as the `parent_call_sid`,
   `human_call_sid` and `human_dial_status` dynamic variables, so the post-call
   webhook folds all the legs onto one call record. If register-call fails, the
   caller hears "Sorry, we can't take your call right now" instead of Twilio's
   application error.

Turn it on by setting all four of `TWILIO_AUTH_TOKEN`, `GATE_HUMAN_NUMBER`,
`ELEVENLABS_API_KEY` and `ELEVENLABS_AGENT_ID`. Setting some but not all stops the
app from starting. Then set the Twilio number's voice webhook to
`POST https://YOUR-HOST/twilio/voice`. You can add `?line=+1...` to that URL to
tell the agent which of your lines was called; it arrives as the `line` dynamic
variable, normalized to E.164.

What register-call changes, per ElevenLabs' docs:

- The agent's audio must be μ-law 8000 Hz in and out
  (`conversation_config.tts.agent_output_audio_format` and
  `conversation_config.asr.user_input_audio_format` set to `ulaw_8000`; the
  example agent in [`examples/elevenlabs/agent.json`](../examples/elevenlabs/agent.json)
  does).
- ElevenLabs doesn't hold your Twilio credentials on this path, so its
  transfer tools don't work. Transfers are yours.
- The number doesn't show in ElevenLabs' phone-number list.

### Twilio signatures behind a proxy

Every gate request must carry a valid `X-Twilio-Signature`: HMAC-SHA1 of the exact
URL Twilio called plus the sorted POST fields, keyed with your auth token. Twilio
signs the URL as it called it, so a proxy that rewrites the host or scheme, drops
a trailing slash, or re-encodes the query breaks the check even though the request
is genuine. The kit tries every URL the request could have been signed with:

- the URL as the app received it;
- the scheme and host from `X-Forwarded-Proto` and `X-Forwarded-Host`;
- `TWILIO_PUBLIC_BASE_URL`, if you set it (use this when your proxy doesn't send
  forwarded headers);
- each of those with a `+` restored where a query value decoded to a space (a
  literal `+` in a query string means a space, so `?line=+12025550100` arrives as
  `" 12025550100"`), with the `+` both literal and as `%2B`;
- with and without a trailing slash, and with and without the default port.

Trying more URLs doesn't weaken the check: each one still needs a signature made
with your auth token. It fails closed. There is no setting that skips it, the gate
won't start without the auth token, and a missing or wrong signature is a 403.

Phone numbers are normalized to E.164 as they arrive: `" 12025550143"`,
`"(202) 555-0143"` and `"12025550143"` all become `+12025550143`. Callers that
aren't phone numbers (`anonymous`, `client:...`) are passed to register-call as
Twilio sent them.

`tests/test_twilio_gate.py` signs requests as Twilio does (checked against a
vector computed with `openssl`), sends one with `?line=+12025550100` to an app
behind a rewritten host, and runs register-call against a fake ElevenLabs API on
`127.0.0.1`. No test calls Twilio or ElevenLabs.

## Other ways in

For a Twilio Elastic SIP Trunk, follow [sip-trunking.md](sip-trunking.md).
