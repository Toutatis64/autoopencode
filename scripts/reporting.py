from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.run_autopilot import RunContext

try:
    from scripts.autocode_config import ROOT
except ImportError:
    from autocode_config import ROOT  # type: ignore[no-redef]

try:
    from scripts.checkpoint import normalize_checkpoint
except ImportError:
    from checkpoint import normalize_checkpoint  # type: ignore[no-redef]

try:
    from scripts.controller import load_kb_yaml
except ImportError:
    from controller import load_kb_yaml  # type: ignore[no-redef]

try:
    from scripts.memory import compact_summary, utc_now
except ImportError:
    from memory import compact_summary, utc_now  # type: ignore[no-redef]


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
        "# Autopilot Run Report",
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
        "# Autopilot Knowledge Base — Snapshot",
        "",
        f"Generated: {now.isoformat(timespec='seconds')}",
        f"Source: `autopilot_kb.yaml` (schema v{data.get('schema_version', '?')})",
        "",
        "---",
        "",
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
            lines.append(
                f"| `{b.get('name', '?')}` | {b.get('timeframe', '?')} | {b.get('status', '?')} | {b.get('notes', '')} |"
            )
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
    lines.append("*Edit the YAML file, not this markdown.*")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"KB snapshot written to: {output_path}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from scripts.run_autopilot import active_run_id, ensure_runtime, load_run

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
