"""Materialize (or restore) the six mart objects as physical tables.

Why: ``mart_funnel_summary`` / ``mart_retention_cohort`` are *views* that run
``COUNT(DISTINCT ...)`` over the full behaviour log on every query.  On the sample
data that is instant, but on full scale (3M behaviour rows) a single query needs
~30-60s and can exceed ``SQL_TIMEOUT_SECONDS``.

This script rewrites the same six object names as physical tables with the same
columns and a primary key on their grain, so the Catalog, the SQL validator, the
read-only grants and every node contract stay exactly the same.

    $env:ADMIN_DB_PASSWORD = "<管理员密码>"
    python scripts/materialize_marts.py --mode materialize
    python scripts/materialize_marts.py --mode restore        # back to views

Re-run ``materialize`` after reloading raw data (``load_full_data.py``).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import pymysql

REPO_ROOT = Path(__file__).resolve().parents[1]
PARENT_ROOT = REPO_ROOT.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from text2sql.config import Settings  # noqa: E402

# table/view name -> (primary key column, extra index columns)
MARTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "mart_user_behavior_summary": ("user_id", ("last_event_time",)),
    "mart_user_order_summary": ("user_id", ("is_repeat_buyer",)),
    "mart_campaign_summary": ("campaign_id", ()),
    "mart_funnel_summary": ("event_type", ()),
    "mart_retention_cohort": ("cohort_date", ()),
    "dim_user": ("user_id", ("channel", "vip_level")),
}

VIEWS_SQL = PARENT_ROOT / "mysql" / "sql" / "02_views.sql"


def _connect(settings: Settings, admin_user: str, admin_password: str):
    return pymysql.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=admin_user,
        password=admin_password,
        database=settings.db_name,
        charset=settings.db_charset,
        autocommit=True,
        read_timeout=3600,
        write_timeout=3600,
    )


def _object_type(cursor, name: str) -> str | None:
    cursor.execute(
        "SELECT TABLE_TYPE FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (name,),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def materialize(connection, names: list[str]) -> None:
    with connection.cursor() as cursor:
        for name in names:
            current = _object_type(cursor, name)
            if current == "BASE TABLE":
                print(f"  {name}: 已经是物理表，跳过")
                continue
            if current is None:
                print(f"  警告：{name} 不存在，跳过")
                continue
            primary_key, extra_indexes = MARTS[name]
            staging = f"{name}__mv"
            started = time.perf_counter()
            print(f"  {name}: 物化中 ...", flush=True)
            cursor.execute(f"DROP TABLE IF EXISTS {staging}")
            cursor.execute(f"CREATE TABLE {staging} AS SELECT * FROM {name}")
            cursor.execute(f"ALTER TABLE {staging} ADD PRIMARY KEY ({primary_key})")
            for column in extra_indexes:
                cursor.execute(f"ALTER TABLE {staging} ADD KEY ix_{name}_{column} ({column})")
            cursor.execute(f"DROP VIEW {name}")
            cursor.execute(f"RENAME TABLE {staging} TO {name}")
            cursor.execute(f"SELECT COUNT(*) FROM {name}")
            rows = cursor.fetchone()[0]
            print(
                f"  {name}: 完成，{rows:,} 行，耗时 {time.perf_counter() - started:.1f}s",
                flush=True,
            )


def restore_views(connection, names: list[str]) -> None:
    if not VIEWS_SQL.is_file():
        raise SystemExit(f"找不到视图定义文件：{VIEWS_SQL}")
    script = VIEWS_SQL.read_text(encoding="utf-8")
    with connection.cursor() as cursor:
        for name in names:
            if _object_type(cursor, name) == "BASE TABLE":
                cursor.execute(f"DROP TABLE {name}")
                print(f"  {name}: 已删除物理表")

    # Re-create every view exactly as the mysql package defines it.
    statements = [
        statement.strip()
        for statement in script.split(";")
        if statement.strip() and "CREATE OR REPLACE VIEW" in statement.upper()
    ]
    with connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)
            view_name = statement.split("VIEW", 1)[1].split("AS")[0].strip()
            print(f"  {view_name}: 视图已重建")


def report(connection, settings: Settings) -> None:
    from text2sql.db import ReadOnlyExecutor

    executor = ReadOnlyExecutor(settings)
    try:
        for name in [*MARTS]:
            started = time.perf_counter()
            result = executor.execute(f"SELECT COUNT(*) AS rows_count FROM {name}")
            elapsed = time.perf_counter() - started
            print(f"  {name:32s} {result.rows[0]['rows_count']:>10,} 行  {elapsed:6.2f}s")
    finally:
        executor.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="物化/还原 6 张业务对象")
    parser.add_argument("--mode", choices=["materialize", "restore", "report"], default="materialize")
    parser.add_argument("--admin-user", default=os.environ.get("ADMIN_DB_USER", "root"))
    parser.add_argument("--only", nargs="*", default=None, help="只处理指定的对象名")
    args = parser.parse_args()

    settings = Settings.load()
    admin_password = os.environ.get("ADMIN_DB_PASSWORD", "")
    if not admin_password:
        print("请通过环境变量 ADMIN_DB_PASSWORD 提供管理员密码", file=sys.stderr)
        return 2

    names = args.only or list(MARTS)
    connection = _connect(settings, args.admin_user, admin_password)
    try:
        if args.mode == "materialize":
            print("物化业务对象为物理表（名称、字段、口径保持不变）...")
            materialize(connection, names)
        elif args.mode == "restore":
            print("还原为视图 ...")
            restore_views(connection, names)
    finally:
        connection.close()

    print("只读账号查询计时：")
    report(connection=None, settings=settings)  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
