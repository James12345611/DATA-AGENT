"""CLI tests: self-check, ask, trace, catalog, validate and log files."""

from __future__ import annotations

import json

import pytest

from text2sql.cli import main


def test_check_command_reports_catalog_and_join_rules(executor, capsys):
    exit_code = main(["check"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Catalog 版本" in captured.out
    assert "数据库视图字段与 Catalog 一致" in captured.out
    assert "允许的 JOIN 规则" in captured.out


def test_ask_offline_returns_result_table(executor, capsys):
    exit_code = main(["ask", "按渠道统计用户数", "--offline", "--show-sql"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "结果" in captured.out
    assert "LIMIT 100" in captured.out.upper()


def test_ask_json_matches_output_contract(executor, capsys):
    exit_code = main(["ask", "按渠道统计用户数", "--offline", "--json"])
    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert set(payload) == {
        "question",
        "rewrite_question",
        "selected_tables",
        "sql",
        "result_rows",
        "result_columns",
        "row_count",
        "status",
        "error",
    }
    assert payload["status"] == "success"
    assert payload["row_count"] == len(payload["result_rows"])


def test_trace_shows_every_node_and_timings(executor, capsys):
    exit_code = main(["trace", "按渠道统计用户数", "--offline"])
    captured = capsys.readouterr()
    assert exit_code == 0
    for node in (
        "information_extraction",
        "schema_linking",
        "generate_sql",
        "validate_sql",
        "execute_sql",
    ):
        assert node in captured.out
    assert "最终选表" in captured.out
    assert "节点耗时" in captured.out
    assert "结论：成功" in captured.out


def test_trace_renders_failure_reason(executor, capsys):
    exit_code = main(["trace", "删除所有订单", "--offline"])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "意图解析失败" in captured.out
    assert "unsafe_request" in captured.out
    assert "abort" in captured.out
    assert "结论：失败" in captured.out


def test_catalog_command_without_match_reports_no_guess(executor, capsys):
    exit_code = main(["catalog", "今天天气怎么样"])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "没有命中任何候选表" in captured.out


def test_validate_command_accepts_and_rejects(executor, capsys):
    assert main(["validate", "--sql", "SELECT channel FROM dim_user LIMIT 500"]) == 0
    captured = capsys.readouterr()
    assert "LIMIT 100" in captured.out, "超限 LIMIT 应被规范化"

    assert main(["validate", "--sql", "DROP TABLE dim_user"]) == 1
    rejected = capsys.readouterr()
    assert "unsafe_sql" in rejected.err


def test_verbose_logging_writes_to_file(executor, tmp_path, capsys):
    log_file = tmp_path / "run.log"
    exit_code = main(
        ["-v", "--log-file", str(log_file), "ask", "各会员等级的用户数量", "--offline"]
    )
    capsys.readouterr()
    assert exit_code == 0
    assert log_file.is_file()
    content = log_file.read_text(encoding="utf-8")
    assert "information_extraction" in content
    assert "schema_linking" in content
    assert "SQL 执行成功" in content


def test_log_file_never_contains_credentials(settings, executor, tmp_path, capsys):
    log_file = tmp_path / "secrets.log"
    main(["-v", "--log-file", str(log_file), "ask", "按渠道统计用户数", "--offline"])
    capsys.readouterr()
    content = log_file.read_text(encoding="utf-8")
    assert settings.db_password not in content
    assert settings.llm_api_key not in content
    assert "mysql+pymysql://" not in content


def test_missing_question_is_rejected(executor, capsys):
    with pytest.raises(SystemExit):
        main(["ask"])
