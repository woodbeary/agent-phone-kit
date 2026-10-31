"""Replay the tool requests an agent made on a recorded call against your tools.

A recorded conversation is in ElevenLabs' own shape: the body of a
``post_call_transcription`` webhook, its ``data`` object, or the response of
``GET /v1/convai/conversations/{conversation_id}``. For each webhook tool call in
the transcript, ``tool_details`` holds the HTTP request the agent sent (method,
url, query params, body), and a ``tool_results`` entry with the same
``request_id`` holds the ``result_value`` it got back. Replay re-sends each
request to your app and compares the answers.

It does not re-run the LLM or the voice. It checks that your tools still answer
recorded calls the way they did; ElevenLabs' agent tests cover agent behavior.
"""

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, quote, urlsplit


class HttpClient(Protocol):
    """What replay needs from a client: FastAPI's TestClient, httpx or httpx2."""

    def request(self, method: str, url: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class RecordedToolCall:
    request_id: str
    tool_name: str
    method: str
    path: str
    query: dict[str, str]
    body: str | None
    result: Any
    """The ``result_value`` the agent received, or None if none was recorded."""
    result_is_error: bool


@dataclass(frozen=True)
class Mismatch:
    call: RecordedToolCall
    problem: str


@dataclass
class ReplayReport:
    source: str
    calls: list[RecordedToolCall]
    mismatches: list[Mismatch] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatches

    def explain(self) -> str:
        if self.ok:
            return f"{self.source}: {len(self.calls)} tool call(s) replayed, all match"
        lines = [f"{self.source}: {len(self.mismatches)} of {len(self.calls)} tool call(s) differ"]
        for mismatch in self.mismatches:
            call = mismatch.call
            lines.append(f"  {call.tool_name} [{call.request_id}] {call.method} {call.path}")
            lines.extend(f"    {line}" for line in mismatch.problem.splitlines())
        return "\n".join(lines)


def load_conversation(source: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    """Return the conversation object (the one holding ``transcript``)."""
    if isinstance(source, Mapping):
        raw: Any = dict(source)
    else:
        raw = json.loads(Path(source).read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "transcript" not in raw and isinstance(raw.get("data"), dict):
        raw = raw["data"]
    if not isinstance(raw, dict) or not isinstance(raw.get("transcript"), list):
        label = "conversation" if isinstance(source, Mapping) else str(source)
        raise ValueError(f"{label}: no transcript list found")
    return raw


def recorded_tool_calls(conversation: Mapping[str, Any]) -> list[RecordedToolCall]:
    """The webhook tool calls in a transcript, in call order, with their recorded results."""
    turns = [turn for turn in conversation.get("transcript") or [] if isinstance(turn, dict)]
    results = {
        result.get("request_id"): result
        for turn in turns
        for result in turn.get("tool_results") or []
        if isinstance(result, dict)
    }
    calls = []
    for turn in turns:
        for call in turn.get("tool_calls") or []:
            details = call.get("tool_details") or {}
            if details.get("type") != "webhook":
                continue  # system and client tools never reach your server
            url = urlsplit(details["url"])
            path = url.path or "/"
            for name, value in (details.get("path_params") or {}).items():
                path = path.replace("{" + name + "}", quote(str(value), safe=""))
            query = dict(parse_qsl(url.query))
            query.update({k: str(v) for k, v in (details.get("query_params") or {}).items()})
            result = results.get(call.get("request_id"))
            calls.append(
                RecordedToolCall(
                    request_id=str(call.get("request_id")),
                    tool_name=str(call.get("tool_name")),
                    method=str(details.get("method") or "POST").upper(),
                    path=path,
                    query=query,
                    body=details.get("body"),
                    result=None if result is None else result.get("result_value"),
                    result_is_error=bool(result and result.get("is_error")),
                )
            )
    return calls


def replay(
    client: HttpClient,
    source: str | Path | Mapping[str, Any],
    *,
    headers: Mapping[str, str] | None = None,
    ignore_fields: Collection[str] = (),
) -> ReplayReport:
    """Re-send every recorded webhook tool call through ``client`` and compare.

    ``client`` is FastAPI's ``TestClient`` for in-process tests, or an httpx
    client with ``base_url`` set to a staging server.
    ``headers`` carries your auth; recorded headers are never replayed.
    ``ignore_fields`` names top-level response fields that change between runs.
    """
    conversation = load_conversation(source)
    if isinstance(source, Mapping):
        name = str(conversation.get("conversation_id", "<conversation>"))
    else:
        name = Path(source).name
    report = ReplayReport(name, recorded_tool_calls(conversation))
    for call in report.calls:
        response = client.request(
            call.method,
            call.path,
            params=call.query,
            content=call.body,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        problem = _compare(call, response.status_code, response.text, ignore_fields)
        if problem:
            report.mismatches.append(Mismatch(call, problem))
    return report


class _Missing:
    def __repr__(self) -> str:
        return "<missing>"


_MISSING = _Missing()


def _parse(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value.strip()


def _short(text: str, limit: int = 200) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


def _compare(
    call: RecordedToolCall, status: int, text: str, ignore_fields: Collection[str]
) -> str | None:
    is_error = status >= 400
    if is_error != call.result_is_error:
        expected = "an error" if call.result_is_error else "success"
        return f"expected {expected}, got HTTP {status}: {_short(text)}"
    if is_error or call.result is None:
        return None
    expected, actual = _parse(call.result), _parse(text)
    if isinstance(expected, dict) and isinstance(actual, dict):
        diffs = [
            f"{key}: expected {expected.get(key, _MISSING)!r}, got {actual.get(key, _MISSING)!r}"
            for key in sorted(expected.keys() | actual.keys())
            if key not in ignore_fields and expected.get(key, _MISSING) != actual.get(key, _MISSING)
        ]
        return "\n".join(diffs) or None
    if expected != actual:
        return f"expected {_short(str(call.result))}\ngot      {_short(text)}"
    return None
