# Attach the agent to a SIP trunk

Use this when calls come from a PBX, a contact-center platform or a carrier's SIP
trunk instead of a Twilio number. The kit's three endpoints don't change; only
the path the call takes into ElevenLabs does.

Source: [SIP trunking](https://elevenlabs.io/docs/eleven-agents/phone-numbers/sip-trunking),
[Personalization](https://elevenlabs.io/docs/eleven-agents/customization/personalization),
[Dynamic variables](https://elevenlabs.io/docs/eleven-agents/customization/personalization/dynamic-variables)
(all read 2026-10-31).

## Where to send calls

| Transport | URI | Use |
| --- | --- | --- |
| TLS | `sip:+1XXXXXXXXXX@sip.rtc.elevenlabs.io:5061;transport=tls` | Recommended |
| TCP | `sip:+1XXXXXXXXXX@sip.rtc.elevenlabs.io:5060;transport=tcp` | When TLS isn't available |
| UDP | `sip:+1XXXXXXXXXX@sip.rtc.elevenlabs.io:5060;transport=udp` | Testing only (ElevenLabs calls it experimental) |

- The user part before `@` is required: the E.164 number you imported (`+1XXXXXXXXXX`
  above is a placeholder). A URI without it won't route.
- Codecs: G.711 (PCMU or PCMA) or G.722.
- Requests can come from several ElevenLabs IP addresses. Enterprise accounts can
  ask for static-IP SIP servers (`sip-static.rtc.elevenlabs.io`).

## Import the number into ElevenLabs

Dashboard: **Phone Numbers → Import number → From SIP Trunk**. API: fill in
[`examples/elevenlabs/sip_trunk_phone_number.json`](../examples/elevenlabs/sip_trunk_phone_number.json)
(keep the filled copy out of git) and `POST` it to
`https://api.elevenlabs.io/v1/convai/phone-numbers` with your `xi-api-key`, as in
[twilio.md](twilio.md).

What the example sets, and why:

| Setting | Example | Why |
| --- | --- | --- |
| `inbound_trunk_config.media_encryption` | `required` | Encrypted audio (SRTP) on calls into ElevenLabs. Try `allowed` first if your side can't do SRTP yet. |
| `inbound_trunk_config.remote_domains` | your SIP servers' FQDN | Lets ElevenLabs validate your TLS certificate on follow-up requests like `BYE`. |
| `inbound_trunk_config.credentials` | digest username and password | ElevenLabs recommends digest auth over IP allowlists. |
| `outbound_trunk_config.address` | hostname only, no `sip:` | Where ElevenLabs sends calls it places. With TLS it needs a valid certificate. |
| `outbound_trunk_config.transport` | `tls` | Encrypted signaling. |
| `outbound_trunk_config.enabled_codecs` | `G722/8000`, `PCMU/8000`, `PCMA/8000` | Trim to what your provider supports. |

You can also restrict inbound calls by caller number (`allowed_numbers`) or by
source IP (`allowed_addresses`, TCP and TLS only). The JSON's field names are
checked against the ElevenLabs Python SDK's request model in CI.

## Pass call context in SIP headers

- Send `X-CALL-ID` and `X-CALLER-ID` on the `INVITE`. ElevenLabs uses them for
  the call and caller IDs it passes to the initiation webhook and exposes as
  `system__` dynamic variables (the caller's is `{{system__caller_id}}`). On a
  Twilio Elastic SIP Trunk it falls back to Twilio's `sip.twilio.callSid`.
- Any other `X-` header becomes a dynamic variable named `sip_` plus the header in
  lower case with underscores: `X-Campaign-ID` becomes `{{sip_campaign_id}}`.
- To hand a result back to your PBX when the call ends, map a dynamic variable to
  a header on the `BYE` with `attributes_to_headers`, for example
  `{"disposition_code": "X-Disposition"}`.

The kit's initiation webhook accepts the extra fields SIP calls send (`call_id`,
`sip_headers`); see `InitiationRequest` in `agent_phone_kit/initiation.py`.

## Check it with a real call

These steps need your PBX or carrier and an ElevenLabs account; nothing here is
exercised in CI.

1. From your side, dial the TLS URI above with `X-CALLER-ID` set.
2. In the ElevenLabs call history, open the call's **Phone Call** tab and check the
   SIP values arrived.
3. In the kit's log, look for the `post-call {...}` line.

If the call fails before the agent answers: check port 5061 (TLS) or 5060 (TCP)
through your firewall, certificate validity and **Remote domains**, a shared
codec, and the digest credentials, in that order. Testing over TCP with media
encryption set to `allowed` isolates TLS and SRTP problems.
