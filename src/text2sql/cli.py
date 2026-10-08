"""Command line interface for the V1 Text2SQL agent.

Examples::

    python -m text2sql check
    python -m text2sql ask "按渠道统计用户数"
    python -m text2sql ask "各活动的曝光量、点击率和转化率" --show-sql --json
    python -m text2sql trace "行为次数最多的 10 个用户"     # 逐节点观测（推荐排错用）
    python -m text2sql catalog "行为次数最多的 10 个用户"
    python -m text2sql validate --sql "SELECT channel, COUNT(*) FROM dim_user LIMIT 200"

全局选项（放在子命令之前或之后都可以）::

    -v / --verbose     打印 text2sql 的详细日志（INFO/DEBUG）
    --log-file FILE    把日志同时写入文件，便于事后排查
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from .catalog import CatalogConsistencyError, load_catalog
from .config import ConfigurationError, Settings, get_settings
from .db import ReadOnlyExecutor
from .graph import build_graph, make_deps, run_query
from .state import SQLOutputState, state_to_json, to_jsonable
from .validation import SQLValidator, ValidationConfig

TRACE_ORDER = (
    "information_extraction",
    "schema_linking",
    "generate_sql",
    "validate_sql",
    "execute_sql",
    "regenerate_sql",
    "failed",
)


def _setup_logging(verbose: bool, log_file: str | None = None) -> None:
    """INFO for the project loggers, WARNING for third-party libraries."""
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
        print(f"日志写入：{path}", file=sys.stderr)

    project_logger = logging.getLogger("text2sql")
    project_logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not verbose:
        # INFO 级别的节点日志只在 -v 时输出，避免默认刷屏
        project_logger.setLevel(logging.WARNING)
    logging.getLogger("text2sql.db").setLevel(
        logging.DEBUG if verbose else logging.INFO
    )


def _print_table(columns: Sequence[str], rows: Sequence[dict[str, Any]], limit: int = 20) -> None:
    if not columns:
        print("(无返回列)")
        return
    widths = {name: len(str(name)) for name in columns}
    display_rows = list(rows[:limit])
    for row in display_rows:
        for name in columns:
            widths[name] = max(widths[name], len(_cell(row.get(name))))
    header = " | ".join(str(name).ljust(widths[name]) for name in columns)
    print(header)
    print("-+-".join("-" * widths[name] for name in columns))
    for row in display_rows:
        print(" | ".join(_cell(row.get(name)).ljust(widths[name]) for name in columns))
    if len(rows) > len(display_rows):
        print(f"... 共 {len(rows)} 行，仅显示前 {len(display_rows)} 行")


def _cell(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _cmd_check(settings: Settings, args: argparse.Namespace) -> int:
    print("运行配置：", json.dumps(settings.describe(), ensure_ascii=False))
    executor = ReadOnlyExecutor(settings)
    try:
        live = executor.live_columns()
        catalog = load_catalog(settings, verify_live_columns=live)
    except CatalogConsistencyError as exc:
        print("Catalog 一致性校验失败：", file=sys.stderr)
        for issue in exc.issues:
            print("  - " + issue, file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"数据库或 Catalog 检查失败：{settings.redact(exc)}", file=sys.stderr)
        return 1

    print(f"Catalog 版本 {catalog.version}，暴露表 {len(catalog.allowed_tables)} 张：")
    for table_name in catalog.allowed_tables:
        table = catalog.get_table(table_name)
        columns = catalog.columns_of(table_name)
        print(
            f"  - {table_name}（{table.grain}，{len(columns)} 字段）：{table.description}"
        )
    print("数据库视图字段与 Catalog 一致")
    print(f"允许的 JOIN 规则：{len(catalog.allowed_join_rules())} 条")
    for rule in catalog.allowed_join_rules():
        print(
            f"  - {rule['left_table']}.{rule['left_column']} = "
            f"{rule['right_table']}.{rule['right_column']}（{rule['cardinality']}）"
        )
    executor.close()
    return 0


def _cmd_ask(settings: Settings, args: argparse.Namespace) -> int:
    messages = _load_messages(args.messages) if args.messages else []
    deps = make_deps(settings, force_offline_llm=args.offline)
    try:
        graph = build_graph(deps)
        state = run_query(args.question, messages=messages, deps=deps, graph=graph)
    finally:
        deps.close()

    if args.json:
        print(json.dumps(to_jsonable(state), ensure_ascii=False, indent=2))
        return 0 if state["status"] == "success" else 1

    if args.show_sql or args.verbose:
        print("选中表：")
        for selection in state["selected_tables"]:
            print(f"  - {selection['table']}: {', '.join(selection['columns'])}")
        print("SQL:")
        print(state["sql"] or "(无)")
        print()

    if state["status"] == "success":
        print(f"结果 {state['row_count']} 行：")
        _print_table(state["result_columns"], state["result_rows"])
        return 0

    error = state["error"] or {}
    print(f"查询失败：{error.get('error_type')} - {error.get('message')}", file=sys.stderr)
    print(f"恢复策略：{error.get('recovery_strategy')}", file=sys.stderr)
    return 1


def _cmd_trace(settings: Settings, args: argparse.Namespace) -> int:
    """Run one question and print every node's decision, step by step."""
    deps = make_deps(settings, force_offline_llm=args.offline)
    graph = build_graph(deps)
    initial = {
        "question": args.question,
        "messages": [],
        "retry_count": 0,
        "previous_sql_errors": [],
        "status": "pending",
        "error": None,
    }
    print(f"问题：{args.question}")
    print(f"模式：{'离线确定性客户端' if args.offline else settings.llm_provider + '/' + settings.llm_model}")
    print("=" * 78)

    step = 0
    timings: list[tuple[str, float]] = []
    final_state: dict[str, Any] = dict(initial)
    started = time.perf_counter()
    try:
        for chunk in graph.stream(initial, stream_mode="updates"):
            for node, update in chunk.items():
                step += 1
                elapsed = time.perf_counter() - started
                timings.append((node, elapsed))
                print(f"\n[{step}] {node}   (+{elapsed:.2f}s)")
                print("-" * 78)
                for line in _render_node(node, update):
                    print("  " + line)
                if isinstance(update, dict):
                    final_state.update(update)
                started = time.perf_counter()
    finally:
        deps.close()

    total = sum(elapsed for _, elapsed in timings)
    print("\n" + "=" * 78)
    print("节点耗时：")
    for node, elapsed in timings:
        print(f"  {node:24s} {elapsed:6.2f}s")
    print(f"  {'合计':24s} {total:6.2f}s")
    print("=" * 78)

    status = final_state.get("status")
    if status == "success":
        print(f"结论：成功，返回 {final_state.get('row_count', 0)} 行")
        _print_table(
            final_state.get("result_columns", []), final_state.get("result_rows", [])
        )
        return 0
    error = final_state.get("error") or {}
    print(f"结论：失败（{error.get('error_type')} / {error.get('recovery_strategy')}）")
    print(f"原因：{error.get('message')}")
    print("提示：错误类型与恢复策略的含义见 docs/测试指南.md 第 4 节")
    return 1


def _render_node(node: str, update: Any) -> list[str]:
    if not isinstance(update, dict):
        return [str(update)]
    lines: list[str] = []
    error_rendered = False

    if node == "information_extraction":
        if update.get("rewrite_question"):
            lines.append(f"规范化问题：{update.get('rewrite_question')}")
        entities = update.get("info_entities") or {}
        if entities:
            lines.append(f"关键词：{'、'.join(entities.get('keywords') or []) or '（无）'}")
            lines.append(f"维度：{'、'.join(entities.get('dimensions') or []) or '（无）'}")
            lines.append(f"指标：{'、'.join(entities.get('metrics') or []) or '（无）'}")
            filters = entities.get("filters") or []
            if filters:
                lines.append(
                    "过滤：" + "；".join(
                        f"{f.get('field_hint')} {f.get('operator')} {f.get('value')}"
                        for f in filters
                    )
                )
            if entities.get("start_time") or entities.get("end_time"):
                lines.append(
                    f"时间范围：{entities.get('start_time')} ~ {entities.get('end_time')}"
                )
        if update.get("error"):
            lines.append("意图解析失败：")

    elif node == "schema_linking":
        candidates = update.get("candidate_tables") or []
        if candidates:
            lines.append("候选表（Catalog 检索结果）：")
            for candidate in candidates:
                lines.append(
                    f"  · {candidate['table']}  字段={', '.join(candidate['columns'])}"
                )
                if candidate.get("reasoning"):
                    lines.append(f"    理由：{candidate['reasoning']}")
        selected = update.get("selected_tables") or []
        if selected:
            lines.append("最终选表（字段已通过白名单与覆盖度校验）：")
            for selection in selected:
                lines.append(
                    f"  ✓ {selection['table']}（{', '.join(selection['columns'])}）"
                )
        elif update.get("error"):
            lines.append("选表失败：")
        else:
            lines.append("未选出任何表")

    elif node == "generate_sql":
        lines.append(f"重试次数：{update.get('retry_count', 0)}")
        lines.append("生成的 SQL：")
        lines.extend(f"  {line}" for line in str(update.get("sql") or "(无)").splitlines())

    elif node == "validate_sql":
        if update.get("status") == "pending" and update.get("error") is None:
            lines.append("安全校验：通过（单语句 / 白名单表字段 / LIMIT / 无危险语句）")
            if update.get("sql"):
                lines.append("校验器规范化了 LIMIT，SQL 被改写为：")
                lines.extend(f"  {line}" for line in str(update["sql"]).splitlines())
        else:
            error = update.get("error") or {}
            lines.append(f"安全校验：拒绝（{error.get('error_type')}）")
            lines.append(f"原因：{error.get('message')}")
            lines.append("V1 策略：安全拒绝直接失败，不会交给模型反复重试")
            error_rendered = True

    elif node == "execute_sql":
        if update.get("status") == "success":
            lines.append(f"执行成功：{update.get('row_count', 0)} 行")
            lines.append(f"返回列：{', '.join(update.get('result_columns') or []) or '（无）'}")
        else:
            error = update.get("error") or {}
            lines.append(f"执行失败：{error.get('error_type')}（恢复策略 {error.get('recovery_strategy')}）")
            lines.append(f"原因：{error.get('message')}")
            history = update.get("previous_sql_errors") or []
            if history:
                lines.append(f"已累计错误 {len(history)} 条，供 regenerate_sql 修正时参考")

    elif node == "regenerate_sql":
        lines.append(f"修正后重试次数：{update.get('retry_count')}")
        lines.append("修正后的 SQL：")
        lines.extend(f"  {line}" for line in str(update.get("sql") or "(无)").splitlines())

    elif node == "failed":
        lines.append("进入失败出口，状态置为 failed")

    else:  # pragma: no cover - defensive
        lines.append(json.dumps(to_jsonable(update), ensure_ascii=False))

    error = update.get("error")
    if error and not error_rendered:
        lines.append(f"错误类型：{error.get('error_type')}")
        lines.append(f"错误说明：{error.get('message')}")
        lines.append(f"恢复策略：{error.get('recovery_strategy')}")
    return lines


def _cmd_catalog(settings: Settings, args: argparse.Namespace) -> int:
    catalog = load_catalog(settings)
    result = catalog.search(
        question=args.question,
        keywords=args.keywords or [],
        dimensions=args.dimensions or [],
        metrics=args.metrics or [],
    )
    print(f"Catalog 版本：{result['catalog_version']}")
    if not result["candidates"]:
        print("没有命中任何候选表（V1 不会猜测表）")
        return 1
    for candidate in result["candidates"]:
        print(
            f"  - {candidate['table']} score={candidate['score']} "
            f"columns={candidate['matched_columns']} terms={candidate['matched_terms']}"
        )
        print(f"      {candidate['reason']}")
    return 0


def _cmd_validate(settings: Settings, args: argparse.Namespace) -> int:
    catalog = load_catalog(settings)
    selected = None
    if args.tables:
        selected = [
            {"table": name, "columns": sorted(catalog.column_names(name))} for name in args.tables
        ]
    validator = SQLValidator(catalog, ValidationConfig.from_settings(settings), settings)
    result = validator.validate(args.sql, selected)
    if result.ok:
        print("校验通过")
        print(result.sql)
        return 0
    error = result.error or {}
    print(f"校验拒绝：{error.get('error_type')} - {error.get('message')}", file=sys.stderr)
    return 1


def _load_messages(path: str) -> list[Any]:
    from langchain_core.messages import AIMessage, HumanMessage

    payload = json.loads(open(path, encoding="utf-8").read())
    messages: list[Any] = []
    for item in payload:
        role = item.get("role")
        content = item.get("content", "")
        if role == "human":
            messages.append(HumanMessage(content=content))
        elif role == "ai":
            messages.append(AIMessage(content=content))
    return messages


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="text2sql", description="V1 Text2SQL agent")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出详细日志")
    parser.add_argument(
        "--log-file",
        default=None,
        help="把日志同时写入文件（例如 logs/run.log）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="自检：配置、Catalog 与数据库对象是否一致")
    check.set_defaults(func=_cmd_check)

    ask = sub.add_parser("ask", help="用自然语言提问")
    ask.add_argument("question")
    ask.add_argument("--json", action="store_true", help="输出完整状态 JSON")
    ask.add_argument("--show-sql", action="store_true", help="展示选表与 SQL")
    ask.add_argument("--offline", action="store_true", help="强制使用离线确定性客户端")
    ask.add_argument("--messages", help="多轮上下文 JSON 文件（[{role, content}]）")
    ask.set_defaults(func=_cmd_ask)

    trace = sub.add_parser("trace", help="逐节点观测一次查询（排错首选）")
    trace.add_argument("question")
    trace.add_argument("--offline", action="store_true", help="强制使用离线确定性客户端")
    trace.set_defaults(func=_cmd_trace)

    catalog_cmd = sub.add_parser("catalog", help="只跑 Catalog 检索，观察候选表")
    catalog_cmd.add_argument("question")
    catalog_cmd.add_argument("--keywords", nargs="*", default=[])
    catalog_cmd.add_argument("--dimensions", nargs="*", default=[])
    catalog_cmd.add_argument("--metrics", nargs="*", default=[])
    catalog_cmd.set_defaults(func=_cmd_catalog)

    validate = sub.add_parser("validate", help="只跑 SQL 安全校验")
    validate.add_argument("--sql", required=True)
    validate.add_argument("--tables", nargs="*", default=None)
    validate.set_defaults(func=_cmd_validate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose, args.log_file)
    try:
        settings = get_settings()
    except ConfigurationError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    return int(args.func(settings, args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
