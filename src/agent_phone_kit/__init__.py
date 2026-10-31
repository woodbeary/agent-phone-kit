"""Server tools, signed post-call webhooks and replay tests for ElevenLabs phone agents."""

from agent_phone_kit.signature import SignatureError, sign_payload, verify_signature

__version__ = "0.1.0"

__all__ = ["SignatureError", "__version__", "sign_payload", "verify_signature"]
