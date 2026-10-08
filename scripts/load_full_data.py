"""Load the project CSV data (full or sample scale) into the practice database.

    $env:ADMIN_DB_PASSWORD = "<管理员密码>"
    python scripts/load_full_data.py --mode full
    python scripts/load_full_data.py --mode sample

Layout of the parent repository:

* ``..\\data\\raw\\*.csv``     full scale:  50万用户 / 300万行为 / 52万订单 / 100万曝光
* ``..\\data\\sample\\*.csv``  sample scale: 1000 用户 / 3000 行为 / 1000 订单 / 1000 曝光

The six business views are plain views over the four raw tables, so reloading the
raw data refreshes every ``mart_*`` view automatically: schema, Catalog and node
contracts stay identical between the two scales (text2sql_V1.md 8.4.9).
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

import pymysql

REPO_ROOT = Path(__file__).resolve().parents[1]
PARENT_ROOT = REPO_ROOT.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from text2sql.config import Settings  # noqa: E402

DATA_DIRS = {
    "full": (PARENT_ROOT / "data" / "raw", ""),
    "sample": (PARENT_ROOT / "data" / "sample", "_sample"),
}

LOAD_STATEMENTS = {
    "raw_users": """
        LOAD DATA LOCAL INFILE '{path}'
        INTO TABLE raw_users
        CHARACTER SET utf8mb4
        FIELDS TERMINATED BY ',' ENCLOSED BY '"'
        LINES TERMINATED BY '\\n'
        IGNORE 1 LINES
        (user_id, gender, age, city_tier, channel, vip_level, @register_date)
        SET register_date = NULLIF(TRIM(TRAILING '\\r' FROM @register_date), '')
    """,
    "raw_behavior_logs": """
        LOAD DATA LOCAL INFILE '{path}'
        INTO TABLE raw_behavior_logs
        CHARACTER SET utf8mb4
        FIELDS TERMINATED BY ',' ENCLOSED BY '"'
        LINES TERMINATED BY '\\n'
        IGNORE 1 LINES
        (user_id, @event_time, event_type, event_id, session_id, product_id, @source_page)
        SET event_time = STR_TO_DATE(TRIM(TRAILING '\\r' FROM @event_time), '%Y-%m-%d %H:%i:%s'),
            source_page = NULLIF(TRIM(TRAILING '\\r' FROM @source_page), '')
    """,
    "raw_orders": """
        LOAD DATA LOCAL INFILE '{path}'
        INTO TABLE raw_orders
        CHARACTER SET utf8mb4
        FIELDS TERMINATED BY ',' ENCLOSED BY '"'
        LINES TERMINATED BY '\\n'
        IGNORE 1 LINES
        (user_id, @order_time, order_id, order_amount, discount_amount, pay_status, refund_flag, @product_id)
        SET order_time = STR_TO_DATE(TRIM(TRAILING '\\r' FROM @order_time), '%Y-%m-%d %H:%i:%s'),
            product_id = NULLIF(TRIM(TRAILING '\\r' FROM @product_id), '')
    """,
    "raw_campaign_exposure": """
        LOAD DATA LOCAL INFILE '{path}'
        INTO TABLE raw_campaign_exposure
        CHARACTER SET utf8mb4
        FIELDS TERMINATED BY ',' ENCLOSED BY '"'
        LINES TERMINATED BY '\\n'
        IGNORE 1 LINES
        (user_id, @exposure_time, exposure_id, campaign_id, clicked, @converted)
        SET exposure_time = STR_TO_DATE(TRIM(TRAILING '\\r' FROM @exposure_time), '%Y-%m-%d %H:%i:%s'),
            converted = NULLIF(TRIM(TRAILING '\\r' FROM @converted), '')
    """,
}

COUNT_SQL = """
SELECT 'raw_users' AS object_name, COUNT(*) AS rows_count FROM raw_users
UNION ALL SELECT 'raw_behavior_logs', COUNT(*) FROM raw_behavior_logs
UNION ALL SELECT 'raw_orders', COUNT(*) FROM raw_orders
UNION ALL SELECT 'raw_campaign_exposure', COUNT(*) FROM raw_campaign_exposure
UNION ALL SELECT 'dim_user', COUNT(*) FROM dim_user
UNION ALL SELECT 'mart_user_behavior_summary', COUNT(*) FROM mart_user_behavior_summary
UNION ALL SELECT 'mart_user_order_summary', COUNT(*) FROM mart_user_order_summary
UNION ALL SELECT 'mart_campaign_summary', COUNT(*) FROM mart_campaign_summary
UNION ALL SELECT 'mart_funnel_summary', COUNT(*) FROM mart_funnel_summary
UNION ALL SELECT 'mart_retention_cohort', COUNT(*) FROM mart_retention_cohort
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Load full/sample CSV data into MySQL")
    parser.add_argument("--mode", choices=sorted(DATA_DIRS), default="full")
    parser.add_argument("--admin-user", default=os.environ.get("ADMIN_DB_USER", "root"))
    parser.add_argument("--keep-existing", action="store_true", help="不先清空原始表")
    parser.add_argument(
        "--staging",
        default=str(REPO_ROOT / ".tmp" / "load_staging"),
        help="LOAD DATA 的暂存目录（避免非 ASCII 路径问题）",
    )
    args = parser.parse_args()

    settings = Settings.load()
    admin_password = os.environ.get("ADMIN_DB_PASSWORD", "")
    if not admin_password:
        print("请通过环境变量 ADMIN_DB_PASSWORD 提供管理员密码", file=sys.stderr)
        return 2

    data_dir, suffix = DATA_DIRS[args.mode]
    if not data_dir.is_dir():
        print(f"找不到数据目录：{data_dir}", file=sys.stderr)
        return 2

    staging = Path(args.staging)
    staging.mkdir(parents=True, exist_ok=True)

    connection = pymysql.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=args.admin_user,
        password=admin_password,
        database=settings.db_name,
        charset=settings.db_charset,
        local_infile=True,
        autocommit=True,
        read_timeout=1800,
        write_timeout=1800,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET GLOBAL local_infile = 1")
            if not args.keep_existing:
                print("清空四张原始表 ...", flush=True)
                cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
                for table in (
                    "raw_campaign_exposure",
                    "raw_orders",
                    "raw_behavior_logs",
                    "raw_users",
                ):
                    cursor.execute(f"TRUNCATE TABLE {table}")
                cursor.execute("SET FOREIGN_KEY_CHECKS = 1")

            for table, template in LOAD_STATEMENTS.items():
                source = data_dir / f"{table.replace('raw_', '')}{suffix}.csv"
                if table == "raw_users":
                    source = data_dir / f"users{suffix}.csv"
                elif table == "raw_behavior_logs":
                    source = data_dir / f"behavior_logs{suffix}.csv"
                elif table == "raw_orders":
                    source = data_dir / f"orders{suffix}.csv"
                elif table == "raw_campaign_exposure":
                    source = data_dir / f"campaign_exposure{suffix}.csv"
                if not source.is_file():
                    print(f"找不到 CSV：{source}", file=sys.stderr)
                    return 2
                staged = staging / source.name
                shutil.copyfile(source, staged)
                path = str(staged).replace("\\", "/").replace("'", "''")
                print(f"导入 {source.name} -> {table} [{args.mode}] ...", flush=True)
                cursor.execute(template.format(path=path))
                print(f"  {cursor.rowcount} 行", flush=True)

            cursor.execute(COUNT_SQL)
            print("\n数据量统计：")
            for object_name, rows_count in cursor.fetchall():
                print(f"  {object_name:32s} {rows_count:>10,}")
    finally:
        connection.close()
    print(f"\n完成：{settings.db_name} [{args.mode}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
