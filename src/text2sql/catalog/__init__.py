"""Catalog package: the V1 business semantic registry.

Public entry points::

    from text2sql.catalog import load_catalog, Catalog

``load_catalog()`` builds the catalog from the static YAML definition, verifies
the section 6.7 consistency rules and (optionally) checks the live MySQL views.
"""

from __future__ import annotations

from ..config import Settings, get_settings
from .models import (
    CatalogCandidate,
    CatalogConsistencyError,
    CatalogError,
    CatalogSearchResult,
    ColumnMeta,
    ColumnRole,
    JoinRule,
    MetricSemantic,
    TableKind,
    TableMeta,
    TermMatch,
)
from .service import (
    MAX_TABLES_DEFAULT,
    SCORE_THRESHOLD,
    Catalog,
    match_specificity,
    normalize_term,
)

__all__ = [
    "MAX_TABLES_DEFAULT",
    "SCORE_THRESHOLD",
    "Catalog",
    "CatalogCandidate",
    "CatalogConsistencyError",
    "CatalogError",
    "CatalogSearchResult",
    "ColumnMeta",
    "ColumnRole",
    "JoinRule",
    "MetricSemantic",
    "TableKind",
    "TableMeta",
    "TermMatch",
    "load_catalog",
    "match_specificity",
    "normalize_term",
]


def load_catalog(
    settings: Settings | None = None,
    *,
    verify_live_columns: dict[str, list[str]] | None = None,
) -> Catalog:
    """Load and verify the V1 catalog.

    ``verify_live_columns`` maps ``table_name -> [column_name, ...]`` from the
    live MySQL views; when provided the catalog checks its own columns against
    the database (section 6.7.2) and refuses to start on a mismatch.
    """
    settings = settings or get_settings()
    from .mysql_source import load_catalog_from_settings

    catalog = load_catalog_from_settings(settings)
    if catalog.version != settings.catalog_version:
        raise CatalogConsistencyError(
            [
                f"CATALOG_VERSION={settings.catalog_version} 与 catalog 文件版本 "
                f"{catalog.version} 不一致"
            ]
        )
    if verify_live_columns is not None:
        catalog.verify_live(verify_live_columns)
    return catalog
