#!/usr/bin/env python3

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import concurrent.futures
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import yaml

# Ensure this directory is on sys.path for sibling module imports
_D = Path(__file__).resolve().parent
if str(_D) not in sys.path:
    sys.path.insert(0, str(_D))


# When this file lives at .opencode/autopilot/run_autopilot.py (the deploy
# mirror), the canonical source tree at `scripts/` is not on sys.path, so
# `from scripts.X import ...` below would fail. Detect the repo root by
# walking up to the directory that contains `scripts/autocode_config.py`
# and prepend it. This makes the deploy copy self-bootstrappable — no
# PYTHONPATH or launcher-script env needed.
def _ensure_repo_root_on_path() -> None:
    marker = Path("scripts") / "autocode_config.py"
    here = Path(__file__).resolve().parent
    for cand in (here, *here.parents):
        if (cand / marker).is_file():
            if str(cand) not in sys.path:
                sys.path.insert(0, str(cand))
            return


_ensure_repo_root_on_path()

try:
    from scripts.autocode_config import ROOT, load_config, get_path, get_validation
except ImportError:
    from autocode_config import ROOT, load_config, get_path, get_validation  # type: ignore[no-redef]

from scripts.memory import (  # noqa: E402
    _log,
    clean_text,
    compact_summary,
    persist_memory_entries,
    prune_memory_entries,
    render_memory_topics,
    retrieve_relevant_memory,
    render_relevant_memory,
    utc_now,
)

from scripts.self_improving_loop import (  # noqa: E402
    MetaControllerParams,
    run_self_improving_loop,
)

from scripts.checkpoint import (  # noqa: E402
    PARALLEL_BRANCHES_MAX,
    STATUS_MAP,
    extract_checkpoint,
    normalize_checkpoint,
)

# Meta-autopilot: hierarchical component-level self-improvement
try:
    from scripts.meta_autopilot import MetaController

    _META_AVAILABLE = True
except ImportError:
    MetaController = None  # type: ignore[assignment,misc]
    _META_AVAILABLE = False

_CONFIG = load_config()

DEFAULT_GOAL_FILE = get_path("goal_file", _CONFIG)
RUNTIME_ROOT = get_path("runtime_dir", _CONFIG)
ACTIVE_RUN_FILE = RUNTIME_ROOT / "active_run.txt"
CHECKPOINT_OUTPUT_FILE = ROOT / ".opencode" / "checkpoint_output.json"
from scripts.controller import (  # noqa: E402
    CONTROLLER_EXHAUST_FAMILY_AFTER,
    CONTROLLER_HARD_PIVOT_AFTER,
    classify_transient_failure,
    cleanup_training_scratch,
    coerce_done_checkpoint,
    goal_stop_after_iterations,
    load_kb_yaml,
    novelty_angle_suggestions,
    recent_family_diversity,
    render_iteration_table,
    render_list,
    resolve_branch_count,
    retry_backoff_seconds,
)

from scripts.reporting import cmd_export_kb, cmd_report, render_report  # noqa: E402,F401


@dataclass
class RunContext:
    run_id: str
    goal_text: str
    status: str
    iteration_count: int
    session_goal: str = ""


@dataclass
class PendingIterationState:
    iteration_number: int
    attempt_number: int
    resume_session_id: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the long-horizon opencode autopilot loop.")
    subparsers = parser.add_subparsers(dest="subcommand")

    # ── report subcommand ──────────────────────────────────────────────────
    report_parser = subparsers.add_parser("report", help="Render a Markdown report for a run and exit.")
    report_group = report_parser.add_mutually_exclusive_group()
    report_group.add_argument("--run-id", help="Render report for this specific run id.")
    report_group.add_argument("--latest", action="store_true", help="Render report for the active run.")
    report_parser.add_argument("--output", type=Path, help="Write report to this file (default: stdout).")
    report_parser.add_argument(
        "--runtime-dir", type=Path, default=RUNTIME_ROOT, help="Override runtime storage directory."
    )

    export_kb_parser = subparsers.add_parser("export-kb", help="Export autopilot_kb.yaml to a dated Markdown snapshot.")
    export_kb_parser.add_argument("--output", type=Path, help="Write to this file instead of auto-named snapshot.")
    export_kb_parser.add_argument(
        "--no-date-suffix", action="store_true", help="Skip the date suffix in the auto-generated filename."
    )

    # ── run subcommand (default behaviour) ────────────────────────────────
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--goal", help="Goal text for a new run.")
    group.add_argument("--goal-file", type=Path, help="Markdown file describing the goal for a new run.")
    group.add_argument("--resume-run", help="Resume a specific run id.")
    group.add_argument("--resume-latest", action="store_true", help="Resume the active run recorded on disk.")
    parser.add_argument(
        "--session-goal", help="Session-specific goal text. Overrides the permanent goal for this iteration."
    )
    parser.add_argument(
        "--session-goal-file", type=Path, help="Markdown file with a session-specific goal. Re-read each iteration."
    )
    parser.add_argument("--agent", default="goal-autopilot", help="Opencode agent name to run.")
    parser.add_argument("--model", help="Optional provider/model override.")
    parser.add_argument("--variant", help="Optional model variant override.")
    parser.add_argument(
        "--max-iterations", type=int, default=1, help="How many iterations to execute in this invocation."
    )
    parser.add_argument("--sleep-seconds", type=int, default=60, help="Delay between iterations when looping.")
    parser.add_argument("--timeout-seconds", type=int, default=3600, help="Timeout for each opencode iteration.")
    parser.add_argument(
        "--max-retries-per-iteration",
        type=int,
        default=2,
        help="Retry count for transient iteration failures before marking the iteration failed.",
    )
    parser.add_argument(
        "--retry-backoff-seconds",
        type=int,
        default=15,
        help="Base backoff between transient failure retries.",
    )
    parser.add_argument(
        "--max-retry-backoff-seconds",
        type=int,
        default=300,
        help="Upper bound for transient failure retry backoff.",
    )
    parser.add_argument("--runtime-dir", type=Path, default=RUNTIME_ROOT, help="Override runtime storage directory.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Build memory and print the next prompt without calling opencode."
    )
    parser.add_argument(
        "--no-skip-permissions",
        action="store_true",
        help="Do not pass --dangerously-skip-permissions to opencode run.",
    )
    parser.add_argument(
        "--cpu-budget-percent",
        type=int,
        default=90,
        help="Cap opencode child workloads to this share of currently available CPU cores.",
    )
    parser.add_argument(
        "--parallel-branches",
        type=int,
        default=PARALLEL_BRANCHES_MAX,
        metavar="N",
        help=(
            "Maximum number of parallel branch iterations the agent is allowed to request. "
            f"Default {PARALLEL_BRANCHES_MAX} (agent decides up to this cap). "
            "Set to 1 to force serial execution regardless of agent preference."
        ),
    )
    parser.add_argument(
        "--force-parallel",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Force exactly N parallel branches every iteration, overriding the agent's own "
            "parallel_branches request. 0 (default) means let the agent decide (subject to --parallel-branches cap)."
        ),
    )
    args = parser.parse_args()
    if args.subcommand == "report":
        return args
    if args.max_iterations < 1:
        parser.error("--max-iterations must be at least 1")
    if args.sleep_seconds < 0:
        parser.error("--sleep-seconds must be non-negative")
    if args.timeout_seconds < 1:
        parser.error("--timeout-seconds must be at least 1")
    if args.max_retries_per_iteration < 0:
        parser.error("--max-retries-per-iteration must be non-negative")
    if args.retry_backoff_seconds < 0:
        parser.error("--retry-backoff-seconds must be non-negative")
    if args.max_retry_backoff_seconds < 0:
        parser.error("--max-retry-backoff-seconds must be non-negative")
    if not 1 <= args.cpu_budget_percent <= 100:
        parser.error("--cpu-budget-percent must be between 1 and 100")
    if args.parallel_branches < 1:
        parser.error("--parallel-branches must be at least 1")
    if args.force_parallel < 0:
        parser.error("--force-parallel must be non-negative")
    if args.force_parallel > PARALLEL_BRANCHES_MAX:
        parser.error(f"--force-parallel cannot exceed {PARALLEL_BRANCHES_MAX}")
    return args


def ensure_runtime(runtime_dir: Path) -> sqlite3.Connection:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    db_path = runtime_dir / "memory.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            goal_text TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            iteration_count INTEGER NOT NULL DEFAULT 0,
            latest_summary TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS iterations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            iteration_number INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            exit_code INTEGER,
            status TEXT NOT NULL,
            summary TEXT NOT NULL,
            checkpoint_json TEXT NOT NULL,
            stdout_path TEXT NOT NULL,
            stderr_path TEXT NOT NULL,
            UNIQUE(run_id, iteration_number)
        );

        CREATE TABLE IF NOT EXISTS facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            fact TEXT NOT NULL,
            first_iteration INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, fact)
        );

        CREATE TABLE IF NOT EXISTS artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            iteration_number INTEGER NOT NULL,
            path TEXT NOT NULL,
            description TEXT NOT NULL,
            UNIQUE(run_id, iteration_number, path, description)
        );

        CREATE TABLE IF NOT EXISTS iteration_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            iteration_number INTEGER NOT NULL,
            attempt_number INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            exit_code INTEGER,
            status TEXT NOT NULL,
            transient_failure INTEGER NOT NULL DEFAULT 0,
            retry_reason TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL,
            checkpoint_json TEXT NOT NULL,
            stdout_path TEXT NOT NULL,
            stderr_path TEXT NOT NULL,
            UNIQUE(run_id, iteration_number, attempt_number)
        );

        CREATE TABLE IF NOT EXISTS memory_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            entry_type TEXT NOT NULL,
            content TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            importance REAL NOT NULL DEFAULT 1.0,
            source_iteration INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            UNIQUE(run_id, entry_type, content)
        );

        -- Indexes for memory query performance (ORDER BY source_iteration DESC)
        CREATE INDEX IF NOT EXISTS idx_memory_run_source
            ON memory_entries(run_id, source_iteration DESC);
        CREATE INDEX IF NOT EXISTS idx_memory_run_importance
            ON memory_entries(run_id, importance DESC);
        CREATE INDEX IF NOT EXISTS idx_memory_run_type_source
            ON memory_entries(run_id, entry_type, source_iteration DESC);

        -- Full-text search on memory content for better relevance matching
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
            content,
            entry_type UNINDEXED,
            content_rowid=rowid
        );
        """
    )
    conn.commit()
    return conn


def read_goal_text(args: argparse.Namespace) -> str | None:
    if args.goal:
        return args.goal.strip()
    if args.goal_file:
        return args.goal_file.read_text(encoding="utf-8").strip()
    return None


def active_run_id(runtime_dir: Path) -> str | None:
    active_path = runtime_dir / ACTIVE_RUN_FILE.name
    if not active_path.exists():
        return None
    value = active_path.read_text(encoding="utf-8").strip()
    return value or None


def set_active_run(runtime_dir: Path, run_id: str) -> None:
    active_path = runtime_dir / ACTIVE_RUN_FILE.name
    active_path.write_text(f"{run_id}\n", encoding="utf-8")


def sync_goal_from_canonical_file(conn: sqlite3.Connection, ctx: RunContext) -> RunContext:
    if not DEFAULT_GOAL_FILE.exists():
        return ctx

    goal_text = DEFAULT_GOAL_FILE.read_text(encoding="utf-8").strip()
    if not goal_text or goal_text == clean_text(ctx.goal_text):
        return ctx

    now = utc_now()
    conn.execute(
        "UPDATE runs SET goal_text = ?, updated_at = ? WHERE run_id = ?",
        (goal_text, now, ctx.run_id),
    )
    conn.commit()
    return RunContext(
        run_id=ctx.run_id,
        goal_text=goal_text,
        status=ctx.status,
        iteration_count=ctx.iteration_count,
    )


def autopilot_session_title(run_id: str, iteration_number: int, attempt_number: int) -> str:
    return f"autopilot {run_id} #{iteration_number}.{attempt_number}"


def list_opencode_sessions(timeout_seconds: int = 30) -> list[tuple[str, str]]:
    try:
        completed = subprocess.run(
            ["opencode", "session", "list"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    if completed.returncode != 0:
        return []

    sessions: list[tuple[str, str]] = []
    pattern = re.compile(r"^(?P<session_id>ses_\S+)\s+(?P<title>.+?)\s{2,}\S.*$")
    for raw_line in completed.stdout.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("Session ID") or stripped.startswith("─"):
            continue
        if not stripped.startswith("ses_"):
            continue
        match = pattern.match(line)
        if not match:
            continue
        sessions.append((match.group("session_id"), match.group("title").strip()))
    return sessions


def find_session_id_by_title(title: str) -> str | None:
    for session_id, session_title in list_opencode_sessions():
        if session_title == title:
            return session_id
    return None


def available_cpu_ids() -> list[int]:
    if hasattr(os, "sched_getaffinity"):
        try:
            return sorted(os.sched_getaffinity(0))
        except OSError:
            pass
    cpu_count = os.cpu_count() or 1
    return list(range(max(1, cpu_count)))


def cpu_budget_ids(cpu_budget_percent: int) -> list[int]:
    cpu_ids = available_cpu_ids()
    if cpu_budget_percent >= 100 or len(cpu_ids) <= 1:
        return cpu_ids
    limited_count = max(1, int(len(cpu_ids) * (cpu_budget_percent / 100.0)))
    if limited_count >= len(cpu_ids):
        return cpu_ids
    return cpu_ids[:limited_count]


def opencode_child_env(cpu_budget_percent: int) -> tuple[dict[str, str], list[int]]:
    env = os.environ.copy()
    cpu_ids = cpu_budget_ids(cpu_budget_percent)
    thread_cap = max(1, len(cpu_ids))
    for var_name in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
        "BLIS_NUM_THREADS",
        "RAYON_NUM_THREADS",
        "POLARS_MAX_THREADS",
    ):
        env[var_name] = str(thread_cap)
    env["AUTOPILOT_CPU_BUDGET_PERCENT"] = str(cpu_budget_percent)
    env["AUTOPILOT_CPU_BUDGET_THREADS"] = str(thread_cap)
    return env, cpu_ids


def opencode_preexec_fn(cpu_ids: list[int]) -> Any:
    if not cpu_ids or not hasattr(os, "sched_setaffinity"):
        return None

    def _apply_affinity() -> None:
        try:
            os.sched_setaffinity(0, set(cpu_ids))
        except OSError:
            pass

    return _apply_affinity


def pending_iteration_state(conn: sqlite3.Connection, ctx: RunContext) -> PendingIterationState:
    iteration_number = ctx.iteration_count + 1
    row = conn.execute(
        "SELECT MAX(attempt_number) AS max_attempt FROM iteration_attempts WHERE run_id = ? AND iteration_number = ?",
        (ctx.run_id, iteration_number),
    ).fetchone()
    attempt_number = int(row["max_attempt"] or 0) + 1
    return PendingIterationState(iteration_number=iteration_number, attempt_number=attempt_number)


def recover_resume_latest_state(conn: sqlite3.Connection, ctx: RunContext) -> PendingIterationState:
    state = pending_iteration_state(conn, ctx)
    if ctx.status != "running":
        return state
    session_title = autopilot_session_title(ctx.run_id, state.iteration_number, state.attempt_number)
    session_id = find_session_id_by_title(session_title)
    if session_id:
        state.resume_session_id = session_id
    return state


def create_run(conn: sqlite3.Connection, runtime_dir: Path, goal_text: str, session_goal: str = "") -> RunContext:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    now = utc_now()
    conn.execute(
        "INSERT INTO runs (run_id, goal_text, status, created_at, updated_at, iteration_count, latest_summary) VALUES (?, ?, ?, ?, ?, 0, '')",
        (run_id, goal_text, "running", now, now),
    )
    conn.commit()
    set_active_run(runtime_dir, run_id)

    # ── Cross-run memory import: bring in high-importance entries from last run ──
    last_run = conn.execute(
        "SELECT run_id FROM runs WHERE run_id != ? ORDER BY created_at DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if last_run:
        last_run_id = last_run["run_id"]
        imports = conn.execute(
            "SELECT entry_type, content, tags_json, importance, source_iteration, created_at FROM memory_entries "
            "WHERE run_id = ? AND importance >= 4.0 AND entry_type IN ('fact', 'decision') "
            "ORDER BY source_iteration DESC LIMIT 30",
            (last_run_id,),
        ).fetchall()
        for imp in imports:
            conn.execute(
                """
                INSERT OR IGNORE INTO memory_entries
                    (run_id, entry_type, content, tags_json, importance, source_iteration, created_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    imp["entry_type"],
                    imp["content"],
                    imp["tags_json"],
                    imp["importance"],
                    0,
                    imp["created_at"],
                    now,
                ),
            )
            # Also populate FTS5 for imported entries
            row_id = conn.execute(
                "SELECT id FROM memory_entries WHERE run_id = ? AND entry_type = ? AND content = ?",
                (run_id, imp["entry_type"], imp["content"]),
            ).fetchone()
            if row_id:
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO memory_fts (rowid, content, entry_type) VALUES (?, ?, ?)",
                        (row_id["id"], imp["content"], imp["entry_type"]),
                    )
                except sqlite3.OperationalError:
                    pass
        conn.commit()
        if imports:
            _log(f"cross-run import: {len(imports)} high-importance entries from {last_run_id}")

    return RunContext(
        run_id=run_id, goal_text=goal_text, status="running", iteration_count=0, session_goal=session_goal
    )


def load_run(conn: sqlite3.Connection, run_id: str, session_goal: str = "") -> RunContext:
    row = conn.execute(
        "SELECT run_id, goal_text, status, iteration_count FROM runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"Run id not found: {run_id}")
    return RunContext(
        run_id=row["run_id"],
        goal_text=row["goal_text"],
        status=row["status"],
        iteration_count=row["iteration_count"],
        session_goal=session_goal,
    )


def resolve_run(conn: sqlite3.Connection, args: argparse.Namespace) -> RunContext:
    goal_text = read_goal_text(args)
    runtime_dir = args.runtime_dir
    resumed_existing_run = False

    # Read session goal if provided
    session_goal = ""
    if hasattr(args, "session_goal") and args.session_goal:
        session_goal = args.session_goal.strip()
    elif hasattr(args, "session_goal_file") and args.session_goal_file:
        try:
            session_goal = args.session_goal_file.read_text(encoding="utf-8").strip()
        except OSError:
            _log(f"Warning: could not read session goal file {args.session_goal_file}")
    if args.resume_run:
        ctx = load_run(conn, args.resume_run, session_goal=session_goal)
        resumed_existing_run = True
    elif args.resume_latest:
        run_id = active_run_id(runtime_dir)
        if not run_id:
            raise SystemExit("No active run recorded. Start a new run with --goal or --goal-file.")
        ctx = load_run(conn, run_id, session_goal=session_goal)
        resumed_existing_run = True
    elif goal_text:
        ctx = create_run(conn, runtime_dir, goal_text, session_goal=session_goal)
    else:
        run_id = active_run_id(runtime_dir)
        if not run_id:
            raise SystemExit("Provide --goal, --goal-file, --resume-run, or --resume-latest.")
        ctx = load_run(conn, run_id, session_goal=session_goal)
        resumed_existing_run = True
    if resumed_existing_run:
        ctx = sync_goal_from_canonical_file(conn, ctx)
    set_active_run(runtime_dir, ctx.run_id)
    return ctx


def run_dir(runtime_dir: Path, run_id: str) -> Path:
    return runtime_dir / "runs" / run_id


def ensure_text(content: str | bytes | None) -> str:
    if content is None:
        return ""
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return content


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = ensure_text(content)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def build_controller_state(conn: sqlite3.Connection, ctx: RunContext) -> str:
    stop_after = goal_stop_after_iterations(ctx.goal_text)
    rows = conn.execute(
        "SELECT iteration_number, checkpoint_json FROM iterations WHERE run_id = ? ORDER BY iteration_number DESC LIMIT 24",
        (ctx.run_id,),
    ).fetchall()
    entries: list[dict[str, Any]] = []
    for row in rows:
        checkpoint = normalize_checkpoint(
            json.loads(row["checkpoint_json"]), fallback_status="continue", fallback_summary=""
        )
        entries.append(
            {
                "iteration_number": row["iteration_number"],
                "summary": checkpoint["summary"],
                "branch_family": checkpoint["branch_family"] or "unknown",
                "family_novelty": checkpoint["family_novelty"] or "minor",
                "competitiveness": checkpoint["competitiveness"] or "unknown",
                "evidence_quality": checkpoint["evidence_quality"] or "artifact_only",
                "promotion_recommendation": checkpoint["promotion_recommendation"] or "hold",
                "parallel_branches": checkpoint.get("parallel_branches", 1),
            }
        )

    consecutive_noncompetitive = 0
    for entry in entries:
        if entry["competitiveness"] == "non_competitive":
            consecutive_noncompetitive += 1
            continue
        break

    family_entries: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        family_entries[entry["branch_family"]].append(entry)

    shortlist: list[dict[str, Any]] = []
    exhausted: list[tuple[str, str]] = []
    for family, family_rows in family_entries.items():
        ordered = sorted(family_rows, key=lambda item: item["iteration_number"], reverse=True)
        latest = ordered[0]
        noncompetitive_count = sum(1 for item in ordered if item["competitiveness"] == "non_competitive")
        if latest["competitiveness"] in {"promising", "marginal"}:
            shortlist.append(latest)
        if noncompetitive_count >= CONTROLLER_EXHAUST_FAMILY_AFTER and latest["promotion_recommendation"] != "promote":
            exhausted.append((family, compact_summary(latest["summary"], 120)))

    shortlist.sort(key=lambda item: (item["competitiveness"], item["iteration_number"]), reverse=True)
    shortlist = shortlist[:4]
    exhausted.sort(key=lambda item: item[0])
    diversity = recent_family_diversity(entries)
    novelty_pressure = diversity["low_diversity"] or consecutive_noncompetitive >= CONTROLLER_HARD_PIVOT_AFTER

    if shortlist and novelty_pressure:
        recommendation = (
            f"Recent work is too concentrated in `{diversity['dominant_family']}`. The shortlist is advisory only. Start the next iteration with "
            "a 3-way divergent slate: one incremental fix, one adjacent improvement, and one novel approach in a different module. "
            "Choose deliberately; do not default to the easiest option."
        )
    elif shortlist:
        recommendation = (
            "Use the shortlist as one candidate set, not as a cage. Compare the best path against at least one structurally different "
            "hypothesis and only pursue it if it still wins on quality and validation cost."
        )
    elif consecutive_noncompetitive >= stop_after:
        recommendation = "The current improvement program looks exhausted. You MUST invent completely new approaches or pivot to a different module area."
    elif consecutive_noncompetitive >= CONTROLLER_HARD_PIVOT_AFTER:
        recommendation = (
            "Force a hard pivot. Repeating the same family is no longer allowed without a concrete structural reason."
        )
    else:
        recommendation = "Prefer a completely new area or approach over another small local variant."

    lines = ["# Controller State", "", "## Scorecard", ""]
    lines.append(f"- Completed iterations: `{ctx.iteration_count}`")
    lines.append(f"- Consecutive non-competitive iterations: `{consecutive_noncompetitive}`")
    lines.append(f"- Stop-after budget from goal: `{stop_after}`")
    lines.append(f"- Shortlisted families: `{len(shortlist)}`")
    lines.append(f"- Exhausted families: `{len(exhausted)}`")
    lines.append(f"- Recent diversity window: `{diversity['window']}`")
    lines.append(f"- Unique families in window: `{diversity['unique_count']}`")
    lines.append(
        f"- Dominant family concentration: `{diversity['dominant_family']}` at `{diversity['dominant_count']}/{max(1, diversity['window'])}` (`{diversity['dominant_share']:.0%}`)"
    )
    lines.append(f"- Same-family streak: `{diversity['same_family_streak']}`")
    lines.append(f"- Novelty pressure: `{'active' if novelty_pressure else 'inactive'}`")
    if entries:
        last_branches = entries[0].get("parallel_branches", 1)
        lines.append(f"- Last checkpoint requested parallel branches: `{last_branches}`")
    lines.append("")
    lines.append("## Recommendation")
    lines.append("")
    lines.append(f"- {recommendation}")
    lines.append("")

    lines.append("## Shortlist")
    lines.append("")
    if shortlist:
        for entry in shortlist:
            lines.append(
                f"- Iteration {entry['iteration_number']} [{entry['branch_family']}] [{entry['competitiveness']}/{entry['evidence_quality']}]: {compact_summary(entry['summary'])}"
            )
    else:
        lines.append("- No active shortlist.")
    lines.append("")

    lines.append("## Fresh Angles")
    lines.append("")
    if novelty_pressure or not shortlist:
        for angle in novelty_angle_suggestions()[:4]:
            lines.append(f"- {angle}")
    else:
        lines.append(
            "- Fresh-angle mode is optional right now, but the next step should still beat at least one novel alternative."
        )
    lines.append("")

    # KB-derived exhausted families from YAML
    kb_data = load_kb_yaml()
    kb_exhausted = kb_data.get("exhausted_families", []) if kb_data else []

    lines.append("## Exhausted Families")
    lines.append("")
    if exhausted or kb_exhausted:
        seen_families = set()
        for family, summary in exhausted[:6]:
            clean_fam = family.rstrip(":")
            lines.append(f"- `{clean_fam}` (from checkpoints): {summary}")
            seen_families.add(clean_fam)
        for ef in kb_exhausted:
            ef_name = ef.get("family", "") if isinstance(ef, dict) else ef
            if ef_name and ef_name not in seen_families:
                lines.append(f"- `{ef_name}` (from YAML KB)")
                seen_families.add(ef_name)
    else:
        lines.append("- None yet.")
    lines.append("")

    # Structured data section (machine-parseable)
    lines.append("## Structured Data")
    lines.append("")
    lines.append("```yaml")
    structured = {
        "iteration_count": ctx.iteration_count,
        "consecutive_noncompetitive": consecutive_noncompetitive,
        "novelty_pressure": novelty_pressure,
        "stagnation_score": round(consecutive_noncompetitive / max(stop_after, 1), 2),
        "shortlist_count": len(shortlist),
        "exhausted_families_count": len(exhausted) + len(kb_exhausted),
        "dominant_family": diversity["dominant_family"],
        "dominant_share_pct": round(diversity["dominant_share"] * 100),
        "unique_families_in_window": diversity["unique_count"],
    }
    if kb_data:
        hp = kb_data.get("hypothesis_queue", [])
        if hp:
            open_hypotheses = [h["title"] for h in hp if h.get("status") == "open"]
            structured["open_hypotheses"] = open_hypotheses[:5]
    lines.append(yaml.dump(structured, default_flow_style=False, sort_keys=False).rstrip())
    lines.append("```")
    lines.append("")

    lines.append("## Guardrails")
    lines.append("")
    lines.append(
        "- Do not revisit an exhausted family unless the next idea is explicitly major and structurally different."
    )
    lines.append(
        "- If you choose `continue`, compare the shortlist against at least one adjacent-new and one high-novelty alternative before deciding."
    )
    lines.append(
        "- If you appear stuck, invent a completely new approach instead of stopping. This is an unbounded perpetual loop."
    )
    lines.append(
        "- Before spending another try, name the exact code weakness being targeted, why the chosen approach should fix it, and what test result would falsify that belief."
    )
    lines.append("- Prefer one deeper, well-validated step over several shallow nearby variants.")
    lines.append(
        "- Respect repo conventions: all UI text via `t('...')`, no `any`, dedup before external side effects."
    )
    lines.append("")
    return "\n".join(lines)


def controller_should_stop(conn: sqlite3.Connection, ctx: RunContext, checkpoint: dict[str, Any]) -> tuple[bool, str]:
    stop_after = goal_stop_after_iterations(ctx.goal_text)
    if checkpoint.get("status") != "continue":
        return False, ""
    if checkpoint.get("promotion_recommendation") == "abandon_run":
        return True, "checkpoint requested run abandonment"

    rows = conn.execute(
        "SELECT checkpoint_json FROM iterations WHERE run_id = ? ORDER BY iteration_number DESC LIMIT ?",
        (ctx.run_id, stop_after - 1),
    ).fetchall()
    streak = 1 if checkpoint.get("competitiveness") == "non_competitive" else 0
    for row in rows:
        prior = normalize_checkpoint(
            json.loads(row["checkpoint_json"]), fallback_status="continue", fallback_summary=""
        )
        if prior.get("competitiveness") == "non_competitive":
            streak += 1
            continue
        break
    if streak >= stop_after:
        return True, f"{streak} consecutive non-competitive iterations"
    return False, ""


def build_memory_files(conn: sqlite3.Connection, runtime_dir: Path, ctx: RunContext) -> tuple[list[Path], Path]:
    target_dir = run_dir(runtime_dir, ctx.run_id) / "current"
    target_dir.mkdir(parents=True, exist_ok=True)

    recent_rows = conn.execute(
        "SELECT iteration_number, status, summary, checkpoint_json FROM iterations WHERE run_id = ? ORDER BY iteration_number DESC LIMIT 8",
        (ctx.run_id,),
    ).fetchall()
    # Fetch more rows for the structured table
    table_rows = conn.execute(
        "SELECT iteration_number, status, summary, checkpoint_json FROM iterations WHERE run_id = ? ORDER BY iteration_number DESC LIMIT 24",
        (ctx.run_id,),
    ).fetchall()
    fact_rows = conn.execute(
        "SELECT fact FROM facts WHERE run_id = ? ORDER BY id ASC LIMIT 200",
        (ctx.run_id,),
    ).fetchall()
    latest_row = conn.execute(
        "SELECT checkpoint_json FROM iterations WHERE run_id = ? ORDER BY iteration_number DESC LIMIT 1",
        (ctx.run_id,),
    ).fetchone()

    facts = [row["fact"] for row in fact_rows]
    recent_lines: list[str] = []
    for row in recent_rows:
        checkpoint = json.loads(row["checkpoint_json"])
        next_steps = checkpoint.get("next_steps", [])
        risks = checkpoint.get("risks", [])
        recent_lines.append(f"- Iteration {row['iteration_number']} [{row['status']}]: {row['summary']}")
        for step in next_steps[:3]:
            recent_lines.append(f"  next: {step}")
        for risk in risks[:2]:
            recent_lines.append(f"  risk: {risk}")

    latest_checkpoint = json.loads(latest_row["checkpoint_json"]) if latest_row else None
    relevant_entries, query_tags, topic_counts = retrieve_relevant_memory(
        conn, ctx.run_id, ctx.goal_text, ctx.iteration_count, latest_checkpoint
    )
    open_threads = []
    if latest_checkpoint:
        open_threads.extend(latest_checkpoint.get("next_steps", []))
        open_threads.extend(latest_checkpoint.get("risks", []))

    goal_path = target_dir / "goal.md"
    session_goal_path = target_dir / "session_goal.md"
    knowledge_path = target_dir / "knowledge.md"
    memory_topics_path = target_dir / "memory_topics.md"
    progress_path = target_dir / "recent_progress.md"
    run_state_path = target_dir / "run_state.md"
    controller_state_path = target_dir / "controller_state.md"
    iteration_table_path = target_dir / "iteration_table.md"
    self_improving_path = target_dir / "self_improving_diagnostics.md"

    write_text(goal_path, f"# Goal (permanent)\n\n{ctx.goal_text}\n")
    if ctx.session_goal:
        write_text(session_goal_path, f"# Session Goal\n\n{ctx.session_goal}\n")
    if not relevant_entries and facts:
        write_text(knowledge_path, render_list("Durable Knowledge", facts))
    else:
        write_text(knowledge_path, render_relevant_memory(relevant_entries, query_tags))
    write_text(memory_topics_path, render_memory_topics(topic_counts))
    write_text(progress_path, render_list("Recent Progress", recent_lines))
    write_text(controller_state_path, build_controller_state(conn, ctx))
    write_text(iteration_table_path, render_iteration_table(table_rows))
    state_lines = [
        "# Run State\n",
        f"- Run id: `{ctx.run_id}`",
        f"- Status: `{ctx.status}`",
        f"- Completed iterations: `{ctx.iteration_count}`",
    ]
    if open_threads:
        state_lines.append("")
        state_lines.append("## Open Threads")
        state_lines.append("")
        state_lines.extend(f"- {item}" for item in open_threads)
    write_text(run_state_path, "\n".join(state_lines))

    # ── Self-improving loop diagnostics ──────────────────────────────────
    if ctx.iteration_count >= 2:
        try:
            diag_rows: list[dict[str, Any]] = []
            families: list[str] = []
            exhausted: list[str] = []

            # Parse checkpoint JSONs from table_rows
            for row in table_rows:
                cp = json.loads(row["checkpoint_json"])
                cp["iteration_number"] = row["iteration_number"]
                diag_rows.append(cp)
                families.append(cp.get("branch_family", "unknown"))

            # Count consecutive non-competitive
            cc_nc = 0
            for row in diag_rows:
                if row.get("competitiveness") == "non_competitive":
                    cc_nc += 1
                else:
                    break

            # Detect exhausted families (3+ non-competitive)
            family_nc: Counter[str] = Counter()
            for row in diag_rows:
                if row.get("competitiveness") == "non_competitive":
                    family_nc[row.get("branch_family", "unknown")] += 1
            for fam, cnt in family_nc.items():
                if cnt >= 3:
                    exhausted.append(fam)

            unique_families = list(dict.fromkeys(families))

            diag_md, _meta_advice = run_self_improving_loop(
                conn=conn,
                run_id=ctx.run_id,
                checkpoint_rows=diag_rows,
                total_iterations=ctx.iteration_count,
                consecutive_noncompetitive=cc_nc,
                current_params=MetaControllerParams(
                    sleep_seconds=60,
                    max_retries_per_iteration=2,
                    cpu_budget_percent=90,
                    parallel_branches_max=3,
                    force_parallel=0,
                ),
                recent_families=unique_families[:8],
                exhausted_families=exhausted,
            )
            write_text(self_improving_path, diag_md)
        except Exception as exc:
            write_text(self_improving_path, f"# Self-Improving Loop Diagnostics\n\nDiagnostics unavailable: {exc}\n")

    attachments = [
        goal_path,
        knowledge_path,
        memory_topics_path,
        progress_path,
        run_state_path,
        controller_state_path,
        iteration_table_path,
    ]
    if ctx.session_goal and session_goal_path.exists():
        attachments.insert(1, session_goal_path)
    if self_improving_path.exists():
        attachments.append(self_improving_path)

    return attachments, target_dir


def _parse_git_status_porcelain(stdout: str) -> list[str]:
    paths: list[str] = []
    for line in stdout.split("\n"):
        raw = line.rstrip("\n\r")
        if not raw or len(raw) < 4:
            continue
        path = raw[3:].strip()
        if path and path not in paths:
            paths.append(path)
    return paths


def synthesize_checkpoint_from_git(exit_code: int, raw_checkpoint: dict[str, Any] | None) -> dict[str, Any] | None:
    if raw_checkpoint is not None:
        return raw_checkpoint
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            return None
        changed = _parse_git_status_porcelain(result.stdout)
        if not changed:
            return None
        summary = f"Auto-synthesized: {len(changed)} modified/new files"
        branch_family = "unknown"
        for f in changed:
            if f.startswith("tests/") or "test_" in f:
                branch_family = "tests"
                break
            if f.startswith("scripts/"):
                branch_family = "scripts"
                break
            if f.startswith("knowledge/"):
                branch_family = "knowledge"
                break
        status_label = "continue" if exit_code == 0 else "failed"
        exit_note = "" if exit_code == 0 else f" Agent exit code was {exit_code}."
        return {
            "summary": summary,
            "status": status_label,
            "branch_family": branch_family,
            "family_novelty": "",
            "evidence_quality": "artifact_only",
            "competitiveness": "",
            "promotion_recommendation": "",
            "artifacts": [{"path": f, "description": "modified"} for f in changed],
            "new_facts": [],
            "decisions": [
                f"Checkpoint auto-synthesized from git status: inner agent produced no checkpoint markers.{exit_note}"
            ],
            "next_steps": ["Review auto-synthesized checkpoint and continue"],
            "risks": ["Checkpoint was auto-synthesized from git status; agent's full intent may not be captured."],
            "parallel_branches": 1,
            "goal_complete": False,
        }
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def build_iteration_prompt(ctx: RunContext, iteration_number: int, resume_session_id: str | None = None) -> str:
    prefix = ""
    if resume_session_id:
        prefix = (
            "Recovery mode: the outer autopilot runner was interrupted after creating this iteration's opencode session. "
            "Continue the same unfinished attempt in the existing session, reuse any in-flight repository state or running trainings that are still relevant, "
            "and avoid restarting work that was already launched unless the prior attempt is clearly unusable.\n\n"
        )
    session_line = (
        (
            f"SESSION GOAL (this iteration):\n{ctx.session_goal}\n\n"
            "This session goal overrides or refines the permanent goal for this iteration. "
            "Prioritize completing the session goal over broad exploration.\n\n"
        )
        if ctx.session_goal
        else ""
    )
    prompt = prefix + (
        f"Run id: {ctx.run_id}\n"
        f"Iteration: {iteration_number}\n\n"
        f"{session_line}"
        "Attached files contain the canonical goal plus compact disk-backed memory reconstructed from prior iterations, including tagged relevant memory retrieval, controller-generated shortlist/blacklist context, diversity pressure signals, and self-improving loop diagnostics (phase bottleneck analysis, creative divergence slate, meta-controller advice). "
        "Use them as your starting point, then inspect the repository as needed.\n\n"
        "Primary objective for this run:\n"
        "- Prioritize bug fixes, new features, test coverage, type safety, performance, and architectural improvements.\n"
        "- Treat the current production code only as baselines to beat, not as the default path to keep refining forever.\n"
        "- If recent iterations stayed too close to the same module or pattern, deliberately pivot to a meaningfully different area of the codebase.\n"
        "- Optimize for breadth of improvement, not just local polish. A useful failure in a fresh area is better than another stale near-copy.\n\n"
        "Required behavior:\n"
        "- Choose the best bounded next step toward the goal.\n"
        "- Think deeply before acting: first diagnose what the weakest area of the codebase is, then choose the step that most directly improves it.\n"
        "- Before choosing a path, generate a divergence slate with exactly three hypotheses: one incremental fix, one adjacent improvement, and one novel approach. Then pick deliberately instead of defaulting to the easy option.\n"
        "- At least one divergence-slate hypothesis should target a different module area than the most recent iterations.\n"
        "- Make each iteration advance a genuinely meaningful improvement, not just cosmetic changes.\n"
        "- Make durable progress in the repository or collect decisive evidence.\n"
        "- Validate the most relevant change or hypothesis when feasible.\n"
        "- Use the narrowest validation command that reduces uncertainty (from project config):\n"
        "    - Default: `" + get_validation("default", _CONFIG) + "`\n"
        "    - Typecheck: `" + get_validation("typecheck", _CONFIG) + "`\n"
        "    - Lint: `" + get_validation("lint", _CONFIG) + "`\n"
        "    - Build: `" + get_validation("build", _CONFIG) + "`\n"
        "- Do not spend an iteration until you can state: the specific code weakness targeted, why the chosen fix should help, the smallest test to validate it, and why this is better than the alternative.\n"
        "- Prefer structural improvements (reducing duplication, improving types, adding tests) over formatting or style-only changes.\n"
        "- Use current baselines only to benchmark; they are evaluation references, not the ideation template.\n"
        "- Treat `controller_state.md` as binding for guardrails and exhaustion rules but advisory for the shortlist.\n"
        "- If recent failures are not yet explained, spend the next iteration on diagnosis and sharper hypothesis formation.\n"
        "- If a line of work looks stale or repeatedly fails validation, record that evidence and pivot instead of repeating the same approach.\n"
        "- If repeated iterations fail to produce materially better results, explicitly summarize why the path is weak and pivot to a novel untested area.\n"
        "- Prefer fewer, higher-conviction iterations. Reuse an iteration for deeper comparison and hypothesis sharpening.\n"
        "- Use status `done` only when the overall run goal is complete. Exhausting one branch/family must return `continue` with the next pivot named.\n"
        "- Refresh the repository knowledge graph with `graphify update .` after meaningful code changes.\n"
        "- Respect repo conventions: all UI text via `t('...')`, no `any`, dedup before external side effects.\n"
        "- Record negative results, rejected approaches, and pivot decisions clearly in the checkpoint.\n"
        "- End with a valid checkpoint between AUTOPILOT_CHECKPOINT_BEGIN and AUTOPILOT_CHECKPOINT_END.\n"
        "- As a fallback, also write the exact same checkpoint JSON to `.opencode/checkpoint_output.json`.\n"
        "  This file-based side channel is more reliable when terminal output is large or ANSI-rich.\n"
        "- Example checkpoint format:\n"
        "```\n"
        "AUTOPILOT_CHECKPOINT_BEGIN\n"
        "{\n"
        '  "status": "continue",\n'
        '  "goal_complete": false,\n'
        '  "summary": "Brief description of what was done this iteration.",\n'
        '  "goal_progress": "How the iteration advanced the permanent goal.",\n'
        '  "branch_family": "tests|scripts|config|infra|docs",\n'
        '  "family_novelty": "major|moderate|minor",\n'
        '  "competitiveness": "promotable|promising|marginal|non_competitive",\n'
        '  "evidence_quality": "model_level|artifact_only|anecdotal|none",\n'
        '  "promotion_recommendation": "promote|hold|deprecate|kill",\n'
        '  "new_facts": ["Fact 1 discovered this iteration.", "Fact 2 discovered this iteration."],\n'
        '  "decisions": ["Decision 1 made this iteration."],\n'
        '  "next_steps": ["Next step 1.", "Next step 2."],\n'
        '  "files_touched": ["path/to/file1.py", "path/to/file2.py"],\n'
        '  "risks": ["Risk 1."],\n'
        '  "artifacts": [{"type": "test", "path": "tests/test_file.py", "description": "Tests for X"}]\n'
        "}\n"
        "AUTOPILOT_CHECKPOINT_END\n"
        "```\n"
        "- Do not ask the user questions. Use status `blocked` if a human decision is truly required.\n"
    )
    return prompt


def run_iteration(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    runtime_dir: Path,
    ctx: RunContext,
    iteration_number: int,
    attempt_number: int,
    resume_session_id: str | None = None,
) -> tuple[int, str, str, dict[str, Any]]:
    attachments, current_dir = build_memory_files(conn, runtime_dir, ctx)
    logs_dir = run_dir(runtime_dir, ctx.run_id) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = logs_dir / f"iteration-{iteration_number:04d}.attempt-{attempt_number:02d}.stdout.txt"
    stderr_path = logs_dir / f"iteration-{iteration_number:04d}.attempt-{attempt_number:02d}.stderr.txt"

    prompt_text = build_iteration_prompt(ctx, iteration_number, resume_session_id=resume_session_id)
    prompt_path = current_dir / "prompt.txt"
    write_text(prompt_path, prompt_text)

    session_title = autopilot_session_title(ctx.run_id, iteration_number, attempt_number)
    command = [
        "opencode",
        "run",
        "--agent",
        args.agent,
        "--dir",
        str(ROOT),
    ]
    if resume_session_id:
        command.extend(["--session", resume_session_id])
    else:
        command.extend(["--title", session_title])
    if not args.no_skip_permissions:
        command.append("--dangerously-skip-permissions")
    if args.model:
        command.extend(["--model", args.model])
    if args.variant:
        command.extend(["--variant", args.variant])
    for attachment in attachments:
        command.extend(["--file", str(attachment)])
    command.append("--")
    command.append(prompt_text)

    if args.dry_run:
        print(" ".join(command))
        print(f"\nPrompt saved to: {prompt_path}")
        checkpoint = normalize_checkpoint(
            {
                "status": "continue",
                "summary": "Dry run only. No opencode invocation was executed.",
                "next_steps": ["Run without --dry-run to execute the agent."],
            },
            fallback_status="continue",
            fallback_summary="Dry run only.",
        )
        checkpoint["_stdout_path"] = str(stdout_path.relative_to(ROOT))
        checkpoint["_stderr_path"] = str(stderr_path.relative_to(ROOT))
        return 0, "", "", checkpoint

    child_env, cpu_ids = opencode_child_env(args.cpu_budget_percent)
    preexec_fn = opencode_preexec_fn(cpu_ids)

    _log(f"run={ctx.run_id} iter={iteration_number} attempt={attempt_number} — launching opencode")

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def _pipe_reader(pipe: Any, lines: list[str], echo: bool) -> None:
        for raw_line in iter(pipe.readline, ""):
            lines.append(raw_line)
            if echo:
                print(raw_line, end="", flush=True)
        pipe.close()

    try:
        proc = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=child_env,
            preexec_fn=preexec_fn,
        )
        t_out = threading.Thread(target=_pipe_reader, args=(proc.stdout, stdout_lines, True), daemon=True)
        t_err = threading.Thread(target=_pipe_reader, args=(proc.stderr, stderr_lines, False), daemon=True)
        t_out.start()
        t_err.start()
        try:
            proc.wait(timeout=args.timeout_seconds)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            _log(f"iter={iteration_number} attempt={attempt_number} — TIMEOUT after {args.timeout_seconds}s, killing")
            proc.kill()
            exit_code = 124
        t_out.join()
        t_err.join()
        stdout_text = "".join(stdout_lines)
        stderr_text = "".join(stderr_lines)
    except OSError as exc:
        stdout_text = ""
        stderr_text = str(exc)
        exit_code = 1

    _log(f"run={ctx.run_id} iter={iteration_number} attempt={attempt_number} — opencode exited (code={exit_code})")

    write_text(stdout_path, stdout_text or "")
    write_text(stderr_path, stderr_text or "")

    # Check for file-based checkpoint written by the inner agent
    raw_checkpoint: dict[str, Any] | None = None
    checkpoint_error: str | None = None
    if CHECKPOINT_OUTPUT_FILE.exists():
        try:
            payload = json.loads(CHECKPOINT_OUTPUT_FILE.read_text())
            if isinstance(payload, dict) and isinstance(payload.get("status"), str):
                raw_checkpoint = payload
                _log(f"run={ctx.run_id} iter={iteration_number} — checkpoint read from {CHECKPOINT_OUTPUT_FILE}")
            else:
                _log(f"run={ctx.run_id} iter={iteration_number} — checkpoint file found but invalid (no status)")
        except json.JSONDecodeError:
            try:
                from scripts.checkpoint import _parse_checkpoint_kv

                payload = _parse_checkpoint_kv(CHECKPOINT_OUTPUT_FILE.read_text())
                if payload is not None:
                    raw_checkpoint = payload
                    _log(
                        f"run={ctx.run_id} iter={iteration_number} — checkpoint read (YAML) from {CHECKPOINT_OUTPUT_FILE}"
                    )
                else:
                    _log(f"run={ctx.run_id} iter={iteration_number} — checkpoint file found but could not parse")
            except Exception:
                _log(f"run={ctx.run_id} iter={iteration_number} — checkpoint file found but could not parse")
        except OSError as exc:
            _log(f"run={ctx.run_id} iter={iteration_number} — checkpoint file read error: {exc}")
        try:
            CHECKPOINT_OUTPUT_FILE.unlink(missing_ok=True)
        except OSError:
            pass

    if raw_checkpoint is None:
        # Reload checkpoint module to pick up agent edits made during this iteration
        try:
            import importlib
            import scripts.checkpoint

            importlib.reload(scripts.checkpoint)
            from scripts.checkpoint import extract_checkpoint as _extract_checkpoint
        except Exception:
            _extract_checkpoint = extract_checkpoint  # fall back to in-memory definition

        raw_checkpoint, checkpoint_error = _extract_checkpoint(stdout_text)
        if raw_checkpoint is None and stderr_text:
            raw_checkpoint, checkpoint_error = _extract_checkpoint(stderr_text)
            if raw_checkpoint is not None:
                _log(f"run={ctx.run_id} iter={iteration_number} — checkpoint found in stderr (not stdout)")
    fallback_status = "continue" if exit_code == 0 else "failed"
    fallback_summary = "Iteration completed without a valid checkpoint."
    if checkpoint_error:
        fallback_summary = checkpoint_error

    if raw_checkpoint is None and exit_code == 0:
        synthesized = synthesize_checkpoint_from_git(exit_code, raw_checkpoint)
        if synthesized is not None:
            _log(f"run={ctx.run_id} iter={iteration_number} — checkpoint auto-synthesized from git diff")
            raw_checkpoint = synthesized
            checkpoint_error = None
            fallback_summary = synthesized.get("summary", "")

    checkpoint = normalize_checkpoint(raw_checkpoint, fallback_status, fallback_summary)
    if checkpoint_error and checkpoint_error not in checkpoint["risks"]:
        checkpoint["risks"].append(checkpoint_error)
    if exit_code != 0 and f"opencode exited with status {exit_code}" not in checkpoint["risks"]:
        checkpoint["risks"].append(f"opencode exited with status {exit_code}")

    checkpoint["_stdout_path"] = str(stdout_path.relative_to(ROOT))
    checkpoint["_stderr_path"] = str(stderr_path.relative_to(ROOT))
    return exit_code, stdout_text, stderr_text, checkpoint


def persist_iteration(
    conn: sqlite3.Connection,
    runtime_dir: Path,
    ctx: RunContext,
    iteration_number: int,
    attempt_count: int,
    transient_failures: int,
    started_at: str,
    finished_at: str,
    exit_code: int,
    checkpoint: dict[str, Any],
) -> RunContext:
    checkpoint["_attempt_count"] = attempt_count
    checkpoint["_transient_failures"] = transient_failures
    checkpoint_json = json.dumps(checkpoint, indent=2, sort_keys=True)
    run_status = STATUS_MAP[checkpoint["status"]]
    conn.execute(
        """
        INSERT INTO iterations (
            run_id, iteration_number, started_at, finished_at, exit_code, status, summary, checkpoint_json, stdout_path, stderr_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ctx.run_id,
            iteration_number,
            started_at,
            finished_at,
            exit_code,
            checkpoint["status"],
            checkpoint["summary"],
            checkpoint_json,
            checkpoint["_stdout_path"],
            checkpoint["_stderr_path"],
        ),
    )
    for fact in checkpoint["new_facts"]:
        conn.execute(
            "INSERT OR IGNORE INTO facts (run_id, fact, first_iteration, created_at) VALUES (?, ?, ?, ?)",
            (ctx.run_id, fact, iteration_number, finished_at),
        )
    for artifact in checkpoint["artifacts"]:
        conn.execute(
            "INSERT OR IGNORE INTO artifacts (run_id, iteration_number, path, description) VALUES (?, ?, ?, ?)",
            (ctx.run_id, iteration_number, artifact["path"], artifact["description"]),
        )
    persist_memory_entries(conn, ctx.run_id, iteration_number, checkpoint, finished_at)

    # ── Prune old memory entries every 20 iterations to cap token growth ──
    if iteration_number % 20 == 0:
        prune_memory_entries(conn, ctx.run_id, iteration_number)

    conn.execute(
        "UPDATE runs SET status = ?, updated_at = ?, iteration_count = ?, latest_summary = ? WHERE run_id = ?",
        (run_status, finished_at, iteration_number, checkpoint["summary"], ctx.run_id),
    )
    conn.commit()

    current_dir = run_dir(runtime_dir, ctx.run_id) / "current"
    write_text(current_dir / "latest_checkpoint.json", checkpoint_json)
    summary_lines = [
        "# Autopilot Status\n",
        f"- Run id: `{ctx.run_id}`",
        f"- Status: `{run_status}`",
        f"- Iteration count: `{iteration_number}`",
        f"- Attempts used: `{attempt_count}`",
        f"- Transient retries: `{transient_failures}`",
        "",
        "## Latest Summary",
        "",
        checkpoint["summary"],
        "",
        "## Next Steps",
        "",
    ]
    if checkpoint["next_steps"]:
        summary_lines.extend(f"- {item}" for item in checkpoint["next_steps"])
    else:
        summary_lines.append("- None recorded.")
    write_text(current_dir / "status.md", "\n".join(summary_lines))

    cleanup_training_scratch(iteration_number)

    return RunContext(
        run_id=ctx.run_id,
        goal_text=ctx.goal_text,
        status=run_status,
        iteration_count=iteration_number,
    )


def persist_attempt(
    conn: sqlite3.Connection,
    ctx: RunContext,
    iteration_number: int,
    attempt_number: int,
    started_at: str,
    finished_at: str,
    exit_code: int,
    checkpoint: dict[str, Any],
    transient_failure: bool,
    retry_reason: str,
) -> None:
    checkpoint_json = json.dumps(checkpoint, indent=2, sort_keys=True)
    conn.execute(
        """
        INSERT INTO iteration_attempts (
            run_id, iteration_number, attempt_number, started_at, finished_at, exit_code, status,
            transient_failure, retry_reason, summary, checkpoint_json, stdout_path, stderr_path
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ctx.run_id,
            iteration_number,
            attempt_number,
            started_at,
            finished_at,
            exit_code,
            checkpoint["status"],
            int(transient_failure),
            retry_reason,
            checkpoint["summary"],
            checkpoint_json,
            checkpoint["_stdout_path"],
            checkpoint["_stderr_path"],
        ),
    )
    conn.commit()


def render_promotion_advisory(conn: sqlite3.Connection, ctx: RunContext) -> str | None:
    """Scan all iterations for promotable merge-ready candidates.
    Returns a formatted advisory string if any exist, or None.
    """
    rows = conn.execute(
        "SELECT iteration_number, started_at, checkpoint_json FROM iterations WHERE run_id = ? ORDER BY iteration_number ASC",
        (ctx.run_id,),
    ).fetchall()

    candidates: list[dict[str, Any]] = []
    for row in rows:
        cp = normalize_checkpoint(
            json.loads(row["checkpoint_json"]),
            fallback_status="continue",
            fallback_summary="",
        )
        if cp.get("promotion_recommendation") == "promote":
            candidates.append(
                {
                    "iteration_number": row["iteration_number"],
                    "started_at": row["started_at"],
                    "summary": cp["summary"],
                    "goal_progress": cp.get("goal_progress", ""),
                    "artifacts": cp.get("artifacts", []),
                    "decisions": cp.get("decisions", []),
                    "competitiveness": cp.get("competitiveness", "-"),
                    "branch_family": cp.get("branch_family", "-"),
                }
            )

    if not candidates:
        return None

    lines = [
        "",
        "╔" + "═" * 68 + "╗",
        "║" + " ⚑  MERGE ADVISORY — HUMAN REVIEW REQUIRED  ⚑ ".center(68) + "║",
        "║" + " Autopilot found candidate(s) that may be merge-ready. ".center(68) + "║",
        "║" + " No merge has been triggered. This is advisory only. ".center(68) + "║",
        "╚" + "═" * 68 + "╝",
        "",
    ]
    for i, c in enumerate(candidates, 1):
        lines.append(f"  Candidate {i}/{len(candidates)} — Iteration {c['iteration_number']} ({c['started_at']})")
        lines.append(f"  Family   : {c['branch_family']}")
        lines.append(f"  Score    : {c['competitiveness']}")
        lines.append(f"  Summary  : {compact_summary(c['summary'], 200)}")
        if c["goal_progress"]:
            lines.append(f"  Progress : {compact_summary(c['goal_progress'], 200)}")
        if c["artifacts"]:
            lines.append("  Artifacts:")
            for art in c["artifacts"]:
                desc = f" — {art['description']}" if art.get("description") else ""
                lines.append(f"    • {art['path']}{desc}")
        if c["decisions"]:
            lines.append("  Key decisions:")
            for d in c["decisions"][:5]:
                lines.append(f"    • {d}")
        lines.append("")
    lines += [
        "  ► Check knowledge/autopilot_kb.md for full details.",
        "  ► Run ID: " + ctx.run_id,
        "",
        "╔" + "═" * 68 + "╗",
        "║" + " NO ACTION HAS BEEN TAKEN. REVIEW IS YOUR DECISION. ".center(68) + "║",
        "╚" + "═" * 68 + "╝",
        "",
    ]
    return "\n".join(lines)


def _apply_meta_overrides_to_args(args: argparse.Namespace, meta_overrides: dict[str, Any]) -> None:
    if not meta_overrides:
        return
    for key, value in meta_overrides.items():
        if hasattr(args, key):
            setattr(args, key, value)


def _resolve_meta_controller_overrides(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    ctx: RunContext,
) -> dict[str, Any]:
    """Run the meta-controller and return param overrides for the next cycle.

    Returns an empty dict when iteration_count < 3 or when the controller raises.
    """
    if ctx.iteration_count < 3:
        return {}
    try:
        mc_rows = conn.execute(
            "SELECT checkpoint_json FROM iterations WHERE run_id = ? ORDER BY iteration_number DESC LIMIT 12",
            (ctx.run_id,),
        ).fetchall()
        mc_checkpoints: list[dict[str, Any]] = []
        mc_families: list[str] = []
        for i, row in enumerate(mc_rows):
            cp = json.loads(row["checkpoint_json"])
            cp["iteration_number"] = ctx.iteration_count - i
            mc_checkpoints.append(cp)
            mc_families.append(cp.get("branch_family", "unknown"))

        mc_nc = 0
        for row in mc_checkpoints:
            if row.get("competitiveness") == "non_competitive":
                mc_nc += 1
            else:
                break

        mc_params = MetaControllerParams(
            sleep_seconds=args.sleep_seconds,
            max_retries_per_iteration=args.max_retries_per_iteration,
            cpu_budget_percent=args.cpu_budget_percent,
            parallel_branches_max=args.parallel_branches,
            force_parallel=getattr(args, "force_parallel", 0),
        )
        _diag_md, mc_advice = run_self_improving_loop(
            conn=conn,
            run_id=ctx.run_id,
            checkpoint_rows=mc_checkpoints,
            total_iterations=ctx.iteration_count,
            consecutive_noncompetitive=mc_nc,
            current_params=mc_params,
            recent_families=list(dict.fromkeys(mc_families))[:8],
            exhausted_families=[],
        )
        _log(f"meta-controller: {mc_advice.performance_summary}")
        if not mc_advice.changed:
            return {}
        overrides: dict[str, Any] = {}
        for param, (old, new) in mc_advice.changed.items():
            overrides[param] = new
            _log(f"meta-controller: {param} {old} → {new} ({mc_advice.reasoning})")
        return overrides
    except Exception as exc:
        _log(f"meta-controller error: {exc}")
        return {}


def _maybe_run_meta_autopilot_cycle(ctx: RunContext, checkpoint: dict[str, Any]) -> None:
    """Run a meta-autopilot improvement cycle when iteration_count hits a 10-iter boundary."""
    if not _META_AVAILABLE:
        return
    if ctx.iteration_count < 5 or ctx.iteration_count % 10 != 0:
        return
    try:
        meta_ctrl = MetaController()  # type: ignore[misc]
        meta_ctrl.get_or_create_level(0)
        meta_ctrl.get_or_create_level(1)
        meta_ctrl.auto_discover()
        cycle_result = meta_ctrl.run_cycle(
            1,
            metrics={
                "iteration_efficiency": 1.0,
                "improvement_rate": checkpoint.get("competitiveness", "unknown") in ("promising", "promotable"),
                "novelty_diversity": 1.0 if checkpoint.get("family_novelty") in ("moderate", "major") else 0.0,
            },
        )
        _log(
            f"meta-autopilot cycle: level=1 advanced={cycle_result['experiments_advanced']} "
            f"started={cycle_result['experiments_started']} "
            f"stagnation={cycle_result['stagnation_score']:.3f}"
        )
        if cycle_result.get("escalated"):
            _log(f"meta-autopilot ESCALATION: {cycle_result.get('reason', '')}")
    except Exception as exc:
        _log(f"meta-autopilot error: {exc}")


def _print_run_end_advisory(conn: sqlite3.Connection, ctx: RunContext) -> None:
    advisory = render_promotion_advisory(conn, ctx)
    if advisory:
        print(advisory, flush=True)
    else:
        _log("No promotable candidates found in this run.")


def _pick_best_branch_checkpoint(
    branch_results: list[tuple[int, int, int, str, str, str, dict[str, Any], int]],
) -> dict[str, Any]:
    """Select the highest-ranked checkpoint from a list of branch results.

    Rank: promotable > promising > marginal > non_competitive > unknown > "".
    """
    rank = {
        "promotable": 4,
        "promising": 3,
        "marginal": 2,
        "non_competitive": 1,
        "unknown": 0,
        "": 0,
    }
    best: dict[str, Any] | None = None
    for _it, _at, _ec, _s, _f, _so, cp, _tf in branch_results:
        if best is None or rank.get(cp.get("competitiveness", ""), 0) > rank.get(best.get("competitiveness", ""), 0):
            best = cp
    assert best is not None
    return best


@dataclass
class CycleResult:
    """Outcome of a single autopilot cycle.

    Attributes:
        ctx: Updated run context (iteration_count incremented, etc.).
        checkpoint: Best checkpoint selected from this cycle's branches.
        meta_overrides: New overrides to apply at the start of the next cycle.
        stop: True when the loop should terminate (controller stop, done/blocked/failed).
        stop_exit_code: 0 on clean termination, 1 on failed/blocked.
        stop_reason: Human-readable reason for stopping (for logs).
    """

    ctx: RunContext
    checkpoint: dict[str, Any]
    meta_overrides: dict[str, Any]
    stop: bool
    stop_exit_code: int
    stop_reason: str


def run_cycle(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
    runtime_dir: Path,
    ctx: RunContext,
    offset: int,
    base_iteration_number: int,
    resume_session_id: str | None,
    prev_checkpoint: dict[str, Any] | None,
    meta_overrides: dict[str, Any],
) -> CycleResult:
    """Execute one iteration cycle of the autopilot loop.

    Runs the configured number of parallel branches, persists all branch iterations,
    invokes the meta-controller (if iter >= 3) and the meta-autopilot (if iter % 10 == 0
    and iter >= 5), then evaluates the controller stop condition. Returns a
    ``CycleResult`` describing the outcome; the caller decides whether to continue the
    outer loop based on ``stop`` and ``stop_exit_code``.
    """
    if meta_overrides:
        _apply_meta_overrides_to_args(args, meta_overrides)
        override_log = " | ".join(f"{k}={v}" for k, v in meta_overrides.items())
        _log(f"meta-controller active: {override_log}")

    num_branches = resolve_branch_count(args, prev_checkpoint)
    _log("══════════════════════════════════════════════════")
    _log(
        f"ITERATION CYCLE {offset}/{args.max_iterations} | run={ctx.run_id} | branches={num_branches} | starting at iteration {base_iteration_number}"
    )
    _log("══════════════════════════════════════════════════")

    branch_results: list[tuple[int, int, int, str, str, str, dict[str, Any], int]] = []

    def _run_branch(branch_index: int) -> tuple[int, int, int, str, str, str, dict[str, Any], int]:
        iter_num = base_iteration_number + branch_index
        attempt_num = 1
        rsid = resume_session_id if (branch_index == 0 and offset == 1) else None
        transient_failures_b = 0

        if hasattr(args, "session_goal_file") and args.session_goal_file:
            try:
                new_sg = args.session_goal_file.read_text(encoding="utf-8").strip()
                if new_sg and new_sg != ctx.session_goal:
                    ctx.session_goal = new_sg
                    _log(f"session goal updated from {args.session_goal_file}")
            except OSError:
                pass

        while True:
            _log(f"━━━ BRANCH {branch_index} | iteration {iter_num} | attempt {attempt_num} | run={ctx.run_id} ━━━")
            s_at = utc_now()
            ec, sout, serr, cp = run_iteration(
                conn, args, runtime_dir, ctx, iter_num, attempt_num, resume_session_id=rsid
            )
            cp, coerced_reason = coerce_done_checkpoint(cp)
            f_at = utc_now()
            tf, retry_reason = classify_transient_failure(ec, sout, serr, cp)
            persist_attempt(conn, ctx, iter_num, attempt_num, s_at, f_at, ec, cp, tf, retry_reason)
            if coerced_reason:
                _log(f"branch={branch_index} iter={iter_num} — checkpoint coerced: {coerced_reason}")
            if args.dry_run:
                return iter_num, attempt_num, ec, s_at, f_at, sout, cp, transient_failures_b
            should_retry = tf and cp["status"] == "failed" and attempt_num <= args.max_retries_per_iteration
            if should_retry:
                transient_failures_b += 1
                backoff = retry_backoff_seconds(args, attempt_num)
                _log(
                    f"branch={branch_index} iter={iter_num} attempt={attempt_num} — transient failure, retrying in {backoff}s ({retry_reason})"
                )
                if backoff:
                    time.sleep(backoff)
                attempt_num += 1
                rsid = None
                continue
            _log(
                f"━━━ BRANCH {branch_index} | iteration {iter_num} DONE | status={cp['status']} competitiveness={cp.get('competitiveness', '-')} evidence={cp.get('evidence_quality', '-')} ━━━"
            )
            _log(f"summary: {compact_summary(cp['summary'])}")
            return iter_num, attempt_num, ec, s_at, f_at, sout, cp, transient_failures_b

    if num_branches == 1 or args.dry_run:
        branch_results = [_run_branch(0)]
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_branches) as pool:
            futures = {pool.submit(_run_branch, i): i for i in range(num_branches)}
            for fut in concurrent.futures.as_completed(futures):
                try:
                    branch_results.append(fut.result())
                except Exception as exc:
                    print(f"Branch {futures[fut]} raised: {exc}", file=sys.stderr)

    if args.dry_run:
        return CycleResult(
            ctx=ctx,
            checkpoint=normalize_checkpoint({}, "continue", "dry run"),
            meta_overrides=meta_overrides,
            stop=True,
            stop_exit_code=0,
            stop_reason="dry_run",
        )

    if not branch_results:
        print("All branches failed to produce results.", file=sys.stderr)
        return CycleResult(
            ctx=ctx,
            checkpoint=normalize_checkpoint({}, "failed", "no branch results"),
            meta_overrides=meta_overrides,
            stop=True,
            stop_exit_code=1,
            stop_reason="no_branch_results",
        )

    for iter_num, attempt_num, ec, s_at, f_at, _sout, cp, tf in branch_results:
        ctx = persist_iteration(conn, runtime_dir, ctx, iter_num, attempt_num, tf, s_at, f_at, ec, cp)

    checkpoint = _pick_best_branch_checkpoint(branch_results)

    new_overrides = _resolve_meta_controller_overrides(conn, args, ctx)
    if new_overrides:
        meta_overrides = {**meta_overrides, **new_overrides}

    _maybe_run_meta_autopilot_cycle(ctx, checkpoint)

    _log(
        f"══ CYCLE {offset} COMPLETE | run={ctx.run_id} | iterations={base_iteration_number}+{num_branches - 1} | status={checkpoint['status']} competitiveness={checkpoint.get('competitiveness', '-')} promotion={checkpoint.get('promotion_recommendation', '-')} ══"
    )
    if checkpoint.get("next_steps"):
        _log("next steps:")
        for step in checkpoint["next_steps"]:
            print(f"  → {step}", flush=True)

    if checkpoint.get("promotion_recommendation") == "promote":
        advisory = render_promotion_advisory(conn, ctx)
        if advisory:
            print(advisory, flush=True)

    forced_stop, forced_reason = controller_should_stop(conn, ctx, checkpoint)
    if forced_stop:
        _log(f"CONTROLLER STOP: {forced_reason}")
        return CycleResult(
            ctx=ctx,
            checkpoint=checkpoint,
            meta_overrides=meta_overrides,
            stop=True,
            stop_exit_code=0,
            stop_reason=forced_reason,
        )

    if checkpoint["status"] in {"done", "blocked", "failed"}:
        _log(f"Run ending with status={checkpoint['status']}")
        return CycleResult(
            ctx=ctx,
            checkpoint=checkpoint,
            meta_overrides=meta_overrides,
            stop=True,
            stop_exit_code=0 if checkpoint["status"] == "done" else 1,
            stop_reason=f"status_{checkpoint['status']}",
        )

    return CycleResult(
        ctx=ctx,
        checkpoint=checkpoint,
        meta_overrides=meta_overrides,
        stop=False,
        stop_exit_code=0,
        stop_reason="",
    )


def main() -> int:
    args = parse_args()

    if args.subcommand == "report":
        return cmd_report(args)
    if args.subcommand == "export-kb":
        return cmd_export_kb(args)

    runtime_dir = args.runtime_dir.resolve()
    conn = ensure_runtime(runtime_dir)

    ctx = resolve_run(conn, args)
    initial_state = recover_resume_latest_state(conn, ctx) if args.resume_latest else pending_iteration_state(conn, ctx)

    latest_row = conn.execute(
        "SELECT checkpoint_json FROM iterations WHERE run_id = ? ORDER BY iteration_number DESC LIMIT 1",
        (ctx.run_id,),
    ).fetchone()
    prev_checkpoint: dict[str, Any] | None = (
        normalize_checkpoint(json.loads(latest_row["checkpoint_json"]), "continue", "") if latest_row else None
    )

    meta_overrides: dict[str, Any] = {}

    for offset in range(1, args.max_iterations + 1):
        if offset == 1:
            base_iteration_number = initial_state.iteration_number
            resume_session_id = initial_state.resume_session_id
        else:
            base_iteration_number = ctx.iteration_count + 1
            resume_session_id = None

        if offset == 1 and args.resume_latest:
            _log(
                f"Resume-latest: run={ctx.run_id} iteration={base_iteration_number}"
                + (f" session={resume_session_id}" if resume_session_id else "")
            )

        result = run_cycle(
            conn=conn,
            args=args,
            runtime_dir=runtime_dir,
            ctx=ctx,
            offset=offset,
            base_iteration_number=base_iteration_number,
            resume_session_id=resume_session_id,
            prev_checkpoint=prev_checkpoint,
            meta_overrides=meta_overrides,
        )
        ctx = result.ctx
        meta_overrides = result.meta_overrides
        prev_checkpoint = result.checkpoint

        if result.stop_reason == "controller_stop":
            _print_run_end_advisory(conn, ctx)
            return result.stop_exit_code
        if result.stop_reason == "no_branch_results":
            return result.stop_exit_code
        if result.stop_reason == "dry_run":
            return result.stop_exit_code
        if result.stop_reason.startswith("status_"):
            _print_run_end_advisory(conn, ctx)
            return result.stop_exit_code

        if offset < args.max_iterations and args.sleep_seconds:
            _log(f"Sleeping {args.sleep_seconds}s before next cycle...")
            time.sleep(args.sleep_seconds)

    _print_run_end_advisory(conn, ctx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
