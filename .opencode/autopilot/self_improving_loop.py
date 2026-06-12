#!/usr/bin/env python3
"""
Self-Improving Loop — Phase Diagnostics, Creative Divergence, and Meta-Controller.

Three subsystems that make the autopilot loop aware of its own bottlenecks
and able to adjust its strategy when stuck.

Usage (imported by run_autopilot.py):
    from self_improving_loop import diagnose_phase_bottleneck, creative_divergence_slate, run_meta_controller
"""

from __future__ import annotations

import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_D = Path(__file__).resolve().parent
if str(_D) not in sys.path:
    sys.path.insert(0, str(_D))

try:
    from scripts.autocode_config import (
        get_module_keywords,
        get_type_approaches,
        get_type_architectures,
        get_type_objectives,
    )
except ImportError:
    from autocode_config import (  # type: ignore[no-redef]
        get_module_keywords,
        get_type_approaches,
        get_type_architectures,
        get_type_objectives,
    )


# ── Phase classification ────────────────────────────────────────────────────

STAGE_PATTERNS: list[tuple[str, str, list[str]]] = [
    (
        "stage_0_gate",
        "Gate Check",
        [
            r"exhausted (?:family|approach)",
            r"dead end",
            r"confirmed dead",
            r"abandon",
            r"pivot",
        ],
    ),
    (
        "stage_1_assess",
        "Assess Current State",
        [
            r"assess(?:ing)? current",
            r"read(?:ing)? (?:the )?kb",
            r"identify (?:current|baseline)",
            r"diagnos(?:e|is)",
            r"git status",
            r"baseline",
        ],
    ),
    (
        "stage_2_hypothesis",
        "Hypothesis Formation",
        [
            r"hypothesis",
            r"new feature",
            r"new approach",
            r"alternative pattern",
            r"divergence",
            r"fresh angle",
            r"breakthrough",
            r"consider",
        ],
    ),
    (
        "stage_3_implementation",
        "Implementation",
        [
            r"implement(?:ing|ation)",
            r"writ(?:e|ing).{0,10}(?:code|service|component|test|function|module)",
            r"creat(?:e|ing).{0,15}(?:service|component|page|hook|dto|module|test)",
            r"build(?:ing)?",
            r"refactor(?:ing)?",
            r"add(?:ing)? test",
            r"fix(?:ing)? bug",
            r"module",
            r"component",
            r"service",
            r"function",
            r"class",
            r"file",
        ],
    ),
    (
        "stage_4_validation",
        "Validation",
        [
            r"validat(?:e|ion)",
            r"test(?:ing)?",
            r"vitest",
            r"typecheck",
            r"build.?check",
            r"lint(?:ing)?",
            r"regression",
            r"coverage",
            r"pnpm",
        ],
    ),
    (
        "stage_5_integration",
        "Integration & Promote",
        [
            r"integrat(?:e|ion)",
            r"merg(?:e|ing)",
            r"promot(?:e|ion)",
            r"deploy(?:ment)?",
            r"review",
            r"ci",
            r"build.?pass",
        ],
    ),
    (
        "stage_6_review",
        "Review & Document",
        [
            r"document(?:ation)?",
            r"architecture decision",
            r"knowledge.*record",
            r"decision.*record",
            r"bottleneck",
        ],
    ),
]


def classify_stage(
    summary: str,
    goal_progress: str,
    decisions: list[str],
    next_steps: list[str],
    branch_family: str,
) -> str:
    """Classify which pipeline stage an iteration belongs to."""
    text = " ".join(
        [
            summary or "",
            goal_progress or "",
            " ".join(decisions or []),
            " ".join(next_steps or []),
            branch_family or "",
        ]
    ).lower()

    # Check from most specific to least
    for stage_id, _stage_label, patterns in STAGE_PATTERNS:
        for pat in patterns:
            if re.search(pat, text):
                return stage_id
    # Default based on family
    if branch_family and branch_family != "unknown":
        return "stage_2_hypothesis"
    return "stage_1_assess"


STAGE_LABELS = {
    "stage_0_gate": "Stage 0: Gate Check",
    "stage_1_assess": "Stage 1: Assess Current State",
    "stage_2_hypothesis": "Stage 2: Hypothesis Formation",
    "stage_3_implementation": "Stage 3: Implementation",
    "stage_4_validation": "Stage 4: Validation",
    "stage_5_integration": "Stage 5: Integration & Promote",
    "stage_6_review": "Stage 6: Review & Document",
}


# ── Phase Diagnostics ────────────────────────────────────────────────────────


@dataclass
class PhaseStats:
    stage_id: str
    stage_label: str
    count: int
    consecutive: int
    last_iteration: int
    competitiveness_dist: Counter[str]
    status_dist: Counter[str]
    avg_competitiveness_score: float


@dataclass
class PhaseDiagnosticResult:
    bottleneck_stage: str | None
    bottleneck_reason: str
    stage_stats: list[PhaseStats]
    stagnation_score: float  # 0.0 = no stagnation, 1.0 = fully stuck
    stuck_since_iteration: int | None
    recommendation: str
    per_stage_detail: str


COMPETITIVENESS_SCORE = {
    "promotable": 5.0,
    "promising": 4.0,
    "marginal": 3.0,
    "non_competitive": 1.0,
    "unknown": 2.0,
    "": 2.0,
}


def compute_phase_stats(
    checkpoint_rows: list[dict[str, Any]],
) -> list[PhaseStats]:
    """Compute per-stage statistics from checkpoint history."""
    stage_data: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in checkpoint_rows:
        stage = row.get("_stage", "stage_1_assess")
        stage_data[stage].append(row)

    ordered_stages = [s[0] for s in STAGE_PATTERNS]
    stats_list: list[PhaseStats] = []
    for stage_id in ordered_stages:
        rows = stage_data.get(stage_id, [])
        if not rows:
            continue
        comp_dist: Counter[str] = Counter()
        stat_dist: Counter[str] = Counter()
        stage_sum = 0.0
        for r in rows:
            c = r.get("competitiveness", "unknown")
            comp_dist[c] += 1
            stage_sum += COMPETITIVENESS_SCORE.get(c, 2.0)
            stat_dist[r.get("status", "unknown")] += 1

        # Count consecutive at the end of the sequence
        consecutive = 0
        last_iter = max(r.get("iteration_number", 0) for r in rows)
        for r in reversed(checkpoint_rows):
            if r.get("_stage", "") == stage_id:
                consecutive += 1
            elif r.get("iteration_number", 0) < last_iter:
                break

        avg_score = stage_sum / max(len(rows), 1)
        stats_list.append(
            PhaseStats(
                stage_id=stage_id,
                stage_label=STAGE_LABELS.get(stage_id, stage_id),
                count=len(rows),
                consecutive=consecutive,
                last_iteration=last_iter,
                competitiveness_dist=comp_dist,
                status_dist=stat_dist,
                avg_competitiveness_score=avg_score,
            )
        )
    return stats_list


def detect_bottleneck(
    stats: list[PhaseStats],
    total_iterations: int,
    consecutive_noncompetitive: int,
) -> tuple[str | None, str, int | None]:
    """Identify the bottleneck stage and return (stage_id, reason, stuck_since)."""
    if not stats:
        return None, "No iteration data available for phase analysis.", None

    # 1. Stage with the most consecutive iterations flagged as bottleneck
    high_consecutive = [s for s in stats if s.consecutive >= 3]
    if high_consecutive:
        worst = max(high_consecutive, key=lambda s: s.consecutive)
        noncomp_ratio = worst.competitiveness_dist.get("non_competitive", 0) / max(worst.count, 1)
        if noncomp_ratio > 0.5:
            return (
                worst.stage_id,
                f"Stage {worst.stage_id.replace('stage_', '').split('_')[0]}: {worst.consecutive} consecutive iterations, "
                f"{noncomp_ratio:.0%} non-competitive — loop is stuck in this phase.",
                worst.last_iteration - worst.consecutive + 1,
            )

    # 2. Stage with lowest average competitiveness and significant representation
    significant = [s for s in stats if s.count >= max(3, total_iterations * 0.15)]
    if significant:
        lowest = min(significant, key=lambda s: s.avg_competitiveness_score)
        if lowest.avg_competitiveness_score <= 2.0:
            return (
                lowest.stage_id,
                f"Stage {lowest.stage_id.replace('stage_', '').split('_')[0]}: consistently low competitiveness "
                f"(avg={lowest.avg_competitiveness_score:.1f}/5.0, {lowest.count} iterations) — "
                f"hypotheses from this phase fail to produce signal.",
                None,
            )

    # 3. Stage with high failure rate
    for s in stats:
        fail_ratio = s.status_dist.get("failed", 0) / max(s.count, 1)
        if fail_ratio > 0.4 and s.count >= 3:
            return (
                s.stage_id,
                f"Stage {s.stage_id.replace('stage_', '').split('_')[0]}: {fail_ratio:.0%} iteration failure rate "
                f"({s.count} attempts) — execution problems in this phase.",
                None,
            )

    # 4. General stagnation
    if consecutive_noncompetitive >= 5:
        return (
            "stage_2_hypothesis",
            f"{consecutive_noncompetitive} consecutive non-competitive iterations across all stages — "
            f"the hypothesis generation pipeline is likely the root cause.",
            None,
        )

    return None, "No clear bottleneck detected. Loop is making acceptable progress across phases.", None


def render_stage_detail(stats: list[PhaseStats]) -> str:
    """Render a compact Markdown table of per-stage diagnostics."""
    lines = [
        "## Phase Bottleneck Analysis",
        "",
        "| Stage | Iterations | Consecutive | Competitiveness | Failure Rate |",
        "|---|---|---|---|---|",
    ]
    for s in stats:
        noncomp = s.competitiveness_dist.get("non_competitive", 0)
        comp_str = f"avg={s.avg_competitiveness_score:.1f} ({noncomp} nc)"
        fail_rate = s.status_dist.get("failed", 0) / max(s.count, 1)
        lines.append(f"| {s.stage_label} | {s.count} | {s.consecutive} | {comp_str} | {fail_rate:.0%} |")
    lines.append("")
    return "\n".join(lines)


def diagnose_phase_bottleneck(
    conn: sqlite3.Connection,
    run_id: str,
    checkpoint_rows: list[dict[str, Any]],
    total_iterations: int,
    consecutive_noncompetitive: int,
) -> PhaseDiagnosticResult:
    """Full phase diagnostics: classify iterations, compute stats, detect bottleneck."""
    for row in checkpoint_rows:
        cp = row
        row["_stage"] = classify_stage(
            summary=cp.get("summary", ""),
            goal_progress=cp.get("goal_progress", ""),
            decisions=cp.get("decisions", []),
            next_steps=cp.get("next_steps", []),
            branch_family=cp.get("branch_family", ""),
        )

    stats = compute_phase_stats(checkpoint_rows)
    bottle_id, bottle_reason, stuck_since = detect_bottleneck(stats, total_iterations, consecutive_noncompetitive)
    per_stage_detail = render_stage_detail(stats)

    # Compute stagnation score
    noncomp_total = sum(s.competitiveness_dist.get("non_competitive", 0) for s in stats)
    stagnation_score = min(
        1.0,
        (noncomp_total / max(total_iterations, 1)) * 1.5
        + (sum(s.consecutive for s in stats if s.consecutive >= 3) / max(total_iterations, 1)) * 2.0,
    )

    # Generate recommendation
    recommendations = {
        "stage_0_gate": "Revisit the KB 'Confirmed Dead Ends'. If no fresh approaches exist, "
        "escalate to the creative divergence engine for genuinely novel hypothesis generation.",
        "stage_1_assess": "The loop is spending too long assessing. Accelerate: "
        "pre-populate findings from the KB and move directly to hypothesis generation.",
        "stage_2_hypothesis": "Hypothesis formation is the bottleneck. Force a structured divergence: "
        "generate 3 orthogonal hypotheses that differ on: module area, "
        "improvement type (bug/feature/test/perf), and risk level.",
        "stage_3_implementation": "Implementation is the bottleneck. The hypothesis-to-code translation "
        "is failing. Break the work into smaller steps. Consider a simpler implementation path first.",
        "stage_4_validation": "Validation is the bottleneck. Tests are failing or type checking errors. "
        "Fix validation issues before expanding scope. Run the narrowest test first.",
        "stage_5_integration": "Integration is the bottleneck. Work passes validation but fails to integrate. "
        "Check shared contract compatibility, merge conflicts, and CI pipeline issues.",
        "stage_6_review": "Review/documentation is the bottleneck. Record decisions, update knowledge files, "
        "and ensure the KB is current before starting new work.",
    }

    rec = recommendations.get(bottle_id or "", "No specific bottleneck recommendation available.")
    if not bottle_id:
        rec = "No clear bottleneck. Continue with current approach but monitor for emerging patterns."

    return PhaseDiagnosticResult(
        bottleneck_stage=bottle_id,
        bottleneck_reason=bottle_reason,
        stage_stats=stats,
        stagnation_score=stagnation_score,
        stuck_since_iteration=stuck_since,
        recommendation=rec,
        per_stage_detail=per_stage_detail,
    )


# ── Creative Divergence Engine ────────────────────────────────────────────────


@dataclass
class DivergenceHypothesis:
    name: str
    description: str
    feature_domain: str
    model_type: str
    objective: str
    novelty_rating: str  # moderate | major | radical
    risk: str
    validation_cost: str  # cheap | moderate | expensive
    why_orthogonal: str


@dataclass
class CreativeDivergenceSlate:
    blocked_stage: str | None
    blocked_reason: str
    hypotheses: list[DivergenceHypothesis]
    meta_suggestion: str
    force_pivot_to: str  # which Stage to jump to


FAMILY_DOMAINS = [
    "Backend API (NestJS services/controllers/modules)",
    "Frontend UI (React components/pages/hooks/MUI)",
    "Database layer (MongoDB/TypeORM queries/migrations)",
    "Authentication & Authorization (OAuth/JWT/Cognito)",
    "Payment integration (Stripe/webhooks/billing)",
    "File/Media pipeline (S3/CloudFront/image processing)",
    "Deployment & Infrastructure (Lambda/Docker/CI)",
    "Testing & Coverage (unit/integration/e2e)",
    "Email & Notifications (SES/EventBridge/templates)",
    "Developer Experience (tooling/scripts/docs/configs)",
    "Performance Optimization (caching/bundling/CDN/lazy)",
    "Security Hardening (validation/rate-limiting/WAF/sanitize)",
]

APPROACH_TYPES = [
    "Refactor existing (improve structure, preserve behavior)",
    "New feature (add capability with tests)",
    "Bug fix (diagnose + regression test)",
    "Test coverage (add tests for untested code paths)",
    "Performance (measurable speed/memory/bundle improvement)",
    "Type safety (replace `any`, add proper interfaces)",
    "Documentation (architecture decisions, README, JSDoc)",
    "Code removal (delete dead/unused code/files)",
]

OBJECTIVES = [
    "Reduce code duplication",
    "Improve error handling and edge cases",
    "Add input validation and sanitization",
    "Reduce coupling / increase cohesion",
    "Improve response time / reduce latency",
    "Reduce bundle size / tree-shaking",
    "Increase test coverage (line/branch)",
    "Remove dead code and unused dependencies",
    "Improve type safety (strict mode, no any)",
    "Add logging / observability / monitoring",
    "Improve accessibility (a11y)",
    "Add i18n coverage for missing locales",
]

ARCHITECTURES = [
    "Backend module decomposition (split/merge services)",
    "Frontend component composition (extract/shared components)",
    "Shared contract extraction (common types/utils to shared)",
    "Middleware pipeline improvement",
    "Error boundary and global error handling",
    "State management rationalization",
    "API versioning and deprecation strategy",
    "Event-driven decoupling (queues/events)",
    "Caching layer (Redis/memoization/CDN)",
]


def _recent_domains(checkpoint_rows: list[dict[str, Any]]) -> list[str]:
    """Extract recently tried domains from checkpoint text."""
    text = " ".join(r.get("summary", "") + " " + r.get("branch_family", "") for r in checkpoint_rows[:12]).lower()
    domains = _project_family_domains()
    tried = []
    for domain in domains:
        for word in domain.lower().split()[:3]:
            if word in text:
                tried.append(domain)
                break
    return tried


def _project_family_domains() -> list[str]:
    """Generate project-aware family domains from autocode config or fall back to generic."""
    try:
        modules = get_module_keywords()
        if modules:
            domains = []
            for name, desc, keywords in modules:
                hint = desc or ", ".join(keywords[:2])
                domains.append(f"{name.replace('_', ' ').title()} ({hint})")
            return domains
    except Exception:
        pass
    return list(FAMILY_DOMAINS)


def _project_approaches() -> list[str]:
    """Return project-type-aware approaches or fall back to hardcoded list."""
    try:
        return get_type_approaches()
    except Exception:
        pass
    return list(APPROACH_TYPES)


def _project_objectives() -> list[str]:
    """Return project-type-aware objectives or fall back to hardcoded list."""
    try:
        return get_type_objectives()
    except Exception:
        pass
    return list(OBJECTIVES)


def _project_architectures() -> list[str]:
    """Return project-type-aware architectures or fall back to hardcoded list."""
    try:
        return get_type_architectures()
    except Exception:
        pass
    return list(ARCHITECTURES)


def creative_divergence_slate(
    checkpoint_rows: list[dict[str, Any]],
    diagnostic: PhaseDiagnosticResult,
    recent_families: list[str],
    exhausted_families: list[str],
    critique: SelfCritiqueResult | None = None,
) -> CreativeDivergenceSlate:
    """Generate a structured creative divergence slate when the loop is stuck.

    When a SelfCritiqueResult is provided, the slate is adjusted:
    - If critique recommends higher novelty → hypotheses get "major" or "radical"
    - If critique identifies failing families → those domains are deprioritized
    - Critique lessons/recommendations are appended to the meta suggestion
    """
    tried_domains = _recent_domains(checkpoint_rows)
    family_domains = _project_family_domains()
    available_domains = [d for d in family_domains if d not in tried_domains and d not in exhausted_families]
    if not available_domains:
        available_domains = [d for d in family_domains if d not in tried_domains]
    if not available_domains:
        available_domains = family_domains[:4]

    # ── Critique-informed domain deprioritization ─────────────────────────
    critique_needs_novelty = False
    critique_failing_keywords: list[str] = []
    if critique is not None:
        for rec in critique.recommendations:
            if "novelty" in rec.lower() or "higher-novelty" in rec.lower() or "exploitation dominant" in rec.lower():
                critique_needs_novelty = True
            if "reducing investment" in rec.lower() or "fail" in rec.lower():
                pass  # handled via family matching below
        for pat in critique.patterns_detected:
            if "Recurring failures in '" in pat:
                m = re.search(r"'([^']+)'", pat)
                if m:
                    critique_failing_keywords.append(m.group(1).lower())

    # Deprioritize domains that overlap with failing families
    if critique_failing_keywords:
        deprioritized = [d for d in available_domains if any(kw in d.lower() for kw in critique_failing_keywords)]
        for d in deprioritized:
            if d in available_domains and len(available_domains) > 3:
                available_domains.remove(d)
                available_domains.append(d)  # move to end

    available_approaches = _project_approaches()
    available_objectives = _project_objectives()
    available_archs = _project_architectures()

    # Avoid recently tried approaches
    recent_text = " ".join(r.get("summary", "") + " " + r.get("branch_family", "") for r in checkpoint_rows[:6]).lower()
    for approach in list(available_approaches):
        tokens = approach.lower().split()[:2]
        if any(t in recent_text for t in tokens):
            if approach in available_approaches and len(available_approaches) > 3:
                available_approaches.remove(approach)

    hypotheses: list[DivergenceHypothesis] = []
    used_combos: set[tuple[str, str, str]] = set()

    # Generate until we have 3 distinct hypotheses
    attempts = 0
    while len(hypotheses) < 3 and attempts < 30:
        attempts += 1
        domain = available_domains[len(hypotheses) % len(available_domains)]
        approach = available_approaches[len(hypotheses) % len(available_approaches)]
        obj = available_objectives[len(hypotheses) % len(available_objectives)]
        arch = available_archs[len(hypotheses) % len(available_archs)]

        combo = (domain, approach, obj)
        if combo in used_combos:
            domain = available_domains[(len(hypotheses) + 3) % len(available_domains)]
            approach = available_approaches[(len(hypotheses) + 2) % len(available_approaches)]
            obj = available_objectives[(len(hypotheses) + 1) % len(available_objectives)]
            combo = (domain, approach, obj)
            if combo in used_combos:
                continue

        used_combos.add(combo)

        # ── Critique-informed novelty boost ──────────────────────────────
        base_novelty = "major" if (domain not in tried_domains and len(tried_domains) > 0) else "moderate"
        if critique_needs_novelty:
            novelty = "major" if base_novelty == "moderate" else "radical"
        else:
            novelty = base_novelty

        # Estimate validation cost
        cost = "moderate"
        if "test" in approach.lower() or "removal" in approach.lower() or "doc" in approach.lower():
            cost = "cheap"
        elif "performance" in approach.lower() or "feature" in approach.lower():
            cost = "expensive"

        # Risk rating
        risk = "medium"
        if "refactor" in approach.lower() or "decompos" in arch.lower() or "decoupling" in arch.lower():
            risk = "high"

        why_orth = f"Uses {domain} ({'not recently tried' if domain not in tried_domains else 'novel combination with ' + approach})"
        if arch != "Backend module decomposition (split/merge services)":
            why_orth += f", paired with {arch}"

        hypotheses.append(
            DivergenceHypothesis(
                name=f"Divergence: {domain.split()[0]}+{approach.split()[0]}+{obj.split()[0]}",
                description=f"{domain} → {approach} → {obj} via {arch}",
                feature_domain=domain,
                model_type=approach,
                objective=obj,
                novelty_rating=novelty,
                risk=risk,
                validation_cost=cost,
                why_orthogonal=why_orth,
            )
        )

    if diagnostic.stagnation_score > 0.6:
        meta = (
            f"STAGNATION CRITICAL (score={diagnostic.stagnation_score:.2f}). "
            f"The loop has been stuck in {STAGE_LABELS.get(diagnostic.bottleneck_stage or '', 'unknown stage')} "
            f"for {diagnostic.bottleneck_reason}. "
            f"Force a hard pivot: pick one of the hypotheses below and skip directly to implementation. "
            f"Do NOT spend iterations on assessment or hypothesis refinement — the current approach has failed."
        )
        force_pivot = "stage_3_implementation"
    elif diagnostic.stagnation_score > 0.3:
        meta = (
            f"Stagnation building (score={diagnostic.stagnation_score:.2f}). "
            f"The bottleneck is in {STAGE_LABELS.get(diagnostic.bottleneck_stage or '', 'unknown stage')}. "
            f"Before the next hypothesis iteration, read the divergence hypotheses below. "
            f"Consider using at least one of them as the next step instead of refining the current family."
        )
        force_pivot = "stage_2_hypothesis"
    else:
        meta = (
            f"No critical stagnation (score={diagnostic.stagnation_score:.2f}). "
            f"Continue normal operation but keep the divergence hypotheses as a reserve."
        )
        force_pivot = "stage_1_assess"

    # ── Append critique insights to meta suggestion ──────────────────────
    critique_lines: list[str] = []
    if critique is not None:
        if critique_needs_novelty:
            critique_lines.append("Critique-informed: increased novelty targets due to detected over-exploitation.")
        if critique.lessons_learned:
            critique_lines.append(f"Critique lesson: {critique.lessons_learned[0]}")
        if critique.recommendations:
            critique_lines.append(f"Critique recommendation: {critique.recommendations[0]}")
        if critique_failing_keywords:
            kw_list = ", ".join(critique_failing_keywords)
            critique_lines.append(f"Critique-informed: deprioritized domains matching failing families ({kw_list}).")
    if critique_lines:
        meta += "\n\n" + "\n".join(critique_lines)

    return CreativeDivergenceSlate(
        blocked_stage=diagnostic.bottleneck_stage,
        blocked_reason=diagnostic.bottleneck_reason,
        hypotheses=hypotheses,
        meta_suggestion=meta,
        force_pivot_to=force_pivot,
    )


# ── Meta-Controller ──────────────────────────────────────────────────────────


@dataclass
class MetaControllerParams:
    sleep_seconds: int
    max_retries_per_iteration: int
    cpu_budget_percent: int
    parallel_branches_max: int
    force_parallel: int


@dataclass
class MetaControllerAdvice:
    params: MetaControllerParams
    changed: dict[str, tuple[Any, Any]]  # param -> (old, new)
    reasoning: str
    performance_summary: str
    adjusted: bool


def _extract_row_metrics(row: dict[str, Any]) -> dict[str, Any]:
    """Extract performance metrics from a checkpoint row."""
    return {
        "status": row.get("status", "unknown"),
        "competitiveness": row.get("competitiveness", "unknown"),
        "evidence_quality": row.get("evidence_quality", "unknown"),
        "promotion_recommendation": row.get("promotion_recommendation", "hold"),
        "branch_family": row.get("branch_family", "unknown"),
        "family_novelty": row.get("family_novelty", "minor"),
    }


def run_meta_controller(
    current_params: MetaControllerParams,
    checkpoint_rows: list[dict[str, Any]],
    consecutive_noncompetitive: int,
    total_iterations: int,
    critique: SelfCritiqueResult | None = None,
) -> MetaControllerAdvice:
    """Analyze recent performance and suggest parameter adjustments."""
    window = [row for row in checkpoint_rows[:12]]  # last 12
    if not window:
        return MetaControllerAdvice(
            params=current_params,
            changed={},
            reasoning="No iteration data yet. Using default parameters.",
            performance_summary="No data.",
            adjusted=False,
        )

    metrics = [_extract_row_metrics(row) for row in window]

    # Compute performance indicators
    failed_count = sum(1 for m in metrics if m["status"] == "failed")
    noncomp_count = sum(1 for m in metrics if m["competitiveness"] == "non_competitive")
    success_count = sum(1 for m in metrics if m["competitiveness"] in ("promising", "promotable"))
    avg_family_novelty: list[str] = [
        m["family_novelty"] for m in metrics if m["family_novelty"] in ("moderate", "major")
    ]

    changed: dict[str, tuple[Any, Any]] = {}
    new_params = MetaControllerParams(
        sleep_seconds=current_params.sleep_seconds,
        max_retries_per_iteration=current_params.max_retries_per_iteration,
        cpu_budget_percent=current_params.cpu_budget_percent,
        parallel_branches_max=current_params.parallel_branches_max,
        force_parallel=current_params.force_parallel,
    )

    reasoning_parts: list[str] = []

    # Rule 1: High failure rate → increase retries
    if failed_count >= 4 and len(window) >= 6:
        new_val = min(current_params.max_retries_per_iteration + 1, 5)
        if new_val > current_params.max_retries_per_iteration:
            changed["max_retries_per_iteration"] = (current_params.max_retries_per_iteration, new_val)
            new_params.max_retries_per_iteration = new_val
            reasoning_parts.append(f"High failure rate ({failed_count}/{len(window)}) → increased retries to {new_val}")

    # Rule 2: Low failure rate → reduce retries
    if failed_count <= 1 and len(window) >= 6 and current_params.max_retries_per_iteration > 2:
        new_val = current_params.max_retries_per_iteration - 1
        changed["max_retries_per_iteration"] = (current_params.max_retries_per_iteration, new_val)
        new_params.max_retries_per_iteration = new_val
        reasoning_parts.append(f"Low failure rate → reduced retries to {new_val}")

    # Rule 3: Stagnation → increase parallel branches for exploration
    if consecutive_noncompetitive >= 4 and current_params.force_parallel < 2:
        new_val = min(current_params.force_parallel + 1, 3)
        changed["force_parallel"] = (current_params.force_parallel, new_val)
        new_params.force_parallel = new_val
        reasoning_parts.append(
            f"Stagnation ({consecutive_noncompetitive} nc) → increased parallel branches to {new_val}"
        )

    # Rule 4: Good progress → allow serial (focus)
    if success_count >= 3 and current_params.force_parallel > 0:
        changed["force_parallel"] = (current_params.force_parallel, 0)
        new_params.force_parallel = 0
        reasoning_parts.append(
            f"Good progress ({success_count} wins) → disabled forced parallelism for focused execution"
        )

    # Rule 5: Many novel families with low success → slow down, sleep more
    if len(avg_family_novelty) >= 4 and noncomp_count >= len(avg_family_novelty) * 0.5:
        new_val = min(current_params.sleep_seconds + 30, 300)
        if new_val > current_params.sleep_seconds:
            changed["sleep_seconds"] = (current_params.sleep_seconds, new_val)
            new_params.sleep_seconds = new_val
            reasoning_parts.append(f"Novel families failing ({noncomp_count} nc) → increased sleep to {new_val}s")

    # Rule 6: Consistently good → speed up
    if success_count >= 3 and current_params.sleep_seconds > 30:
        new_val = max(current_params.sleep_seconds - 15, 10)
        changed["sleep_seconds"] = (current_params.sleep_seconds, new_val)
        new_params.sleep_seconds = new_val
        reasoning_parts.append(f"Consistent wins → reduced sleep to {new_val}s")

    # Rule 7: CPU budget decay if all iterations are failing
    if failed_count / max(len(window), 1) > 0.5 and current_params.cpu_budget_percent > 50:
        new_val = current_params.cpu_budget_percent - 10
        changed["cpu_budget_percent"] = (current_params.cpu_budget_percent, new_val)
        new_params.cpu_budget_percent = new_val
        reasoning_parts.append(f"High failure → reduced CPU budget to {new_val}%")

    # ── Critique-aware rules ─────────────────────────────────────────────────
    if critique is not None and critique.recommendations:
        recs_lower = [r.lower() for r in critique.recommendations]

        # Rule 8: Low health + exploration signal → increase force_parallel
        if critique.health_score < 0.4 and any(
            "explor" in r or "pivot" in r or "diversif" in r or "different" in r or "new" in r for r in recs_lower
        ):
            new_val = min(current_params.force_parallel + 1, 3)
            if new_val > current_params.force_parallel:
                changed["force_parallel"] = (current_params.force_parallel, new_val)
                new_params.force_parallel = new_val
                reasoning_parts.append(
                    f"Critique: low health ({critique.health_score:.2f}) + exploration signal "
                    f"→ increased force_parallel to {new_val}"
                )

        # Rule 9: Recommendation to narrow focus → decrease parallelism
        if (
            any("narrow" in r or "focus" in r or "depth" in r or "concentrate" in r for r in recs_lower)
            and current_params.parallel_branches_max > 1
        ):
            new_val = max(current_params.parallel_branches_max - 1, 1)
            changed["parallel_branches_max"] = (current_params.parallel_branches_max, new_val)
            new_params.parallel_branches_max = new_val
            reasoning_parts.append(f"Critique: narrowing signal → reduced parallel_branches_max to {new_val}")

        # Rule 10: Recommendation to retry → increase max_retries
        if (
            any("retry" in r or "retries" in r or "repeat" in r or "revisit" in r for r in recs_lower)
            and current_params.max_retries_per_iteration < 5
        ):
            new_val = current_params.max_retries_per_iteration + 1
            changed["max_retries_per_iteration"] = (current_params.max_retries_per_iteration, new_val)
            new_params.max_retries_per_iteration = new_val
            reasoning_parts.append(f"Critique: retry signal → increased max_retries to {new_val}")

        # Rule 11: Very low health score → multi-factor crisis response
        if critique.health_score < 0.3:
            if current_params.max_retries_per_iteration < 5:
                new_val = min(current_params.max_retries_per_iteration + 1, 5)
                changed["max_retries_per_iteration"] = (current_params.max_retries_per_iteration, new_val)
                new_params.max_retries_per_iteration = new_val
                reasoning_parts.append(
                    f"Critique: crisis (health={critique.health_score:.2f}) → increased retries to {new_val}"
                )
            if current_params.force_parallel < 2:
                changed["force_parallel"] = (current_params.force_parallel, 2)
                new_params.force_parallel = 2
                reasoning_parts.append("Critique: crisis → forced parallelism to 2 for broader exploration")

    perf = (
        f"Window: {len(window)} iterations | "
        f"Failed: {failed_count} | Non-competitive: {noncomp_count} | "
        f"Wins: {success_count} | Novelty: {len(avg_family_novelty)} moderate+ | "
        f"NC streak: {consecutive_noncompetitive}"
    )

    reasoning = " ".join(reasoning_parts) if reasoning_parts else "No adjustments needed."

    return MetaControllerAdvice(
        params=new_params,
        changed=changed,
        reasoning=reasoning,
        performance_summary=perf,
        adjusted=bool(changed),
    )


# ── Self-Critique Retrospective ─────────────────────────────────────────────


@dataclass
class SelfCritiqueResult:
    patterns_detected: list[str]
    lessons_learned: list[str]
    recommendations: list[str]
    health_score: float  # 0.0 (struggling) to 1.0 (thriving)


def self_critique_retrospective(
    checkpoint_rows: list[dict[str, Any]],
) -> SelfCritiqueResult:
    """Analyze past iteration patterns and generate lessons learned.

    Examines checkpoint history to detect patterns, successes, failures,
    and generates actionable recommendations for future iterations.
    """
    if not checkpoint_rows:
        return SelfCritiqueResult(
            patterns_detected=[],
            lessons_learned=["No iteration data yet. Start executing to gather patterns."],
            recommendations=["Begin with a structured hypothesis and validate quickly."],
            health_score=0.5,
        )

    patterns: list[str] = []
    lessons: list[str] = []
    recommendations: list[str] = []

    families = [r.get("branch_family", "unknown") or "unknown" for r in checkpoint_rows]
    family_counts = Counter(families)
    unique_families = len(family_counts)

    if unique_families <= 2 and len(checkpoint_rows) >= 5:
        top_family = family_counts.most_common(1)[0][0] if family_counts else "none"
        patterns.append(
            f"Narrow focus: {unique_families} families across {len(checkpoint_rows)} iterations ({top_family} dominant)"
        )
        recommendations.append(
            "Broaden exploration: consider a fundamentally different family domain "
            "to increase coverage of the problem space."
        )
    elif unique_families >= 4 and len(checkpoint_rows) >= unique_families:
        patterns.append(
            f"Good breadth: {unique_families} distinct families explored across {len(checkpoint_rows)} iterations"
        )

    competitive = sum(1 for r in checkpoint_rows if r.get("competitiveness") in ("promising", "promotable"))
    non_competitive = sum(1 for r in checkpoint_rows if r.get("competitiveness") == "non_competitive")

    if competitive > non_competitive and competitive > 0:
        patterns.append(f"Positive signal: {competitive} competitive vs {non_competitive} non-competitive outcomes")
        lessons.append("Current hypothesis direction is productive. Continue refining with increasing specificity.")
    elif non_competitive > competitive and non_competitive > 0:
        patterns.append(f"Weak signal: {non_competitive} non-competitive vs {competitive} competitive outcomes")
        lessons.append(
            "Hypotheses are not producing competitive results. Consider "
            "reducing iteration scope and focusing on smaller, verifiable steps."
        )

    novelties = [r.get("family_novelty", "unknown") or "unknown" for r in checkpoint_rows]
    novel_count = sum(1 for n in novelties if n in ("moderate", "major"))
    minor_count = sum(1 for n in novelties if n in ("minor", "incremental", "unknown"))

    if novel_count > minor_count and novel_count >= 2:
        patterns.append(f"Exploration dominant: {novel_count} novel iterations vs {minor_count} incremental")
        lessons.append(
            "Strong exploration phase. Balance with exploitation once a promising "
            "direction is confirmed to avoid spreading too thin."
        )
    elif minor_count > novel_count and minor_count >= 3:
        patterns.append(f"Exploitation dominant: {minor_count} incremental iterations vs {novel_count} novel")
        recommendations.append(
            "Introduce higher-novelty hypotheses to avoid local optima. "
            "The system may be over-exploiting a narrow trajectory."
        )

    failed = [r for r in checkpoint_rows if r.get("status") == "failed"]
    if failed:
        failure_families = Counter(r.get("branch_family", "unknown") for r in failed)
        top_fail = failure_families.most_common(1)
        if top_fail:
            patterns.append(f"Recurring failures in '{top_fail[0][0]}' family ({top_fail[0][1]} failures)")
            recommendations.append(
                f"Consider reducing investment in the '{top_fail[0][0]}' family or "
                f"approach it with a fundamentally different strategy."
            )

    evidence_counts = Counter(r.get("evidence_quality", "unknown") for r in checkpoint_rows)
    artifact_only = evidence_counts.get("artifact_only", 0)
    strategy_level = evidence_counts.get("strategy_level", 0)

    if artifact_only > strategy_level and artifact_only >= 3:
        patterns.append(
            f"Artifact-heavy: {artifact_only} iterations produced artifacts "
            f"but only {strategy_level} validated strategy-level evidence"
        )
        lessons.append(
            "Most iterations produce artifacts without strong evidence. "
            "Focus on validation with concrete metrics (test passes, benchmarks, type checks)."
        )
    elif strategy_level > artifact_only and strategy_level >= 2:
        lessons.append(
            "Iterations consistently produce strategy-level evidence. Continue maintaining high validation standards."
        )

    competitive_ratio = competitive / max(len(checkpoint_rows), 1)
    novelty_ratio = novel_count / max(len(checkpoint_rows), 1)
    failure_ratio = len(failed) / max(len(checkpoint_rows), 1)
    diversity_bonus = min(0.2, unique_families * 0.05)

    health_score = min(
        1.0,
        max(
            0.0,
            competitive_ratio * 0.5 + novelty_ratio * 0.2 + (1.0 - failure_ratio) * 0.2 + diversity_bonus,
        ),
    )

    if not patterns:
        patterns.append("No strong patterns detected yet. Continue collecting data.")
    if not lessons:
        lessons.append("Insufficient data for lesson extraction. More iterations needed.")
    if not recommendations:
        recommendations.append("Continue current trajectory with periodic re-evaluation.")

    return SelfCritiqueResult(
        patterns_detected=patterns,
        lessons_learned=lessons,
        recommendations=recommendations,
        health_score=round(health_score, 2),
    )


def render_self_critique(result: SelfCritiqueResult) -> str:
    """Render self-critique retrospective as a markdown block."""
    lines = [
        "## Self-Critique Retrospective",
        "",
        f"**Health Score**: {result.health_score:.2f}/1.0",
        "",
    ]
    if result.patterns_detected:
        lines.append("### Patterns Detected")
        lines.append("")
        for p in result.patterns_detected:
            lines.append(f"- {p}")
        lines.append("")
    if result.lessons_learned:
        lines.append("### Lessons Learned")
        lines.append("")
        for lesson in result.lessons_learned:
            lines.append(f"- {lesson}")
        lines.append("")
    if result.recommendations:
        lines.append("### Recommendations")
        lines.append("")
        for r in result.recommendations:
            lines.append(f"- {r}")
        lines.append("")
    return "\n".join(lines)


def render_meta_controller_advice(advice: MetaControllerAdvice) -> str:
    """Render meta-controller advice as a markdown block."""
    lines = [
        "## Meta-Controller Advice",
        "",
        f"**Performance**: {advice.performance_summary}",
        "",
    ]
    if advice.changed:
        lines.append("**Parameter changes**:")
        for param, (old, new) in advice.changed.items():
            lines.append(f"- `{param}`: `{old}` → `{new}`")
        lines.append("")
        lines.append(f"**Reasoning**: {advice.reasoning}")
    else:
        lines.append(f"**No changes**: {advice.reasoning}")
    lines.append("")
    return "\n".join(lines)


def render_creative_divergence(slate: CreativeDivergenceSlate) -> str:
    """Render creative divergence slate as a markdown block."""
    lines = [
        "## Creative Divergence Slate",
        "",
    ]
    if slate.blocked_stage:
        lines.append(f"**Blocked at**: {STAGE_LABELS.get(slate.blocked_stage, slate.blocked_stage)}")
        lines.append(f"**Reason**: {slate.blocked_reason}")
        lines.append("")
    lines.append(f"**Meta**: {slate.meta_suggestion}")
    lines.append("")
    lines.append("| Hypothesis | Domain | Model | Objective | Novelty | Cost | Risk | Why Orthogonal |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for h in slate.hypotheses:
        lines.append(
            f"| {h.name} | {h.feature_domain} | {h.model_type} | {h.objective} "
            f"| {h.novelty_rating} | {h.validation_cost} | {h.risk} | {h.why_orthogonal} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_phase_diagnostic(diag: PhaseDiagnosticResult) -> str:
    """Render phase diagnostic as a markdown block."""
    lines = [
        "# Self-Improving Loop Diagnostics",
        "",
        f"**Stagnation Score**: {diag.stagnation_score:.2f}/1.0",
        f"**Bottleneck**: {STAGE_LABELS.get(diag.bottleneck_stage or 'none', 'None') if diag.bottleneck_stage else 'None detected'}",
        f"**Reason**: {diag.bottleneck_reason}",
        "",
    ]
    if diag.stuck_since_iteration:
        lines.append(f"**Stuck since iteration**: {diag.stuck_since_iteration}")
        lines.append("")
    lines.append(f"**Recommendation**: {diag.recommendation}")
    lines.append("")
    lines.append(diag.per_stage_detail)
    lines.append("")
    return "\n".join(lines)


def run_self_improving_loop(
    conn: sqlite3.Connection,
    run_id: str,
    checkpoint_rows: list[dict[str, Any]],
    total_iterations: int,
    consecutive_noncompetitive: int,
    current_params: MetaControllerParams,
    recent_families: list[str],
    exhausted_families: list[str],
) -> tuple[str, MetaControllerAdvice]:
    """
    Run the full self-improving loop pipeline.

    Returns:
        (markdown_block, meta_controller_advice)
    """
    diagnostic = diagnose_phase_bottleneck(conn, run_id, checkpoint_rows, total_iterations, consecutive_noncompetitive)
    critique = self_critique_retrospective(checkpoint_rows)
    slate = creative_divergence_slate(checkpoint_rows, diagnostic, recent_families, exhausted_families, critique)
    meta_advice = run_meta_controller(
        current_params, checkpoint_rows, consecutive_noncompetitive, total_iterations, critique
    )

    blocks = [
        render_phase_diagnostic(diagnostic),
        render_creative_divergence(slate),
        render_self_critique(critique),
        render_meta_controller_advice(meta_advice),
    ]
    return "\n".join(blocks), meta_advice
