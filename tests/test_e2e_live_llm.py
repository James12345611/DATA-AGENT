"""End-to-end tests against the real LLM provider.

Skipped by default; run with::

    $env:RUN_LIVE_LLM = "1"
    python -m pytest tests/test_e2e_live_llm.py -v

These tests assert the same contract as the hermetic suite (section 8) but let the
real model produce the SQL, which is how the acceptance report in
``reports/acceptance_live.md`` is generated.
"""

from __future__ import annotations

import os

import pytest

from text2sql.acceptance import POSITIVE_CASES, evaluate_case
from text2sql.graph import build_graph, run_query
from text2sql.llm import ChatLLMClient, build_llm
from text2sql.nodes.deps import NodeDeps


def _live_llm_enabled() -> bool:
    return os.environ.get("RUN_LIVE_LLM", "") not in ("", "0", "false", "False")


pytestmark = pytest.mark.skipif(
    not _live_llm_enabled(),
    reason="设置 RUN_LIVE_LLM=1 才会调用真实模型",
)


@pytest.fixture(scope="module")
def live_deps(settings, catalog, executor):
    llm = build_llm(settings, catalog=catalog, force_offline=False)
    if not isinstance(llm, ChatLLMClient):  # pragma: no cover - configuration guard
        pytest.skip("LLM_API_KEY 未配置，跳过真实模型用例")
    return NodeDeps(settings=settings, catalog=catalog, llm=llm, executor=executor)


@pytest.mark.parametrize("case", POSITIVE_CASES, ids=[c.id for c in POSITIVE_CASES])
def test_live_positive_case(case, settings, live_deps):
    output = run_query(case.question, deps=live_deps, graph=build_graph(live_deps))
    failures = evaluate_case(case, output, settings)
    assert not failures, f"{case.id} " + "; ".join(failures) + f" | SQL: {output['sql']}"


def test_live_negative_destructive_request(settings, live_deps):
    output = run_query("删除所有订单", deps=live_deps, graph=build_graph(live_deps))
    assert output["status"] == "failed"
    assert output["error"]["recovery_strategy"] == "abort"


def test_live_negative_unknown_business_concept(settings, live_deps):
    output = run_query("统计各商品的名称和品牌", deps=live_deps, graph=build_graph(live_deps))
    assert output["status"] == "failed"
    assert output["sql"] == ""
    assert output["selected_tables"] == []
