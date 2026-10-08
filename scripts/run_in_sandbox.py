"""Run pip (or any module) inside the DSH file sandbox.

Two sandbox behaviours break pip's package installation:

1. A directory created with a restrictive mode at creation time
   (``os.mkdir(path, 0o700)`` / ``os.makedirs(path, mode=0o700)``) cannot be
   written to afterwards, and ``tempfile.mkdtemp`` creates exactly such
   directories.
2. ``tempfile.mkdtemp`` directories are write-denied even when recreated with a
   permissive mode.

This wrapper therefore drops the ``mode`` argument of ``os.mkdir`` and replaces
``tempfile.mkdtemp`` with a plain, permissively created directory, then runs the
requested module as ``__main__``.

Usage::

    python scripts/run_in_sandbox.py pip --python .venv\\Scripts\\python.exe install <packages>
    python scripts/run_in_sandbox.py pip install -r requirements.txt
"""

from __future__ import annotations

import os
import runpy
import sys
import tempfile
import uuid

_REAL_MKDIR = os.mkdir
_REAL_MKDTEMP = tempfile.mkdtemp


def _sandbox_safe_mkdir(path, mode=0o777, *args, **kwargs):  # noqa: ARG001
    """Create a directory while ignoring restrictive permission bits."""
    return _REAL_MKDIR(path, *args, **kwargs)


def _sandbox_safe_mkdtemp(suffix=None, prefix=None, dir=None):  # noqa: A002
    base = dir or tempfile.gettempdir()
    os.makedirs(base, exist_ok=True)
    for _ in range(100):
        candidate = os.path.join(
            base,
            (prefix or "tmp") + uuid.uuid4().hex[:10] + (suffix or ""),
        )
        try:
            _REAL_MKDIR(candidate)
        except FileExistsError:
            continue
        return candidate
    return _REAL_MKDTEMP(suffix=suffix, prefix=prefix, dir=dir)


os.mkdir = _sandbox_safe_mkdir  # type: ignore[assignment]
os.makedirs.__globals__["mkdir"] = _sandbox_safe_mkdir
tempfile.mkdtemp = _sandbox_safe_mkdtemp
tempfile.TemporaryDirectory._mkdtemp = staticmethod(_sandbox_safe_mkdtemp)  # type: ignore[attr-defined]

if __name__ == "__main__":
    module = sys.argv[1] if len(sys.argv) > 1 else "pip"
    sys.argv = [module] + sys.argv[2:]
    runpy.run_module(module, run_name="__main__", alter_sys=True)
