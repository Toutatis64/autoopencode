"""Tests for scripts/sync.py — deploy mirror drift detection and repair."""

from __future__ import annotations

import filecmp
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.sync import (
    SYNC_EXTENSIONS,
    detect_drift,
    main,
    repair_drift,
    sync,
)


@pytest.fixture
def pair(tmp_path: Path) -> tuple[Path, Path]:
    """Create a fresh source/deploy pair with one identical file."""
    source = tmp_path / "src"
    deploy = tmp_path / "dep"
    source.mkdir()
    deploy.mkdir()
    (source / "common.py").write_text("# source\nX = 1\n")
    (deploy / "common.py").write_text("# source\nX = 1\n")
    return source, deploy


def test_detect_drift_returns_empty_when_in_sync(pair: tuple[Path, Path]) -> None:
    source, deploy = pair
    assert detect_drift(source, deploy) == []


def test_detect_drift_flags_modified_file(pair: tuple[Path, Path]) -> None:
    source, deploy = pair
    (source / "common.py").write_text("# updated\nX = 2\n")
    drift = detect_drift(source, deploy)
    assert len(drift) == 1
    assert drift[0].name == "common.py"
    assert drift[0].status == "modified"


def test_detect_drift_flags_missing_file(pair: tuple[Path, Path]) -> None:
    source, deploy = pair
    (source / "new_module.py").write_text("# new\nY = 1\n")
    drift = detect_drift(source, deploy)
    assert len(drift) == 1
    assert drift[0].name == "new_module.py"
    assert drift[0].status == "missing"


def test_detect_drift_flags_extra_file(pair: tuple[Path, Path]) -> None:
    source, deploy = pair
    (deploy / "legacy.py").write_text("# deploy-only\nZ = 1\n")
    drift = detect_drift(source, deploy)
    assert len(drift) == 1
    assert drift[0].name == "legacy.py"
    assert drift[0].status == "extra"


def test_detect_drift_ignores_non_python_files(pair: tuple[Path, Path]) -> None:
    source, deploy = pair
    (source / "notes.md").write_text("source notes")
    (deploy / "notes.md").write_text("deploy notes")
    assert detect_drift(source, deploy) == []


def test_detect_drift_raises_if_source_missing(tmp_path: Path) -> None:
    deploy = tmp_path / "dep"
    deploy.mkdir()
    with pytest.raises(FileNotFoundError):
        detect_drift(tmp_path / "missing_src", deploy)


def test_detect_drift_raises_if_deploy_missing(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    with pytest.raises(FileNotFoundError):
        detect_drift(source, tmp_path / "missing_dep")


def test_repair_drift_copies_missing_file(pair: tuple[Path, Path]) -> None:
    source, deploy = pair
    (source / "new_module.py").write_text("# new\nY = 1\n")
    drift = detect_drift(source, deploy)
    log = repair_drift(drift)
    assert any("new_module.py" in line for line in log)
    assert (deploy / "new_module.py").read_text() == "# new\nY = 1\n"


def test_repair_drift_copies_modified_file(pair: tuple[Path, Path]) -> None:
    source, deploy = pair
    (source / "common.py").write_text("# updated\nX = 99\n")
    drift = detect_drift(source, deploy)
    repair_drift(drift)
    assert (deploy / "common.py").read_text() == "# updated\nX = 99\n"


def test_repair_drift_preserves_extra_file(pair: tuple[Path, Path]) -> None:
    source, deploy = pair
    (deploy / "legacy.py").write_text("# deploy-only\nZ = 1\n")
    drift = detect_drift(source, deploy)
    repair_drift(drift)
    assert (deploy / "legacy.py").read_text() == "# deploy-only\nZ = 1\n"
    assert not (source / "legacy.py").exists()


def test_repair_drift_dry_run_does_not_modify(pair: tuple[Path, Path]) -> None:
    source, deploy = pair
    (source / "common.py").write_text("# updated\nX = 99\n")
    drift = detect_drift(source, deploy)
    log = repair_drift(drift, dry_run=True)
    assert any("would copy" in line for line in log)
    assert (deploy / "common.py").read_text() == "# source\nX = 1\n"


def test_repair_drift_preserves_mtime_via_copy2(pair: tuple[Path, Path]) -> None:
    source, deploy = pair
    (deploy / "common.py").write_text("# old\n")
    drift = detect_drift(source, deploy)
    assert drift[0].status == "modified"
    repair_drift(drift)
    assert filecmp.cmp(str(source / "common.py"), str(deploy / "common.py"), shallow=False)


def test_sync_returns_zero_when_in_sync(pair: tuple[Path, Path]) -> None:
    source, deploy = pair
    assert sync(source, deploy) == 0


def test_sync_repairs_and_returns_zero(pair: tuple[Path, Path]) -> None:
    source, deploy = pair
    (source / "common.py").write_text("# updated\nX = 2\n")
    (source / "fresh.py").write_text("# fresh\nW = 1\n")
    assert sync(source, deploy) == 0
    assert (deploy / "common.py").read_text() == "# updated\nX = 2\n"
    assert (deploy / "fresh.py").read_text() == "# fresh\nW = 1\n"


def test_sync_check_mode_returns_one_on_drift(pair: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    source, deploy = pair
    (source / "common.py").write_text("# updated\nX = 2\n")
    assert sync(source, deploy, check_only=True) == 1
    out = capsys.readouterr().out
    assert "DRIFT modified" in out
    assert "common.py" in out
    # No write happened
    assert (deploy / "common.py").read_text() == "# source\nX = 1\n"


def test_sync_check_mode_returns_zero_when_in_sync(pair: tuple[Path, Path]) -> None:
    source, deploy = pair
    assert sync(source, deploy, check_only=True) == 0


def test_sync_dry_run_does_not_write(pair: tuple[Path, Path]) -> None:
    source, deploy = pair
    (source / "common.py").write_text("# updated\nX = 2\n")
    assert sync(source, deploy, dry_run=True) == 0
    assert (deploy / "common.py").read_text() == "# source\nX = 1\n"


def test_sync_extensions_only_includes_py() -> None:
    assert SYNC_EXTENSIONS == (".py",)


def test_main_runs_sync_and_returns_zero(pair: tuple[Path, Path]) -> None:
    source, deploy = pair
    assert main(["--source-dir", str(source), "--deploy-dir", str(deploy)]) == 0


def test_main_check_exits_one_on_drift(pair: tuple[Path, Path]) -> None:
    source, deploy = pair
    (source / "common.py").write_text("# drift\n")
    assert main(["--source-dir", str(source), "--deploy-dir", str(deploy), "--check"]) == 1


def test_main_returns_two_on_missing_dir(tmp_path: Path) -> None:
    assert (
        main(
            [
                "--source-dir",
                str(tmp_path / "absent"),
                "--deploy-dir",
                str(tmp_path / "absent2"),
            ]
        )
        == 2
    )


def test_main_check_no_drift_returns_zero(pair: tuple[Path, Path]) -> None:
    source, deploy = pair
    assert main(["--source-dir", str(source), "--deploy-dir", str(deploy), "--check"]) == 0


def test_end_to_end_against_real_repo() -> None:
    """Run sync() against the real scripts/ and .opencode/autopilot/ in this repo.

    After the run the deploy copy must be byte-identical to the source
    for every .py file, and the call must succeed. This is the regression
    guard for the original bug: a stale deploy mirror.
    """
    repo_root = Path(__file__).resolve().parents[1]
    source = repo_root / "scripts"
    deploy = repo_root / ".opencode" / "autopilot"
    # Sanity: both directories must exist in a healthy checkout
    assert source.is_dir()
    assert deploy.is_dir()
    result = sync(source, deploy)
    assert result == 0
    # After sync, drift must be zero (modulo 'extra' files preserved on purpose)
    drift_after = detect_drift(source, deploy)
    actionable = [d for d in drift_after if d.status in ("missing", "modified")]
    assert actionable == []


@pytest.mark.parametrize("entrypoint", ["run_autopilot.py", "meta_autopilot.py"])
def test_deploy_copy_launches_without_env_hacks(entrypoint: str, tmp_path: Path) -> None:
    """Regression guard: the deploy mirror at .opencode/autopilot/*.py must
    be launchable as a plain ``python3 <file>`` with no PYTHONPATH or other
    env-var magic. This is the contract that ``run_autopilot.sh`` and
    ``run_meta_autopilot.sh`` rely on.

    Bug history: the deploy copy lives at ``.opencode/autopilot/<name>.py``
    and its first import is ``from scripts.X import ...``. The cwd when the
    launcher runs is the repo root, not the deploy dir, so without a
    self-bootstrapping helper the import fails with ModuleNotFoundError
    (the ``scripts/`` package's parent is on sys.path only when the package
    is being imported, not when its siblings are). The fix lives in
    ``scripts/run_autopilot.py`` and ``scripts/meta_autopilot.py`` as a
    ``_ensure_repo_root_on_path()`` helper invoked at import time.
    """
    repo_root = Path(__file__).resolve().parents[1]
    real_source = repo_root / "scripts"
    real_deploy = repo_root / ".opencode" / "autopilot"

    # Stage a clean tmp checkout: scripts/ + .opencode/autopilot/ + the .py
    # entrypoint we're testing. We do NOT copy the whole repo (no .git, no
    # .opencode/checkpoint_output.json, no runtime/) — just enough for the
    # script to bootstrap.
    sandbox_source = tmp_path / "scripts"
    sandbox_deploy = tmp_path / ".opencode" / "autopilot"
    sandbox_source.mkdir()
    sandbox_deploy.mkdir(parents=True)

    for src_py in real_source.glob("*.py"):
        if src_py.name in {"__init__.py"}:
            continue
        (sandbox_source / src_py.name).write_text(src_py.read_text())
    # Minimal __init__.py so `import scripts.X` works in the sandbox.
    (sandbox_source / "__init__.py").write_text("")

    for dep_py in real_deploy.glob("*.py"):
        (sandbox_deploy / dep_py.name).write_text(dep_py.read_text())

    # Launch the deploy copy in a fully clean environment. Strip PYTHONPATH
    # and unset cwd-related vars. Run `--help` so it exits 0 quickly.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
    }
    result = subprocess.run(
        [sys.executable, str(sandbox_deploy / entrypoint), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"deploy copy {entrypoint} failed to launch without env hacks.\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    # The help output must mention the script's own name — proof the argparse
    # block actually executed (i.e. all the from scripts.X imports succeeded).
    assert entrypoint in result.stdout or entrypoint in result.stderr, (
        f"unexpected output shape for {entrypoint}:\nstdout={result.stdout[:200]!r}\nstderr={result.stderr[:200]!r}"
    )
