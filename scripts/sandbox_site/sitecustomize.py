"""Sandbox compatibility shim, imported automatically through ``sitecustomize``.

The DSH file sandbox (workspace-write mode) denies writes into directories that
were created either

* with a restrictive mode at creation time, e.g. ``os.mkdir(path, 0o700)``, or
* by ``tempfile.mkdtemp`` (whose directories stay denied even when recreated).

pip and ``venv``/``ensurepip`` rely on both, so every Python process that may
install packages must be started with this shim on ``PYTHONPATH``::

    $env:PYTHONPATH = "<repo>\\scripts\\sandbox_site"
    python -m pip install ...

The shim only relaxes the *creation mode* of new directories inside the
workspace; it grants no access outside the sandbox policy.
"""

from __future__ import annotations

import os
import tempfile
import uuid

_REAL_MKDIR = os.mkdir
_REAL_MKDTEMP = tempfile.mkdtemp
_APPLIED = False


def _sandbox_safe_mkdir(path, mode=0o777, *args, **kwargs):  # noqa: ARG001
    return _REAL_MKDIR(path, *args, **kwargs)


def _sandbox_safe_mkdtemp(suffix=None, prefix=None, dir=None):  # noqa: A002
    base = dir or tempfile.gettempdir()
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        pass
    for _ in range(100):
        candidate = os.path.join(
            base,
            (prefix or "tmp") + uuid.uuid4().hex[:12] + (suffix or ""),
        )
        try:
            _REAL_MKDIR(candidate)
        except FileExistsError:
            continue
        return candidate
    return _REAL_MKDTEMP(suffix=suffix, prefix=prefix, dir=dir)


def apply() -> None:
    global _APPLIED
    if _APPLIED:
        return
    os.mkdir = _sandbox_safe_mkdir  # type: ignore[assignment]
    try:
        os.makedirs.__globals__["mkdir"] = _sandbox_safe_mkdir
    except Exception:  # pragma: no cover - defensive
        pass
    tempfile.mkdtemp = _sandbox_safe_mkdtemp
    tempfile.TemporaryDirectory._mkdtemp = staticmethod(_sandbox_safe_mkdtemp)  # type: ignore[attr-defined]
    _APPLIED = True


apply()
