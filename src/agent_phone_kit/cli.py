"""The ``agent-phone-kit`` command.

``agent-phone-kit lint agent.json``  check an agent config for consent in the call.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from agent_phone_kit.consent import lint_agent_config

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-phone-kit")
    commands = parser.add_subparsers(dest="command", required=True)
    lint = commands.add_parser(
        "lint",
        help="check agent configs for a recording or AI disclosure the caller can't talk over",
    )
    lint.add_argument("paths", nargs="+", type=Path, metavar="agent.json")
    return parser


def main(argv: Sequence[str] | None = None, *, out: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    stream = out or sys.stdout
    if args.command == "lint":
        return _lint(args.paths, stream)
    return UNUSABLE  # pragma: no cover (argparse rejects unknown commands)


if __name__ == "__main__":
    raise SystemExit(main())
