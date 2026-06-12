"""Tests for the controller module (scripts/controller.py)."""
# mypy: disable-error-code="arg-type,list-item,type-arg"

from __future__ import annotations

import argparse
import json
from typing import Any
from unittest.mock import MagicMock, patch

from scripts.controller import (
    CONTROLLER_DEFAULT_STOP_AFTER,
    CONTROLLER_DIVERSITY_WINDOW,
    CONTROLLER_EXHAUST_FAMILY_AFTER,
    CONTROLLER_HARD_PIVOT_AFTER,
    NON_TRANSIENT_FAILURE_PATTERNS,
    TRANSIENT_FAILURE_PATTERNS,
    checkpoint_signals_goal_complete,
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


# ── render_list ──────────────────────────────────────────────────────────────


def test_render_list_empty() -> None:
    result = render_list("Test Title", [])
    assert result == "# Test Title\n\n- None recorded yet.\n"


def test_render_list_with_items() -> None:
    result = render_list("Fruits", ["apple", "banana", "cherry"])
    assert result == "# Fruits\n\n- apple\n- banana\n- cherry\n"


def test_render_list_single_item() -> None:
    result = render_list("Single", ["only"])
    assert result == "# Single\n\n- only\n"


# ── goal_stop_after_iterations ───────────────────────────────────────────────


def test_goal_stop_after_empty_text() -> None:
    assert goal_stop_after_iterations("") == CONTROLLER_DEFAULT_STOP_AFTER


def test_goal_stop_after_none_text() -> None:
    assert goal_stop_after_iterations(None) == CONTROLLER_DEFAULT_STOP_AFTER  # type: ignore[arg-type]


def test_goal_stop_after_after_n() -> None:
    assert goal_stop_after_iterations("after 5 tries") == 5


def test_goal_stop_after_after_n_iterations() -> None:
    assert goal_stop_after_iterations("after 10 iterations") == 10


def test_goal_stop_after_stop_after_n() -> None:
    assert goal_stop_after_iterations("stop after 25") == 25


def test_goal_stop_after_case_insensitive() -> None:
    assert goal_stop_after_iterations("AFTER 3 TRIES") == 3


def test_goal_stop_after_attempts() -> None:
    assert goal_stop_after_iterations("after 7 attempts") == 7


def test_goal_stop_after_zero_rejected() -> None:
    assert goal_stop_after_iterations("after 0 tries") == CONTROLLER_DEFAULT_STOP_AFTER


def test_goal_stop_after_no_match() -> None:
    assert goal_stop_after_iterations("just keep going forever") == CONTROLLER_DEFAULT_STOP_AFTER


# ── recent_family_diversity ──────────────────────────────────────────────────


def test_recent_family_diversity_empty() -> None:
    result = recent_family_diversity([])
    assert result["window"] == 0
    assert result["unique_count"] == 0
    assert result["dominant_family"] == "unknown"
    assert result["low_diversity"] is False


def test_recent_family_diversity_single_entry() -> None:
    result = recent_family_diversity([{"branch_family": "tests"}])
    assert result["window"] == 1
    assert result["unique_count"] == 1
    assert result["dominant_family"] == "tests"
    assert result["low_diversity"] is False


def test_recent_family_diversity_diverse() -> None:
    entries = [
        {"branch_family": "tests"},
        {"branch_family": "scripts"},
        {"branch_family": "docs"},
    ]
    result = recent_family_diversity(entries)
    assert result["window"] == 3
    assert result["unique_count"] == 3
    assert result["low_diversity"] is False


def test_recent_family_diversity_dominant_share_triggers() -> None:
    entries = [
        {"branch_family": "tests"},
        {"branch_family": "tests"},
        {"branch_family": "scripts"},
    ]
    result = recent_family_diversity(entries)
    assert result["low_diversity"] is True
    assert result["dominant_family"] == "tests"
    assert result["dominant_share"] == 2 / 3


def test_recent_family_diversity_same_family_streak_triggers() -> None:
    entries = [
        {"branch_family": "tests"},
        {"branch_family": "tests"},
        {"branch_family": "tests"},
    ]
    result = recent_family_diversity(entries)
    assert result["low_diversity"] is True
    assert result["same_family_streak"] == 3


def test_recent_family_diversity_four_entries_two_families() -> None:
    entries = [
        {"branch_family": "tests"},
        {"branch_family": "scripts"},
        {"branch_family": "tests"},
        {"branch_family": "scripts"},
    ]
    result = recent_family_diversity(entries)
    assert result["window"] == 4
    assert result["unique_count"] == 2
    assert result["low_diversity"] is True


def test_recent_family_diversity_respects_window() -> None:
    entries = [{"branch_family": f"fam_{i}"} for i in range(CONTROLLER_DIVERSITY_WINDOW + 5)]
    result = recent_family_diversity(entries)
    assert result["window"] == CONTROLLER_DIVERSITY_WINDOW


def test_recent_family_diversity_missing_family_field() -> None:
    result = recent_family_diversity([{"not_family": "x"}, {"not_family": "y"}])
    assert result["dominant_family"] == "unknown"


# ── novelty_angle_suggestions ────────────────────────────────────────────────


def test_novelty_angle_suggestions_returns_list() -> None:
    suggestions = novelty_angle_suggestions()
    assert isinstance(suggestions, list)
    assert len(suggestions) > 0


def test_novelty_angle_suggestions_contains_expected_topics() -> None:
    suggestions = novelty_angle_suggestions()
    all_text = " ".join(suggestions)
    assert "Backend" in all_text
    assert "Testing" in all_text
    assert "Security" in all_text


# ── load_kb_yaml ─────────────────────────────────────────────────────────────


def test_load_kb_yaml_missing_file() -> None:
    with patch("scripts.controller.ROOT") as mock_root:
        mock_root.__truediv__.return_value.exists.return_value = False
        assert load_kb_yaml() == {}


def test_load_kb_yaml_invalid_yaml() -> None:
    mock_path = MagicMock()
    mock_path.exists.return_value = True
    mock_path.__truediv__.return_value = mock_path
    with patch("scripts.controller.ROOT", return_value=mock_path):
        with patch("builtins.open", MagicMock(side_effect=OSError)):
            assert load_kb_yaml() == {}


@patch("scripts.controller.yaml.safe_load", return_value={"key": "value"})
def test_load_kb_yaml_valid(mock_safe_load: MagicMock) -> None:
    with patch("scripts.controller.ROOT") as mock_root:
        mock_path = MagicMock()
        mock_path.exists.return_value = True
        mock_root.__truediv__.return_value = mock_path
        with patch("builtins.open", MagicMock()):
            result = load_kb_yaml()
            assert result == {"key": "value"}


# ── resolve_branch_count ─────────────────────────────────────────────────────


def _make_args(parallel_branches: int = 1, force_parallel: int = 0) -> argparse.Namespace:
    return argparse.Namespace(parallel_branches=parallel_branches, force_parallel=force_parallel)


def test_resolve_branch_count_default() -> None:
    args = _make_args()
    assert resolve_branch_count(args, None) == 1


def test_resolve_branch_count_force_parallel() -> None:
    args = _make_args(parallel_branches=5, force_parallel=3)
    assert resolve_branch_count(args, None) == 3


def test_resolve_branch_count_force_parallel_capped() -> None:
    args = _make_args(parallel_branches=2, force_parallel=5)
    assert resolve_branch_count(args, None) == 2


def test_resolve_branch_count_from_checkpoint() -> None:
    args = _make_args(parallel_branches=5)
    checkpoint = {"parallel_branches": 3}
    assert resolve_branch_count(args, checkpoint) == 3


def test_resolve_branch_count_checkpoint_capped() -> None:
    args = _make_args(parallel_branches=2)
    checkpoint = {"parallel_branches": 5}
    assert resolve_branch_count(args, checkpoint) == 2


def test_resolve_branch_count_checkpoint_not_int() -> None:
    args = _make_args(parallel_branches=3)
    checkpoint = {"parallel_branches": "lots"}
    assert resolve_branch_count(args, checkpoint) == 1


# ── checkpoint_signals_goal_complete ─────────────────────────────────────────


def test_checkpoint_signals_goal_complete_overall_goal() -> None:
    assert checkpoint_signals_goal_complete({"summary": "overall goal complete"}) is True


def test_checkpoint_signals_goal_complete_goal_is_complete() -> None:
    assert checkpoint_signals_goal_complete({"summary": "goal is complete"}) is True


def test_checkpoint_signals_goal_complete_success_criteria() -> None:
    assert checkpoint_signals_goal_complete({"summary": "success criteria met"}) is True


def test_checkpoint_signals_goal_complete_run_complete() -> None:
    assert checkpoint_signals_goal_complete({"summary": "run complete"}) is True


def test_checkpoint_signals_goal_complete_no_match() -> None:
    assert checkpoint_signals_goal_complete({"summary": "still working"}) is False


def test_checkpoint_signals_goal_complete_empty_summary() -> None:
    assert checkpoint_signals_goal_complete({"summary": ""}) is False


# ── coerce_done_checkpoint ───────────────────────────────────────────────────


def test_coerce_done_not_done_status() -> None:
    cp = {"status": "continue"}
    result, reason = coerce_done_checkpoint(cp)
    assert result["status"] == "continue"
    assert reason == ""


def test_coerce_done_with_promote() -> None:
    cp = {"status": "done", "promotion_recommendation": "promote", "risks": [], "decisions": []}
    result, reason = coerce_done_checkpoint(cp)
    assert result["status"] == "continue"
    assert result["goal_complete"] is False
    assert reason != ""


def test_coerce_done_with_goal_complete() -> None:
    cp = {"status": "done", "goal_complete": True}
    result, reason = coerce_done_checkpoint(cp)
    assert result["status"] == "done"
    assert reason == ""


def test_coerce_done_signals_goal_complete() -> None:
    cp = {"status": "done", "summary": "overall goal complete"}
    result, reason = coerce_done_checkpoint(cp)
    assert result["goal_complete"] is True
    assert reason == ""


def test_coerce_done_plain_done() -> None:
    cp = {"status": "done", "risks": [], "decisions": []}
    result, reason = coerce_done_checkpoint(cp)
    assert result["status"] == "continue"
    assert "coerced to 'continue'" in reason


def test_coerce_done_with_abandon_family() -> None:
    cp = {"status": "done", "promotion_recommendation": "abandon_family", "risks": [], "decisions": []}
    result, reason = coerce_done_checkpoint(cp)
    assert result["status"] == "continue"
    assert any("Treat exhausted-family" in d for d in result["decisions"])


def test_coerce_done_promote_adds_decision_only_once() -> None:
    cp = {
        "status": "done",
        "promotion_recommendation": "promote",
        "risks": [],
        "decisions": [
            "After surfacing a deployment advisory, always return status='continue' and proceed to the next hypothesis."
        ],
    }
    result, _ = coerce_done_checkpoint(cp)
    assert len(result["decisions"]) == 1


# ── retry_backoff_seconds ────────────────────────────────────────────────────


def test_retry_backoff_zero_disabled() -> None:
    args = argparse.Namespace(retry_backoff_seconds=0, max_retry_backoff_seconds=300)
    assert retry_backoff_seconds(args, 1) == 0


def test_retry_backoff_exponential() -> None:
    args = argparse.Namespace(retry_backoff_seconds=10, max_retry_backoff_seconds=300)
    assert retry_backoff_seconds(args, 1) == 10
    assert retry_backoff_seconds(args, 2) == 20
    assert retry_backoff_seconds(args, 3) == 40


def test_retry_backoff_capped() -> None:
    args = argparse.Namespace(retry_backoff_seconds=10, max_retry_backoff_seconds=50)
    assert retry_backoff_seconds(args, 4) == 50


# ── classify_transient_failure ───────────────────────────────────────────────


def test_classify_transient_not_failed() -> None:
    is_transient, reason = classify_transient_failure(0, "", "", {"status": "continue", "summary": "", "risks": []})
    assert is_transient is False
    assert reason == ""


def test_classify_transient_exit_code_124() -> None:
    is_transient, reason = classify_transient_failure(124, "", "", {"status": "failed", "summary": "", "risks": []})
    assert is_transient is True
    assert reason == "iteration timed out"


def test_classify_transient_missing_checkpoint() -> None:
    is_transient, reason = classify_transient_failure(
        1, "", "", {"status": "failed", "summary": "No checkpoint markers found in opencode output.", "risks": []}
    )
    assert is_transient is True
    assert reason == "missing checkpoint in agent output"


def test_classify_transient_invalid_checkpoint_json() -> None:
    is_transient, reason = classify_transient_failure(
        1, "", "", {"status": "failed", "summary": "Checkpoint JSON was invalid", "risks": []}
    )
    assert is_transient is True
    assert reason == "invalid checkpoint JSON"


def test_classify_transient_timeout_pattern_in_stdout() -> None:
    is_transient, reason = classify_transient_failure(
        1, "connection timed out", "", {"status": "failed", "summary": "", "risks": []}
    )
    assert is_transient is True
    assert reason == "timed? out"


def test_classify_transient_rate_limit() -> None:
    is_transient, reason = classify_transient_failure(
        1, "", "rate limit exceeded", {"status": "failed", "summary": "", "risks": []}
    )
    assert is_transient is True


def test_classify_transient_non_transient_pattern_wins() -> None:
    is_transient, reason = classify_transient_failure(
        1, "permission denied", "", {"status": "failed", "summary": "", "risks": []}
    )
    assert is_transient is False
    assert reason == ""


def test_classify_transient_empty_output_nonzero() -> None:
    is_transient, reason = classify_transient_failure(1, "", "", {"status": "failed", "summary": "", "risks": []})
    assert is_transient is True
    assert reason == "non-zero exit with empty output"


def test_classify_transient_syntax_error_not_transient() -> None:
    is_transient, reason = classify_transient_failure(
        1, "syntax error on line 5", "", {"status": "failed", "summary": "", "risks": []}
    )
    assert is_transient is False
    assert reason == ""


# ── render_iteration_table ───────────────────────────────────────────────────


class MockRow:
    def __init__(self, iteration_number: int, status: str, checkpoint_json: str, summary: str) -> None:
        self.iteration_number = iteration_number
        self.status = status
        self.checkpoint_json = checkpoint_json
        self.summary = summary

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


def test_render_iteration_table_empty() -> None:
    result = render_iteration_table([])
    assert "# Iteration History" in result
    assert "No iterations recorded yet" in result


def test_render_iteration_table_single_row() -> None:
    cp = {
        "branch_family": "tests",
        "family_novelty": "minor",
        "competitiveness": "promising",
        "evidence_quality": "artifact_only",
        "promotion_recommendation": "hold",
        "summary": "Added tests",
    }
    rows = [MockRow(1, "continue", json.dumps(cp), "Added tests")]
    result = render_iteration_table(rows)  # type: ignore[arg-type]
    assert "| 1 | continue | tests | minor | promising | artifact_only | hold | Added tests |" in result


def test_render_iteration_table_multiple_rows() -> None:
    cp1 = {
        "branch_family": "tests",
        "family_novelty": "minor",
        "competitiveness": "promising",
        "evidence_quality": "artifact_only",
        "promotion_recommendation": "hold",
        "summary": "First",
    }
    cp2 = {
        "branch_family": "scripts",
        "family_novelty": "major",
        "competitiveness": "competitive",
        "evidence_quality": "model_level",
        "promotion_recommendation": "promote",
        "summary": "Second",
    }
    rows = [
        MockRow(1, "continue", json.dumps(cp1), "First"),
        MockRow(2, "continue", json.dumps(cp2), "Second"),
    ]
    result = render_iteration_table(rows)  # type: ignore[arg-type]
    assert "| 1 | continue | tests |" in result
    assert "| 2 | continue | scripts |" in result


def test_render_iteration_table_missing_fields() -> None:
    cp = {
        "branch_family": "",
        "family_novelty": "",
        "competitiveness": "",
        "evidence_quality": "",
        "promotion_recommendation": "",
        "summary": "test summary",
    }
    rows = [MockRow(1, "continue", json.dumps(cp), "test summary")]
    result = render_iteration_table(rows)  # type: ignore[arg-type]
    assert "| 1 | continue |" in result
    assert "test summary" in result


def test_render_iteration_table_long_summary_truncated() -> None:
    long_summary = "x" * 200
    cp = {
        "branch_family": "tests",
        "family_novelty": "minor",
        "competitiveness": "promising",
        "evidence_quality": "artifact_only",
        "promotion_recommendation": "hold",
        "summary": long_summary,
    }
    rows = [MockRow(1, "continue", json.dumps(cp), long_summary)]
    result = render_iteration_table(rows)  # type: ignore[arg-type]
    lines = result.strip().split("\n")
    assert len(lines) >= 3
    assert "| 1 |" in lines[-1]


# ── cleanup_training_scratch ─────────────────────────────────────────────────


@patch("shutil.rmtree")
@patch("scripts.controller.ROOT")
def test_cleanup_training_scratch_nothing_to_remove(mock_root: MagicMock, mock_rmtree: MagicMock) -> None:
    mock_scratch = MagicMock()
    mock_scratch.is_dir.return_value = False

    def truediv_chain(key: str) -> MagicMock:
        if key == "tmp":
            mock_tmp = MagicMock()
            mock_tmp.__truediv__.return_value = mock_scratch
            return mock_tmp
        return MagicMock()

    mock_root.__truediv__.side_effect = truediv_chain

    cleanup_training_scratch(1)
    mock_rmtree.assert_not_called()


@patch("shutil.rmtree")
@patch("scripts.controller.ROOT")
def test_cleanup_training_scratch_removes_old_iters(mock_root: MagicMock, mock_rmtree: MagicMock) -> None:
    mock_scratch = MagicMock()
    mock_scratch.is_dir.return_value = True
    mock_scratch.iterdir.return_value = [
        MagicMock(is_dir=lambda: True, name="iter_1"),
        MagicMock(is_dir=lambda: True, name="iter_2"),
        MagicMock(is_dir=lambda: True, name="iter_5"),
    ]

    def mock_truediv(key: str) -> MagicMock:
        if key == "tmp":
            parent = MagicMock()
            parent.__truediv__.return_value = mock_scratch
            return parent
        return MagicMock(is_dir=lambda: False)

    mock_root.__truediv__.side_effect = mock_truediv

    cleanup_training_scratch(5)
    assert mock_rmtree.call_count >= 1


# ── Constants sanity ─────────────────────────────────────────────────────────


def test_constants_are_positive() -> None:
    assert CONTROLLER_EXHAUST_FAMILY_AFTER >= 1
    assert CONTROLLER_HARD_PIVOT_AFTER >= CONTROLLER_EXHAUST_FAMILY_AFTER
    assert CONTROLLER_DEFAULT_STOP_AFTER >= 10


def test_failure_patterns_are_compiled() -> None:
    for pat in TRANSIENT_FAILURE_PATTERNS:
        assert hasattr(pat, "search")
    for pat in NON_TRANSIENT_FAILURE_PATTERNS:
        assert hasattr(pat, "search")
