#!/usr/bin/env python3
"""
Autocode KPI Dashboard — analyze autopilot runs and generate HTML reports.

Reads from:
  - .opencode/autopilot/runtime/memory.sqlite3 (iteration data)
  - knowledge/autopilot_kb.yaml (wins, dead ends)
  - autocode.yaml (project config)

Outputs:
  - .opencode/autopilot/kpi/dashboard-<run_id>.html (self-contained report)
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import warnings
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ── Path resolution (same approach as autocode_config) ───────────────────────


def _find_root() -> Path:
    start = Path(__file__).resolve()
    for parent in [start] + list(start.parents):
        if (parent / "autocode.yaml").exists():
            return parent
        if (parent / ".git").exists():
            return parent
    return start.parents[2]


ROOT = Path(os.environ.get("AUTOPILOT_ROOT", str(_find_root()))).resolve()
RUNTIME_DIR = ROOT / ".opencode" / "autopilot" / "runtime"
DB_PATH = RUNTIME_DIR / "memory.sqlite3"
KB_PATH = ROOT / "knowledge" / "autopilot_kb.yaml"
META_KB_PATH = ROOT / "knowledge" / "meta_kb.yaml"
OUTPUT_DIR = ROOT / ".opencode" / "autopilot" / "kpi"


# ── Data extraction ──────────────────────────────────────────────────────────


def load_kb(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"entries": []}
    try:
        import yaml

        with open(path) as f:
            return yaml.safe_load(f) or {"entries": []}
    except Exception:
        return {"entries": []}


def extract_run_metrics(run_id: str, conn: sqlite3.Connection) -> dict[str, Any]:
    """Extract all metrics for a single run."""
    row = conn.execute(
        "SELECT run_id, goal_text, status, created_at, updated_at, iteration_count, latest_summary FROM runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if not row:
        return {}

    iterations = conn.execute(
        "SELECT iteration_number, started_at, finished_at, exit_code, status, summary, checkpoint_json "
        "FROM iterations WHERE run_id = ? ORDER BY iteration_number ASC",
        (run_id,),
    ).fetchall()

    checkpoints = []
    comps: list[str] = []
    families: list[str] = []
    novelty_counts: Counter[str] = Counter()
    comp_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    family_comp: dict[str, list[str]] = defaultdict(list)

    for it in iterations:
        cp_raw = json.loads(it["checkpoint_json"]) if it["checkpoint_json"] else {}
        cp = {
            "iteration": it["iteration_number"],
            "started_at": it["started_at"],
            "status": it["status"],
            "summary": cp_raw.get("summary", ""),
            "competitiveness": cp_raw.get("competitiveness", "unknown"),
            "branch_family": cp_raw.get("branch_family", "unknown"),
            "family_novelty": cp_raw.get("family_novelty", "unknown"),
            "evidence_quality": cp_raw.get("evidence_quality", "unknown"),
            "promotion": cp_raw.get("promotion_recommendation", "hold"),
            "new_facts": cp_raw.get("new_facts", []),
            "decisions": cp_raw.get("decisions", []),
            "files_touched": cp_raw.get("files_touched", []),
        }
        checkpoints.append(cp)
        comp = cp["competitiveness"]
        comps.append(comp)
        comp_counts[comp] += 1
        status_counts[cp["status"]] += 1
        family = cp["branch_family"]
        families.append(family)
        novelty_counts[cp["family_novelty"]] += 1
        family_comp[family].append(comp)

    # Consecutive non-competitive
    consec_noncomp = 0
    for c in reversed(comps):
        if c == "non_competitive":
            consec_noncomp += 1
        else:
            break

    # Family diversity
    total_iters = len(iterations)
    unique_families = len(set(families))
    dominant = Counter(families).most_common(1)

    # Wins (promising or promotable)
    wins = sum(1 for c in comps if c in ("promising", "promotable"))

    first_iter = checkpoints[0]["started_at"] if checkpoints else "?"
    last_iter = checkpoints[-1]["started_at"] if checkpoints else "?"

    return {
        "run_id": run_id,
        "status": row["status"],
        "goal_preview": row["goal_text"][:120] if row["goal_text"] else "",
        "created_at": row["created_at"],
        "total_iterations": row["iteration_count"],
        "first_iteration": first_iter,
        "last_iteration": last_iter,
        "checkpoints": checkpoints,
        "comp_counts": dict(comp_counts),
        "status_counts": dict(status_counts),
        "novelty_counts": dict(novelty_counts),
        "families": list(Counter(families).items()),
        "unique_families": unique_families,
        "dominant_family": dominant[0][0] if dominant else "?",
        "dominant_count": dominant[0][1] if dominant else 0,
        "consecutive_noncompetitive": consec_noncomp,
        "wins": wins,
        "win_rate": round(wins / max(total_iters, 1) * 100, 1),
        "trend_comps": list(zip(range(1, len(comps) + 1), comps)),
    }


def collect_all_metrics(
    db_path: Path | None = None,
    kb_path: Path | None = None,
    meta_kb_path: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Collect metrics from all runs and KB.

    Parameters may be injected for testability; default to module-level globals.
    """
    metrics: list[dict[str, Any]] = []

    target_db = db_path or DB_PATH
    target_kb = kb_path or KB_PATH
    target_meta_kb = meta_kb_path or META_KB_PATH

    if target_db.exists():
        try:
            conn = sqlite3.connect(str(target_db))
            conn.row_factory = sqlite3.Row
            run_ids = conn.execute("SELECT run_id FROM runs ORDER BY created_at DESC").fetchall()
            for r in run_ids:
                m = extract_run_metrics(r["run_id"], conn)
                if m:
                    metrics.append(m)
            conn.close()
        except Exception as exc:
            warnings.warn(f"Failed to read autopilot database {target_db}: {exc}")

    kb = load_kb(target_kb)
    meta_kb = load_kb(target_meta_kb)

    return metrics, kb, meta_kb


# ── HTML Rendering ───────────────────────────────────────────────────────────

COMP_COLORS = {
    "promotable": "#22c55e",
    "promising": "#86efac",
    "marginal": "#fde047",
    "non_competitive": "#fca5a5",
    "unknown": "#d1d5db",
}

STATUS_COLORS = {
    "running": "#3b82f6",
    "done": "#22c55e",
    "blocked": "#f59e0b",
    "failed": "#ef4444",
}


def _timeline_chart(checkpoints: list[dict]) -> str:
    """Generate an inline SVG timeline of competitiveness."""
    if not checkpoints:
        return "<p>No iteration data.</p>"
    n = len(checkpoints)
    w = max(300, n * 18)
    h = 100
    bar_w = max(6, min(16, (w - 40) / n))
    gap = max(1, (w - 40 - n * bar_w) / max(n - 1, 1)) if n > 1 else 0

    comp_order = ["promotable", "promising", "marginal", "non_competitive", "unknown"]
    comp_score = {name: i for i, name in enumerate(comp_order)}
    max_score = len(comp_order) - 1

    bars: list[str] = []
    labels: list[str] = []
    legend_ok: set[str] = set()
    for i, cp in enumerate(checkpoints):
        comp = cp.get("competitiveness", "unknown")
        if comp not in comp_order:
            comp = "unknown"
        score = comp_score[comp]
        bar_h = max(4, (max_score - score) / max_score * (h - 20))
        color = COMP_COLORS.get(comp, "#d1d5db")
        x = 20 + i * (bar_w + gap)
        y = h - 10 - bar_h
        bars.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" fill="{color}" rx="2"/>')
        labels.append(
            f'<text x="{x + bar_w / 2:.1f}" y="{h - 2:.1f}" font-size="6" text-anchor="middle" fill="#666">{cp.get("iteration", i + 1)}</text>'
        )
        legend_ok.add(comp)

    # Legend
    legend_items = "".join(
        f'<span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:{color};margin:0 2px 0 8px;"></span> {name}'
        for name, color in COMP_COLORS.items()
        if name in legend_ok
    )

    return f"""
    <div style="margin:12px 0">
      <div style="font-size:11px;color:#666;margin-bottom:4px">{legend_items}</div>
      <svg width="{w}" height="{h}" style="background:#fafafa;border-radius:6px">
        {"".join(bars)}
        {"".join(labels)}
      </svg>
    </div>"""


def _family_chart(families: list[tuple[str, int]], total: int) -> str:
    """Simple bar chart showing family distribution."""
    if not families:
        return "<p>No family data.</p>"
    bars: list[str] = []
    for name, count in sorted(families, key=lambda x: -x[1]):
        pct = count / max(total, 1) * 100
        bar_w = max(20, pct * 2)
        bars.append(f"""
        <div style="margin:3px 0;font-size:12px">
          <span style="display:inline-block;width:100px;text-align:right;margin-right:8px;color:#374151">{name}</span>
          <span style="display:inline-block;height:18px;width:{bar_w:.0f}px;background:#3b82f6;border-radius:3px;vertical-align:middle;"></span>
          <span style="margin-left:4px;color:#6b7280;font-size:11px">{count} ({pct:.0f}%)</span>
        </div>""")
    return "".join(bars)


def _kb_summary(kb: dict[str, Any]) -> str:
    """Render knowledge base metrics."""
    entries = kb.get("entries", [])
    if not entries:
        return "<p style='color:#9ca3af'>No KB entries yet.</p>"
    types: Counter[str] = Counter()
    for e in entries:
        types[e.get("type", "unknown")] += 1
    items = "".join(
        f'<div style="margin:2px 0;font-size:13px"><span style="color:#6b7280;width:100px;display:inline-block">{t}</span> <strong>{c}</strong></div>'
        for t, c in types.most_common()
    )
    items += f'<div style="margin-top:6px;font-size:12px;color:#9ca3af">Total: {len(entries)} entries</div>'
    return items


def _scorecard(m: dict[str, Any]) -> str:
    """Render the scorecard section for a single run."""
    checkpoints = m.get("checkpoints", [])
    return f"""
    <div style="background:white;border-radius:8px;padding:20px;margin:12px 0;box-shadow:0 1px 3px rgba(0,0,0,0.1)">
      <div style="display:flex;justify-content:space-between;align-items:start;flex-wrap:wrap">
        <div>
          <h3 style="margin:0 0 4px;font-size:16px;color:#111827">
            {m["run_id"][:20]}…
          </h3>
          <div style="font-size:12px;color:#6b7280">
            Started {m["created_at"][:10]} · {m["total_iterations"]} iterations · Status: {m["status"]}
          </div>
        </div>
        <div style="text-align:right;font-size:13px">
          <span style="display:inline-block;padding:2px 8px;border-radius:4px;background:{STATUS_COLORS.get(m["status"], "#d1d5db")}20;color:{STATUS_COLORS.get(m["status"], "#374151")};font-weight:600">{m["status"]}</span>
        </div>
      </div>

      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px;margin:16px 0 8px">
        {_metric_box("Iterations", str(m["total_iterations"]), "#3b82f6")}
        {_metric_box("Wins", str(m["wins"]), "#22c55e", f"{m['win_rate']}%")}
        {_metric_box("Families", str(m["unique_families"]), "#8b5cf6")}
        {_metric_box("Consec. NC", str(m["consecutive_noncompetitive"]), "#ef4444" if m["consecutive_noncompetitive"] > 5 else "#f59e0b")}
        {_metric_box("Dominant", m["dominant_family"][:12], "#06b6d4", f"{m['dominant_count']}/{m['total_iterations']}")}
      </div>

      {_timeline_chart(checkpoints)}

      <details style="margin-top:8px">
        <summary style="cursor:pointer;font-size:13px;color:#6b7280">Family distribution</summary>
        <div style="margin-top:4px">{_family_chart(m.get("families", []), m["total_iterations"])}</div>
      </details>

      <details style="margin-top:8px">
        <summary style="cursor:pointer;font-size:13px;color:#6b7280">Competitiveness breakdown</summary>
        <pre style="font-size:12px;background:#f9fafb;padding:8px;border-radius:4px;margin-top:4px">{json.dumps(m.get("comp_counts", {}), indent=2)}</pre>
      </details>

      <div style="margin-top:8px;font-size:12px;color:#6b7280">
        <strong>Goal:</strong> {m.get("goal_preview", "")[:100]}
      </div>
    </div>"""


def _metric_box(label: str, value: str, color: str, suffix: str = "") -> str:
    return f"""
    <div style="background:#f9fafb;border-radius:6px;padding:10px;text-align:center;border-left:3px solid {color}">
      <div style="font-size:20px;font-weight:700;color:#111827">{value}</div>
      <div style="font-size:11px;color:#6b7280;margin-top:2px">{label} {suffix}</div>
    </div>"""


def generate_dashboard(metrics: list[dict[str, Any]], kb: dict[str, Any], meta_kb: dict[str, Any]) -> str:
    """Generate a complete self-contained HTML dashboard."""
    runs_html = ""
    for m in metrics:
        runs_html += _scorecard(m)

    # Aggregated stats
    total_iters = sum(m.get("total_iterations", 0) for m in metrics)
    total_wins = sum(m.get("wins", 0) for m in metrics)
    total_runs = len(metrics)
    all_families: Counter[str] = Counter()
    for m in metrics:
        for f, c in m.get("families", []):
            all_families[f] += c

    families_html = _family_chart(all_families.most_common(10), total_iters)

    # Find last run metrics
    last_run = metrics[0] if metrics else {}
    kb_entries = kb.get("entries", [])
    kb_wins = sum(
        1 for e in kb_entries if e.get("type") in ("bug_fix", "new_feature", "test_improvement", "arch_improvement")
    )
    kb_dead = sum(1 for e in kb_entries if e.get("type") == "dead_end")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Autocode Dashboard</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:#f3f4f6; color:#111827; padding:24px }}
  .header {{ max-width:1000px; margin:0 auto 20px }}
  .grid {{ max-width:1000px; margin:0 auto; display:grid; gap:16px }}
  h1 {{ font-size:24px; font-weight:700 }}
  h2 {{ font-size:18px; font-weight:600; margin:20px 0 8px; color:#374151 }}
  .stale {{ color: #9ca3af; font-size: 12px; text-align:center; margin-top:24px }}
</style>
</head>
<body>
<div class="header">
  <h1>📊 Autocode Dashboard</h1>
  <p style="color:#6b7280;font-size:14px">
    Generated {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
    · {total_runs} run(s) · {total_iters} iteration(s) · {total_wins} win(s)
    · {kb_wins} KB wins · {kb_dead} dead ends
  </p>
</div>

<div class="grid">
  <h2>Aggregate</h2>
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px">
    {_metric_box("Total iterations", str(total_iters), "#3b82f6")}
    {_metric_box("Total runs", str(total_runs), "#8b5cf6")}
    {_metric_box("Total wins", str(total_wins), "#22c55e")}
    {_metric_box("KB entries", str(len(kb_entries)), "#f59e0b")}
    {_metric_box("Last run iters", str(last_run.get("total_iterations", 0)), "#06b6d4") if last_run else ""}
    {_metric_box("Last run wins", str(last_run.get("wins", 0)), "#22c55e") if last_run else ""}
  </div>

  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
    <div style="background:white;border-radius:8px;padding:16px;box-shadow:0 1px 3px rgba(0,0,0,0.1)">
      <h3 style="font-size:14px;margin-bottom:8px">Top families (all runs)</h3>
      {families_html}
    </div>
    <div style="background:white;border-radius:8px;padding:16px;box-shadow:0 1px 3px rgba(0,0,0,0.1)">
      <h3 style="font-size:14px;margin-bottom:8px">Knowledge Base</h3>
      {_kb_summary(kb)}
    </div>
  </div>

  <h2 style="margin-top:8px">Runs</h2>
  {runs_html}
</div>

<div class="stale">
  Autocode · <a href="https://github.com/org/autocode" style="color:#3b82f6">github.com/org/autocode</a>
</div>
</body>
</html>"""


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(
    output_dir: Path | None = None,
    db_path: Path | None = None,
    kb_path: Path | None = None,
    meta_kb_path: Path | None = None,
) -> int:
    target_output = output_dir or OUTPUT_DIR
    os.makedirs(target_output, exist_ok=True)

    metrics, kb, meta_kb = collect_all_metrics(db_path=db_path, kb_path=kb_path, meta_kb_path=meta_kb_path)

    if not metrics:
        print(f"No autopilot runs found in {db_path or DB_PATH}")
        print("The autopilot must run at least once before KPI data is available.")
        return 1

    html = generate_dashboard(metrics, kb, meta_kb)

    # Save to file
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = target_output / f"dashboard_{timestamp}.html"
    out_path.write_text(html, encoding="utf-8")

    print(f"Dashboard written to: {out_path}")
    print(f"  {len(metrics)} runs, {sum(m['total_iterations'] for m in metrics)} total iterations")
    print(f"  Open in browser: file://{out_path.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
