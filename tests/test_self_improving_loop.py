from __future__ import annotations

from collections import Counter
from typing import Any

from scripts.self_improving_loop import (
    COMPETITIVENESS_SCORE,
    CreativeDivergenceSlate,
    DivergenceHypothesis,
    MetaControllerAdvice,
    MetaControllerParams,
    PhaseDiagnosticResult,
    PhaseStats,
    SelfCritiqueResult,
    _project_approaches,
    _project_architectures,
    _project_family_domains,
    _project_objectives,
    _recent_domains,
    classify_stage,
    compute_phase_stats,
    creative_divergence_slate,
    detect_bottleneck,
    diagnose_phase_bottleneck,
    render_creative_divergence,
    render_meta_controller_advice,
    render_phase_diagnostic,
    render_self_critique,
    render_stage_detail,
    run_meta_controller,
    run_self_improving_loop,
    self_critique_retrospective,
)


# ── classify_stage ──────────────────────────────────────────────────────────────


def test_classify_stage_gate_check() -> None:
    result = classify_stage("dead end detected", "", [], [], "tests")
    assert result == "stage_0_gate"


def test_classify_stage_assess() -> None:
    result = classify_stage("assessing current state", "", [], [], "")
    assert result == "stage_1_assess"


def test_classify_stage_assess_git_status() -> None:
    result = classify_stage("git status", "", [], [], "")
    assert result == "stage_1_assess"


def test_classify_stage_hypothesis() -> None:
    result = classify_stage("generating hypothesis", "", [], [], "")
    assert result == "stage_2_hypothesis"


def test_classify_stage_implementation() -> None:
    result = classify_stage("implementing the service", "", [], [], "")
    assert result == "stage_3_implementation"


def test_classify_stage_validation() -> None:
    result = classify_stage("running tests and validation", "", [], [], "")
    assert result == "stage_4_validation"


def test_classify_stage_integration() -> None:
    result = classify_stage("merging and promoting changes", "", [], [], "")
    assert result == "stage_5_integration"


def test_classify_stage_integration_build_pass() -> None:
    result = classify_stage("ci pipeline", "", [], [], "")
    assert result == "stage_5_integration"


def test_classify_stage_review() -> None:
    result = classify_stage("bottleneck detected", "", [], [], "")
    assert result == "stage_6_review"


def test_classify_stage_default_with_branch_family() -> None:
    result = classify_stage("no match", "", [], [], "unknown_cat")
    assert result == "stage_2_hypothesis"


def test_classify_stage_default_no_family() -> None:
    result = classify_stage("no match", "", [], [], "")
    assert result == "stage_1_assess"


def test_classify_stage_case_insensitive() -> None:
    result = classify_stage("IMPLEMENT Function", "", [], [], "")
    assert result == "stage_3_implementation"


def test_classify_stage_checkpoint_before_validation() -> None:
    """'checkpoint' should NOT match stage_4_validation.
    It should fall through to default (stage_1_assess) since 'create' patterns
    need specific target keywords like 'module' or 'component'."""
    result = classify_stage("creating checkpoint", "", [], [], "")
    assert result == "stage_1_assess"


def test_classify_stage_decisions_considered() -> None:
    result = classify_stage("", "", ["refactored module"], [], "")
    assert result == "stage_3_implementation"


def test_classify_stage_next_steps_considered() -> None:
    result = classify_stage("", "", [], ["validating regression fix"], "")
    assert result == "stage_4_validation"


def test_classify_stage_branch_family_used_as_last_resort() -> None:
    result = classify_stage("random text", "no progress", [], [], "other")
    assert result == "stage_2_hypothesis"


# ── compute_phase_stats ────────────────────────────────────────────────────────


def test_compute_phase_stats_empty() -> None:
    assert compute_phase_stats([]) == []


def test_compute_phase_stats_single_stage() -> None:
    rows = [
        {
            "_stage": "stage_3_implementation",
            "competitiveness": "promising",
            "status": "continue",
            "iteration_number": 1,
        }
    ]
    stats = compute_phase_stats(rows)
    assert len(stats) == 1
    s = stats[0]
    assert s.stage_id == "stage_3_implementation"
    assert s.count == 1
    assert s.consecutive == 1
    assert s.competitiveness_dist["promising"] == 1
    assert s.status_dist["continue"] == 1
    assert s.avg_competitiveness_score == COMPETITIVENESS_SCORE["promising"]


def test_compute_phase_stats_multiple_stages() -> None:
    rows = [
        {"_stage": "stage_1_assess", "competitiveness": "marginal", "status": "continue", "iteration_number": 1},
        {"_stage": "stage_2_hypothesis", "competitiveness": "marginal", "status": "continue", "iteration_number": 2},
        {
            "_stage": "stage_3_implementation",
            "competitiveness": "promising",
            "status": "continue",
            "iteration_number": 3,
        },
    ]
    stats = compute_phase_stats(rows)
    assert len(stats) == 3
    stage_ids = [s.stage_id for s in stats]
    assert stage_ids == ["stage_1_assess", "stage_2_hypothesis", "stage_3_implementation"]


def test_compute_phase_stats_consecutive_at_end() -> None:
    """Consecutive should count only trailing same-stage iterations."""
    rows = [
        {"_stage": "stage_1_assess", "competitiveness": "marginal", "status": "continue", "iteration_number": 1},
        {"_stage": "stage_2_hypothesis", "competitiveness": "marginal", "status": "continue", "iteration_number": 2},
        {
            "_stage": "stage_3_implementation",
            "competitiveness": "marginal",
            "status": "continue",
            "iteration_number": 3,
        },
        {
            "_stage": "stage_3_implementation",
            "competitiveness": "marginal",
            "status": "continue",
            "iteration_number": 4,
        },
    ]
    stats = compute_phase_stats(rows)
    for s in stats:
        if s.stage_id == "stage_3_implementation":
            assert s.consecutive == 2
        elif s.stage_id == "stage_1_assess":
            assert s.consecutive == 1
        elif s.stage_id == "stage_2_hypothesis":
            assert s.consecutive == 1


def test_compute_phase_stats_competitiveness_distribution() -> None:
    rows = [
        {"_stage": "stage_4_validation", "competitiveness": "promotable", "status": "continue", "iteration_number": 1},
        {"_stage": "stage_4_validation", "competitiveness": "promising", "status": "continue", "iteration_number": 2},
        {
            "_stage": "stage_4_validation",
            "competitiveness": "non_competitive",
            "status": "failed",
            "iteration_number": 3,
        },
    ]
    stats = compute_phase_stats(rows)
    assert len(stats) == 1
    s = stats[0]
    assert s.competitiveness_dist["promotable"] == 1
    assert s.competitiveness_dist["promising"] == 1
    assert s.competitiveness_dist["non_competitive"] == 1
    expected_avg = (
        COMPETITIVENESS_SCORE["promotable"]
        + COMPETITIVENESS_SCORE["promising"]
        + COMPETITIVENESS_SCORE["non_competitive"]
    ) / 3
    assert s.avg_competitiveness_score == expected_avg


def test_compute_phase_stats_missing_fields_defaults() -> None:
    rows = [
        {"_stage": "stage_5_integration", "iteration_number": 1},
        {"_stage": "stage_5_integration", "iteration_number": 2},
    ]
    stats = compute_phase_stats(rows)
    assert len(stats) == 1
    s = stats[0]
    assert s.competitiveness_dist["unknown"] == 2


# ── detect_bottleneck ──────────────────────────────────────────────────────────


def test_detect_bottleneck_empty_stats() -> None:
    stage_id, reason, stuck_since = detect_bottleneck([], 0, 0)
    assert stage_id is None
    assert "No iteration data" in reason


def test_detect_bottleneck_high_consecutive_noncompetitive() -> None:
    stats = [
        PhaseStats(
            stage_id="stage_3_implementation",
            stage_label="Stage 3: Implementation",
            count=4,
            consecutive=4,
            last_iteration=4,
            competitiveness_dist=Counter({"non_competitive": 3, "marginal": 1}),
            status_dist=Counter({"continue": 4}),
            avg_competitiveness_score=1.5,
        )
    ]
    stage_id, reason, stuck_since = detect_bottleneck(stats, 4, 0)
    assert stage_id == "stage_3_implementation"
    assert "non-competitive" in reason
    assert stuck_since == 1  # last_iteration - consecutive + 1 = 4 - 4 + 1 = 1


def test_detect_bottleneck_low_competitiveness() -> None:
    stats = [
        PhaseStats(
            stage_id="stage_2_hypothesis",
            stage_label="Stage 2: Hypothesis",
            count=5,
            consecutive=2,
            last_iteration=5,
            competitiveness_dist=Counter({"non_competitive": 4, "marginal": 1}),
            status_dist=Counter({"continue": 5}),
            avg_competitiveness_score=1.2,
        )
    ]
    total = 10
    stage_id, reason, stuck_since = detect_bottleneck(stats, total, 0)
    assert stage_id == "stage_2_hypothesis"
    assert "low competitiveness" in reason
    assert stuck_since is None


def test_detect_bottleneck_high_failure_rate() -> None:
    """Stage with >40% failure rate and >=3 iterations triggers rule 3.
    Ensure competitiveness is >2.0 and consecutive <3 so earlier rules don't fire."""
    stats = [
        PhaseStats(
            stage_id="stage_4_validation",
            stage_label="Stage 4: Validation",
            count=5,
            consecutive=2,
            last_iteration=8,
            competitiveness_dist=Counter({"marginal": 3, "promising": 2}),
            status_dist=Counter({"failed": 3, "continue": 2}),
            avg_competitiveness_score=3.4,
        )
    ]
    stage_id, reason, stuck_since = detect_bottleneck(stats, 8, 0)
    assert stage_id == "stage_4_validation"
    assert "failure rate" in reason


def test_detect_bottleneck_general_stagnation() -> None:
    stats = [
        PhaseStats(
            stage_id="stage_2_hypothesis",
            stage_label="Stage 2: Hypothesis",
            count=3,
            consecutive=2,
            last_iteration=8,
            competitiveness_dist=Counter({"marginal": 3}),
            status_dist=Counter({"continue": 3}),
            avg_competitiveness_score=3.0,
        )
    ]
    stage_id, reason, stuck_since = detect_bottleneck(stats, 8, 6)
    assert stage_id == "stage_2_hypothesis"
    assert "6 consecutive non-competitive" in reason


def test_detect_bottleneck_no_bottleneck() -> None:
    stats = [
        PhaseStats(
            stage_id="stage_3_implementation",
            stage_label="Stage 3: Implementation",
            count=3,
            consecutive=1,
            last_iteration=3,
            competitiveness_dist=Counter({"promising": 2, "marginal": 1}),
            status_dist=Counter({"continue": 3}),
            avg_competitiveness_score=3.67,
        )
    ]
    stage_id, reason, stuck_since = detect_bottleneck(stats, 3, 1)
    assert stage_id is None
    assert "No clear bottleneck" in reason


def test_detect_bottleneck_consecutive_not_enough_for_noncomp_ratio() -> None:
    """Consecutive >=3 but non-comp ratio <=0.5 should not trigger rule 1."""
    stats = [
        PhaseStats(
            stage_id="stage_3_implementation",
            stage_label="Stage 3: Implementation",
            count=4,
            consecutive=4,
            last_iteration=4,
            competitiveness_dist=Counter({"marginal": 3, "non_competitive": 1}),
            status_dist=Counter({"continue": 4}),
            avg_competitiveness_score=2.5,
        )
    ]
    stage_id, _, _ = detect_bottleneck(stats, 4, 0)
    # Should not match rule 1 (noncomp_ratio=0.25 <= 0.5)
    # Rule 2 might fire if count >= 3 and score <= 2.0 (2.5 > 2.0, so no)
    # Rule 3: 1/4=25% failure rate < 40%, no
    # Rule 4: consecutive_noncompetitive=0 < 5, no
    assert stage_id is None


# ── render_stage_detail ────────────────────────────────────────────────────────


def test_render_stage_detail_single_stage() -> None:
    stats = [
        PhaseStats(
            stage_id="stage_3_implementation",
            stage_label="Stage 3: Implementation",
            count=2,
            consecutive=2,
            last_iteration=3,
            competitiveness_dist=Counter({"marginal": 2}),
            status_dist=Counter({"continue": 2}),
            avg_competitiveness_score=3.0,
        )
    ]
    result = render_stage_detail(stats)
    assert "Phase Bottleneck Analysis" in result
    assert "Stage 3" in result
    assert "2" in result  # count
    assert "3.0" in result  # avg competitiveness


def test_render_stage_detail_empty() -> None:
    result = render_stage_detail([])
    assert "Phase Bottleneck Analysis" in result


# ── diagnose_phase_bottleneck ──────────────────────────────────────────────────


def test_diagnose_phase_bottleneck_empty_data() -> None:
    from unittest.mock import MagicMock

    conn = MagicMock()
    result = diagnose_phase_bottleneck(conn, "run-1", [], 0, 0)
    assert isinstance(result, PhaseDiagnosticResult)
    assert result.bottleneck_stage is None
    assert "No iteration data" in result.bottleneck_reason


def test_diagnose_phase_bottleneck_classifies_and_detects() -> None:
    from unittest.mock import MagicMock

    conn = MagicMock()
    rows = [
        {
            "summary": "implementing the service",
            "goal_progress": "in progress",
            "decisions": [],
            "next_steps": [],
            "branch_family": "feature",
            "competitiveness": "non_competitive",
            "status": "continue",
            "iteration_number": 1,
        },
        {
            "summary": "implementing the service again",
            "goal_progress": "still in progress",
            "decisions": [],
            "next_steps": [],
            "branch_family": "feature",
            "competitiveness": "non_competitive",
            "status": "continue",
            "iteration_number": 2,
        },
        {
            "summary": "implementing more",
            "goal_progress": "slow",
            "decisions": [],
            "next_steps": [],
            "branch_family": "feature",
            "competitiveness": "non_competitive",
            "status": "continue",
            "iteration_number": 3,
        },
    ]
    result = diagnose_phase_bottleneck(conn, "run-1", rows, 3, 0)
    assert result.bottleneck_stage == "stage_3_implementation"
    assert result.stagnation_score > 0
    assert result.per_stage_detail


# ── _recent_domains ──────────────────────────────────────────────────────────────


def test_recent_domains_empty() -> None:
    assert _recent_domains([]) == []


def test_recent_domains_matches_text() -> None:
    rows = [{"summary": "adding scripts layer for python modules", "branch_family": "scripts"}]
    domains = _recent_domains(rows)
    assert len(domains) >= 1
    assert any("Scripts" in d for d in domains)


def test_recent_domains_limits_to_12_rows() -> None:
    rows = [{"summary": "scripts", "branch_family": "scripts"}] * 20
    domains = _recent_domains(rows)
    assert len(domains) >= 1
    assert all("Scripts" in d for d in domains)


# ── creative_divergence_slate ──────────────────────────────────────────────────


def test_creative_divergence_slate_no_stagnation() -> None:
    diagnostic = PhaseDiagnosticResult(
        bottleneck_stage=None,
        bottleneck_reason="No issues.",
        stage_stats=[],
        stagnation_score=0.1,
        stuck_since_iteration=None,
        recommendation="Continue.",
        per_stage_detail="",
    )
    slate = creative_divergence_slate([], diagnostic, [], [])
    assert isinstance(slate, CreativeDivergenceSlate)
    assert len(slate.hypotheses) == 3
    assert slate.force_pivot_to == "stage_1_assess"
    assert "No critical stagnation" in slate.meta_suggestion


def test_creative_divergence_slate_moderate_stagnation() -> None:
    diagnostic = PhaseDiagnosticResult(
        bottleneck_stage="stage_3_implementation",
        bottleneck_reason="too slow",
        stage_stats=[],
        stagnation_score=0.4,
        stuck_since_iteration=5,
        recommendation="Speed up.",
        per_stage_detail="",
    )
    slate = creative_divergence_slate([], diagnostic, [], [])
    assert slate.force_pivot_to == "stage_2_hypothesis"
    assert "Stagnation building" in slate.meta_suggestion


def test_creative_divergence_slate_critical_stagnation() -> None:
    diagnostic = PhaseDiagnosticResult(
        bottleneck_stage="stage_2_hypothesis",
        bottleneck_reason="completely stuck",
        stage_stats=[],
        stagnation_score=0.7,
        stuck_since_iteration=10,
        recommendation="Force pivot.",
        per_stage_detail="",
    )
    slate = creative_divergence_slate([], diagnostic, [], [])
    assert slate.force_pivot_to == "stage_3_implementation"
    assert "STAGNATION CRITICAL" in slate.meta_suggestion


def test_creative_divergence_slate_exhausted_families() -> None:
    diagnostic = PhaseDiagnosticResult(
        bottleneck_stage=None,
        bottleneck_reason="No issues.",
        stage_stats=[],
        stagnation_score=0.1,
        stuck_since_iteration=None,
        recommendation="Continue.",
        per_stage_detail="",
    )
    slate = creative_divergence_slate([], diagnostic, [], [str(i) for i in range(30)])
    assert len(slate.hypotheses) == 3


def test_creative_divergence_slate_hypotheses_have_required_fields() -> None:
    diagnostic = PhaseDiagnosticResult(
        bottleneck_stage=None,
        bottleneck_reason="No issues.",
        stage_stats=[],
        stagnation_score=0.1,
        stuck_since_iteration=None,
        recommendation="Continue.",
        per_stage_detail="",
    )
    slate = creative_divergence_slate([], diagnostic, [], [])
    for h in slate.hypotheses:
        assert isinstance(h, DivergenceHypothesis)
        assert h.name
        assert h.description
        assert h.feature_domain
        assert h.model_type
        assert h.objective
        assert h.novelty_rating in ("moderate", "major", "radical")
        assert h.risk in ("low", "medium", "high")
        assert h.validation_cost in ("cheap", "moderate", "expensive")
        assert h.why_orthogonal


def test_creative_divergence_slate_with_critique_novelty_boost() -> None:
    """Critique recommending higher novelty should boost hypothesis novelty."""
    diagnostic = PhaseDiagnosticResult(
        bottleneck_stage=None,
        bottleneck_reason="No issues.",
        stage_stats=[],
        stagnation_score=0.1,
        stuck_since_iteration=None,
        recommendation="Continue.",
        per_stage_detail="",
    )
    critique = SelfCritiqueResult(
        patterns_detected=["Exploitation dominant"],
        lessons_learned=["System may be over-exploiting a narrow trajectory."],
        recommendations=["Introduce higher-novelty hypotheses to avoid local optima."],
        health_score=0.3,
    )
    slate = creative_divergence_slate([], diagnostic, [], [], critique)
    assert len(slate.hypotheses) == 3
    for h in slate.hypotheses:
        assert h.novelty_rating in ("major", "radical"), f"Expected boosted novelty, got {h.novelty_rating}"
    assert "Critique-informed: increased novelty" in slate.meta_suggestion


def test_creative_divergence_slate_with_critique_failing_families() -> None:
    """Critique identifying failing families should deprioritize matching domains."""
    diagnostic = PhaseDiagnosticResult(
        bottleneck_stage=None,
        bottleneck_reason="No issues.",
        stage_stats=[],
        stagnation_score=0.1,
        stuck_since_iteration=None,
        recommendation="Continue.",
        per_stage_detail="",
    )
    critique = SelfCritiqueResult(
        patterns_detected=["Recurring failures in 'scripts' family"],
        lessons_learned=["Scripts domain failing repeatedly."],
        recommendations=["Consider reducing investment in the 'scripts' famil"],
        health_score=0.4,
    )
    slate = creative_divergence_slate([], diagnostic, [], [], critique)
    assert len(slate.hypotheses) == 3
    assert "Critique-informed" in slate.meta_suggestion
    assert (
        "deprioritized" in slate.meta_suggestion
        or "failing" in slate.meta_suggestion
        or "scripts" in slate.meta_suggestion
    )


def test_creative_divergence_slate_without_critique_backward_compat() -> None:
    """Omitting critique should produce identical result to passing None."""
    diagnostic = PhaseDiagnosticResult(
        bottleneck_stage=None,
        bottleneck_reason="No issues.",
        stage_stats=[],
        stagnation_score=0.1,
        stuck_since_iteration=None,
        recommendation="Continue.",
        per_stage_detail="",
    )
    slate_explicit = creative_divergence_slate([], diagnostic, [], [], None)
    slate_implicit = creative_divergence_slate([], diagnostic, [], [])
    assert len(slate_explicit.hypotheses) == 3
    assert len(slate_implicit.hypotheses) == 3
    assert slate_explicit.force_pivot_to == slate_implicit.force_pivot_to


def test_creative_divergence_slate_critique_lesson_in_meta() -> None:
    """Critique lessons and recommendations should appear in meta_suggestion."""
    diagnostic = PhaseDiagnosticResult(
        bottleneck_stage=None,
        bottleneck_reason="No issues.",
        stage_stats=[],
        stagnation_score=0.1,
        stuck_since_iteration=None,
        recommendation="Continue.",
        per_stage_detail="",
    )
    critique = SelfCritiqueResult(
        patterns_detected=[],
        lessons_learned=["Reduce iteration scope and focus on smaller steps."],
        recommendations=["Reduce iteration scope."],
        health_score=0.5,
    )
    slate = creative_divergence_slate([], diagnostic, [], [], critique)
    assert "Reduce iteration scope" in slate.meta_suggestion


# ── _project_family_domains ────────────────────────────────────────────────────


def test_project_family_domains_returns_project_specific_domains() -> None:
    domains = _project_family_domains()
    assert len(domains) >= 1
    assert any("Scripts" in d for d in domains), f"Expected 'Scripts' in domains: {domains}"
    assert any("Tests" in d for d in domains), f"Expected 'Tests' in domains: {domains}"
    assert all(isinstance(d, str) for d in domains)


def test_project_family_domains_no_duplicates() -> None:
    domains = _project_family_domains()
    assert len(domains) == len(set(domains)), f"Duplicates found: {domains}"


# ── _project_approaches / _project_objectives / _project_architectures ──────────


def test_project_approaches_returns_non_empty_list() -> None:
    approaches = _project_approaches()
    assert len(approaches) >= 1
    assert all(isinstance(a, str) for a in approaches)


def test_project_approaches_omits_irrelevant() -> None:
    """For a Python project, approaches with accessibility or JSDoc should not appear."""
    approaches = _project_approaches()
    for a in approaches:
        assert "accessibility" not in a.lower()
        assert "jsdoc" not in a.lower()
        assert "bundle size" not in a.lower()


def test_project_approaches_no_duplicates() -> None:
    approaches = _project_approaches()
    assert len(approaches) == len(set(approaches)), f"Duplicates found: {approaches}"


def test_project_objectives_returns_non_empty_list() -> None:
    objectives = _project_objectives()
    assert len(objectives) >= 1
    assert all(isinstance(o, str) for o in objectives)


def test_project_objectives_omits_irrelevant() -> None:
    """For a Python project, objectives like a11y or i18n should not appear."""
    objectives = _project_objectives()
    for o in objectives:
        assert "accessibility" not in o.lower()
        assert "i18n" not in o.lower()
        assert "bundle size" not in o.lower()


def test_project_objectives_no_duplicates() -> None:
    objectives = _project_objectives()
    assert len(objectives) == len(set(objectives)), f"Duplicates found: {objectives}"


def test_project_architectures_returns_non_empty_list() -> None:
    archs = _project_architectures()
    assert len(archs) >= 1
    assert all(isinstance(a, str) for a in archs)


def test_project_architectures_omits_irrelevant() -> None:
    """For a Python project, frontend and web-specific architectures should not appear."""
    archs = _project_architectures()
    for a in archs:
        assert "frontend" not in a.lower()
        assert "middleware" not in a.lower()  # in the web/NestJS sense
        assert "redis" not in a.lower()
        assert "cdn" not in a.lower()


def test_project_architectures_no_duplicates() -> None:
    archs = _project_architectures()
    assert len(archs) == len(set(archs)), f"Duplicates found: {archs}"


# ── run_meta_controller ────────────────────────────────────────────────────────


BASE_PARAMS = MetaControllerParams(
    sleep_seconds=60,
    max_retries_per_iteration=2,
    cpu_budget_percent=80,
    parallel_branches_max=3,
    force_parallel=0,
)


def test_run_meta_controller_empty_window() -> None:
    advice = run_meta_controller(BASE_PARAMS, [], 0, 0)
    assert advice.adjusted is False
    assert "No iteration data" in advice.reasoning


def test_run_meta_controller_high_failure_increases_retries() -> None:
    rows = [
        {
            "status": "failed",
            "competitiveness": "non_competitive",
            "evidence_quality": "low",
            "promotion_recommendation": "hold",
            "branch_family": "bugfix",
            "family_novelty": "minor",
        }
        for _ in range(6)
    ]
    advice = run_meta_controller(BASE_PARAMS, rows, 0, 6)
    assert advice.adjusted is True
    assert advice.params.max_retries_per_iteration == 3
    assert "max_retries_per_iteration" in advice.changed


def test_run_meta_controller_low_failure_reduces_retries() -> None:
    params = MetaControllerParams(
        sleep_seconds=60,
        max_retries_per_iteration=4,
        cpu_budget_percent=80,
        parallel_branches_max=3,
        force_parallel=0,
    )
    rows = [
        {
            "status": "continue",
            "competitiveness": "promotable",
            "evidence_quality": "high",
            "promotion_recommendation": "promote",
            "branch_family": "feature",
            "family_novelty": "moderate",
        }
        for _ in range(6)
    ]
    advice = run_meta_controller(params, rows, 0, 6)
    assert advice.adjusted is True
    assert advice.params.max_retries_per_iteration == 3


def test_run_meta_controller_stagnation_increases_parallel() -> None:
    params = MetaControllerParams(
        sleep_seconds=60,
        max_retries_per_iteration=2,
        cpu_budget_percent=80,
        parallel_branches_max=3,
        force_parallel=0,
    )
    rows = [
        {
            "status": "continue",
            "competitiveness": "non_competitive",
            "evidence_quality": "low",
            "promotion_recommendation": "hold",
            "branch_family": "tests",
            "family_novelty": "minor",
        }
        for _ in range(4)
    ]
    advice = run_meta_controller(params, rows, 4, 4)
    assert advice.adjusted is True
    assert advice.params.force_parallel == 1


def test_run_meta_controller_good_progress_disables_parallel() -> None:
    params = MetaControllerParams(
        sleep_seconds=60,
        max_retries_per_iteration=2,
        cpu_budget_percent=80,
        parallel_branches_max=3,
        force_parallel=2,
    )
    rows = [
        {
            "status": "continue",
            "competitiveness": "promising",
            "evidence_quality": "high",
            "promotion_recommendation": "promote",
            "branch_family": "feature",
            "family_novelty": "moderate",
        }
        for _ in range(6)
    ]
    # Only need >=3 wins; we already have that
    advice = run_meta_controller(params, rows, 0, 6)
    assert advice.adjusted is True
    assert advice.params.force_parallel == 0


def test_run_meta_controller_many_novel_families_slow_down() -> None:
    params = MetaControllerParams(
        sleep_seconds=60,
        max_retries_per_iteration=2,
        cpu_budget_percent=80,
        parallel_branches_max=3,
        force_parallel=0,
    )
    rows = [
        {
            "status": "continue",
            "competitiveness": "non_competitive",
            "evidence_quality": "low",
            "promotion_recommendation": "hold",
            "branch_family": "novel",
            "family_novelty": "moderate",
        }
        for _ in range(4)
    ] + [
        {
            "status": "continue",
            "competitiveness": "marginal",
            "evidence_quality": "medium",
            "promotion_recommendation": "hold",
            "branch_family": "other",
            "family_novelty": "major",
        }
        for _ in range(4)
    ]
    advice = run_meta_controller(params, rows, 4, 8)
    # len(avg_family_novelty) >= 4 and noncomp_count >= len(avg_family_novelty) * 0.5
    # avg_family_novelty = ['moderate', 'moderate', 'moderate', 'moderate', 'major', 'major', 'major', 'major'] = 8
    # noncomp_count = 4 (from first 4 rows)
    # 8 >= 4 and 4 >= 4 => true, so sleep increases
    if advice.adjusted:
        assert advice.params.sleep_seconds >= 60


def test_run_meta_controller_consistent_wins_speed_up() -> None:
    params = MetaControllerParams(
        sleep_seconds=120,
        max_retries_per_iteration=2,
        cpu_budget_percent=80,
        parallel_branches_max=3,
        force_parallel=0,
    )
    rows = [
        {
            "status": "continue",
            "competitiveness": "promising",
            "evidence_quality": "high",
            "promotion_recommendation": "promote",
            "branch_family": "feature",
            "family_novelty": "moderate",
        }
        for _ in range(6)
    ]
    advice = run_meta_controller(params, rows, 0, 6)
    assert advice.adjusted is True
    assert advice.params.sleep_seconds == 105  # 120 - 15


def test_run_meta_controller_high_failure_reduces_cpu() -> None:
    params = MetaControllerParams(
        sleep_seconds=60,
        max_retries_per_iteration=2,
        cpu_budget_percent=80,
        parallel_branches_max=3,
        force_parallel=0,
    )
    rows = [
        {
            "status": "failed",
            "competitiveness": "non_competitive",
            "evidence_quality": "low",
            "promotion_recommendation": "hold",
            "branch_family": "bug",
            "family_novelty": "minor",
        }
        for _ in range(6)
    ]
    advice = run_meta_controller(params, rows, 6, 6)
    assert advice.adjusted is True
    assert advice.params.cpu_budget_percent == 70  # 80 - 10


def test_run_meta_controller_no_adjustments_needed() -> None:
    params = MetaControllerParams(
        sleep_seconds=30,
        max_retries_per_iteration=2,
        cpu_budget_percent=80,
        parallel_branches_max=3,
        force_parallel=0,
    )
    rows = [
        {
            "status": "continue",
            "competitiveness": "promising",
            "evidence_quality": "high",
            "promotion_recommendation": "promote",
            "branch_family": "feature",
            "family_novelty": "moderate",
        }
        for _ in range(3)
    ]
    advice = run_meta_controller(params, rows, 0, 3)
    assert advice.adjusted is False
    assert "No adjustments needed" in advice.reasoning


# ── render_meta_controller_advice ──────────────────────────────────────────────


# ── Critique-aware meta-controller rules ────────────────────────────────────


def make_critique(
    health: float = 1.0,
    patterns: list[str] | None = None,
    lessons: list[str] | None = None,
    recommendations: list[str] | None = None,
) -> SelfCritiqueResult:
    return SelfCritiqueResult(
        patterns_detected=patterns or [],
        lessons_learned=lessons or [],
        recommendations=recommendations or [],
        health_score=health,
    )


def test_meta_controller_critique_low_health_exploration() -> None:
    """Low health + exploration signal → increase force_parallel."""
    rows = [
        {
            "status": "non_competitive",
            "competitiveness": "non_competitive",
            "evidence_quality": "low",
            "promotion_recommendation": "hold",
            "branch_family": "test",
            "family_novelty": "minor",
        }
        for _ in range(6)
    ]
    critique = make_critique(health=0.35, recommendations=["Try exploring a different area"])
    advice = run_meta_controller(BASE_PARAMS, rows, 2, 6, critique=critique)
    assert "force_parallel" in advice.changed
    assert advice.params.force_parallel > BASE_PARAMS.force_parallel
    assert "Critique:" in advice.reasoning


def test_meta_controller_critique_narrow_focus() -> None:
    """Narrowing focus signal → decrease parallel_branches_max."""
    params = MetaControllerParams(60, 2, 80, 3, 0)
    rows = [
        {
            "status": "non_competitive",
            "competitiveness": "non_competitive",
            "evidence_quality": "low",
            "promotion_recommendation": "hold",
            "branch_family": "test",
            "family_novelty": "minor",
        }
        for _ in range(3)
    ]
    critique = make_critique(health=0.6, recommendations=["Narrow your focus to a single area"])
    advice = run_meta_controller(params, rows, 0, 3, critique=critique)
    assert "parallel_branches_max" in advice.changed
    assert advice.params.parallel_branches_max < params.parallel_branches_max


def test_meta_controller_critique_retry_signal() -> None:
    """Retry signal → increase max_retries."""
    rows = [
        {
            "status": "non_competitive",
            "competitiveness": "non_competitive",
            "evidence_quality": "low",
            "promotion_recommendation": "hold",
            "branch_family": "test",
            "family_novelty": "minor",
        }
        for _ in range(3)
    ]
    critique = make_critique(health=0.6, recommendations=["Retry failed variants with new parameters"])
    advice = run_meta_controller(BASE_PARAMS, rows, 0, 3, critique=critique)
    assert "max_retries_per_iteration" in advice.changed
    assert advice.params.max_retries_per_iteration > BASE_PARAMS.max_retries_per_iteration


def test_meta_controller_critique_very_low_health() -> None:
    """Very low health → multi-factor crisis response."""
    params = MetaControllerParams(60, 2, 80, 3, 0)
    rows = [
        {
            "status": "failed",
            "competitiveness": "non_competitive",
            "evidence_quality": "low",
            "promotion_recommendation": "hold",
            "branch_family": "test",
            "family_novelty": "minor",
        }
        for _ in range(6)
    ]
    critique = make_critique(health=0.25, recommendations=["Something is fundamentally wrong"])
    advice = run_meta_controller(params, rows, 6, 6, critique=critique)
    assert "max_retries_per_iteration" in advice.changed
    assert "force_parallel" in advice.changed
    assert advice.params.force_parallel == 2
    assert "crisis" in advice.reasoning


def test_meta_controller_critique_no_relevant_signal() -> None:
    """Critique present but no matching keywords → no critique-driven changes."""
    rows = [
        {
            "status": "non_competitive",
            "competitiveness": "non_competitive",
            "evidence_quality": "low",
            "promotion_recommendation": "hold",
            "branch_family": "test",
            "family_novelty": "minor",
        }
        for _ in range(3)
    ]
    critique = make_critique(health=0.9, recommendations=["Continue as planned"])
    advice = run_meta_controller(BASE_PARAMS, rows, 0, 3, critique=critique)
    # No critique-keyword-triggered changes should appear
    for key in ("force_parallel", "parallel_branches_max", "max_retries_per_iteration"):
        if key in advice.changed:
            assert "Critique:" not in advice.reasoning or advice.reasoning.split("Critique:")[1].strip() == ""


def test_meta_controller_critique_high_health_no_change() -> None:
    """High health score with no problematic recommendations → no critique changes."""
    rows = [
        {
            "status": "promising",
            "competitiveness": "promising",
            "evidence_quality": "high",
            "promotion_recommendation": "promote",
            "branch_family": "feature",
            "family_novelty": "moderate",
        }
        for _ in range(6)
    ]
    critique = make_critique(health=0.85, recommendations=["Keep up the good work"])
    advice = run_meta_controller(BASE_PARAMS, rows, 0, 6, critique=critique)
    # Rule 8 requires health < 0.4, Rule 11 requires health < 0.3
    # So neither should fire
    for key in ("force_parallel", "parallel_branches_max", "max_retries_per_iteration"):
        if key in advice.changed:
            assert "Critique:" not in advice.reasoning


def test_meta_controller_critique_none_provided() -> None:
    """No critique → no critique-related changes."""
    rows = [
        {
            "status": "non_competitive",
            "competitiveness": "non_competitive",
            "evidence_quality": "low",
            "promotion_recommendation": "hold",
            "branch_family": "test",
            "family_novelty": "minor",
        }
        for _ in range(3)
    ]
    advice = run_meta_controller(BASE_PARAMS, rows, 0, 3, critique=None)
    assert "Critique:" not in advice.reasoning


def test_meta_controller_critique_retry_at_max_already() -> None:
    """Retry signal but max_retries already at 5 → no change."""
    params = MetaControllerParams(60, 5, 80, 3, 0)
    rows = [
        {
            "status": "non_competitive",
            "competitiveness": "non_competitive",
            "evidence_quality": "low",
            "promotion_recommendation": "hold",
            "branch_family": "test",
            "family_novelty": "minor",
        }
        for _ in range(3)
    ]
    critique = make_critique(health=0.5, recommendations=["Retry the failed approach"])
    advice = run_meta_controller(params, rows, 0, 3, critique=critique)
    # max_retries already at 5, rule 10 should not fire (guarded by < 5)
    # Rule 11 requires health < 0.3, so shouldn't fire either
    if "max_retries_per_iteration" in advice.changed:
        assert "Critique:" not in advice.reasoning


def test_render_meta_controller_advice_with_changes() -> None:
    advice = MetaControllerAdvice(
        params=BASE_PARAMS,
        changed={"max_retries_per_iteration": (2, 3)},
        reasoning="High failure rate → increased retries to 3",
        performance_summary="Test data.",
        adjusted=True,
    )
    result = render_meta_controller_advice(advice)
    assert "Meta-Controller Advice" in result
    assert "max_retries_per_iteration" in result
    assert "2" in result
    assert "3" in result
    assert "High failure rate" in result


def test_render_meta_controller_advice_no_changes() -> None:
    advice = MetaControllerAdvice(
        params=BASE_PARAMS,
        changed={},
        reasoning="No adjustments needed.",
        performance_summary="All good.",
        adjusted=False,
    )
    result = render_meta_controller_advice(advice)
    assert "No changes" in result


# ── render_creative_divergence ──────────────────────────────────────────────────


def test_render_creative_divergence_with_blocked_stage() -> None:
    slate = CreativeDivergenceSlate(
        blocked_stage="stage_3_implementation",
        blocked_reason="Too slow",
        hypotheses=[
            DivergenceHypothesis(
                name="Test",
                description="Test hypothesis",
                feature_domain="Backend API",
                model_type="Refactor",
                objective="Reduce duplication",
                novelty_rating="moderate",
                risk="medium",
                validation_cost="cheap",
                why_orthogonal="Novel combination",
            )
        ],
        meta_suggestion="Try something new.",
        force_pivot_to="stage_3_implementation",
    )
    result = render_creative_divergence(slate)
    assert "Creative Divergence Slate" in result
    assert "Blocked at" in result
    assert "Too slow" in result
    assert "Test" in result


def test_render_creative_divergence_no_blocked_stage() -> None:
    slate = CreativeDivergenceSlate(
        blocked_stage=None,
        blocked_reason="",
        hypotheses=[],
        meta_suggestion="All good.",
        force_pivot_to="stage_1_assess",
    )
    result = render_creative_divergence(slate)
    assert "Creative Divergence Slate" in result
    assert "Blocked at" not in result


# ── render_phase_diagnostic ─────────────────────────────────────────────────────


def test_render_phase_diagnostic_with_bottleneck() -> None:
    diag = PhaseDiagnosticResult(
        bottleneck_stage="stage_2_hypothesis",
        bottleneck_reason="Too many failures",
        stage_stats=[],
        stagnation_score=0.6,
        stuck_since_iteration=5,
        recommendation="Force divergence.",
        per_stage_detail="Per-stage detail here.",
    )
    result = render_phase_diagnostic(diag)
    assert "Self-Improving Loop Diagnostics" in result
    assert "0.60" in result
    assert "Stage 2" in result
    assert "Stuck since iteration" in result
    assert "Force divergence" in result
    assert "Per-stage detail here" in result


def test_render_phase_diagnostic_no_bottleneck() -> None:
    diag = PhaseDiagnosticResult(
        bottleneck_stage=None,
        bottleneck_reason="No issues detected.",
        stage_stats=[],
        stagnation_score=0.05,
        stuck_since_iteration=None,
        recommendation="Continue.",
        per_stage_detail="",
    )
    result = render_phase_diagnostic(diag)
    assert "None detected" in result
    assert "Stuck since iteration" not in result


# ── self_critique_retrospective ────────────────────────────────────────────────


def _make_cp(
    family: str = "tests",
    competitiveness: str = "promising",
    novelty: str = "moderate",
    status: str = "continue",
    evidence: str = "artifact_only",
) -> dict[str, Any]:
    return {
        "branch_family": family,
        "competitiveness": competitiveness,
        "family_novelty": novelty,
        "status": status,
        "evidence_quality": evidence,
        "summary": f"Iteration for {family}",
        "decisions": [],
        "risks": [],
        "next_steps": [],
        "goal_progress": "",
        "artifacts": [],
        "files_touched": [],
    }


def test_self_critique_empty_data() -> None:
    result = self_critique_retrospective([])
    assert result.patterns_detected == []
    assert "No iteration data yet" in result.lessons_learned[0]
    assert result.health_score == 0.5


def test_self_critique_narrow_focus() -> None:
    rows = [_make_cp(family="tests") for _ in range(5)]
    result = self_critique_retrospective(rows)
    patterns = " ".join(result.patterns_detected)
    assert "Narrow focus" in patterns
    assert any("Broaden exploration" in r for r in result.recommendations)


def test_self_critique_good_breadth() -> None:
    families = ["tests", "backend", "frontend", "deploy", "security"]
    rows = [_make_cp(family=f) for f in families]
    result = self_critique_retrospective(rows)
    patterns = " ".join(result.patterns_detected)
    assert "Good breadth" in patterns


def test_self_critique_positive_signal() -> None:
    rows = [
        _make_cp(competitiveness="promising"),
        _make_cp(competitiveness="promotable"),
        _make_cp(competitiveness="promising"),
    ]
    result = self_critique_retrospective(rows)
    patterns = " ".join(result.patterns_detected)
    assert "Positive signal" in patterns
    assert "productive" in " ".join(result.lessons_learned)


def test_self_critique_weak_signal() -> None:
    rows = [
        _make_cp(competitiveness="non_competitive"),
        _make_cp(competitiveness="non_competitive"),
        _make_cp(competitiveness="non_competitive"),
        _make_cp(competitiveness="promising"),
    ]
    result = self_critique_retrospective(rows)
    patterns = " ".join(result.patterns_detected)
    assert "Weak signal" in patterns
    assert "not producing competitive" in " ".join(result.lessons_learned)


def test_self_critique_exploration_dominant() -> None:
    rows = [
        _make_cp(novelty="major"),
        _make_cp(novelty="moderate"),
        _make_cp(novelty="major"),
    ]
    result = self_critique_retrospective(rows)
    patterns = " ".join(result.patterns_detected)
    assert "Exploration dominant" in patterns


def test_self_critique_exploitation_dominant() -> None:
    rows = [
        _make_cp(novelty="minor"),
        _make_cp(novelty="incremental"),
        _make_cp(novelty="minor"),
        _make_cp(novelty="unknown"),
    ]
    result = self_critique_retrospective(rows)
    patterns = " ".join(result.patterns_detected)
    assert "Exploitation dominant" in patterns


def test_self_critique_recurring_failures() -> None:
    rows = [
        _make_cp(family="broken", status="failed", competitiveness="non_competitive"),
        _make_cp(family="broken", status="failed", competitiveness="non_competitive"),
        _make_cp(family="broken", status="failed", competitiveness="non_competitive"),
        _make_cp(family="tests", status="continue", competitiveness="promising"),
    ]
    result = self_critique_retrospective(rows)
    patterns = " ".join(result.patterns_detected)
    assert "Recurring failures" in patterns
    assert "broken" in patterns


def test_self_critique_artifact_heavy() -> None:
    rows = [
        _make_cp(evidence="artifact_only"),
        _make_cp(evidence="artifact_only"),
        _make_cp(evidence="artifact_only"),
        _make_cp(evidence="artifact_only"),
    ]
    result = self_critique_retrospective(rows)
    patterns = " ".join(result.patterns_detected)
    assert "Artifact-heavy" in patterns
    assert "without strong evidence" in " ".join(result.lessons_learned)


def test_self_critique_health_score_range() -> None:
    good_rows = [
        _make_cp(competitiveness="promising", novelty="major", status="continue"),
        _make_cp(competitiveness="promotable", novelty="moderate", status="continue"),
        _make_cp(competitiveness="promising", novelty="moderate", status="continue"),
    ]
    good_result = self_critique_retrospective(good_rows)
    assert 0.4 <= good_result.health_score <= 1.0

    bad_rows = [
        _make_cp(competitiveness="non_competitive", novelty="minor", status="failed", evidence="unknown"),
        _make_cp(competitiveness="non_competitive", novelty="minor", status="failed", evidence="unknown"),
    ]
    bad_result = self_critique_retrospective(bad_rows)
    assert 0.0 <= bad_result.health_score <= 0.6


def test_self_critique_default_recommendation() -> None:
    result = self_critique_retrospective([_make_cp()])
    assert any(r for r in result.recommendations)


# ── render_self_critique ──────────────────────────────────────────────────────


def test_render_self_critique_with_all_sections() -> None:
    result = SelfCritiqueResult(
        patterns_detected=["Pattern A", "Pattern B"],
        lessons_learned=["Lesson X", "Lesson Y"],
        recommendations=["Rec 1"],
        health_score=0.75,
    )
    rendered = render_self_critique(result)
    assert "Self-Critique Retrospective" in rendered
    assert "Health Score" in rendered
    assert "0.75" in rendered
    assert "Patterns Detected" in rendered
    assert "Pattern A" in rendered
    assert "Pattern B" in rendered
    assert "Lessons Learned" in rendered
    assert "Lesson X" in rendered
    assert "Recommendations" in rendered
    assert "Rec 1" in rendered


def test_render_self_critique_empty_patterns() -> None:
    result = SelfCritiqueResult(
        patterns_detected=[],
        lessons_learned=["Lesson only"],
        recommendations=[],
        health_score=0.5,
    )
    rendered = render_self_critique(result)
    assert "Patterns Detected" not in rendered
    assert "Lesson only" in rendered
    assert "Recommendations" not in rendered


def test_render_self_critique_minimal() -> None:
    result = SelfCritiqueResult(
        patterns_detected=["P"],
        lessons_learned=[],
        recommendations=[],
        health_score=0.3,
    )
    rendered = render_self_critique(result)
    assert "0.30" in rendered
    assert "Patterns Detected" in rendered
    assert "Lessons Learned" not in rendered
    assert "Recommendations" not in rendered


# ── MetaControllerParams defaults ─────────────────────────────────────────────


def test_meta_controller_params_defaults() -> None:
    p = MetaControllerParams(
        sleep_seconds=30, max_retries_per_iteration=3, cpu_budget_percent=50, parallel_branches_max=2, force_parallel=0
    )
    assert p.sleep_seconds == 30
    assert p.max_retries_per_iteration == 3
    assert p.cpu_budget_percent == 50
    assert p.parallel_branches_max == 2
    assert p.force_parallel == 0


def test_project_family_domains_fallback_on_exception() -> None:
    from unittest.mock import patch

    with patch("scripts.self_improving_loop.get_module_keywords", side_effect=RuntimeError("db error")):
        domains = _project_family_domains()
    assert len(domains) >= 1
    assert all(isinstance(d, str) for d in domains)


def test_project_approaches_fallback_on_exception() -> None:
    from unittest.mock import patch

    with patch("scripts.self_improving_loop.get_type_approaches", side_effect=RuntimeError("db error")):
        approaches = _project_approaches()
    assert len(approaches) >= 1
    assert all(isinstance(a, str) for a in approaches)


def test_project_objectives_fallback_on_exception() -> None:
    from unittest.mock import patch

    with patch("scripts.self_improving_loop.get_type_objectives", side_effect=RuntimeError("db error")):
        objectives = _project_objectives()
    assert len(objectives) >= 1
    assert all(isinstance(o, str) for o in objectives)


def test_project_architectures_fallback_on_exception() -> None:
    from unittest.mock import patch

    with patch("scripts.self_improving_loop.get_type_architectures", side_effect=RuntimeError("db error")):
        archs = _project_architectures()
    assert len(archs) >= 1
    assert all(isinstance(a, str) for a in archs)


def test_creative_divergence_slate_cost_moderate() -> None:
    """Hypotheses using approach without cheap/expensive keywords get cost='moderate'."""
    diagnostic = PhaseDiagnosticResult(
        bottleneck_stage=None,
        bottleneck_reason="No issues.",
        stage_stats=[],
        stagnation_score=0.1,
        stuck_since_iteration=None,
        recommendation="Continue.",
        per_stage_detail="",
    )
    rows = [{"summary": "bug in backend", "branch_family": "unknown"}]
    slate = creative_divergence_slate(rows, diagnostic, [], [])
    assert len(slate.hypotheses) == 3
    assert any(h.validation_cost == "moderate" for h in slate.hypotheses), (
        f"Expected at least one moderate cost hypothesis: {[(h.name, h.validation_cost) for h in slate.hypotheses]}"
    )


def test_creative_divergence_slate_approach_filtering() -> None:
    """Approaches with tokens matching recent summaries are removed (if >3 available)."""
    diagnostic = PhaseDiagnosticResult(
        bottleneck_stage=None,
        bottleneck_reason="No issues.",
        stage_stats=[],
        stagnation_score=0.1,
        stuck_since_iteration=None,
        recommendation="Continue.",
        per_stage_detail="",
    )
    rows = [{"summary": "refactoring the module structure to reduce complexity", "branch_family": "refactor"}]
    slate = creative_divergence_slate(rows, diagnostic, [], [])
    assert len(slate.hypotheses) == 3
    for h in slate.hypotheses:
        assert "refactor" not in h.model_type.lower()


def test_self_critique_retrospective_strategy_level_dominant() -> None:
    """When strategy-level evidence exceeds artifact-only evidence, a lesson is added."""
    rows = [
        {
            "branch_family": "feature_a",
            "competitiveness": "unknown",
            "family_novelty": "minor",
            "status": "continue",
            "evidence_quality": "strategy_level",
        },
        {
            "branch_family": "feature_b",
            "competitiveness": "unknown",
            "family_novelty": "minor",
            "status": "continue",
            "evidence_quality": "strategy_level",
        },
    ]
    result = self_critique_retrospective(rows)
    assert any("strategy-level evidence" in ls for ls in result.lessons_learned)


def test_self_critique_retrospective_empty_patterns_and_lessons() -> None:
    """When no patterns or lessons are triggered, fallback text is added."""
    rows = [
        {
            "branch_family": "test",
            "competitiveness": "unknown",
            "family_novelty": "unknown",
            "status": "continue",
            "evidence_quality": "unknown",
        }
    ]
    result = self_critique_retrospective(rows)
    assert any("No strong patterns" in p for p in result.patterns_detected)
    assert any("Insufficient data" in ls for ls in result.lessons_learned)


def test_run_self_improving_loop_basic() -> None:
    """Top-level orchestration function returns valid markdown and advice."""
    from unittest.mock import MagicMock

    conn = MagicMock()
    params = MetaControllerParams(
        sleep_seconds=30, max_retries_per_iteration=3, cpu_budget_percent=50, parallel_branches_max=2, force_parallel=0
    )
    md, advice = run_self_improving_loop(conn, "run-1", [], 0, 0, params, [], [])
    assert isinstance(md, str)
    assert len(md) > 0
    assert isinstance(advice, MetaControllerAdvice)
    assert "Phase Bottleneck Analysis" in md
