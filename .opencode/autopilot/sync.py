#!/usr/bin/env python3
"""
Deploy-sync — keep the deploy mirror at .opencode/autopilot/ aligned with
the canonical source in scripts/.

Background
----------
The autopilot loop runs ``python3 .opencode/autopilot/run_autopilot.py``
(see ``run_autopilot.sh``). That deploy copy is a hand-maintained mirror of
``scripts/*.py``. Without an automated check, edits to ``scripts/`` can
silently diverge from the deploy copy, so a long-running process never sees
recent improvements (run_cycle decomposition, importlib.reload fix,
project-aware divergence lists, etc.). This module solves that by
diffing + (optionally) repairing the mirror, with a tiny testable surface.

Scope
-----
- Only ``.py`` files are synced. Other artefacts under
  ``.opencode/autopilot/`` (goal.md, components/, prompts/, runtime/) are
  project-owned config and must not be overwritten.
- Source of truth is always ``scripts/``. The deploy copy is regenerated
  to match it on every repair.

Exit codes
----------
- 0 — in sync (no repair needed, or repair succeeded)
- 1 — drift detected in --check mode
- 2 — error (file missing, permission denied, etc.)
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# Ensure repo root is on sys.path so `scripts.*` imports resolve
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.autocode_config import ROOT  # noqa: E402


DEPLOY_DIR_DEFAULT = ROOT / ".opencode" / "autopilot"
SOURCE_DIR_DEFAULT = ROOT / "scripts"
SYNC_EXTENSIONS = (".py",)


@dataclass(frozen=True)
class DriftEntry:
    """A single drift item detected between source and deploy."""

    name: str
    status: str  # "missing" | "modified" | "extra"
    source_path: Path
    deploy_path: Path


def _iter_source_files(source_dir: Path, extensions: tuple[str, ...]) -> Iterable[Path]:
    for path in sorted(source_dir.glob("*")):
        if path.is_file() and path.suffix in extensions:
            yield path


def _iter_deploy_files(deploy_dir: Path, extensions: tuple[str, ...]) -> Iterable[Path]:
    for path in sorted(deploy_dir.glob("*")):
        if path.is_file() and path.suffix in extensions:
            yield path


def detect_drift(
    source_dir: Path = SOURCE_DIR_DEFAULT,
    deploy_dir: Path = DEPLOY_DIR_DEFAULT,
    extensions: tuple[str, ...] = SYNC_EXTENSIONS,
) -> list[DriftEntry]:
    """Return the list of drift items between source and deploy.

    Drift kinds:
    - ``missing`` — file exists in source but not in deploy.
    - ``modified`` — file exists in both but contents differ.
    - ``extra`` — file exists in deploy but not in source (left untouched,
      so locally-added deploy-only scripts are preserved).
    """
    if not source_dir.is_dir():
        raise FileNotFoundError(f"Source directory not found: {source_dir}")
    if not deploy_dir.is_dir():
        raise FileNotFoundError(f"Deploy directory not found: {deploy_dir}")

    drift: list[DriftEntry] = []
    source_names = {p.name for p in _iter_source_files(source_dir, extensions)}
    deploy_names = {p.name for p in _iter_deploy_files(deploy_dir, extensions)}

    for src in _iter_source_files(source_dir, extensions):
        dep = deploy_dir / src.name
        if src.name not in deploy_names:
            drift.append(
                DriftEntry(
                    name=src.name,
                    status="missing",
                    source_path=src,
                    deploy_path=dep,
                )
            )
            continue
        if not filecmp.cmp(str(src), str(dep), shallow=False):
            drift.append(
                DriftEntry(
                    name=src.name,
                    status="modified",
                    source_path=src,
                    deploy_path=dep,
                )
            )

    for name in sorted(deploy_names - source_names):
        dep = deploy_dir / name
        drift.append(
            DriftEntry(
                name=name,
                status="extra",
                source_path=source_dir / name,
                deploy_path=dep,
            )
        )
    return drift


def repair_drift(
    drift: list[DriftEntry],
    dry_run: bool = False,
) -> list[str]:
    """Repair the deploy mirror to match source.

    Only ``missing`` and ``modified`` entries are touched; ``extra`` entries
    are left alone (deploy-only files are preserved). Returns a list of
    human-readable log lines describing what happened.
    """
    log: list[str] = []
    for entry in drift:
        if entry.status == "extra":
            log.append(f"SKIP extra: {entry.name} (deploy-only, preserved)")
            continue
        action = "would copy" if dry_run else "copied"
        log.append(f"{action} {entry.source_path} -> {entry.deploy_path}")
        if not dry_run:
            shutil.copy2(entry.source_path, entry.deploy_path)
    return log


def sync(
    source_dir: Path = SOURCE_DIR_DEFAULT,
    deploy_dir: Path = DEPLOY_DIR_DEFAULT,
    check_only: bool = False,
    dry_run: bool = False,
) -> int:
    """Run the sync. Returns a process-style exit code.

    - 0 — in sync (after any repairs) or check passed
    - 1 — drift detected in check mode
    - 2 — error (raised as exception, caught and converted by main)
    """
    drift = detect_drift(source_dir=source_dir, deploy_dir=deploy_dir)
    if not drift:
        return 0

    if check_only:
        for entry in drift:
            print(f"DRIFT {entry.status}: {entry.name}")
        return 1

    log = repair_drift(drift, dry_run=dry_run)
    for line in log:
        print(line)

    if dry_run:
        return 0

    # After a successful repair, the deploy mirror matches the source.
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Keep .opencode/autopilot/ in sync with scripts/.")
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=SOURCE_DIR_DEFAULT,
        help="Canonical source directory (default: scripts/)",
    )
    parser.add_argument(
        "--deploy-dir",
        type=Path,
        default=DEPLOY_DIR_DEFAULT,
        help="Deploy mirror directory (default: .opencode/autopilot/)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if drift is detected; do not modify anything.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print intended actions without writing to disk.",
    )
    args = parser.parse_args(argv)
    try:
        return sync(
            source_dir=args.source_dir,
            deploy_dir=args.deploy_dir,
            check_only=args.check,
            dry_run=args.dry_run,
        )
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
