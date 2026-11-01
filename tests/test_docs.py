"""The docs show the code as it is."""

import re
import shlex
from pathlib import Path

from agent_phone_kit.calls import CLAIM_SQL, LEG_SQL, SCHEMA_SQL, UPSERT_SQL, pyformat
from agent_phone_kit.claims import SAMPLE_CLAIMS
from agent_phone_kit.cli import build_parser

DOCS = Path(__file__).parent.parent / "docs"


def sql_blocks(name: str) -> list[str]:
    text = (DOCS / name).read_text(encoding="utf-8")
    return [block.strip() for block in re.findall(r"```sql\n(.*?)```", text, re.S)]


def test_postgres_recipe_is_the_sql_the_store_runs() -> None:
    schema, upsert, legs, claim = sql_blocks("postgres.md")
    assert schema == ";\n\n".join(SCHEMA_SQL) + ";"
    assert upsert == pyformat(UPSERT_SQL) + ";"
    assert legs == pyformat(LEG_SQL) + ";"
    assert claim == pyformat(CLAIM_SQL) + ";"


def test_pyformat_rewrites_named_placeholders_only() -> None:
    sql = "SELECT a::text FROM t WHERE b = :b_id AND c = 'x:y' AND d IN (:d)"
    expected = "SELECT a::text FROM t WHERE b = %(b_id)s AND c = 'x:y' AND d IN (%(d)s)"
    assert pyformat(sql) == expected


def test_the_demo_uses_the_tool_the_kit_has() -> None:
    demo = " ".join((DOCS / "demo.md").read_text(encoding="utf-8").split())
    approved = next(claim for claim in SAMPLE_CLAIMS if claim.number == "CLM-1001")
    assert f"Claim CLM-1001 is {approved.status}. {approved.next_step}" in demo
    assert "book" not in demo.lower()


def test_every_command_in_the_demo_parses() -> None:
    demo = (DOCS / "demo.md").read_text(encoding="utf-8")
    commands = re.findall(r"^\$ agent-phone-kit (.+)$", demo, re.M)
    assert len(commands) >= 6
    for command in commands:
        build_parser().parse_args(shlex.split(command))
