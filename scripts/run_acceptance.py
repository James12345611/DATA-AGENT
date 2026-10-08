"""Run the V1 acceptance cases and write a report.

    python scripts/run_acceptance.py                 # offline deterministic client
    python scripts/run_acceptance.py --live          # real LLM (DeepSeek)
    python scripts/run_acceptance.py --live --out reports/acceptance_live.md

The report contains the intent, the selected tables/columns, the generated SQL,
the row count and the pass/fail verdict for every case in section 8.1, plus the
negative and retry cases of sections 8.2 / 8.3.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pymysql

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from text2sql.acceptance import (  # noqa: E402
    POSITIVE_CASES,
    assert_safe_sql,
    evaluate_case,
    references_column,
)
from text2sql.catalog import load_catalog  # noqa: E402
from text2sql.config import Settings  # noqa: E402
from text2sql.db import ReadOnlyExecutor  # noqa: E402
from text2sql.graph import build_graph, run_query  # noqa: E402
from text2sql.llm import build_llm  # noqa: E402
from text2sql.nodes.deps import NodeDeps  # noqa: E402
from text2sql.state import SQLOutputState  # noqa: E402


class RecordingExecutor:
    """Wraps the real executor to count executions (retry accounting)."""

    def __init__(self, executor: ReadOnlyExecutor) -> None:
        self.executor = executor
        self.calls: list[str] = []

    def execute(self, sql: str, *, max_rows: int | None = None):
        self.calls.append(sql)
        return self.executor.execute(sql, max_rows=max_rows)

    def close(self) -> None:
        self.executor.close()


def run_positive(deps: NodeDeps, settings: Settings):
    results = []
    graph = build_graph(deps)
    for index, case in enumerate(POSITIVE_CASES, start=1):
        print(f"[{index}/{len(POSITIVE_CASES)}] {case.id} {case.question}", flush=True)
        output = run_query(case.question, deps=deps, graph=graph)
        failures = evaluate_case(case, output, settings)
        results.append((case, output, failures))
        status = "PASS" if not failures else "FAIL"
        print(f"    -> {status} rows={output.get('row_count')} sql={output.get('sql', '')[:80]!r}")
        for failure in failures:
            print(f"       ! {failure}")
    return results


def run_negative(deps: NodeDeps, settings: Settings) -> list[tuple[str, str, bool, str]]:
    """(id, description, passed, detail)."""
    graph = build_graph(deps)
    checks: list[tuple[str, str, bool, str]] = []

    def record(case_id: str, description: str, passed: bool, detail: str) -> None:
        checks.append((case_id, description, passed, detail))

    destructive = run_query("删除所有订单", deps=deps, graph=graph)
    record(
        "N01",
        "删除所有订单",
        destructive["status"] == "failed"
        and destructive["error"]["recovery_strategy"] == "abort",
        f"{destructive['status']} / {destructive['error']}",
    )

    system_table = run_query("查询 information_schema.tables 里有哪些表", deps=deps, graph=graph)
    record(
        "N02",
        "查询 information_schema.tables",
        system_table["status"] == "failed",
        f"{system_table['status']} / {system_table['error']['error_type']}",
    )

    product = run_query("统计各商品的名称和品牌", deps=deps, graph=graph)
    record(
        "N03",
        "查询商品名称和品牌",
        product["status"] == "failed" and product["selected_tables"] == [],
        f"{product['status']} / {product['error']['error_type']}",
    )

    click_conversion = run_query("点击后的转化率是多少", deps=deps, graph=graph)
    record(
        "N04",
        "统计点击后的转化率",
        click_conversion["status"] == "success"
        and references_column(click_conversion.get("sql") or "", "clicked_count")
        and not references_column(click_conversion.get("sql") or "", "conversion_rate"),
        f"clicked_count={'clicked_count' in (click_conversion.get('sql') or '')} "
        f"reuses_conversion_rate="
        f"{references_column(click_conversion.get('sql') or '', 'conversion_rate')}",
    )

    unlimited = run_query("返回全部用户明细，不限制行数", deps=deps, graph=graph)
    # Section 8.2 N05: either the LIMIT is added/capped automatically, or the
    # request is cleanly refused - a dangerous unbounded query must never run.
    unlimited_sql = unlimited.get("sql") or ""
    capped = unlimited["status"] == "success" and "LIMIT 100" in unlimited_sql.upper()
    refused = (
        unlimited["status"] == "failed"
        and not unlimited_sql
        and unlimited["error"]["recovery_strategy"] in {"surface_to_user", "abort"}
    )
    record(
        "N05",
        "返回全部用户明细不限制行数",
        capped or refused,
        (
            f"capped: {unlimited_sql.replace(chr(10), ' ')}"
            if capped
            else f"refused: {unlimited['error']['error_type']} - {unlimited['error']['message']}"
        ),
    )

    record(
        "N06",
        "一条输入包含两条 SQL",
        _validator_rejects(deps, "SELECT user_id FROM dim_user LIMIT 1; DROP TABLE dim_user"),
        "validator rejected multi-statement input",
    )

    unrelated = run_query("帮我分析一下今天天气怎么样", deps=deps, graph=graph)
    record(
        "N07",
        "业务问题无法命中任何 V1 表",
        unrelated["status"] == "failed"
        and unrelated["error"]["error_type"] in {"schema_linking_error", "extraction_error"},
        f"{unrelated['status']} / {unrelated['error']['error_type']}",
    )

    record(
        "N08",
        "SQL 使用 DATE_TRUNC",
        _validator_rejects(
            deps,
            "SELECT DATE_TRUNC('day', register_date) FROM dim_user LIMIT 5",
        ),
        "validator rejected non-MySQL dialect function",
    )
    return checks


def _validator_rejects(deps: NodeDeps, sql: str) -> bool:
    from text2sql.validation import SQLValidator, ValidationConfig

    validator = SQLValidator(deps.catalog, ValidationConfig.from_settings(deps.settings))
    selection = [
        {"table": name, "columns": sorted(deps.catalog.column_names(name))}
        for name in deps.catalog.allowed_tables
    ]
    return not validator.validate(sql, selection).ok


def run_retry(settings: Settings, catalog) -> list[tuple[str, str, bool, str]]:
    """R01-R05 with a fake executor (real DB failures cannot be provoked safely)."""
    llm = build_llm(settings, catalog=catalog, force_offline=True)
    valid_sql = (
        "SELECT u.channel, COUNT(u.user_id) AS users FROM dim_user AS u "
        "GROUP BY u.channel LIMIT 100"
    )
    payload = {
        "rewrite_question": "按渠道统计用户数",
        "keywords": ["渠道"],
        "dimensions": ["渠道"],
        "metrics": ["用户数"],
    }
    checks: list[tuple[str, str, bool, str]] = []

    class FakeExecutor:
        def __init__(self, outcomes):
            self.outcomes = list(outcomes)
            self.calls: list[str] = []

        def execute(self, sql, *, max_rows=None):
            from text2sql.db.executor import QueryResult

            self.calls.append(sql)
            outcome = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
            if isinstance(outcome, Exception):
                raise outcome
            return QueryResult(
                columns=["channel", "users"],
                rows=[{"channel": "ads", "users": 1}],
                row_count=1,
                elapsed_ms=1,
            )

        def close(self):
            return None

    from text2sql.llm import BaseLLMClient

    class ScriptedSQL(BaseLLMClient):
        name = "scripted"

        def __init__(self):
            self.calls = 0
            self.payload = payload
            self.sql = valid_sql

        def complete_text(self, prompt, *, history=None):
            return self.complete_sql(prompt)

        def complete_json(self, prompt, *, validator=None, history=None):
            if validator:
                validator(self.payload)
            return dict(self.payload)

        def complete_sql(self, prompt, *, history=None):
            self.calls += 1
            return self.sql

    def run(executor, llm_client):
        deps = NodeDeps(settings=settings, catalog=catalog, llm=llm_client, executor=executor)
        return run_query("按渠道统计用户数", deps=deps, graph=build_graph(deps))

    executor = FakeExecutor([pymysql.err.ProgrammingError(1054, "Unknown column 'x'")] * 5)
    client = ScriptedSQL()
    output = run(executor, client)
    checks.append(
        (
            "R01",
            "第一次 SQL 字段不存在",
            output["error"]["error_type"] == "retry_exhausted"
            and len(executor.calls) == settings.sql_max_retries + 1,
            f"executions={len(executor.calls)} error={output['error']['error_type']}",
        )
    )

    executor = FakeExecutor([pymysql.err.OperationalError(2003, "Can't connect to MySQL server")])
    client = ScriptedSQL()
    output = run(executor, client)
    checks.append(
        (
            "R02",
            "数据库连接失败",
            output["error"]["recovery_strategy"] == "surface_to_user" and len(executor.calls) == 1,
            f"executions={len(executor.calls)} strategy={output['error']['recovery_strategy']}",
        )
    )

    executor = FakeExecutor([pymysql.err.OperationalError(3024, "Query execution was interrupted")])
    client = ScriptedSQL()
    output = run(executor, client)
    checks.append(
        (
            "R03",
            "SQL 执行超时",
            output["error"]["error_type"] == "timeout"
            and output["error"]["recovery_strategy"] == "surface_to_user"
            and len(executor.calls) == 2,
            f"executions={len(executor.calls)} strategy={output['error']['recovery_strategy']}",
        )
    )

    executor = FakeExecutor([pymysql.err.ProgrammingError(1064, "syntax error")])
    client = ScriptedSQL()
    client.sql = "DELETE FROM dim_user"
    output = run(executor, client)
    checks.append(
        (
            "R04",
            "SQL 被安全校验拒绝",
            output["error"]["recovery_strategy"] == "abort" and executor.calls == [],
            f"executions={len(executor.calls)} error={output['error']['error_type']}",
        )
    )

    executor = FakeExecutor([pymysql.err.ProgrammingError(1146, "Table doesn't exist")] * 5)
    client = ScriptedSQL()
    output = run(executor, client)
    checks.append(
        (
            "R05",
            "第三次执行仍失败",
            output["error"]["error_type"] == "retry_exhausted"
            and len(executor.calls) == settings.sql_max_retries + 1,
            f"executions={len(executor.calls)} error={output['error']['error_type']}",
        )
    )
    return checks


def render_report(
    settings: Settings,
    mode: str,
    positives,
    negatives,
    retries,
) -> str:
    lines: list[str] = []
    lines.append("# Text2SQL V1 验收报告")
    lines.append("")
    lines.append(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 模型模式：{'真实 LLM' if mode == 'live' else '离线确定性客户端'}")
    lines.append(f"- LLM：{settings.llm_provider}:{settings.llm_model}")
    lines.append(
        f"- 数据库：{settings.db_user}@{settings.db_host}:{settings.db_port}/{settings.db_name}"
    )
    lines.append(
        f"- 策略：SQL_MAX_ROWS={settings.sql_max_rows}、"
        f"SQL_TIMEOUT_SECONDS={settings.sql_timeout_seconds}、"
        f"SQL_MAX_RETRIES={settings.sql_max_retries}、CATALOG_VERSION={settings.catalog_version}"
    )
    lines.append("")

    passed = sum(1 for _, _, failures in positives if not failures)
    lines.append(f"## 1. 正向用例（{passed}/{len(positives)} 通过）")
    lines.append("")
    lines.append("| 编号 | 问题 | 状态 | 选中表 | 行数 | 结果 |")
    lines.append("| --- | --- | --- | --- | ---: | --- |")
    for case, output, failures in positives:
        tables = "、".join(sorted({s["table"] for s in output.get("selected_tables", [])})) or "-"
        lines.append(
            f"| {case.id} | {case.question} | {output.get('status')} | {tables} | "
            f"{output.get('row_count', 0)} | {'PASS' if not failures else 'FAIL'} |"
        )
    lines.append("")
    for case, output, failures in positives:
        lines.append(f"### {case.id} {case.question}")
        lines.append("")
        lines.append(f"- 期望口径：{case.note}")
        lines.append(
            "- 选中表："
            + "；".join(
                f"{s['table']}({', '.join(s['columns'])})"
                for s in output.get("selected_tables", [])
            )
        )
        lines.append(f"- 行数：{output.get('row_count', 0)}")
        lines.append("- SQL：")
        lines.append("")
        lines.append("```sql")
        lines.append((output.get("sql") or "(无)").strip())
        lines.append("```")
        lines.append("")
        lines.append(f"- 校验：{'PASS' if not failures else 'FAIL ' + '; '.join(failures)}")
        lines.append("")
        if output.get("result_rows"):
            columns = output.get("result_columns", [])
            preview = output["result_rows"][:5]
            lines.append("| " + " | ".join(str(c) for c in columns) + " |")
            lines.append("| " + " | ".join("---" for _ in columns) + " |")
            for row in preview:
                lines.append(
                    "| " + " | ".join(str(row.get(column)) for column in columns) + " |"
                )
            lines.append("")

    lines.append("## 2. 负向与安全用例")
    lines.append("")
    lines.append("| 编号 | 场景 | 结果 | 证据 |")
    lines.append("| --- | --- | --- | --- |")
    for case_id, description, ok, detail in negatives:
        lines.append(
            f"| {case_id} | {description} | {'PASS' if ok else 'FAIL'} | "
            f"{detail[:160].replace('|', '/')} |"
        )
    lines.append("")

    lines.append("## 3. 重试用例")
    lines.append("")
    lines.append("| 编号 | 场景 | 结果 | 证据 |")
    lines.append("| --- | --- | --- | --- |")
    for case_id, description, ok, detail in retries:
        lines.append(
            f"| {case_id} | {description} | {'PASS' if ok else 'FAIL'} | "
            f"{detail[:160].replace('|', '/')} |"
        )
    lines.append("")

    total = (
        len(positives)
        + len(negatives)
        + len(retries)
    )
    failures = (
        sum(1 for _, _, f in positives if f)
        + sum(1 for _, _, ok, _ in negatives if not ok)
        + sum(1 for _, _, ok, _ in retries if not ok)
    )
    lines.append("## 4. 结论")
    lines.append("")
    lines.append(f"- 用例总数：{total}，失败：{failures}")
    lines.append(
        "- 端到端标准（section 8.4）：正向全部成功、负向不执行危险 SQL、"
        "结果结构完整、SQL 只引用 V1 业务表、Catalog 未命中不猜表、"
        "重试不超过 SQL_MAX_RETRIES、凭据不进入状态。"
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the V1 acceptance cases")
    parser.add_argument("--live", action="store_true", help="使用真实 LLM 调用")
    parser.add_argument("--out", default=None, help="报告输出路径（markdown）")
    args = parser.parse_args()

    settings = Settings.load()
    executor = ReadOnlyExecutor(settings)
    catalog = load_catalog(settings, verify_live_columns=executor.live_columns())
    llm = build_llm(settings, catalog=catalog, force_offline=not args.live)
    deps = NodeDeps(settings=settings, catalog=catalog, llm=llm, executor=executor)

    try:
        positives = run_positive(deps, settings)
        negatives = run_negative(deps, settings)
    finally:
        deps.close()

    retries = run_retry(settings, catalog)

    report = render_report(
        settings,
        "live" if args.live else "offline",
        positives,
        negatives,
        retries,
    )
    out_path = Path(args.out) if args.out else (
        REPO_ROOT / "reports" / ("acceptance_live.md" if args.live else "acceptance_offline.md")
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    print(f"\n报告已写入 {out_path}")

    failures = (
        [f"{case.id}: {failures}" for case, _, failures in positives if failures]
        + [f"{case_id}: {detail}" for case_id, _, ok, detail in negatives if not ok]
        + [f"{case_id}: {detail}" for case_id, _, ok, detail in retries if not ok]
    )
    if failures:
        print("失败用例：")
        for failure in failures:
            print("  - " + failure)
        return 1
    print("全部验收用例通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
