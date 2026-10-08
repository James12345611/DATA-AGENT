"""Create/refresh the V1 read-only MySQL account.

Usage (the admin password is taken from the environment, never from argv)::

    $env:ADMIN_DB_USER = "root"
    $env:ADMIN_DB_PASSWORD = "..."
    python scripts/setup_readonly_user.py

It grants SELECT on exactly the six V1 business views and then verifies that a
write attempt with the new account fails (fail-closed security check).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pymysql

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from text2sql.config import Settings  # noqa: E402

SQL_TEMPLATE = REPO_ROOT / "scripts" / "setup_readonly_user.sql"

BUSINESS_VIEWS = (
    "dim_user",
    "mart_user_behavior_summary",
    "mart_user_order_summary",
    "mart_campaign_summary",
    "mart_funnel_summary",
    "mart_retention_cohort",
)


def main() -> int:
    settings = Settings.load()
    admin_user = os.environ.get("ADMIN_DB_USER", "root")
    admin_password = os.environ.get("ADMIN_DB_PASSWORD", "")
    if not admin_password:
        print("ADMIN_DB_PASSWORD 环境变量未设置", file=sys.stderr)
        return 2
    if not settings.db_password:
        print("DB_PASSWORD（只读账号密码）未配置", file=sys.stderr)
        return 2

    script = SQL_TEMPLATE.read_text(encoding="utf-8")
    statements = [statement.strip() for statement in script.split(";") if statement.strip()]

    admin = pymysql.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=admin_user,
        password=admin_password,
        charset=settings.db_charset,
        autocommit=True,
    )
    try:
        with admin.cursor() as cursor:
            for statement in statements:
                cleaned = "\n".join(
                    line for line in statement.splitlines() if not line.strip().startswith("--")
                ).strip()
                if not cleaned:
                    continue
                rendered = cleaned.replace("__PASSWORD__", settings.db_password)
                if rendered.upper().startswith("SHOW GRANTS"):
                    continue
                cursor.execute(rendered)
            cursor.execute(f"SHOW GRANTS FOR '{settings.db_user}'@'localhost'")
            grants = [row[0] for row in cursor.fetchall()]
    finally:
        admin.close()

    print(f"账号 {settings.db_user}@localhost 授权结果：")
    for grant in grants:
        print("  " + grant)

    granted_views = {
        view for view in BUSINESS_VIEWS if any(f"`{view}`" in grant for grant in grants)
    }
    missing = set(BUSINESS_VIEWS) - granted_views
    if missing:
        print("缺少视图授权：" + ", ".join(sorted(missing)), file=sys.stderr)
        return 1

    # Fail-closed check: the read-only account must not be able to write.
    readonly = pymysql.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
        charset=settings.db_charset,
        autocommit=True,
    )
    try:
        with readonly.cursor() as cursor:
            for view in BUSINESS_VIEWS:
                cursor.execute(f"SELECT COUNT(*) FROM {view}")
            print("六个业务视图均可 SELECT")
            try:
                cursor.execute("CREATE TABLE _text2sql_write_probe (id INT)")
            except pymysql.err.MySQLError as exc:
                print(f"写入被拒绝（预期行为）：{exc.args[0]} {exc.args[1] if len(exc.args) > 1 else ''}")
            else:
                print("错误：只读账号竟然可以建表", file=sys.stderr)
                return 1
            try:
                cursor.execute("SELECT COUNT(*) FROM raw_orders")
            except pymysql.err.MySQLError as exc:
                print(f"raw_orders 不可读（预期行为）：{exc.args[0]}")
            else:
                print("错误：只读账号可以读取 raw_orders", file=sys.stderr)
                return 1
    finally:
        readonly.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
