"""The docs show the code as it is."""

import re
from pathlib import Path

from agent_phone_kit.calls import CLAIM_SQL, LEG_SQL, SCHEMA_SQL, UPSERT_SQL, pyformat

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
