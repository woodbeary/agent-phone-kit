"""The ``agent-phone-kit`` command.

``agent-phone-kit lint agent.json``
    Check an agent config for consent in the call.
``agent-phone-kit post-call body.json --url URL [--later S] [--drop PATH] [--stale S] [--forge]``
    Sign a saved post-call webhook body the way ElevenLabs does and POST it, to
    show a duplicate, a thinner late copy, a stale signature or a forged body.
    The secret comes from ``ELEVENLABS_WEBHOOK_SECRET``, never the command line.
``agent-phone-kit stats [--db calls.sqlite3] [--since UNIX_SECONDS]``
    Raw call volume, the substantive call rate and tool latency from the call store.
"""

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

import httpx

from agent_phone_kit.calls import SQLiteCallStore
from agent_phone_kit.consent import lint_agent_config
from agent_phone_kit.settings import CALL_STORE_ENV, DEFAULT_CALL_STORE_PATH, WEBHOOK_SECRET_ENV
from agent_phone_kit.signature import SIGNATURE_HEADER, sign_payload

OK = 0
FAILED = 1
UNUSABLE = 2


def _lint(paths: Sequence[Path], out: TextIO) -> int:
    status = OK
    for path in paths:
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"{path}: can't read it as JSON: {exc}", file=out)
            status = max(status, UNUSABLE)
            continue
        if not isinstance(config, dict):
            print(f"{path}: expected a JSON object (an agent config)", file=out)
            status = max(status, UNUSABLE)
            continue
        problems = lint_agent_config(config)
        for problem in problems:
            print(f"{path}: {problem}", file=out)
        if problems:
            status = max(status, FAILED)
        else:
            print(f"{path}: ok", file=out)
    return status


def _drop(event: Any, dotted: str) -> None:
    *parents, last = dotted.split(".")
    target = event
    for key in parents:
        target = target.get(key) if isinstance(target, dict) else None
    if not isinstance(target, dict) or last not in target:
        raise KeyError(dotted)
    del target[last]


def _post_call(args: argparse.Namespace, out: TextIO) -> int:
    secret = os.environ.get(WEBHOOK_SECRET_ENV, "")
    if not secret:
        print(f"set {WEBHOOK_SECRET_ENV} to the webhook's secret", file=out)
        return UNUSABLE
    try:
        body = args.body.read_bytes()
        if args.later or args.drop:
            event = json.loads(body)
            if args.later:
                event["event_timestamp"] = int(event.get("event_timestamp") or 0) + args.later
            for dotted in args.drop:
                _drop(event, dotted)
            body = json.dumps(event, separators=(",", ":")).encode()
    except (OSError, ValueError, TypeError) as exc:
        print(f"{args.body}: can't use it: {exc}", file=out)
        return UNUSABLE
    except KeyError as exc:
        print(f"{args.body}: no field {exc} to drop", file=out)
        return UNUSABLE

    timestamp = int(time.time()) - args.stale
    header = sign_payload(secret, body, timestamp=timestamp)
    if args.forge:
        body += b" "  # one byte added after signing; still valid JSON
    try:
        response = httpx.post(
            args.url,
            content=body,
            headers={SIGNATURE_HEADER: header, "Content-Type": "application/json"},
            timeout=10,
        )
    except httpx.HTTPError as exc:
        print(f"POST {args.url} failed: {exc}", file=out)
        return UNUSABLE
    print(f"HTTP {response.status_code} {response.text}", file=out)
    return OK if response.is_success else FAILED


def _stats(args: argparse.Namespace, out: TextIO) -> int:
    path = args.db or os.environ.get(CALL_STORE_ENV) or DEFAULT_CALL_STORE_PATH
    if not Path(path).is_file():
        print(f"{path}: no call store there", file=out)
        return UNUSABLE
    store = SQLiteCallStore(path)
    try:
        volume = store.volume(since=args.since)
    finally:
        store.close()
    print(json.dumps(volume.as_dict(), indent=2), file=out)
    return OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-phone-kit")
    commands = parser.add_subparsers(dest="command", required=True)
    lint = commands.add_parser(
        "lint",
        help="check agent configs for a recording or AI disclosure the caller can't talk over",
    )
    lint.add_argument("paths", nargs="+", type=Path, metavar="agent.json")

    post = commands.add_parser(
        "post-call", help="sign a saved post-call webhook body and POST it to the kit"
    )
    post.add_argument("body", type=Path, metavar="body.json")
    post.add_argument(
        "--url", default="http://127.0.0.1:8000/webhooks/post-call", help="the post-call webhook"
    )
    post.add_argument(
        "--later", type=int, default=0, metavar="SECONDS", help="add to event_timestamp"
    )
    post.add_argument(
        "--drop",
        action="append",
        default=[],
        metavar="PATH",
        help="remove a field before signing, like data.analysis (repeatable)",
    )
    post.add_argument(
        "--stale", type=int, default=0, metavar="SECONDS", help="sign as if this long ago"
    )
    post.add_argument("--forge", action="store_true", help="change the body after signing")

    stats = commands.add_parser("stats", help="call volume, substantive rate and tool latency")
    stats.add_argument("--db", help=f"the SQLite call store (default ${CALL_STORE_ENV})")
    stats.add_argument("--since", type=int, metavar="UNIX_SECONDS", help="calls started since")
    return parser


def main(argv: Sequence[str] | None = None, *, out: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    stream = out or sys.stdout
    if args.command == "lint":
        return _lint(args.paths, stream)
    if args.command == "post-call":
        return _post_call(args, stream)
    if args.command == "stats":
        return _stats(args, stream)
    return UNUSABLE  # pragma: no cover (argparse rejects unknown commands)


if __name__ == "__main__":
    raise SystemExit(main())
