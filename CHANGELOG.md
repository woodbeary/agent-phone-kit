# Changelog

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
