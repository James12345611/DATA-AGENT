"""Read-only database access for the Text2SQL agent."""

from __future__ import annotations

from .executor import QueryResult, ReadOnlyExecutor, jsonable

__all__ = ["QueryResult", "ReadOnlyExecutor", "jsonable"]
