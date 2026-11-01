import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import uvicorn

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


@contextmanager
def serve_on_loopback(app: Any) -> Iterator[str]:
    """Run an ASGI app with uvicorn on 127.0.0.1 in a thread; yield its base URL."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "server did not start"
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(10)
