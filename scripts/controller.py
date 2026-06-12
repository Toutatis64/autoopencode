from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from typing import Any

import yaml

from scripts.autocode_config import ROOT
from scripts.checkpoint import checkpoint_text, normalize_checkpoint
from scripts.memory import _log, clean_text, compact_summary

CONTROLLER_EXHAUST_FAMILY_AFTER = 3
CONTROLLER_HARD_PIVOT_AFTER = 5
CONTROLLER_DEFAULT_STOP_AFTER = 100
CONTROLLER_DIVERSITY_WINDOW = 6
CONTROLLER_REPEAT_FAMILY_STREAK = 3
CONTROLLER_DOMINANT_FAMILY_SHARE = 0.6

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


def render_list(title: str, items: list[str]) -> str:
    if not items:
        return f"# {title}\n\n- None recorded yet.\n"
    body = "\n".join(f"- {item}" for item in items)
    return f"# {title}\n\n{body}\n"


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


def resolve_branch_count(args: argparse.Namespace, latest_checkpoint: dict[str, Any] | None) -> int:
    cap = max(1, args.parallel_branches)
    if getattr(args, "force_parallel", 0) > 0:
        return min(args.force_parallel, cap)
    if latest_checkpoint is not None:
        agent_req = latest_checkpoint.get("parallel_branches", 1)
        if isinstance(agent_req, int) and agent_req > 1:
            return min(agent_req, cap)
    return 1


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
        decision = (
            "After surfacing a deployment advisory, always return status='continue' and proceed to the next hypothesis."
        )
        if decision not in checkpoint["decisions"]:
            checkpoint["decisions"].append(decision)
        return checkpoint, reason

    if checkpoint.get("goal_complete") is True:
        return checkpoint, ""

    if checkpoint_signals_goal_complete(checkpoint):
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
    if not rows:
        return "# Iteration History\n\n- No iterations recorded yet.\n"

    lines = [
        "# Iteration History",
        "",
        "| # | Status | Family | Novelty | Competitive | Evidence | Promotion | Summary |",
    ]
    for row in reversed(rows):
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


def cleanup_training_scratch(iteration_number: int) -> None:
    import shutil

    removed_dirs = 0
    removed_files = 0

    root_cb = ROOT / "tmp" / "autopilot_scratch"
    if root_cb.is_dir():
        shutil.rmtree(root_cb, ignore_errors=True)
        removed_dirs += 1

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
