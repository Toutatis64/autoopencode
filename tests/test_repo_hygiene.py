"""Repo-hygiene regression tests.

These tests guard the working tree against operational noise that previous
autopilot runs have been observed to leak into the source tree. The goal
is not just to assert the current state, but to fail loudly on the *first*
regression so the next iteration that accidentally re-introduces the
artifact can attribute and fix it.

History
-------
- coverage.py and several editors emit sibling files with a `,cover` suffix
  or `.cover` extension when serialising annotated source. They are pure
  pollution: bytewise duplicates of the source with extra prefixes. They
  have been observed landing in scripts/ and tests/ after a few autopilot
  iterations. The defensive `.gitignore` entries and the
  `make clean-artifacts` target are the operational side; this file is
  the testing side.
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = (REPO_ROOT / "scripts", REPO_ROOT / "tests")


def _collect_cover_artifacts() -> list[Path]:
    """Return every `,cover` or `.cover` file under SCAN_DIRS.

    Returns an empty list when the tree is clean (the expected state).
    """
    found: list[Path] = []
    for root in SCAN_DIRS:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            name = path.name
            if name.endswith(",cover") or name.endswith(".cover"):
                found.append(path)
    return sorted(found)


def test_no_cover_artifacts_in_scripts_or_tests() -> None:
    """`scripts/` and `tests/` must never contain `,cover` / `.cover` files.

    These are coverage/editor annotation artifacts, not source. They have
    polluted the tree in past iterations. Run `make clean-artifacts` to
    repair, then re-run tests.
    """
    artifacts = _collect_cover_artifacts()
    assert not artifacts, (
        "Found coverage/editor artifacts in source tree:\n"
        + "\n".join(str(p.relative_to(REPO_ROOT)) for p in artifacts)
        + "\nRun `make clean-artifacts` to remove them, then re-run tests."
    )


def test_no_tracked_cover_files_in_git() -> None:
    """No `,cover` / `.cover` file should be tracked by git in scripts/ or tests/.

    The index is checked (not the working tree) because we want to catch
    accidental `git add` of these files before they get committed. Uses
    `git ls-files` so it is fast and side-effect free.
    """
    import subprocess

    result = subprocess.run(
        ["git", "ls-files", "scripts/", "tests/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    tracked = [line for line in result.stdout.splitlines() if line.endswith(",cover") or line.endswith(".cover")]
    assert not tracked, (
        "Found tracked coverage artifacts:\n" + "\n".join(tracked) + "\nUntrack with: git rm --cached <file>"
    )


def test_gitignore_blocks_cover_suffix() -> None:
    """The `.gitignore` must list patterns that block `,cover` artifacts.

    This is the operational guarantee that `git add` and `git status` will
    not surface them. We do not pin the exact line count so the file can
    grow naturally; we only require the patterns to be present.
    """
    gitignore = (REPO_ROOT / ".gitignore").read_text()
    # The literal substrings must appear — `,cover` is the dangerous one
    # because it has no leading dot and is the actual suffix `coverage.py`
    # emits by default.
    assert ",cover" in gitignore, ".gitignore is missing the `,cover` pattern; coverage artifacts will leak."
    assert ".coverage" in gitignore, ".gitignore is missing the `.coverage` pattern; SQLite coverage DB will leak."
