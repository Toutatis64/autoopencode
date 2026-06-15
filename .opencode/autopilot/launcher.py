"""
Shared runtime bootstrap helpers for autocode scripts.

This module centralises the "find the repo root and put it on sys.path"
logic that was previously duplicated across ``run_autopilot.py``,
``meta_autopilot.py``, ``self_improving_loop.py``, ``kpi.py`` and
``autocode_config.py``. It has to work in two deployment modes:

* **Source mode** -- imported as ``scripts.launcher`` when the
  ``scripts/`` directory is the canonical source tree.
* **Deploy mirror mode** -- imported as ``.opencode.autopilot.launcher``
  from the deploy-mirror copy (kept in sync by ``scripts/sync.py``). In
  that case ``scripts/`` is not on ``sys.path`` and the relative
  ``from scripts.X import ...`` lookups below would fail without help.

The helpers expose:

* :func:`_find_repo_root` -- walk up from a starting path until a marker
  file is found (default: ``scripts/autocode_config.py``). Returns the
  directory that contains the ``scripts/`` tree.
* :func:`_find_repo_root_by_config` -- same, but stops at the first
  ``autocode.yaml`` or ``.git`` directory (matches the legacy
  ``_find_root`` used by ``kpi.py`` / ``autocode_config.py``).
* :func:`ensure_repo_root_on_path` -- idempotently prepend the repo root
  to ``sys.path`` so ``from scripts.X import ...`` works.
* :func:`ensure_scripts_dir_on_path` -- idempotently prepend *this
  module's own directory* to ``sys.path`` (used by sibling-style
  imports that were previously inlined as
  ``_D = Path(__file__).resolve().parent; sys.path.insert(0, str(_D))``).
* :func:`bootstrap` -- convenience: do both in a single call, in the
  safest order (scripts dir first, then repo root).

The functions tolerate partial / unusual installs (missing marker
files, read-only filesystems) by being no-ops on failure rather than
raising. Importers should keep their ``try/except ImportError`` fallbacks
for the rare case where the bootstrap itself is unreachable.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


_SCRIPTS_MARKER = Path("scripts") / "autocode_config.py"


def _find_repo_root(
    start: Path | None = None,
    marker: Path = _SCRIPTS_MARKER,
) -> Path | None:
    """Walk up from ``start`` (default: this file) until ``marker`` exists.

    Returns the directory that contains the ``scripts/`` tree, or
    ``None`` if the marker was not found within the filesystem root.
    """
    here = (start or Path(__file__)).resolve()
    for cand in (here, *here.parents):
        if (cand / marker).is_file():
            return cand
    return None


def _find_repo_root_by_config(start: Path | None = None) -> Path | None:
    """Walk up until an ``autocode.yaml`` or ``.git`` directory is found.

    Mirrors the legacy ``_find_root`` used by ``kpi.py`` and
    ``autocode_config.py``. Falls back to two levels up (the original
    fallback that pre-dated the marker-based search) when nothing
    matches, so existing behaviour is preserved.
    """
    here = (start or Path(__file__)).resolve()
    for parent in [here, *here.parents]:
        if (parent / "autocode.yaml").exists():
            return parent
        if (parent / ".git").exists():
            return parent
    if len(here.parents) >= 3:
        return here.parents[2]
    return None


def ensure_repo_root_on_path(start: Path | None = None) -> Path | None:
    """Prepend the repo root to ``sys.path`` if it can be located.

    Idempotent: a second call with the same root leaves the path list
    unchanged. Returns the root that was added (or ``None`` when no
    marker was found, in which case ``sys.path`` is left untouched).
    """
    root = _find_repo_root(start=start)
    if root is None:
        return None
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    return root


def ensure_scripts_dir_on_path() -> Path | None:
    """Prepend this module's directory to ``sys.path``.

    Used by the ``try: from scripts.X ... except ImportError: from X ...``
    pattern that several scripts rely on for the deploy-mirror case.
    Idempotent. Returns the directory that was added, or ``None`` when
    the module is loaded from a non-filesystem location.
    """
    try:
        scripts_dir = Path(__file__).resolve().parent
    except (OSError, ValueError):
        return None
    scripts_str = str(scripts_dir)
    if scripts_str not in sys.path:
        sys.path.insert(0, scripts_str)
    return scripts_dir


def bootstrap(start: Path | None = None) -> Path | None:
    """Run both bootstrap steps and return the located repo root.

    Order matters: ``ensure_scripts_dir_on_path`` runs first so the
    ``from scripts.X import ...`` lookups below can fall back to
    ``from X import ...`` if the canonical source tree is unreachable;
    ``ensure_repo_root_on_path`` runs second so the canonical case
    takes precedence once the repo root is on the path.
    """
    ensure_scripts_dir_on_path()
    return ensure_repo_root_on_path(start=start)


def resolve_root(
    env_var: str = "AUTOPILOT_ROOT",
    start: Path | None = None,
) -> Path:
    """Resolve the autocode repo root honouring ``$AUTOPILOT_ROOT``.

    Mirrors the pattern used at module load time in
    ``autocode_config.py`` and ``kpi.py``: the env var wins, otherwise
    the marker-based search is used, otherwise a two-level fallback.
    Returns a resolved :class:`Path`.
    """
    env_val = os.environ.get(env_var)
    if env_val:
        return Path(env_val).resolve()
    found = _find_repo_root(start=start) or _find_repo_root_by_config(start=start)
    if found is not None:
        return found.resolve()
    return (start or Path(__file__)).resolve().parents[2]
