"""Ad-hoc AST inspection helper for validator development."""

from __future__ import annotations

import sys
from pathlib import Path

import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def show(sql: str) -> None:
    print("=" * 70)
    print(sql)
    expression = sqlglot.parse_one(sql, read="mysql")
    print("root:", type(expression).__name__)
    with_clause = expression.args.get("with")
    print("with:", type(with_clause).__name__ if with_clause else None)
    if with_clause:
        for cte in with_clause.expressions:
            print("  cte:", cte.alias_or_name, "->", type(cte.this).__name__)
    for select in expression.find_all(exp.Select):
        from_clause = select.args.get("from")
        joins = select.args.get("joins") or []
        print(
            "  select from=",
            type(from_clause.this).__name__ if from_clause else None,
            getattr(from_clause.this, "name", None) if from_clause else None,
            "joins=",
            [(type(j.this).__name__, getattr(j.this, "name", None)) for j in joins],
        )
    for table in expression.find_all(exp.Table):
        print("  table node:", table.name, "alias:", table.alias_or_name, "db:", table.db)
    for column in expression.find_all(exp.Column):
        print("  column node:", column.name, "table:", column.table, "star:", isinstance(column.this, exp.Star))


if __name__ == "__main__":
    for statement in sys.argv[1:]:
        show(statement)
