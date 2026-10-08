"""Ad-hoc debugging helper: show the offline extraction for a question."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from text2sql.catalog import load_catalog  # noqa: E402
from text2sql.catalog.service import normalize_term  # noqa: E402
from text2sql.config import Settings  # noqa: E402
from text2sql.llm import build_llm  # noqa: E402
from text2sql.prompts import extraction_prompt  # noqa: E402


def main() -> int:
    settings = Settings.load()
    catalog = load_catalog(settings)
    llm = build_llm(settings, catalog=catalog, force_offline=True)
    for question in sys.argv[1:]:
        payload = llm.complete_json(extraction_prompt(question, []))
        print(f"=== {question} (norm={normalize_term(question)}) ===")
        print(json.dumps(payload, ensure_ascii=False))
        for term in payload.get("dimensions", []):
            print("  dimension", term, "->", catalog.resolve_dimension(term))
        for term in payload.get("metrics", []):
            print("  metric", term, "->", catalog.resolve_metric(term))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
