#!/usr/bin/env python3

from __future__ import annotations

# Autocode config
try:
    from autocode_config import ROOT, load_config, get_path
except ImportError:
    import sys
    from pathlib import Path
    _D = Path(__file__).resolve().parent
    if str(_D) not in sys.path:
        sys.path.insert(0, str(_D))
    from autocode_config import ROOT, load_config, get_path

_CONFIG = load_config()

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


# ROOT imported from autocode_config

# Ensure this directory is on sys.path for sibling module imports
_AUTOPILOT_DIR = Path(__file__).resolve().parent
if str(_AUTOPILOT_DIR) not in sys.path:
    sys.path.insert(0, str(_AUTOPILOT_DIR))

from self_improving_loop import (
    MetaControllerParams,
    run_self_improving_loop,
)

# Meta-autopilot: hierarchical component-level self-improvement
try:
    from meta_autopilot import (
        MutationType,
        MetaController,
        ComponentRef,
        initialize_defaults,
    )
    _META_AVAILABLE = True
except ImportError:
    _META_AVAILABLE = False

import yaml
DEFAULT_GOAL_FILE = get_path("goal_file", _CONFIG)
RUNTIME_ROOT = get_path("runtime_dir", _CONFIG)
ACTIVE_RUN_FILE = RUNTIME_ROOT / "active_run.txt"
CHECKPOINT_BEGIN = "AUTOPILOT_CHECKPOINT_BEGIN"
CHECKPOINT_END = "AUTOPILOT_CHECKPOINT_END"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
TOKEN_RE = re.compile(r"[a-z0-9]+")
STATUS_MAP = {
    "continue": "running",
    "done": "done",
    "blocked": "blocked",
    "failed": "failed",
}
CHECKPOINT_COMPETITIVENESS = {"", "unknown", "non_competitive", "marginal", "promising", "promotable"}
CHECKPOINT_EVIDENCE_QUALITY = {"", "artifact_only", "model_level", "strategy_level"}
CHECKPOINT_FAMILY_NOVELTY = {"", "minor", "moderate", "major"}
CHECKPOINT_PROMOTION = {"", "hold", "promote", "abandon_family", "abandon_run"}
PARALLEL_BRANCHES_MAX = 3
CONTROLLER_EXHAUST_FAMILY_AFTER = 3
CONTROLLER_HARD_PIVOT_AFTER = 5
CONTROLLER_DEFAULT_STOP_AFTER = 100
CONTROLLER_DIVERSITY_WINDOW = 6
CONTROLLER_REPEAT_FAMILY_STREAK = 3
CONTROLLER_DOMINANT_FAMILY_SHARE = 0.6
MEMORY_IMPORTANCE = {
    "summary": 2.0,
    "goal_progress": 4.0,
    "fact": 5.0,
    "decision": 4.0,
    "next_step": 4.0,
    "risk": 3.0,
    "artifact": 3.0,
}
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "of",
    "on",
    "or",
    "that",
    "the",
    "their",
    "then",
    "this",
    "to",
    "use",
    "using",
    "with",
    "you",
    "your",
}
TRANSIENT_FAILURE_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"timed? out",
        r"timeout",
        r"rate limit",
        r"too many requests",
        r"temporar(?:y|ily)",
        r"unavailable",
        r"overload",
        r"overloaded",
        r"connection reset",
        r"connection aborted",
        r"connection refused",
        r"network",
        r"econnreset",
        r"broken pipe",
        r"502",
        r"503",
        r"504",
        r"upstream",
        r"try again",
        r"no checkpoint markers found",
        r"checkpoint json was invalid",
    )
]
NON_TRANSIENT_FAILURE_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"permission",
        r"user rejected",
        r"missing secret",
        r"missing credential",
        r"not found",
        r"no such file",
        r"syntax error",
        r"traceback",
        r"module not found",
        r"config invalid",
        r"schema",
    )
]
MEMORY_ENTRY_LABELS = {
    "fact": "Facts",
    "decision": "Decisions",
    "next_step": "Next Steps",
    "risk": "Risks",
    "goal_progress": "Goal Progress",
    "artifact": "Artifacts",
    "summary": "Summaries",
}


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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log(msg: str) -> None:
    """Print a timestamped progress line to stdout immediately."""
    print(f"[{utc_now()}] {msg}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the long-horizon opencode autopilot loop.")
    subparsers = parser.add_subparsers(dest="subcommand")

    # ── report subcommand ──────────────────────────────────────────────────
    report_parser = subparsers.add_parser("report", help="Render a Markdown report for a run and exit.")
    report_group = report_parser.add_mutually_exclusive_group()
    report_group.add_argument("--run-id", help="Render report for this specific run id.")
    report_group.add_argument("--latest", action="store_true", help="Render report for the active run.")
    report_parser.add_argument("--output", type=Path, help="Write report to this file (default: stdout).")
    report_parser.add_argument("--runtime-dir", type=Path, default=RUNTIME_ROOT, help="Override runtime storage directory.")

    export_kb_parser = subparsers.add_parser("export-kb", help="Export autopilot_kb.yaml to a dated Markdown snapshot.")
    export_kb_parser.add_argument("--output", type=Path, help="Write to this file instead of auto-named snapshot.")
    export_kb_parser.add_argument("--no-date-suffix", action="store_true", help="Skip the date suffix in the auto-generated filename.")

    # ── run subcommand (default behaviour) ────────────────────────────────
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--goal", help="Goal text for a new run.")
    group.add_argument("--goal-file", type=Path, help="Markdown file describing the goal for a new run.")
    group.add_argument("--resume-run", help="Resume a specific run id.")
    group.add_argument("--resume-latest", action="store_true", help="Resume the active run recorded on disk.")
    parser.add_argument("--session-goal", help="Session-specific goal text. Overrides the permanent goal for this iteration.")
    parser.add_argument("--session-goal-file", type=Path, help="Markdown file with a session-specific goal. Re-read each iteration.")
    parser.add_argument("--agent", default="goal-autopilot", help="Opencode agent name to run.")
    parser.add_argument("--model", help="Optional provider/model override.")
    parser.add_argument("--variant", help="Optional model variant override.")
    parser.add_argument("--max-iterations", type=int, default=1, help="How many iterations to execute in this invocation.")
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
    parser.add_argument("--dry-run", action="store_true", help="Build memory and print the next prompt without calling opencode.")
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
                (run_id, imp["entry_type"], imp["content"], imp["tags_json"],
                 imp["importance"], 0, imp["created_at"], now),
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

    return RunContext(run_id=run_id, goal_text=goal_text, status="running", iteration_count=0, session_goal=session_goal)


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
        ctx = load_run(conn, args.resume_run)
        resumed_existing_run = True
    elif args.resume_latest:
        run_id = active_run_id(runtime_dir)
        if not run_id:
            raise SystemExit("No active run recorded. Start a new run with --goal or --goal-file.")
        ctx = load_run(conn, run_id)
        resumed_existing_run = True
    elif goal_text:
        ctx = create_run(conn, runtime_dir, goal_text, session_goal=session_goal)
    else:
        run_id = active_run_id(runtime_dir)
        if not run_id:
            raise SystemExit("Provide --goal, --goal-file, --resume-run, or --resume-latest.")
        ctx = load_run(conn, run_id)
        resumed_existing_run = True
    if resumed_existing_run:
        ctx = sync_goal_from_canonical_file(conn, ctx)
    set_active_run(runtime_dir, ctx.run_id)
    return ctx


def run_dir(runtime_dir: Path, run_id: str) -> Path:
    return runtime_dir / "runs" / run_id


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def unique_preserve(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def extract_tags(*values: Any, limit: int = 16) -> list[str]:
    tags: list[str] = []
    for value in values:
        text = clean_text(value).lower()
        if not text:
            continue
        # Single tokens
        tokens = TOKEN_RE.findall(text)
        for token in tokens:
            if token in STOPWORDS:
                continue
            if len(token) < 2 and not token.isdigit():
                continue
            if len(token) > 40:
                continue
            tags.append(token)
        # Bigrams (multi-word concepts joined by underscore)
        for i in range(len(tokens) - 1):
            bigram = tokens[i] + "_" + tokens[i + 1]
            if len(bigram) > 4 and len(bigram) <= 60:
                if tokens[i] not in STOPWORDS or tokens[i + 1] not in STOPWORDS:
                    tags.append(bigram)
    return unique_preserve(tags)[:limit]


def normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        text = clean_text(item)
        if text:
            items.append(text)
    return items


def normalize_artifacts(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    artifacts: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        path = clean_text(item.get("path"))
        description = clean_text(item.get("description"))
        if path:
            artifacts.append({"path": path, "description": description})
    return artifacts


def _parse_parallel_branches(value: Any) -> int:
    """Parse and clamp a parallel_branches value to [1, PARALLEL_BRANCHES_MAX]."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, min(n, PARALLEL_BRANCHES_MAX))


def normalize_checkpoint(raw: dict[str, Any] | None, fallback_status: str, fallback_summary: str) -> dict[str, Any]:
    payload = raw or {}
    status = clean_text(payload.get("status")).lower()
    if status not in STATUS_MAP:
        status = fallback_status

    # goal_complete: accept explicit boolean; fall back to phrase-matching later in coerce_done_checkpoint
    raw_gc = payload.get("goal_complete")
    if isinstance(raw_gc, bool):
        goal_complete = raw_gc
    elif isinstance(raw_gc, str) and raw_gc.strip().lower() in ("true", "1", "yes"):
        goal_complete = True
    else:
        goal_complete = False

    # branch_family: prefer explicit agent-set value; mark as needing inference if absent
    raw_family = clean_text(payload.get("branch_family")).lower().replace("-", "_").replace(" ", "_")

    checkpoint = {
        "status": status,
        "goal_complete": goal_complete,
        "summary": clean_text(payload.get("summary")) or fallback_summary,
        "goal_progress": clean_text(payload.get("goal_progress")),
        "new_facts": normalize_string_list(payload.get("new_facts")),
        "decisions": normalize_string_list(payload.get("decisions")),
        "next_steps": normalize_string_list(payload.get("next_steps")),
        "files_touched": normalize_string_list(payload.get("files_touched")),
        "commands_run": normalize_string_list(payload.get("commands_run")),
        "risks": normalize_string_list(payload.get("risks")),
        "artifacts": normalize_artifacts(payload.get("artifacts")),
        "branch_family": raw_family,
        "family_novelty": clean_text(payload.get("family_novelty")).lower(),
        "evidence_quality": clean_text(payload.get("evidence_quality")).lower(),
        "competitiveness": clean_text(payload.get("competitiveness")).lower(),
        "promotion_recommendation": clean_text(payload.get("promotion_recommendation")).lower(),
        "parallel_branches": _parse_parallel_branches(payload.get("parallel_branches")),
    }
    if checkpoint["family_novelty"] not in CHECKPOINT_FAMILY_NOVELTY:
        checkpoint["family_novelty"] = infer_family_novelty(checkpoint)
    if checkpoint["evidence_quality"] not in CHECKPOINT_EVIDENCE_QUALITY:
        checkpoint["evidence_quality"] = infer_evidence_quality(checkpoint)
    if checkpoint["competitiveness"] not in CHECKPOINT_COMPETITIVENESS:
        checkpoint["competitiveness"] = infer_competitiveness(checkpoint)
    if checkpoint["promotion_recommendation"] not in CHECKPOINT_PROMOTION:
        checkpoint["promotion_recommendation"] = infer_promotion_recommendation(checkpoint)
    # Only run inference if the agent did not set an explicit non-empty family
    if not checkpoint["branch_family"]:
        checkpoint["branch_family"] = infer_branch_family(checkpoint)
    return checkpoint


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def extract_checkpoint(stdout_text: str) -> tuple[dict[str, Any] | None, str | None]:
    clean_stdout = strip_ansi(stdout_text)
    pattern = re.compile(
        rf"{CHECKPOINT_BEGIN}\s*(\{{.*?\}})\s*{CHECKPOINT_END}",
        re.DOTALL,
    )
    match = pattern.search(clean_stdout)
    if not match:
        return None, "No checkpoint markers found in opencode output."
    try:
        return json.loads(match.group(1)), None
    except json.JSONDecodeError as exc:
        return None, f"Checkpoint JSON was invalid: {exc}"


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


def render_list(title: str, items: list[str]) -> str:
    if not items:
        return f"# {title}\n\n- None recorded yet.\n"
    body = "\n".join(f"- {item}" for item in items)
    return f"# {title}\n\n{body}\n"


def memory_tags_json(tags: list[str]) -> str:
    return json.dumps(unique_preserve(tags), sort_keys=True)


def upsert_memory_entry(
    conn: sqlite3.Connection,
    run_id: str,
    entry_type: str,
    content: str,
    tags: list[str],
    importance: float,
    source_iteration: int,
    timestamp: str,
) -> None:
    entry = clean_text(content)
    if not entry:
        return
    conn.execute(
        """
        INSERT INTO memory_entries (
            run_id, entry_type, content, tags_json, importance, source_iteration, created_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id, entry_type, content) DO UPDATE SET
            tags_json = excluded.tags_json,
            importance = MAX(memory_entries.importance, excluded.importance),
            source_iteration = MAX(memory_entries.source_iteration, excluded.source_iteration),
            last_seen_at = excluded.last_seen_at
        """,
        (
            run_id,
            entry_type,
            entry,
            memory_tags_json(tags),
            importance,
            source_iteration,
            timestamp,
            timestamp,
        ),
    )
    # Also populate FTS5 index (INSERT OR IGNORE to handle re-inserts)
    row_id = conn.execute(
        "SELECT id FROM memory_entries WHERE run_id = ? AND entry_type = ? AND content = ?",
        (run_id, entry_type, entry),
    ).fetchone()
    if row_id:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO memory_fts (rowid, content, entry_type) VALUES (?, ?, ?)",
                (row_id["id"], entry, entry_type),
            )
        except sqlite3.OperationalError:
            pass  # FTS5 table may not exist on first call before migration


def touch_memory_entry(conn: sqlite3.Connection, entry_id: int, timestamp: str) -> None:
    """Update last_seen_at on a memory entry when it is retrieved and relevant."""
    conn.execute(
        "UPDATE memory_entries SET last_seen_at = ? WHERE id = ?",
        (timestamp, entry_id),
    )


def backfill_legacy_facts(conn: sqlite3.Connection, run_id: str) -> None:
    rows = conn.execute(
        "SELECT fact, first_iteration, created_at FROM facts WHERE run_id = ? ORDER BY id ASC",
        (run_id,),
    ).fetchall()
    for row in rows:
        upsert_memory_entry(
            conn,
            run_id,
            "fact",
            row["fact"],
            extract_tags(row["fact"]),
            MEMORY_IMPORTANCE["fact"],
            row["first_iteration"],
            row["created_at"],
        )
    conn.commit()


def persist_memory_entries(
    conn: sqlite3.Connection,
    run_id: str,
    iteration_number: int,
    checkpoint: dict[str, Any],
    timestamp: str,
) -> None:
    upsert_memory_entry(
        conn,
        run_id,
        "summary",
        checkpoint["summary"],
        extract_tags(checkpoint["summary"]),
        MEMORY_IMPORTANCE["summary"],
        iteration_number,
        timestamp,
    )
    if checkpoint["goal_progress"]:
        upsert_memory_entry(
            conn,
            run_id,
            "goal_progress",
            checkpoint["goal_progress"],
            extract_tags(checkpoint["goal_progress"]),
            MEMORY_IMPORTANCE["goal_progress"],
            iteration_number,
            timestamp,
        )

    typed_entries = {
        "fact": checkpoint["new_facts"],
        "decision": checkpoint["decisions"],
        "next_step": checkpoint["next_steps"],
        "risk": checkpoint["risks"],
    }
    for entry_type, values in typed_entries.items():
        for value in values:
            upsert_memory_entry(
                conn,
                run_id,
                entry_type,
                value,
                extract_tags(value),
                MEMORY_IMPORTANCE[entry_type],
                iteration_number,
                timestamp,
            )

    for artifact in checkpoint["artifacts"]:
        artifact_text = artifact["path"]
        if artifact["description"]:
            artifact_text = f"{artifact['path']}: {artifact['description']}"
        upsert_memory_entry(
            conn,
            run_id,
            "artifact",
            artifact_text,
            extract_tags(artifact["path"], artifact["description"]),
            MEMORY_IMPORTANCE["artifact"],
            iteration_number,
            timestamp,
        )


def prune_memory_entries(conn: sqlite3.Connection, run_id: str, current_iteration: int) -> None:
    """Delete old memory entries that are unlikely to be relevant again.

    Keeps:
    - The 120 most recent entries overall.
    - All entries with importance >= 4.0 (facts and high-value decisions).
    - The 3 most recent summaries.
    - All artifacts (files on disk are the permanent record).
    - Entries from the last 30 iterations regardless of importance.
    - Entries that have been touched (last_seen_at) in the last 10 iterations.

    This prevents unbounded token growth in the memory retrieval query.
    """
    cutoff_iteration = max(1, current_iteration - 50)

    # Delete entries that are old, low importance, and not recently seen
    conn.execute(
        """
        DELETE FROM memory_entries
        WHERE run_id = ?
          AND source_iteration < ?
          AND importance < 4.0
          AND entry_type NOT IN ('artifact', 'summary')
          AND id NOT IN (
              SELECT id FROM memory_entries
              WHERE run_id = ? AND entry_type = 'summary'
              ORDER BY source_iteration DESC LIMIT 3
          )
          AND id NOT IN (
              SELECT id FROM memory_entries
              WHERE run_id = ?
              ORDER BY source_iteration DESC, id DESC LIMIT 120
          )
          AND id NOT IN (
              SELECT id FROM memory_entries
              WHERE run_id = ?
              ORDER BY last_seen_at DESC
              LIMIT 50
          )
        """,
        (run_id, cutoff_iteration, run_id, run_id, run_id),
    )

    # Also cap the facts table to the most recent 200
    conn.execute(
        """
        DELETE FROM facts
        WHERE run_id = ?
          AND id NOT IN (
              SELECT id FROM facts
              WHERE run_id = ?
              ORDER BY id DESC LIMIT 200
          )
        """,
        (run_id, run_id),
    )

    # Also remove dangling FTS entries
    try:
        conn.execute(
            """
            DELETE FROM memory_fts WHERE rowid NOT IN (
                SELECT id FROM memory_entries WHERE run_id = ?
            )
            """,
            (run_id,),
        )
    except sqlite3.OperationalError:
        pass


def load_tags_json(value: str) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [clean_text(item) for item in parsed if clean_text(item)]


def score_memory_entry(row: sqlite3.Row, query_tags: list[str], current_iteration: int) -> float:
    entry_tags = load_tags_json(row["tags_json"])
    query_set = set(query_tags)
    overlap = len(query_set.intersection(entry_tags))
    text = row["content"].lower()
    direct_hits = sum(1 for tag in query_set if tag in text)
    age = max(0, current_iteration - row["source_iteration"])
    recency_bonus = max(0.0, 3.0 - (age * 0.2))
    return (overlap * 4.0) + min(direct_hits, 6) + float(row["importance"]) + recency_bonus


def build_query_tags(ctx: RunContext, latest_checkpoint: dict[str, Any] | None) -> list[str]:
    query_parts: list[Any] = [ctx.goal_text]
    if latest_checkpoint:
        query_parts.extend(
            [
                latest_checkpoint.get("summary"),
                latest_checkpoint.get("goal_progress"),
                *latest_checkpoint.get("next_steps", []),
                *latest_checkpoint.get("risks", []),
                *latest_checkpoint.get("files_touched", []),
            ]
        )
    return extract_tags(*query_parts, limit=24)


def goal_stop_after_iterations(goal_text: str) -> int:
    text = clean_text(goal_text)
    if not text:
        return CONTROLLER_DEFAULT_STOP_AFTER

    patterns = (
        r"after\s+(\d+)\s+(?:tries|try|iterations|iteration|attempts|attempts?)",
        r"stop\s+after\s+(\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        try:
            value = int(match.group(1))
        except ValueError:
            continue
        if value >= 1:
            return value
    return CONTROLLER_DEFAULT_STOP_AFTER


def retrieve_relevant_memory(
    conn: sqlite3.Connection,
    ctx: RunContext,
    latest_checkpoint: dict[str, Any] | None,
    limit: int = 18,
) -> tuple[list[sqlite3.Row], list[str], list[tuple[str, int]]]:
    backfill_legacy_facts(conn, ctx.run_id)
    rows = conn.execute(
        "SELECT id, entry_type, content, tags_json, importance, source_iteration, last_seen_at FROM memory_entries WHERE run_id = ? ORDER BY source_iteration DESC, id DESC LIMIT 480",
        (ctx.run_id,),
    ).fetchall()
    query_tags = build_query_tags(ctx, latest_checkpoint)

    # Compute FTS relevance for the query text
    query_text = " ".join(query_tags).replace("_", " ")
    fts_scores: dict[int, float] = {}
    if query_text.strip():
        try:
            fts_rows = conn.execute(
                "SELECT rowid, rank FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rank LIMIT 120",
                (query_text,),
            ).fetchall()
            # FTS5 rank: lower = more relevant; convert to 0..10 score
            if fts_rows:
                max_rank = max(abs(r["rank"]) for r in fts_rows) or 1.0
                for r in fts_rows:
                    fts_scores[r["rowid"]] = max(0.0, 5.0 * (1.0 - abs(r["rank"]) / max_rank))
        except sqlite3.OperationalError:
            pass  # FTS table may not exist or empty

    now = utc_now()
    scored_rows = []
    for row in rows:
        score = score_memory_entry(row, query_tags, max(1, ctx.iteration_count))
        # Add FTS boost (up to +5.0)
        fts_boost = fts_scores.get(row["id"], 0.0)
        score += fts_boost
        scored_rows.append((score, row))

    scored_rows.sort(key=lambda item: (item[0], item[1]["source_iteration"], item[1]["id"]), reverse=True)
    selected_rows = [row for score, row in scored_rows[:limit] if score > 0]
    if not selected_rows:
        selected_rows = [row for _score, row in scored_rows[: min(limit, 8)]]

    # Touch last_seen_at for entries that were retrieved and relevant
    for row in selected_rows:
        if row["importance"] >= 3.0:
            touch_memory_entry(conn, row["id"], now)

    topic_counter: Counter[str] = Counter()
    for row in selected_rows:
        for tag in load_tags_json(row["tags_json"]):
            if tag in set(query_tags):
                topic_counter[tag] += 2
            else:
                topic_counter[tag] += 1
    conn.commit()
    return selected_rows, query_tags, topic_counter.most_common(12)


def render_relevant_memory(entries: list[sqlite3.Row], query_tags: list[str]) -> str:
    lines = ["# Relevant Memory", ""]
    if query_tags:
        lines.append("## Query Tags")
        lines.append("")
        lines.append("- " + ", ".join(f"`{tag}`" for tag in query_tags))
        lines.append("")

    if not entries:
        lines.append("- No tagged memory available yet.")
        return "\n".join(lines) + "\n"

    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in entries:
        grouped[row["entry_type"]].append(row)

    for entry_type in ("fact", "decision", "next_step", "risk", "goal_progress", "artifact", "summary"):
        group_rows = grouped.get(entry_type)
        if not group_rows:
            continue
        lines.append(f"## {MEMORY_ENTRY_LABELS[entry_type]}")
        lines.append("")
        for row in group_rows:
            tags = load_tags_json(row["tags_json"])
            tag_suffix = f" [tags: {', '.join(tags[:6])}]" if tags else ""
            lines.append(f"- Iteration {row['source_iteration']}: {row['content']}{tag_suffix}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_memory_topics(topic_counts: list[tuple[str, int]]) -> str:
    lines = ["# Memory Topics", ""]
    if not topic_counts:
        lines.append("- No topic clusters available yet.")
        return "\n".join(lines) + "\n"
    for tag, count in topic_counts:
        lines.append(f"- `{tag}`: {count}")
    return "\n".join(lines) + "\n"


def checkpoint_text(checkpoint: dict[str, Any]) -> str:
    parts: list[str] = [
        checkpoint.get("summary", ""),
        checkpoint.get("goal_progress", ""),
        checkpoint.get("branch_family", ""),
        checkpoint.get("family_novelty", ""),
        checkpoint.get("evidence_quality", ""),
        checkpoint.get("competitiveness", ""),
        checkpoint.get("promotion_recommendation", ""),
    ]
    parts.extend(checkpoint.get("decisions", []))
    parts.extend(checkpoint.get("next_steps", []))
    parts.extend(checkpoint.get("risks", []))
    parts.extend(checkpoint.get("files_touched", []))
    parts.extend(artifact.get("path", "") for artifact in checkpoint.get("artifacts", []))
    return "\n".join(clean_text(part) for part in parts if clean_text(part)).lower()


def infer_branch_family(checkpoint: dict[str, Any]) -> str:
    """Infer the branch family from checkpoint text using config-driven modules."""
    try:
        from autocode_config import get_module_keywords
        families = get_module_keywords()
    except Exception:
        families = []
    if not families:
        families = [
            ("core", "Main code", ["src", "core", "main", "lib"]),
            ("tests", "Tests", ["test", "spec", "check"]),
            ("docs", "Documentation", ["doc", "readme", "guide"]),
            ("scripts", "Scripts/tools", ["script", "tool", "cli"]),
            ("config", "Config/deploy", ["config", "deploy", "ci", "docker"]),
        ]
    text = checkpoint_text(checkpoint)
    for family, _desc, hints in families:
        if any(hint in text for hint in hints):
            return family
    return "unknown"


def infer_family_novelty(checkpoint: dict[str, Any]) -> str:
    text = checkpoint_text(checkpoint)
    if any(phrase in text for phrase in ("pivoted hard", "hard structural pivot", "materially different", "new module", "new architecture")):
        return "major"
    if any(phrase in text for phrase in ("new family", "new approach", "new pattern", "real structural pivot", "new technique")):
        return "moderate"
    return "minor"


def infer_evidence_quality(checkpoint: dict[str, Any]) -> str:
    text = checkpoint_text(checkpoint)
    if any(term in text for term in ("e2e", "integration-test", "build-check", "ci-pass", "deployed", "production")):
        return "strategy_level"
    if any(term in text for term in ("unit-test", "typecheck", "lint-pass", "coverage", "validation", "regression")):
        return "model_level"
    return "artifact_only"


def infer_competitiveness(checkpoint: dict[str, Any]) -> str:
    text = checkpoint_text(checkpoint)
    if any(
        phrase in text
        for phrase in (
            "non-competitive",
            "not production-ready",
            "not deployable",
            "breaking change",
            "regression introduced",
            "should be abandoned",
            "fails the quality gate",
            "fails typecheck",
            "fails tests",
            "far below",
            "performance degraded",
            "underperforms",
            "underperformed",
            "collapsed",
            "not build-ready",
            "below the quality gate",
        )
    ):
        return "non_competitive"
    if any(phrase in text for phrase in ("promising enough", "deployable", "outperform", "promotable", "production-ready", "merge-ready")):
        return "promising"
    if any(
        phrase in text
        for phrase in (
            "clears the quality gate",
            "tests pass",
            "typechecks pass",
            "materially improves",
            "directionally better",
            "interesting but",
            "first working version",
            "useful for review",
        )
    ):
        return "marginal"
    return "unknown"


def infer_promotion_recommendation(checkpoint: dict[str, Any]) -> str:
    text = checkpoint_text(checkpoint)
    competitiveness = checkpoint.get("competitiveness") or infer_competitiveness(checkpoint)
    if any(phrase in text for phrase in ("abandon run", "stop this run", "run is exhausted")):
        return "abandon_run"
    if any(phrase in text for phrase in ("abandon", "should not be deployed", "should not be merged", "pivot away", "do not revisit")):
        return "abandon_family"
    if competitiveness in {"promising", "promotable"}:
        return "promote" if checkpoint.get("evidence_quality") == "strategy_level" else "hold"
    return "hold"


def compact_summary(text: str, limit: int = 180) -> str:
    value = clean_text(text)
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def recent_family_diversity(entries: list[dict[str, Any]]) -> dict[str, Any]:
    recent_entries = entries[:CONTROLLER_DIVERSITY_WINDOW]
    recent_families = [clean_text(entry.get("branch_family")) or "unknown" for entry in recent_entries]
    if not recent_families:
        return {
            "window": 0,
            "unique_count": 0,
            "dominant_family": "unknown",
            "dominant_count": 0,
            "dominant_share": 0.0,
            "same_family_streak": 0,
            "low_diversity": False,
        }

    family_counts = Counter(recent_families)
    dominant_family, dominant_count = family_counts.most_common(1)[0]
    first_family = recent_families[0]
    same_family_streak = 0
    for family in recent_families:
        if family != first_family:
            break
        same_family_streak += 1

    dominant_share = dominant_count / len(recent_families)
    low_diversity = len(recent_families) >= 3 and (
        dominant_share >= CONTROLLER_DOMINANT_FAMILY_SHARE
        or same_family_streak >= CONTROLLER_REPEAT_FAMILY_STREAK
        or (len(recent_families) >= 4 and len(family_counts) <= 2)
    )
    return {
        "window": len(recent_families),
        "unique_count": len(family_counts),
        "dominant_family": dominant_family,
        "dominant_count": dominant_count,
        "dominant_share": dominant_share,
        "same_family_streak": same_family_streak,
        "low_diversity": low_diversity,
    }


def novelty_angle_suggestions() -> list[str]:
    return [
        "Backend architecture: modular decomposition, event-driven design, CQRS, queue-based decoupling, or GraphQL federation.",
        "Frontend innovation: server components, streaming SSR, optimistic updates, edge caching, or WebSocket real-time sync.",
        "Database optimization: read replicas, materialized views, denormalization, TTL indexes, or MongoDB aggregation pipelines.",
        "Testing strategy: contract testing, property-based testing, snapshot testing, visual regression, or load/stress testing.",
        "DevOps and deployment: zero-downtime migrations, Blue/Green deploy, canary releases, or Lambda cold-start mitigation.",
        "Security hardening: rate limiting, WAF rules, input sanitization, audit logging, or secrets rotation.",
        "Performance optimization: Redis caching, CDN tuning, image optimization, lazy loading, or bundle splitting.",
        "Developer experience: monorepo tooling, code generation, API documentation, or automated migration pipelines.",
    ]


def load_kb_yaml() -> dict[str, Any]:
    """Load the machine-readable knowledge base YAML file.

    Returns a dict with structured KB fields, or an empty dict if the file
    cannot be loaded. The caller should handle missing keys gracefully.
    """
    kb_path = ROOT / "knowledge" / "autopilot_kb.yaml"
    if not kb_path.exists():
        return {}
    try:
        with open(kb_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except (yaml.YAMLError, OSError):
        return {}


def build_controller_state(conn: sqlite3.Connection, ctx: RunContext) -> str:
    stop_after = goal_stop_after_iterations(ctx.goal_text)
    rows = conn.execute(
        "SELECT iteration_number, checkpoint_json FROM iterations WHERE run_id = ? ORDER BY iteration_number DESC LIMIT 24",
        (ctx.run_id,),
    ).fetchall()
    entries: list[dict[str, Any]] = []
    for row in rows:
        checkpoint = normalize_checkpoint(json.loads(row["checkpoint_json"]), fallback_status="continue", fallback_summary="")
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
        recommendation = "Force a hard pivot. Repeating the same family is no longer allowed without a concrete structural reason."
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
        lines.append("- Fresh-angle mode is optional right now, but the next step should still beat at least one novel alternative.")
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
    lines.append("- Do not revisit an exhausted family unless the next idea is explicitly major and structurally different.")
    lines.append("- If you choose `continue`, compare the shortlist against at least one adjacent-new and one high-novelty alternative before deciding.")
    lines.append("- If you appear stuck, invent a completely new approach instead of stopping. This is an unbounded perpetual loop.")
    lines.append("- Before spending another try, name the exact code weakness being targeted, why the chosen approach should fix it, and what test result would falsify that belief.")
    lines.append("- Prefer one deeper, well-validated step over several shallow nearby variants.")
    lines.append("- Respect repo conventions: all UI text via `t('...')`, no `any`, dedup before external side effects.")
    lines.append("")
    return "\n".join(lines)


def resolve_branch_count(args: argparse.Namespace, latest_checkpoint: dict[str, Any] | None) -> int:
    """Return the number of parallel branches to run for the next iteration.

    Priority:
    1. --force-parallel N  (explicit CLI override, non-zero)
    2. checkpoint["parallel_branches"]  (agent's own request, clamped to --parallel-branches cap)
    3. 1  (serial fallback)
    """
    cap = max(1, args.parallel_branches)
    if getattr(args, "force_parallel", 0) > 0:
        return min(args.force_parallel, cap)
    if latest_checkpoint is not None:
        agent_req = latest_checkpoint.get("parallel_branches", 1)
        if isinstance(agent_req, int) and agent_req > 1:
            return min(agent_req, cap)
    return 1


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
        prior = normalize_checkpoint(json.loads(row["checkpoint_json"]), fallback_status="continue", fallback_summary="")
        if prior.get("competitiveness") == "non_competitive":
            streak += 1
            continue
        break
    if streak >= stop_after:
        return True, f"{streak} consecutive non-competitive iterations"
    return False, ""


def checkpoint_signals_goal_complete(checkpoint: dict[str, Any]) -> bool:
    text = checkpoint_text(checkpoint)
    return any(
        phrase in text
        for phrase in (
            "overall goal complete",
            "goal is complete",
            "success criteria met",
            "run complete",
        )
    )


def coerce_done_checkpoint(checkpoint: dict[str, Any]) -> tuple[dict[str, Any], str]:
    if checkpoint.get("status") != "done":
        return checkpoint, ""

    # A promotion advisory is an intermediate milestone in a perpetual run, not a terminal event.
    # The run must continue regardless of whether the agent set goal_complete=true.
    if checkpoint.get("promotion_recommendation") == "promote":
        reason = (
            "Checkpoint used status 'done' with a promotion advisory. "
            "Surfacing a deployment candidate is a milestone, not the end of a perpetual improvement run. "
            "Coerced to 'continue' so the next hypothesis in the queue can be pursued."
        )
        checkpoint["status"] = "continue"
        checkpoint["goal_complete"] = False
        if reason not in checkpoint["risks"]:
            checkpoint["risks"].append(reason)
        decision = "After surfacing a deployment advisory, always return status='continue' and proceed to the next hypothesis."
        if decision not in checkpoint["decisions"]:
            checkpoint["decisions"].append(decision)
        return checkpoint, reason

    # Primary signal: explicit boolean set by the agent
    if checkpoint.get("goal_complete") is True:
        return checkpoint, ""

    # Secondary (legacy): phrase-matching in checkpoint text
    if checkpoint_signals_goal_complete(checkpoint):
        # Backfill the boolean so downstream readers don't need phrase-matching
        checkpoint["goal_complete"] = True
        return checkpoint, ""

    reason = (
        "Checkpoint used status 'done' but did not set goal_complete=true; "
        "coerced to 'continue' so the run can pivot beyond the exhausted local branch."
    )
    checkpoint["status"] = "continue"
    if reason not in checkpoint["risks"]:
        checkpoint["risks"].append(reason)
    if checkpoint.get("promotion_recommendation") == "abandon_family":
        decision = "Treat exhausted-family conclusions as run-level continue signals, not final completion."
        if decision not in checkpoint["decisions"]:
            checkpoint["decisions"].append(decision)
    return checkpoint, reason


def retry_backoff_seconds(args: argparse.Namespace, attempt_number: int) -> int:
    if args.retry_backoff_seconds <= 0:
        return 0
    delay = args.retry_backoff_seconds * (2 ** max(0, attempt_number - 1))
    return min(delay, args.max_retry_backoff_seconds)


def classify_transient_failure(
    exit_code: int,
    stdout_text: str,
    stderr_text: str,
    checkpoint: dict[str, Any],
) -> tuple[bool, str]:
    if checkpoint["status"] != "failed":
        return False, ""

    combined_text = "\n".join(
        [
            checkpoint["summary"],
            stdout_text,
            stderr_text,
            *checkpoint["risks"],
        ]
    )
    if exit_code == 124:
        return True, "iteration timed out"
    if "No checkpoint markers found in opencode output." in checkpoint["summary"]:
        return True, "missing checkpoint in agent output"
    if "Checkpoint JSON was invalid" in checkpoint["summary"]:
        return True, "invalid checkpoint JSON"

    for pattern in NON_TRANSIENT_FAILURE_PATTERNS:
        if pattern.search(combined_text):
            return False, ""
    for pattern in TRANSIENT_FAILURE_PATTERNS:
        if pattern.search(combined_text):
            return True, pattern.pattern
    if exit_code != 0 and not combined_text.strip():
        return True, "non-zero exit with empty output"
    return False, ""


def render_iteration_table(rows: list[sqlite3.Row]) -> str:
    """Render a compact Markdown table of iterations for agent navigation."""
    if not rows:
        return "# Iteration History\n\n- No iterations recorded yet.\n"

    lines = [
        "# Iteration History",
        "",
        "| # | Status | Family | Novelty | Competitive | Evidence | Promotion | Summary |",
    ]
    for row in reversed(rows):  # chronological order
        cp = normalize_checkpoint(
            json.loads(row["checkpoint_json"]),
            fallback_status=row["status"],
            fallback_summary=row["summary"],
        )
        family = cp["branch_family"] or "unknown"
        novelty = cp["family_novelty"] or "-"
        competitive = cp["competitiveness"] or "-"
        evidence = cp["evidence_quality"] or "-"
        promo = cp["promotion_recommendation"] or "-"
        summary = compact_summary(cp["summary"], 80)
        lines.append(
            f"| {row['iteration_number']} | {row['status']} | {family} | {novelty} | {competitive} | {evidence} | {promo} | {summary} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_iteration_table(rows: list[sqlite3.Row]) -> str:
    """Render a compact Markdown table of iterations for agent navigation."""
    if not rows:
        return "# Iteration History\n\n- No iterations recorded yet.\n"

    lines = [
        "# Iteration History",
        "",
        "| # | Status | Family | Novelty | Competitive | Evidence | Promotion | Summary |",
        "|---|--------|--------|---------|-------------|----------|-----------|---------|",
    ]
    for row in reversed(rows):  # chronological order
        cp = normalize_checkpoint(
            json.loads(row["checkpoint_json"]),
            fallback_status=row["status"],
            fallback_summary=row["summary"],
        )
        family = cp["branch_family"] or "unknown"
        novelty = cp["family_novelty"] or "-"
        competitive = cp["competitiveness"] or "-"
        evidence = cp["evidence_quality"] or "-"
        promo = cp["promotion_recommendation"] or "-"
        summary = compact_summary(cp["summary"], 80)
        lines.append(
            f"| {row['iteration_number']} | {row['status']} | {family} | {novelty} | {competitive} | {evidence} | {promo} | {summary} |"
        )
    lines.append("")
    return "\n".join(lines)


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
    relevant_entries, query_tags, topic_counts = retrieve_relevant_memory(conn, ctx, latest_checkpoint)
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
        f"# Run State\n",
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


def build_iteration_prompt(ctx: RunContext, iteration_number: int, resume_session_id: str | None = None) -> str:
    prefix = ""
    if resume_session_id:
        prefix = (
            "Recovery mode: the outer autopilot runner was interrupted after creating this iteration's opencode session. "
            "Continue the same unfinished attempt in the existing session, reuse any in-flight repository state or running trainings that are still relevant, "
            "and avoid restarting work that was already launched unless the prior attempt is clearly unusable.\n\n"
        )
    session_line = (f"SESSION GOAL (this iteration):\n{ctx.session_goal}\n\n"
                  "This session goal overrides or refines the permanent goal for this iteration. "
                  "Prioritize completing the session goal over broad exploration.\n\n") if ctx.session_goal else ""
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
        "- Do not ask the user questions. Use status `blocked` if a human decision is truly required.\n"
    )


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

    raw_checkpoint, checkpoint_error = extract_checkpoint(stdout_text)
    fallback_status = "continue" if exit_code == 0 else "failed"
    fallback_summary = "Iteration completed without a valid checkpoint."
    if checkpoint_error:
        fallback_summary = checkpoint_error
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
        f"# Autopilot Status\n",
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


def cleanup_training_scratch(iteration_number: int) -> None:
    """Remove temporary autopilot scratch files.

    Removes old tmp/autopilot_scratch/iter_* directories beyond the last 3.
    """
    import shutil

    removed_dirs = 0
    removed_files = 0

    scratch_dir_names = {"autopilot_scratch"}

    # Root-level autopilot_scratch (may be created by scripts run from repo root)
    root_cb = ROOT / "tmp" / "autopilot_scratch"
    if root_cb.is_dir():
        shutil.rmtree(root_cb, ignore_errors=True)
        removed_dirs += 1

    # Old tmp/autopilot_scratch/iter_* directories
    scratch_root = ROOT / "tmp" / "autopilot_scratch"
    if scratch_root.is_dir():
        for item in scratch_root.iterdir():
            if item.is_dir() and item.name.startswith("iter_") and item.name != f"iter_{iteration_number}":
                shutil.rmtree(item, ignore_errors=True)
                removed_dirs += 1

    if removed_dirs or removed_files:
        _log(f"iter={iteration_number} cleanup: removed {removed_dirs} scratch dirs, {removed_files} scratch files")
    else:
        _log(f"iter={iteration_number} cleanup: nothing to remove")


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


def render_report(conn: sqlite3.Connection, ctx: RunContext) -> str:
    """Render a full Markdown run report from the SQLite database."""
    rows = conn.execute(
        "SELECT iteration_number, status, started_at, finished_at, summary, checkpoint_json FROM iterations WHERE run_id = ? ORDER BY iteration_number ASC",
        (ctx.run_id,),
    ).fetchall()
    artifact_rows = conn.execute(
        "SELECT iteration_number, path, description FROM artifacts WHERE run_id = ? ORDER BY iteration_number ASC, id ASC",
        (ctx.run_id,),
    ).fetchall()
    fact_rows = conn.execute(
        "SELECT fact, first_iteration FROM facts WHERE run_id = ? ORDER BY first_iteration ASC, id ASC",
        (ctx.run_id,),
    ).fetchall()

    lines: list[str] = [
        f"# Autopilot Run Report",
        "",
        f"- **Run id**: `{ctx.run_id}`",
        f"- **Status**: `{ctx.status}`",
        f"- **Iterations**: `{ctx.iteration_count}`",
        f"- **Generated**: `{utc_now()}`",
        "",
        "---",
        "",
        "## Iteration Summary",
        "",
        "| # | Status | Family | Novelty | Competitive | Evidence | Promotion | Summary |",
        "|---|--------|--------|---------|-------------|----------|-----------|---------|",
    ]
    for row in rows:
        cp = normalize_checkpoint(
            json.loads(row["checkpoint_json"]),
            fallback_status=row["status"],
            fallback_summary=row["summary"],
        )
        lines.append(
            f"| {row['iteration_number']} | {row['status']} | {cp['branch_family'] or '-'} "
            f"| {cp['family_novelty'] or '-'} | {cp['competitiveness'] or '-'} "
            f"| {cp['evidence_quality'] or '-'} | {cp['promotion_recommendation'] or '-'} "
            f"| {compact_summary(cp['summary'], 90)} |"
        )
    lines.append("")

    # Per-iteration detail
    lines += ["---", "", "## Iteration Details", ""]
    for row in rows:
        cp = normalize_checkpoint(
            json.loads(row["checkpoint_json"]),
            fallback_status=row["status"],
            fallback_summary=row["summary"],
        )
        lines += [
            f"### Iteration {row['iteration_number']} — {row['status']}",
            "",
            f"- **Started**: `{row['started_at']}` / **Finished**: `{row['finished_at']}`",
            f"- **Family**: `{cp['branch_family'] or 'unknown'}` · **Novelty**: `{cp['family_novelty']}` · **Competitive**: `{cp['competitiveness']}` · **Evidence**: `{cp['evidence_quality']}`",
            f"- **Promotion**: `{cp['promotion_recommendation']}`",
            f"- **Goal complete**: `{cp.get('goal_complete', False)}`",
            "",
            f"**Summary**: {cp['summary']}",
            "",
        ]
        if cp["goal_progress"]:
            lines += [f"**Goal Progress**: {cp['goal_progress']}", ""]
        if cp["decisions"]:
            lines.append("**Decisions**:")
            lines.extend(f"- {d}" for d in cp["decisions"])
            lines.append("")
        if cp["next_steps"]:
            lines.append("**Next Steps**:")
            lines.extend(f"- {s}" for s in cp["next_steps"])
            lines.append("")
        if cp["risks"]:
            lines.append("**Risks**:")
            lines.extend(f"- {r}" for r in cp["risks"])
            lines.append("")
        if cp["new_facts"]:
            lines.append("**New Facts**:")
            lines.extend(f"- {f}" for f in cp["new_facts"])
            lines.append("")

    # Artifacts
    lines += ["---", "", "## Artifacts", ""]
    if artifact_rows:
        lines += [
            "| Iteration | Path | Description |",
            "|-----------|------|-------------|",
        ]
        for ar in artifact_rows:
            lines.append(f"| {ar['iteration_number']} | `{ar['path']}` | {ar['description']} |")
        lines.append("")
    else:
        lines += ["- None recorded.", ""]

    # Durable facts
    lines += ["---", "", "## Durable Facts", ""]
    if fact_rows:
        for fr in fact_rows:
            lines.append(f"- (iter {fr['first_iteration']}) {fr['fact']}")
        lines.append("")
    else:
        lines += ["- None recorded.", ""]

    # Goal
    lines += ["---", "", "## Goal", "", ctx.goal_text, ""]

    return "\n".join(lines)


def cmd_export_kb(args: argparse.Namespace) -> int:
    """Export autopilot_kb.yaml to a dated Markdown snapshot."""
    kb_path = ROOT / "knowledge" / "autopilot_kb.yaml"
    if not kb_path.exists():
        print("autopilot_kb.yaml not found. Nothing to export.", file=sys.stderr)
        return 1

    data = load_kb_yaml()
    if not data:
        print("autopilot_kb.yaml is empty or unparseable.", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)

    # Determine output path
    if args.output:
        output_path = args.output
    else:
        archive_dir = ROOT / "knowledge" / "archive" / "kb_snapshots"
        archive_dir.mkdir(parents=True, exist_ok=True)
        date_suffix = now.strftime("%Y-%m-%dT%H%M%SZ") if not args.no_date_suffix else "latest"
        output_path = archive_dir / f"autopilot_kb_{date_suffix}.md"

    lines: list[str] = [
        f"# Autopilot Knowledge Base — Snapshot",
        f"",
        f"Generated: {now.isoformat(timespec='seconds')}",
        f"Source: `autopilot_kb.yaml` (schema v{data.get('schema_version', '?')})",
        f"",
        f"---",
        f"",
    ]

    # Current Best Challenger
    bc = data.get("current_best_challenger", {})
    if bc:
        lines.append("## Current Best Challenger")
        lines.append("")
        lines.append(f"**{bc.get('name', '?')}** (last updated: {bc.get('last_updated', '?')})")
        lines.append(f"- Architecture: {bc.get('architecture', '?')}")
        if bc.get("key_innovation"):
            lines.append(f"- Key innovation: {bc['key_innovation']}")
        lines.append(f"- Leakage: {bc.get('leakage_status', '?')}")
        lines.append(f"- File: `{bc.get('file', '?')}`")
        if bc.get("promotion_note"):
            lines.append(f"- Note: {bc['promotion_note']}")
        lines.append("")
        perf = bc.get("performance", {})
        if perf:
            lines.append("| Window | Profit | Max DD | Trades | WR |")
            lines.append("|--------|--------|--------|--------|-----|")
            for name, metric in perf.items():
                if isinstance(metric, dict):
                    lines.append(
                        f"| {name} | {metric.get('profit_pct', '?')}% | {metric.get('max_dd_pct', '?')}% "
                        f"| {metric.get('trades', '?')} | {metric.get('win_rate_pct', '?')}% |"
                    )
            lines.append("")

    # Production Baselines
    baselines = data.get("production_baselines", [])
    if baselines:
        lines.append("## Production Baselines")
        lines.append("")
        lines.append("| Strategy | Timeframe | Status | Notes |")
        lines.append("|----------|-----------|--------|-------|")
        for b in baselines:
            lines.append(f"| `{b.get('name', '?')}` | {b.get('timeframe', '?')} | {b.get('status', '?')} | {b.get('notes', '')} |")
        lines.append("")

    # Hypothesis Queue
    queue = data.get("hypothesis_queue", [])
    if queue:
        lines.append("## Hypothesis Queue")
        lines.append("")
        for h in queue:
            lines.append(f"{h.get('rank', '?')}. **{h.get('title', '?')}**")
            if h.get("description"):
                lines.append(f"   - {h['description']}")
            if h.get("target"):
                lines.append(f"   - Target: {h['target']}")
            lines.append(f"   - Status: {h.get('status', 'open')}")
        lines.append("")

    # Confirmed Wins
    wins = data.get("confirmed_wins", [])
    if wins:
        lines.append("## Confirmed Wins")
        lines.append("")
        for w in wins:
            lines.append(f"### {w.get('date', '?')} — {w.get('title', '?')}")
            lines.append(f"- Type: {w.get('type', '?')}")
            if w.get("key_insight"):
                lines.append(f"- Insight: {w['key_insight']}")
            if w.get("files"):
                lines.append(f"- Files: {', '.join(w['files'])}")
            wmetrics = w.get("metrics", {})
            if wmetrics and isinstance(wmetrics, dict):
                for k, v in wmetrics.items():
                    if isinstance(v, dict):
                        lines.append(f"  - {k}: {v}")
                    else:
                        lines.append(f"  - {k}: {v}")
            lines.append("")

    # Confirmed Dead Ends
    deads = data.get("confirmed_dead_ends", [])
    if deads:
        lines.append("## Confirmed Dead Ends")
        lines.append("")
        for d in deads:
            lines.append(f"### {d.get('date', '?')} — {d.get('family', '?')}")
            lines.append(f"- Reason: {d.get('reason', '?')}")
            if d.get("tried_variants"):
                lines.append(f"- Tried: {', '.join(d['tried_variants'][:8])}")
                if len(d["tried_variants"]) > 8:
                    lines.append(f"  ... and {len(d['tried_variants']) - 8} more")
            lines.append("")

    # Key Discoveries
    discoveries = data.get("key_structural_discoveries", [])
    if discoveries:
        lines.append("## Key Structural Discoveries")
        lines.append("")
        for d in discoveries:
            lines.append(f"- **{d.get('description', '?')}**")
            if d.get("evidence"):
                lines.append(f"  - Evidence: {d['evidence']}")
            if d.get("date"):
                lines.append(f"  - Date: {d['date']}")
        lines.append("")

    # Exhausted families
    exhausted = data.get("exhausted_families", [])
    if exhausted:
        lines.append("## Exhausted Families")
        lines.append("")
        for f in exhausted:
            f_name = f.get("family", "") if isinstance(f, dict) else str(f)
            lines.append(f"- {f_name}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"*This snapshot was generated from `autopilot_kb.yaml` on {now.isoformat(timespec='seconds')}.*")
    lines.append(f"*Edit the YAML file, not this markdown.*")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"KB snapshot written to: {output_path}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    runtime_dir = args.runtime_dir.resolve()
    conn = ensure_runtime(runtime_dir)
    if args.run_id:
        ctx = load_run(conn, args.run_id)
    else:
        run_id = active_run_id(runtime_dir)
        if not run_id:
            print("No active run. Use --run-id or start a run first.", file=sys.stderr)
            return 1
        ctx = load_run(conn, run_id)

    report = render_report(conn, ctx)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        print(f"Report written to: {args.output}")
    else:
        print(report)
    return 0


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

    # Seed latest_checkpoint for the first resolve_branch_count call
    latest_row = conn.execute(
        "SELECT checkpoint_json FROM iterations WHERE run_id = ? ORDER BY iteration_number DESC LIMIT 1",
        (ctx.run_id,),
    ).fetchone()
    prev_checkpoint: dict[str, Any] | None = normalize_checkpoint(
        json.loads(latest_row["checkpoint_json"]), "continue", ""
    ) if latest_row else None

    # ── Meta-controller state: dynamically adjusted loop parameters ──────
    meta_overrides: dict[str, Any] = {}

    def _apply_meta_overrides() -> None:
        if not meta_overrides:
            return
        for key, value in meta_overrides.items():
            if hasattr(args, key):
                setattr(args, key, value)

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

        # ── Apply meta-controller overrides before this cycle ────────────
        if meta_overrides:
            _apply_meta_overrides()
            override_log = " | ".join(f"{k}={v}" for k, v in meta_overrides.items())
            _log(f"meta-controller active: {override_log}")

        # ── parallel branch count: agent decides, controller caps ────────
        # Each branch gets its own iteration number and runs concurrently.
        # After all branches finish, we pick the best checkpoint for controller
        # evaluation (highest competitiveness rank, then most recent).
        num_branches = resolve_branch_count(args, prev_checkpoint)
        _log(f"══════════════════════════════════════════════════")
        _log(f"ITERATION CYCLE {offset}/{args.max_iterations} | run={ctx.run_id} | branches={num_branches} | starting at iteration {base_iteration_number}")
        _log(f"══════════════════════════════════════════════════")

        branch_results: list[tuple[int, int, int, str, str, str, dict[str, Any], int]] = []
        # (iteration_number, attempt_number, exit_code, started_at, finished_at, stdout, checkpoint, transient_failures)

        def _run_branch(branch_index: int) -> tuple[int, int, int, str, str, str, dict[str, Any], int]:
            iter_num = base_iteration_number + branch_index
            attempt_num = 1
            # Only the first branch of the first offset gets the recovery session
            rsid = resume_session_id if (branch_index == 0 and offset == 1) else None
            transient_failures_b = 0

            # Re-read session goal file before each iteration (allows mid-run updates)
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
                ec, sout, serr, cp = run_iteration(conn, args, runtime_dir, ctx, iter_num, attempt_num, resume_session_id=rsid)
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
                    _log(f"branch={branch_index} iter={iter_num} attempt={attempt_num} — transient failure, retrying in {backoff}s ({retry_reason})")
                    if backoff:
                        time.sleep(backoff)
                    attempt_num += 1
                    rsid = None
                    continue
                _log(f"━━━ BRANCH {branch_index} | iteration {iter_num} DONE | status={cp['status']} competitiveness={cp.get('competitiveness','-')} evidence={cp.get('evidence_quality','-')} ━━━")
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
            return 0

        if not branch_results:
            print("All branches failed to produce results.", file=sys.stderr)
            return 1

        # Persist all branch iterations and pick best checkpoint for controller
        COMPETITIVENESS_RANK = {"promotable": 4, "promising": 3, "marginal": 2, "non_competitive": 1, "unknown": 0, "": 0}
        best_checkpoint: dict[str, Any] | None = None
        for iter_num, attempt_num, ec, s_at, f_at, _sout, cp, tf in branch_results:
            ctx = persist_iteration(conn, runtime_dir, ctx, iter_num, attempt_num, tf, s_at, f_at, ec, cp)
            if best_checkpoint is None or (
                COMPETITIVENESS_RANK.get(cp.get("competitiveness", ""), 0)
                > COMPETITIVENESS_RANK.get(best_checkpoint.get("competitiveness", ""), 0)
            ):
                best_checkpoint = cp

        checkpoint = best_checkpoint  # type: ignore[assignment]
        assert checkpoint is not None
        prev_checkpoint = checkpoint  # used by resolve_branch_count next iteration

        # ── Meta-controller: analyze recent performance, adjust params ──
        if ctx.iteration_count >= 3:
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
                if mc_advice.changed:
                    for param, (old, new) in mc_advice.changed.items():
                        meta_overrides[param] = new
                        _log(f"meta-controller: {param} {old} → {new} ({mc_advice.reasoning})")
                # Log performance
                _log(f"meta-controller: {mc_advice.performance_summary}")
            except Exception as exc:
                _log(f"meta-controller error: {exc}")

        # ── Meta-autopilot cycle: improve autopilot components ──────────
        if _META_AVAILABLE and ctx.iteration_count >= 5 and ctx.iteration_count % 10 == 0:
            try:
                meta_ctrl = MetaController()
                meta_ctrl.get_or_create_level(0)  # autopilot-level components
                meta_ctrl.get_or_create_level(1)  # meta-level components
                meta_ctrl.auto_discover()
                cycle_result = meta_ctrl.run_cycle(1, metrics={
                    "iteration_efficiency": 1.0,
                    "improvement_rate": checkpoint.get("competitiveness", "unknown") in ("promising", "promotable"),
                    "novelty_diversity": 1.0 if checkpoint.get("family_novelty") in ("moderate", "major") else 0.0,
                })
                _log(f"meta-autopilot cycle: level=1 advanced={cycle_result['experiments_advanced']} "
                     f"started={cycle_result['experiments_started']} "
                     f"stagnation={cycle_result['stagnation_score']:.3f}")
                if cycle_result.get("escalated"):
                    _log(f"meta-autopilot ESCALATION: {cycle_result.get('reason', '')}")
            except Exception as exc:
                _log(f"meta-autopilot error: {exc}")

        finished_at = utc_now()
        forced_stop, forced_reason = controller_should_stop(conn, ctx, checkpoint)
        if forced_stop:
            _log(f"CONTROLLER STOP: {forced_reason}")
            advisory = render_promotion_advisory(conn, ctx)
            if advisory:
                print(advisory, flush=True)
            else:
                _log("No promotable candidates found in this run.")
            return 0

        _log(f"══ CYCLE {offset} COMPLETE | run={ctx.run_id} | iterations={base_iteration_number}+{num_branches-1} | status={checkpoint['status']} competitiveness={checkpoint.get('competitiveness','-')} promotion={checkpoint.get('promotion_recommendation','-')} ══")
        if checkpoint.get("next_steps"):
            _log("next steps:")
            for step in checkpoint["next_steps"]:
                print(f"  → {step}", flush=True)

        # Surface promotion advisory immediately if this cycle produced a promotable candidate
        if checkpoint.get("promotion_recommendation") == "promote":
            advisory = render_promotion_advisory(conn, ctx)
            if advisory:
                print(advisory, flush=True)

        if checkpoint["status"] in {"done", "blocked", "failed"}:
            _log(f"Run ending with status={checkpoint['status']}")
            advisory = render_promotion_advisory(conn, ctx)
            if advisory:
                print(advisory, flush=True)
            elif checkpoint["status"] == "done":
                _log("No promotable candidates found in this run.")
            return 0 if checkpoint["status"] == "done" else 1
        if offset < args.max_iterations and args.sleep_seconds:
            _log(f"Sleeping {args.sleep_seconds}s before next cycle...")
            time.sleep(args.sleep_seconds)

    # Always print promotion advisory at run end
    advisory = render_promotion_advisory(conn, ctx)
    if advisory:
        print(advisory, flush=True)
    else:
        _log("No promotable candidates found in this run.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
