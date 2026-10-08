"""Text2SQL V1 - a LangGraph based NL2SQL MVP for the ecommerce business views.

The public entry points are:

* :func:`text2sql.graph.build_graph` - the compiled LangGraph subgraph
* :func:`text2sql.graph.run_query` - run one question end to end
* :func:`text2sql.catalog.load_catalog` - the V1 business catalog

See ``text2sql_V1.md`` for the full contract (state, nodes, catalog, config).
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
