"""Shared node dependencies and helpers.

Nodes are plain functions of ``(state)``; every external dependency (settings,
catalog, LLM client, executor) is injected through :class:`NodeDeps` when the
graph is built.  This keeps each node unit-testable with fakes and keeps the
state contract free of secrets.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

from ..catalog import Catalog, load_catalog
from ..config import Settings, get_settings
from ..db import ReadOnlyExecutor
from ..llm import BaseLLMClient, build_llm

logger = logging.getLogger(__name__)


@dataclass
class NodeDeps:
    """Everything the nodes need, injected once per graph build."""

    settings: Settings
    catalog: Catalog
    llm: BaseLLMClient
    executor: ReadOnlyExecutor
    verify_catalog_live: bool = False

    @classmethod
    def build(
        cls,
        settings: Settings | None = None,
        *,
        llm: BaseLLMClient | None = None,
        executor: ReadOnlyExecutor | None = None,
        catalog: Catalog | None = None,
        verify_catalog_live: bool = False,
        force_offline_llm: bool = False,
    ) -> "NodeDeps":
        settings = settings or get_settings()
        executor = executor or ReadOnlyExecutor(settings)
        live_columns = None
        if verify_catalog_live:
            live_columns = executor.live_columns()
        catalog = catalog or load_catalog(settings, verify_live_columns=live_columns)
        llm = llm or build_llm(settings, catalog=catalog, force_offline=force_offline_llm)
        return cls(
            settings=settings,
            catalog=catalog,
            llm=llm,
            executor=executor,
            verify_catalog_live=verify_catalog_live,
        )

    def close(self) -> None:
        self.executor.close()
        self.llm.close()


def history_messages(question: str, messages: Sequence[Any] | None) -> list[Any]:
    """Return chat history without duplicating the current question.

    Section 4.1: the current question may appear both in ``question`` and as the
    last ``HumanMessage``; nodes must not send it twice.
    """
    history = list(messages or [])
    if not history:
        return []
    last = history[-1]
    content = getattr(last, "content", None)
    is_human = type(last).__name__ == "HumanMessage" or (
        hasattr(last, "type") and getattr(last, "type", "") == "human"
    )
    if is_human and isinstance(content, str) and content.strip() == question.strip():
        return history[:-1]
    return history


__all__ = ["NodeDeps", "history_messages"]
