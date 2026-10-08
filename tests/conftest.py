"""Shared test fixtures.

Database-backed tests skip automatically when MySQL is unreachable; no test ever
calls a real LLM unless ``RUN_LIVE_LLM=1`` is set.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from text2sql.catalog import Catalog, load_catalog  # noqa: E402
from text2sql.config import Settings  # noqa: E402
from text2sql.db import ReadOnlyExecutor  # noqa: E402
from text2sql.graph import build_graph, run_query  # noqa: E402
from text2sql.llm import BaseLLMClient, build_llm  # noqa: E402
from text2sql.nodes.deps import NodeDeps  # noqa: E402
from text2sql.prompts import Prompt  # noqa: E402


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings.load()


@pytest.fixture(scope="session")
def catalog(settings: Settings) -> Catalog:
    return load_catalog(settings)


@pytest.fixture(scope="session")
def executor(settings: Settings):
    executor = ReadOnlyExecutor(settings)
    try:
        executor.ping()
    except Exception as exc:  # noqa: BLE001
        executor.close()
        pytest.skip(f"MySQL 不可用，跳过数据库相关用例：{settings.redact(exc)}")
    yield executor
    executor.close()


@pytest.fixture
def offline_llm(settings: Settings, catalog: Catalog) -> BaseLLMClient:
    return build_llm(settings, catalog=catalog, force_offline=True)


@pytest.fixture
def deps(settings: Settings, catalog: Catalog, executor, offline_llm: BaseLLMClient) -> NodeDeps:
    return NodeDeps(settings=settings, catalog=catalog, llm=offline_llm, executor=executor)


@pytest.fixture
def graph(deps: NodeDeps):
    return build_graph(deps)


class ScriptedLLM(BaseLLMClient):
    """Deterministic LLM stub for contract and retry tests."""

    name = "scripted"

    def __init__(
        self,
        *,
        sql_script: list[str] | None = None,
        json_payload: dict[str, Any] | None = None,
        sql_error: Exception | None = None,
    ) -> None:
        self.sql_script = list(sql_script or [])
        self.json_payload = json_payload
        self.sql_error = sql_error
        self.sql_calls = 0
        self.json_calls = 0
        self.prompts: list[Prompt] = []

    def complete_text(self, prompt: Prompt, *, history=None) -> str:
        self.prompts.append(prompt)
        if prompt.json_mode:
            return "{}"
        return self.complete_sql(prompt)

    def complete_json(self, prompt: Prompt, *, validator=None, history=None):
        self.json_calls += 1
        self.prompts.append(prompt)
        payload = dict(self.json_payload or {})
        if validator is not None:
            validator(payload)
        return payload

    def complete_sql(self, prompt: Prompt, *, history=None) -> str:
        self.sql_calls += 1
        self.prompts.append(prompt)
        if self.sql_error is not None:
            raise self.sql_error
        if not self.sql_script:
            raise AssertionError("ScriptedLLM 没有可返回的 SQL")
        sql = self.sql_script[min(self.sql_calls - 1, len(self.sql_script) - 1)]
        return sql


@pytest.fixture
def scripted_llm() -> type[ScriptedLLM]:
    return ScriptedLLM


@pytest.fixture
def run_offline(deps: NodeDeps, graph):
    def _run(question: str, messages: list[Any] | None = None):
        return run_query(question, messages=messages, deps=deps, graph=graph)

    return _run


def live_llm_enabled() -> bool:
    return os.environ.get("RUN_LIVE_LLM", "") not in ("", "0", "false", "False")
