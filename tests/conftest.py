import json
from pathlib import Path
from typing import Any

import pytest

pytest_plugins = ["pytester"]

CONVERSATIONS = Path(__file__).parent / "conversations"


@pytest.fixture
def conversation_payload() -> Any:
    """Load a synthetic recorded conversation (a full post-call webhook body) by file name."""

    def load(name: str) -> dict[str, Any]:
        return json.loads((CONVERSATIONS / name).read_text(encoding="utf-8"))

    return load


@pytest.fixture
def tool_headers(phone_kit_settings: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {phone_kit_settings.tool_secret}"}
