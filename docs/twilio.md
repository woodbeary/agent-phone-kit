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

## Other ways in

If you keep your own Twilio voice webhook (an IVR in front of the agent, for
example), ElevenLabs' [Register Twilio calls](https://elevenlabs.io/docs/eleven-agents/phone-numbers/twilio-integration/register-call)
API connects a call you already hold. That path isn't covered by this kit's
examples. For a Twilio Elastic SIP Trunk, follow [sip-trunking.md](sip-trunking.md).
