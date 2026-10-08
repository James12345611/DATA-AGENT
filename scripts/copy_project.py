"""Copy the Text2SQL project into a target directory, skipping secrets and caches."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".venv", ".tmp", ".git", ".pytest_cache", "__pycache__", ".ruff_cache", "logs"}
SKIP_FILES = {".env"}
SKIP_PREFIXES = ("pytest-cache-files-",)


def ignore(directory: str, names: list[str]) -> set[str]:
    skipped: set[str] = set()
    for name in names:
        if name in SKIP_DIRS or name in SKIP_FILES:
            skipped.add(name)
        elif name.startswith(SKIP_PREFIXES) or name.endswith(".pyc"):
            skipped.add(name)
    return skipped


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: copy_project.py <目标目录>", file=sys.stderr)
        return 2
    target = Path(sys.argv[1]).resolve()
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SOURCE, target, dirs_exist_ok=True, ignore=ignore)
    files = [p for p in target.rglob("*") if p.is_file() and ".git" not in p.parts]
    print(f"已复制 {len(files)} 个文件到 {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
