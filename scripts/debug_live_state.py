"""Debug helper: run one question with the real LLM and dump every graph node output."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from text2sql.config import Settings  # noqa: E402
from text2sql.graph import build_graph  # noqa: E402
from text2sql.nodes.deps import NodeDeps  # noqa: E402
from text2sql.state import to_jsonable  # noqa: E402


def main() -> int:
    question = " ".join(sys.argv[1:]) or "哪些用户是复购用户"
    settings = Settings.load()
    deps = NodeDeps.build(settings)
    try:
        graph = build_graph(deps)
        initial = {
            "question": question,
            "messages": [],
            "retry_count": 0,
            "previous_sql_errors": [],
            "status": "pending",
            "error": None,
        }
        for chunk in graph.stream(initial, stream_mode="updates"):
            for node, update in chunk.items():
                print(f"--- {node} ---")
                print(json.dumps(to_jsonable(update), ensure_ascii=False, indent=2))
    finally:
        deps.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
