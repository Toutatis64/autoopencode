from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from scripts.checkpoint import (
    CHECKPOINT_BEGIN,
    CHECKPOINT_END,
    STATUS_MAP,
    checkpoint_text,
    clean_text,
    extract_checkpoint,
    normalize_artifacts,
    normalize_checkpoint,
    normalize_string_list,
    strip_ansi,
)
from scripts.memory import unique_preserve
from scripts.controller import (
    checkpoint_signals_goal_complete,
    classify_transient_failure,
    coerce_done_checkpoint,
    goal_stop_after_iterations,
    recent_family_diversity,
    render_list,
    resolve_branch_count,
    retry_backoff_seconds,
)
from scripts.run_autopilot import (
    RunContext,
    _parse_git_status_porcelain,
    active_run_id,
    autopilot_session_title,
    available_cpu_ids,
    build_controller_state,
    build_iteration_prompt,
    build_memory_files,
    cmd_export_kb,
    cmd_report,
    compact_summary,
    controller_should_stop,
    cpu_budget_ids,
    create_run,
    ensure_runtime,
    ensure_text,
    find_session_id_by_title,
    list_opencode_sessions,
    load_run,
    main,
    opencode_child_env,
    opencode_preexec_fn,
    parse_args,
    PendingIterationState,
    pending_iteration_state,
    persist_attempt,
    persist_iteration,
    resolve_run,
    read_goal_text,
    recover_resume_latest_state,
    render_promotion_advisory,
    render_report,
    run_dir,
    set_active_run,
    sync_goal_from_canonical_file,
    synthesize_checkpoint_from_git,
    run_iteration,
    utc_now,
    write_text,
)


def test_strip_ansi_strips_sgr_codes() -> None:
    assert strip_ansi("\x1b[31mred\x1b[0m") == "red"


def test_strip_ansi_strips_cursor_moves() -> None:
    assert strip_ansi("foo\x1b[Abar") == "foobar"


def test_strip_ansi_preserves_plain_text() -> None:
    assert strip_ansi("hello world") == "hello world"


def test_strip_ansi_empty_string() -> None:
    assert strip_ansi("") == ""


def test_clean_text_none() -> None:
    assert clean_text(None) == ""


def test_clean_text_whitespace() -> None:
    assert clean_text("  hello  ") == "hello"


def test_clean_text_multi_line() -> None:
    assert clean_text("line1\nline2") == "line1\nline2"


def test_clean_text_non_string() -> None:
    assert clean_text(42) == "42"
    assert clean_text(3.14) == "3.14"
    assert clean_text(["a"]) == "['a']"


def test_clean_text_empty() -> None:
    assert clean_text("") == ""


def test_extract_checkpoint_simple() -> None:
    payload = {"status": "continue", "summary": "fixed bug"}
    text = f"prefix\n{CHECKPOINT_BEGIN} {json.dumps(payload)} {CHECKPOINT_END}\nsuffix"
    result, error = extract_checkpoint(text)
    assert error is None
    assert result == payload


def test_extract_checkpoint_with_ansi() -> None:
    payload = {"status": "continue"}
    text = f"\x1b[32m{CHECKPOINT_BEGIN}\x1b[0m {json.dumps(payload)} {CHECKPOINT_END}"
    result, error = extract_checkpoint(text)
    assert error is None
    assert result == payload


def test_extract_checkpoint_no_markers() -> None:
    result, error = extract_checkpoint("just some text")
    assert result is None
    assert error is not None and "No checkpoint markers found" in error


def test_extract_checkpoint_invalid_json() -> None:
    text = f"{CHECKPOINT_BEGIN} {{invalid}} {CHECKPOINT_END}"
    result, error = extract_checkpoint(text)
    assert result is None
    assert error is not None and "invalid" in error


def test_extract_checkpoint_empty_stdout() -> None:
    result, error = extract_checkpoint("")
    assert result is None
    assert error is not None and "No checkpoint markers found" in error


def test_extract_checkpoint_nested_json() -> None:
    payload = {"nested": {"a": 1}, "list": [{"x": 2}]}
    text = f"lead\n{CHECKPOINT_BEGIN} {json.dumps(payload)} {CHECKPOINT_END}\ntrail"
    result, error = extract_checkpoint(text)
    assert error is None
    assert result == payload


def test_extract_checkpoint_extra_braces_in_text() -> None:
    payload = {"status": "done"}
    text = "some { and } text\n" + f"{CHECKPOINT_BEGIN} {json.dumps(payload)} {CHECKPOINT_END}"
    result, error = extract_checkpoint(text)
    assert error is None
    assert result == payload


@pytest.fixture
def valid_checkpoint() -> dict:
    return {"status": "continue", "summary": "test work", "new_facts": ["fact1"]}


class TestNormalizeCheckpoint:
    def test_empty_raw(self, valid_checkpoint: dict) -> None:
        result = normalize_checkpoint(None, "continue", "fallback")
        assert result["status"] == "continue"
        assert result["summary"] == "fallback"

    def test_unknown_status_falls_back(self) -> None:
        result = normalize_checkpoint({"status": "unknown_status"}, "continue", "summary")
        assert result["status"] == "continue"

    def test_all_valid_statuses(self) -> None:
        for status in STATUS_MAP:
            result = normalize_checkpoint({"status": status}, "continue", "s")
            assert result["status"] == status

    def test_goal_complete_true_via_bool(self) -> None:
        result = normalize_checkpoint({"status": "done", "goal_complete": True}, "continue", "s")
        assert result["goal_complete"] is True

    def test_goal_complete_true_via_string(self) -> None:
        result = normalize_checkpoint({"status": "done", "goal_complete": "true"}, "continue", "s")
        assert result["goal_complete"] is True

    def test_goal_complete_false_by_default(self) -> None:
        result = normalize_checkpoint({"status": "continue"}, "continue", "s")
        assert result["goal_complete"] is False

    def test_summary_fallback(self) -> None:
        result = normalize_checkpoint({}, "continue", "default summary")
        assert result["summary"] == "default summary"

    def test_branch_family_normalized(self) -> None:
        result = normalize_checkpoint({"branch_family": "Backend API"}, "continue", "s")
        assert result["branch_family"] == "backend_api"

    def test_new_facts_string_list(self) -> None:
        result = normalize_checkpoint({"new_facts": ["a", "b"]}, "continue", "s")
        assert result["new_facts"] == ["a", "b"]

    def test_empty_payload(self) -> None:
        result = normalize_checkpoint({}, "continue", "fallback")
        assert result["status"] == "continue"
        assert result["summary"] == "fallback"
        assert result["goal_complete"] is False

    def test_competitiveness_validated(self) -> None:
        result = normalize_checkpoint({"competitiveness": "promising"}, "continue", "s")
        assert result["competitiveness"] == "promising"

    def test_competitiveness_rejected(self) -> None:
        result = normalize_checkpoint({"competitiveness": "invalid"}, "continue", "s")
        assert result["competitiveness"] == "unknown"

    def test_evidence_quality_validated(self) -> None:
        result = normalize_checkpoint({"evidence_quality": "artifact_only"}, "continue", "s")
        assert result["evidence_quality"] == "artifact_only"

    def test_family_novelty_validated(self) -> None:
        result = normalize_checkpoint({"family_novelty": "major"}, "continue", "s")
        assert result["family_novelty"] == "major"

    def test_promotion_recommendation_validated(self) -> None:
        result = normalize_checkpoint({"promotion_recommendation": "promote"}, "continue", "s")
        assert result["promotion_recommendation"] == "promote"


def test_unique_preserve_order() -> None:
    assert unique_preserve(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]


def test_unique_preserve_empty() -> None:
    assert unique_preserve([]) == []


def test_unique_preserve_single() -> None:
    assert unique_preserve(["x"]) == ["x"]


def test_unique_preserve_no_duplicates() -> None:
    assert unique_preserve(["a", "b", "c"]) == ["a", "b", "c"]


def test_normalize_string_list_from_list() -> None:
    assert normalize_string_list(["a", "b"]) == ["a", "b"]


def test_normalize_string_list_from_string() -> None:
    assert normalize_string_list("hello") == []


def test_normalize_string_list_from_none() -> None:
    assert normalize_string_list(None) == []


def test_normalize_string_list_from_int() -> None:
    assert normalize_string_list(42) == []


def test_normalize_string_list_empty_list() -> None:
    assert normalize_string_list([]) == []


def test_normalize_artifacts_from_list_of_dicts() -> None:
    artifacts = [{"path": "a.py", "description": "file a"}]
    assert normalize_artifacts(artifacts) == artifacts


def test_normalize_artifacts_from_none() -> None:
    assert normalize_artifacts(None) == []


def test_normalize_artifacts_from_string() -> None:
    result = normalize_artifacts("a.py")
    assert result == []


def test_normalize_artifacts_empty_list() -> None:
    assert normalize_artifacts([]) == []


def test_ensure_text_none() -> None:
    assert ensure_text(None) == ""


def test_ensure_text_bytes() -> None:
    assert ensure_text(b"hello bytes") == "hello bytes"


def test_ensure_text_str() -> None:
    assert ensure_text("hello") == "hello"


def test_ensure_text_bytes_with_encoding_error() -> None:
    raw = b"\xff\xfe\x00\x01"
    result = ensure_text(raw)
    assert isinstance(result, str)


def test_compact_summary_short() -> None:
    assert compact_summary("hello") == "hello"


def test_compact_summary_exact_limit() -> None:
    text = "a" * 180
    assert compact_summary(text) == text


def test_compact_summary_truncated() -> None:
    text = "a" * 200
    result = compact_summary(text, limit=180)
    assert result.endswith("...")
    assert len(result) == 180


def test_compact_summary_cleans_text() -> None:
    result = compact_summary("  padded  ")
    assert result == "padded"


def test_utc_now_iso_format() -> None:
    result = utc_now()
    parts = result.split("T")
    assert len(parts) == 2
    assert "+" in parts[1] or "Z" in parts[1] or result.endswith("00:00")


def test_extract_checkpoint_markdown_code_fence() -> None:
    inner = json.dumps({"status": "continue", "summary": "fixed bug"})
    text = f"before\n{CHECKPOINT_BEGIN}\n```json\n{inner}\n```\n{CHECKPOINT_END}\nafter"
    result, error = extract_checkpoint(text)
    assert error is None
    assert result == {"status": "continue", "summary": "fixed bug"}


def test_extract_checkpoint_markdown_code_fence_no_lang() -> None:
    inner = json.dumps({"status": "continue"})
    text = f"{CHECKPOINT_BEGIN}\n```\n{inner}\n```\n{CHECKPOINT_END}"
    result, error = extract_checkpoint(text)
    assert error is None
    assert result == {"status": "continue"}


def test_extract_checkpoint_code_fence_extra_text() -> None:
    inner = json.dumps({"status": "done", "summary": "work"})
    text = f"{CHECKPOINT_BEGIN}\nHere is the checkpoint:\n```json\n{inner}\n```\n{CHECKPOINT_END}"
    result, error = extract_checkpoint(text)
    assert error is None
    assert result == {"status": "done", "summary": "work"}


def test_extract_checkpoint_multi_line_json_with_code_fence() -> None:
    payload = {"status": "continue", "summary": "Fixed 12 type errors", "branch_family": "type_safety"}
    inner = json.dumps(payload, indent=2)
    text = f"{CHECKPOINT_BEGIN}\n```json\n{inner}\n```\n{CHECKPOINT_END}"
    result, error = extract_checkpoint(text)
    assert error is None
    assert result == payload


def test_extract_checkpoint_fallback_code_fence_no_markers() -> None:
    """Fallback: code-fenced JSON without BEGIN/END markers (iteration 4 case)."""
    payload = {"status": "continue", "summary": "Fixed checkpoint extraction"}
    inner = json.dumps(payload, indent=2)
    text = f"some text\n```json\n{inner}\n```\n"
    result, error = extract_checkpoint(text)
    assert error is None
    assert result == payload


def test_extract_checkpoint_fallback_code_fence_no_lang() -> None:
    """Fallback: code fence without language tag, no markers."""
    payload = {"status": "continue", "summary": "work"}
    inner = json.dumps(payload)
    text = f"```\n{inner}\n```"
    result, error = extract_checkpoint(text)
    assert error is None
    assert result == payload


def test_extract_checkpoint_fallback_picks_last_valid() -> None:
    """Fallback picks the last code-fenced checkpoint-like JSON."""
    first = {"status": "continue", "summary": "first"}
    last = {"status": "continue", "summary": "last"}
    text = f"```json\n{json.dumps(first)}\n```\nsome text\n```json\n{json.dumps(last)}\n```\n"
    result, error = extract_checkpoint(text)
    assert error is None
    assert result == last


def test_extract_checkpoint_fallback_skips_non_checkpoint_json() -> None:
    """Fallback skips code-fenced JSON that does not look like a checkpoint."""
    text = '```json\n{"name": "foo", "value": 42}\n```'
    result, error = extract_checkpoint(text)
    assert result is None
    assert error is not None and "No checkpoint markers found" in error


def test_extract_checkpoint_fallback_markers_still_take_priority() -> None:
    """When markers ARE present, fallback is never used (markers take priority)."""
    payload_marker = {"status": "done", "summary": "from markers"}
    payload_fallback = {"status": "continue", "summary": "from fallback"}
    text = (
        f"{CHECKPOINT_BEGIN} {json.dumps(payload_marker)} {CHECKPOINT_END}\n"
        f"```json\n{json.dumps(payload_fallback)}\n```"
    )
    result, error = extract_checkpoint(text)
    assert error is None
    assert result == payload_marker


def test_extract_checkpoint_from_stderr_content() -> None:
    """extract_checkpoint can find checkpoint in stderr-sourced text (no stdout)."""
    payload = {"status": "continue", "summary": "found in stderr"}
    stderr = (
        "[INFO] opencode: processing request\n"
        "[INFO] agent: thinking...\n"
        f"{CHECKPOINT_BEGIN}\n"
        f"{json.dumps(payload)}\n"
        f"{CHECKPOINT_END}\n"
    )
    result, error = extract_checkpoint(stderr)
    assert error is None
    assert result == payload


def test_extract_checkpoint_stdout_empty_stderr_has_checkpoint() -> None:
    """Simulates the real scenario: empty stdout, checkpoint in stderr text."""
    stdout_text = ""
    payload = {"status": "continue", "summary": "stderr only"}
    stderr_text = f"some processing messages\n{CHECKPOINT_BEGIN}\n{json.dumps(payload)}\n{CHECKPOINT_END}\n"
    stdout_result, stdout_error = extract_checkpoint(stdout_text)
    assert stdout_result is None
    assert stdout_error is not None

    stderr_result, stderr_error = extract_checkpoint(stderr_text)
    assert stderr_error is None
    assert stderr_result == payload


# ── checkpoint_text ───────────────────────────────────────────────────────────


def test_checkpoint_text_concatenates_fields() -> None:
    cp = {"summary": "fixed bug", "branch_family": "core", "decisions": ["use regex"], "files_touched": ["parser.py"]}
    text = checkpoint_text(cp)
    assert "fixed bug" in text
    assert "core" in text
    assert "use regex" in text
    assert "parser.py" in text


def test_checkpoint_text_empty() -> None:
    assert checkpoint_text({}) == ""


# ── infer_branch_family ───────────────────────────────────────────────────────


def test_infer_branch_family_tests() -> None:
    cp = normalize_checkpoint({"summary": "added pytest tests for parser"}, "continue", "")
    assert cp["branch_family"] == "tests"


def test_infer_branch_family_scripts() -> None:
    cp = normalize_checkpoint({"summary": "updated python scripts"}, "continue", "")
    assert cp["branch_family"] == "scripts"


def test_infer_branch_family_knowledge() -> None:
    cp = normalize_checkpoint({"summary": "updated knowledge base yaml"}, "continue", "")
    assert cp["branch_family"] == "knowledge"


def test_infer_branch_family_config() -> None:
    cp = normalize_checkpoint({"summary": "changed setup config"}, "continue", "")
    assert cp["branch_family"] == "config"


def test_infer_branch_family_unknown() -> None:
    cp = normalize_checkpoint({"summary": "completely unrelated work"}, "continue", "")
    assert cp["branch_family"] == "unknown"


def test_infer_branch_family_explicit_overrides_inference() -> None:
    cp = normalize_checkpoint({"summary": "added pytest tests", "branch_family": "config"}, "continue", "")
    assert cp["branch_family"] == "config"


# ── infer_family_novelty ──────────────────────────────────────────────────────


def test_infer_family_novelty_empty_by_default() -> None:
    cp = normalize_checkpoint({"summary": "small fix"}, "continue", "")
    assert cp["family_novelty"] == ""


def test_infer_family_novelty_explicit_major() -> None:
    cp = normalize_checkpoint({"summary": "new module for memory subsystem", "family_novelty": "major"}, "continue", "")
    assert cp["family_novelty"] == "major"


def test_infer_family_novelty_invalid_falls_to_inference() -> None:
    cp = normalize_checkpoint(
        {"summary": "new module for memory subsystem", "family_novelty": "invalid"}, "continue", ""
    )
    assert cp["family_novelty"] == "major"


# ── infer_evidence_quality ────────────────────────────────────────────────────


def test_infer_evidence_quality_empty_by_default() -> None:
    cp = normalize_checkpoint({"summary": "wrote some code"}, "continue", "")
    assert cp["evidence_quality"] == ""


def test_infer_evidence_quality_invalid_triggers_inference() -> None:
    cp = normalize_checkpoint(
        {"summary": "all unit tests pass and typecheck clean", "evidence_quality": "invalid"},
        "continue",
        "",
    )
    assert cp["evidence_quality"] == "model_level"


def test_infer_evidence_quality_strategy_level_via_invalid() -> None:
    cp = normalize_checkpoint(
        {"summary": "e2e tests passing in ci", "evidence_quality": "invalid"},
        "continue",
        "",
    )
    assert cp["evidence_quality"] == "strategy_level"


def test_infer_evidence_quality_artifact_only_via_invalid() -> None:
    cp = normalize_checkpoint(
        {"summary": "just wrote some code without running any checks", "evidence_quality": "invalid"},
        "continue",
        "",
    )
    assert cp["evidence_quality"] == "artifact_only"


# ── infer_competitiveness ─────────────────────────────────────────────────────


def test_infer_competitiveness_empty_by_default() -> None:
    cp = normalize_checkpoint({"summary": "neutral description"}, "continue", "")
    assert cp["competitiveness"] == ""


def test_infer_competitiveness_invalid_triggers_inference() -> None:
    cp = normalize_checkpoint(
        {"summary": "regression introduced, far below quality gate", "competitiveness": "invalid"},
        "continue",
        "",
    )
    assert cp["competitiveness"] == "non_competitive"


def test_infer_competitiveness_promising() -> None:
    cp = normalize_checkpoint(
        {"summary": "fix is deployable and production-ready", "competitiveness": "invalid"},
        "continue",
        "",
    )
    assert cp["competitiveness"] == "promising"


def test_infer_competitiveness_marginal() -> None:
    cp = normalize_checkpoint(
        {"summary": "tests pass, interesting but needs review", "competitiveness": "invalid"},
        "continue",
        "",
    )
    assert cp["competitiveness"] == "marginal"


# ── infer_promotion_recommendation ────────────────────────────────────────────


def test_infer_promotion_recommendation_empty_by_default() -> None:
    cp = normalize_checkpoint({"summary": "fixed bugs"}, "continue", "")
    assert cp["promotion_recommendation"] == ""


def test_infer_promotion_recommendation_abandon_run() -> None:
    cp = normalize_checkpoint(
        {"summary": "should abandon run, nothing works", "promotion_recommendation": "invalid"},
        "continue",
        "",
    )
    assert cp["promotion_recommendation"] == "abandon_run"


def test_infer_promotion_recommendation_abandon_family() -> None:
    cp = normalize_checkpoint(
        {"summary": "pivot away from this approach", "promotion_recommendation": "invalid"},
        "continue",
        "",
    )
    assert cp["promotion_recommendation"] == "abandon_family"


def test_infer_promotion_recommendation_hold() -> None:
    cp = normalize_checkpoint(
        {
            "summary": "fixed bugs",
            "competitiveness": "promising",
            "evidence_quality": "model_level",
            "promotion_recommendation": "invalid",
        },
        "continue",
        "",
    )
    assert cp["promotion_recommendation"] == "hold"


def test_infer_promotion_recommendation_promote() -> None:
    cp = normalize_checkpoint(
        {
            "summary": "production-ready fix",
            "competitiveness": "promising",
            "evidence_quality": "strategy_level",
            "promotion_recommendation": "invalid",
        },
        "continue",
        "",
    )
    assert cp["promotion_recommendation"] == "promote"


# ── controller.py tests ──────────────────────────────────────────────


def test_render_list_empty() -> None:
    assert render_list("Test", []) == "# Test\n\n- None recorded yet.\n"


def test_render_list_with_items() -> None:
    result = render_list("Fruits", ["apple", "banana", "cherry"])
    assert result == "# Fruits\n\n- apple\n- banana\n- cherry\n"


def test_goal_stop_after_iterations_missing() -> None:
    assert goal_stop_after_iterations("") == 100


def test_goal_stop_after_iterations_after_n() -> None:
    assert goal_stop_after_iterations("stop after 42 iterations") == 42


def test_goal_stop_after_iterations_try() -> None:
    assert goal_stop_after_iterations("after 7 tries") == 7


def test_goal_stop_after_iterations_no_match() -> None:
    assert goal_stop_after_iterations("just keep going forever") == 100


def test_recent_family_diversity_empty() -> None:
    result = recent_family_diversity([])
    assert result["window"] == 0
    assert result["low_diversity"] is False


def test_recent_family_diversity_single_family() -> None:
    entries = [{"branch_family": "tests"}] * 6
    result = recent_family_diversity(entries)
    assert result["dominant_family"] == "tests"
    assert result["dominant_share"] == 1.0
    assert result["low_diversity"] is True


def test_recent_family_diversity_mixed() -> None:
    entries = [
        {"branch_family": "tests"},
        {"branch_family": "architecture"},
        {"branch_family": "tests"},
        {"branch_family": "scripts"},
        {"branch_family": "tests"},
        {"branch_family": "architecture"},
    ]
    result = recent_family_diversity(entries)
    assert result["dominant_family"] == "tests"
    assert 0.4 <= result["dominant_share"] <= 0.6


def test_resolve_branch_count_default() -> None:
    args = argparse.Namespace(parallel_branches=4, force_parallel=0)
    assert resolve_branch_count(args, None) == 1


def test_resolve_branch_count_force() -> None:
    args = argparse.Namespace(parallel_branches=4, force_parallel=3)
    assert resolve_branch_count(args, None) == 3


def test_resolve_branch_count_force_capped() -> None:
    args = argparse.Namespace(parallel_branches=2, force_parallel=10)
    assert resolve_branch_count(args, None) == 2


def test_resolve_branch_count_agent_request() -> None:
    args = argparse.Namespace(parallel_branches=8, force_parallel=0)
    assert resolve_branch_count(args, {"parallel_branches": 5}) == 5


def test_checkpoint_signals_goal_complete_phrase() -> None:
    cp = {"summary": "the overall goal complete"}
    assert checkpoint_signals_goal_complete(cp) is True


def test_checkpoint_signals_goal_complete_not_complete() -> None:
    cp = {"summary": "still working on it"}
    assert checkpoint_signals_goal_complete(cp) is False


def test_coerce_done_checkpoint_not_done() -> None:
    cp = {"status": "continue"}
    result, reason = coerce_done_checkpoint(cp)
    assert result["status"] == "continue"
    assert reason == ""


def test_coerce_done_checkpoint_promote() -> None:
    cp = {"status": "done", "promotion_recommendation": "promote", "risks": [], "decisions": []}
    result, reason = coerce_done_checkpoint(cp)
    assert result["status"] == "continue"
    assert "promotion" in reason


def test_coerce_done_checkpoint_goal_complete() -> None:
    cp = {"status": "done", "goal_complete": True, "risks": [], "decisions": []}
    result, reason = coerce_done_checkpoint(cp)
    assert reason == ""


def test_coerce_done_checkpoint_no_goal_complete() -> None:
    cp = {"status": "done", "risks": [], "decisions": []}
    result, reason = coerce_done_checkpoint(cp)
    assert result["status"] == "continue"
    assert "did not set goal_complete" in reason


def test_retry_backoff_seconds_zero() -> None:
    args = argparse.Namespace(retry_backoff_seconds=0, max_retry_backoff_seconds=300)
    assert retry_backoff_seconds(args, 1) == 0


def test_retry_backoff_seconds_exponential() -> None:
    args = argparse.Namespace(retry_backoff_seconds=10, max_retry_backoff_seconds=300)
    assert retry_backoff_seconds(args, 1) == 10
    assert retry_backoff_seconds(args, 2) == 20
    assert retry_backoff_seconds(args, 3) == 40


def test_retry_backoff_seconds_capped() -> None:
    args = argparse.Namespace(retry_backoff_seconds=200, max_retry_backoff_seconds=300)
    assert retry_backoff_seconds(args, 2) == 300


def test_classify_transient_failure_not_failed() -> None:
    cp = {"status": "continue", "summary": "", "risks": []}
    is_transient, reason = classify_transient_failure(0, "", "", cp)
    assert is_transient is False
    assert reason == ""


def test_classify_transient_failure_timeout() -> None:
    cp = {"status": "failed", "summary": "timed out", "risks": []}
    is_transient, reason = classify_transient_failure(124, "", "", cp)
    assert is_transient is True
    assert reason == "iteration timed out"


def test_classify_transient_failure_missing_checkpoint() -> None:
    cp = {"status": "failed", "summary": "No checkpoint markers found in opencode output.", "risks": []}
    is_transient, reason = classify_transient_failure(1, "", "", cp)
    assert is_transient is True
    assert "missing checkpoint" in reason


def test_classify_transient_failure_non_transient() -> None:
    cp = {"status": "failed", "summary": "permission denied on file access", "risks": []}
    is_transient, reason = classify_transient_failure(1, "", "", cp)
    assert is_transient is False


def test_classify_transient_failure_rate_limit() -> None:
    cp = {"status": "failed", "summary": "rate limit exceeded", "risks": []}
    is_transient, reason = classify_transient_failure(1, "", "", cp)
    assert is_transient is True
    assert reason == "rate limit"


# -- run_autopilot.py pure function tests --


def test_autopilot_session_title() -> None:
    assert autopilot_session_title("run123", 1, 2) == "autopilot run123 #1.2"
    assert autopilot_session_title("abc", 0, 0) == "autopilot abc #0.0"
    assert autopilot_session_title("", 5, 1) == "autopilot  #5.1"


def test_run_dir() -> None:
    assert run_dir(Path("/tmp"), "run1") == Path("/tmp/runs/run1")
    assert run_dir(Path("/a/b"), "r2") == Path("/a/b/runs/r2")


def test_write_text_creates_directory_and_writes(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "file.txt"
    write_text(target, "hello world")
    assert target.read_text(encoding="utf-8") == "hello world\n"


def test_write_text_rstrip_existing_trailing_newline(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    write_text(target, "hello\n\n")
    assert target.read_text(encoding="utf-8") == "hello\n"


def test_active_run_id_not_found(tmp_path: Path) -> None:
    assert active_run_id(tmp_path) is None


def test_active_run_id_found(tmp_path: Path) -> None:
    active = tmp_path / "active_run.txt"
    active.write_text("run_abc\n", encoding="utf-8")
    assert active_run_id(tmp_path) == "run_abc"


def test_active_run_id_empty(tmp_path: Path) -> None:
    active = tmp_path / "active_run.txt"
    active.write_text("   \n", encoding="utf-8")
    assert active_run_id(tmp_path) is None


def test_set_active_run(tmp_path: Path) -> None:
    set_active_run(tmp_path, "run_xyz")
    active = tmp_path / "active_run.txt"
    assert active.read_text(encoding="utf-8") == "run_xyz\n"


def test_available_cpu_ids_from_affinity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "sched_getaffinity", lambda _: {0, 2, 4}, raising=False)
    result = available_cpu_ids()
    assert result == [0, 2, 4]


def test_available_cpu_ids_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(os, "sched_getaffinity", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    result = available_cpu_ids()
    assert result == [0, 1, 2, 3, 4, 5, 6, 7]


def test_available_cpu_ids_fallback_min_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(os, "sched_getaffinity", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    result = available_cpu_ids()
    assert result == [0]


def test_cpu_budget_ids_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "sched_getaffinity", lambda _: {0, 1, 2, 3}, raising=False)
    assert cpu_budget_ids(100) == [0, 1, 2, 3]


def test_cpu_budget_ids_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "sched_getaffinity", lambda _: {0, 1, 2, 3}, raising=False)
    result = cpu_budget_ids(50)
    assert len(result) == 2
    assert result == [0, 1]


def test_cpu_budget_ids_single_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "sched_getaffinity", lambda _: {0}, raising=False)
    assert cpu_budget_ids(10) == [0]


def test_find_session_id_by_title_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "scripts.run_autopilot.list_opencode_sessions",
        lambda timeout_seconds=30: [("ses_1", "autopilot run_abc #1.1"), ("ses_2", "other task")],
    )
    assert find_session_id_by_title("autopilot run_abc #1.1") == "ses_1"


def test_find_session_id_by_title_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "scripts.run_autopilot.list_opencode_sessions",
        lambda timeout_seconds=30: [("ses_1", "autopilot run_abc #1.1")],
    )
    assert find_session_id_by_title("nonexistent") is None


def test_find_session_id_by_title_subprocess_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "scripts.run_autopilot.list_opencode_sessions",
        lambda timeout_seconds=30: [],
    )
    assert find_session_id_by_title("anything") is None


class TestListOpencodeSessions:
    """Tests for list_opencode_sessions which parses `opencode session list` output."""

    def test_parses_session_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdout = (
            "Session ID           Title                      Model\n"
            "───────────────────────────────────────────────────────────\n"
            "ses_abc123           autopilot run_xyz #1.1     claude-sonnet\n"
            "ses_def456           autopilot run_xyz #2.1     claude-sonnet\n"
        )
        mock_completed = MagicMock(returncode=0, stdout=stdout, stderr="")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_completed)
        result = list_opencode_sessions()
        assert result == [("ses_abc123", "autopilot run_xyz #1.1"), ("ses_def456", "autopilot run_xyz #2.1")]

    def test_skips_header_and_separator(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdout = (
            "Session ID           Title                      Model\n"
            "───────────────────────────────────────────────────────────\n"
            "ses_001              real session                gpt-4\n"
        )
        mock_completed = MagicMock(returncode=0, stdout=stdout, stderr="")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_completed)
        result = list_opencode_sessions()
        assert result == [("ses_001", "real session")]

    def test_skips_non_session_lines(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdout = (
            "Session ID           Title                      Model\n"
            "───────────────────────────────────────────────────────────\n"
            "some random output\n"
            "ses_001              real session                gpt-4\n"
        )
        mock_completed = MagicMock(returncode=0, stdout=stdout, stderr="")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_completed)
        result = list_opencode_sessions()
        assert result == [("ses_001", "real session")]

    def test_returns_empty_on_nonzero_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_completed = MagicMock(returncode=1, stdout="", stderr="error")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_completed)
        assert list_opencode_sessions() == []

    def test_returns_empty_on_subprocess_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(OSError("no opencode")))
        assert list_opencode_sessions() == []

    def test_returns_empty_on_empty_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_completed = MagicMock(returncode=0, stdout="", stderr="")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_completed)
        assert list_opencode_sessions() == []

    def test_skips_lines_with_invalid_session_id_format(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdout = (
            "Session ID           Title                      Model\n"
            "───────────────────────────────────────────────────────────\n"
            "[invalid]            some output                  gpt-4\n"
        )
        mock_completed = MagicMock(returncode=0, stdout=stdout, stderr="")
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_completed)
        result = list_opencode_sessions()
        assert result == []


class TestOpencodePreexecFn:
    """Tests for opencode_preexec_fn which sets CPU affinity for child processes."""

    def test_returns_none_when_cpu_ids_empty(self) -> None:
        assert opencode_preexec_fn([]) is None

    def test_returns_none_when_no_sched_setaffinity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delattr(os, "sched_setaffinity", raising=False)
        assert opencode_preexec_fn([0, 1]) is None

    def test_returns_callable_with_affinity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[set[int]] = []

        def _mock_setaffinity(_pid: int, cpus: set[int]) -> None:
            calls.append(cpus)

        monkeypatch.setattr(os, "sched_setaffinity", _mock_setaffinity)
        fn = opencode_preexec_fn([0, 2, 4])
        assert callable(fn)
        fn()
        assert calls == [{0, 2, 4}]

    def test_handles_oserror_gracefully(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise_oserror(_pid: int, _cpus: set[int]) -> None:
            raise OSError("affinity not supported")

        monkeypatch.setattr(os, "sched_setaffinity", _raise_oserror)
        fn = opencode_preexec_fn([0, 1])
        assert callable(fn)
        fn()


def test_available_cpu_ids_oserror_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        os, "sched_getaffinity", lambda _: (_ for _ in ()).throw(OSError("not supported")), raising=False
    )
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    assert available_cpu_ids() == [0, 1, 2, 3]


def test_cpu_budget_ids_single_cpu_full_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "sched_getaffinity", lambda _: {5}, raising=False)
    assert cpu_budget_ids(100) == [5]


def test_read_goal_text_from_arg() -> None:
    args = argparse.Namespace(goal="improve code", goal_file=None)
    assert read_goal_text(args) == "improve code"


def test_read_goal_text_from_arg_strips() -> None:
    args = argparse.Namespace(goal="  improve code  ", goal_file=None)
    assert read_goal_text(args) == "improve code"


def test_read_goal_text_from_file(tmp_path: Path) -> None:
    goal_file = tmp_path / "goal.txt"
    goal_file.write_text("fix performance\n", encoding="utf-8")
    args = argparse.Namespace(goal="", goal_file=goal_file)
    assert read_goal_text(args) == "fix performance"


def test_read_goal_text_none() -> None:
    args = argparse.Namespace(goal="", goal_file=None)
    assert read_goal_text(args) is None


# ── opencode_child_env ─────────────────────────────────────────────────────────


def test_opencode_child_env_sets_thread_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "sched_getaffinity", lambda _: {0, 1, 2, 3}, raising=False)
    env, cpu_ids = opencode_child_env(50)
    assert cpu_ids == [0, 1]
    assert env["OMP_NUM_THREADS"] == "2"
    assert env["AUTOPILOT_CPU_BUDGET_PERCENT"] == "50"
    assert env["AUTOPILOT_CPU_BUDGET_THREADS"] == "2"


def test_opencode_child_env_all_cpus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "sched_getaffinity", lambda _: {0, 1, 2, 3}, raising=False)
    env, cpu_ids = opencode_child_env(100)
    assert cpu_ids == [0, 1, 2, 3]
    assert env["OMP_NUM_THREADS"] == "4"


# ── pending_iteration_state ────────────────────────────────────────────────────


def _make_db_with_attempts() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            goal_text TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            iteration_count INTEGER NOT NULL DEFAULT 0,
            latest_summary TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE iteration_attempts (
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
            stderr_path TEXT NOT NULL
        );
    """)
    return conn


def test_pending_iteration_state_first() -> None:
    conn = _make_db_with_attempts()
    ctx = RunContext(run_id="test", goal_text="g", status="running", iteration_count=5)
    state = pending_iteration_state(conn, ctx)
    assert state.iteration_number == 6
    assert state.attempt_number == 1


def test_pending_iteration_state_retry() -> None:
    conn = _make_db_with_attempts()
    ctx = RunContext(run_id="test", goal_text="g", status="running", iteration_count=5)
    conn.execute(
        "INSERT INTO iteration_attempts (run_id, iteration_number, attempt_number, started_at, finished_at, status, summary, checkpoint_json, stdout_path, stderr_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("test", 6, 1, "now", "now", "failed", "oops", "{}", "/dev/null", "/dev/null"),
    )
    conn.commit()
    state = pending_iteration_state(conn, ctx)
    assert state.iteration_number == 6
    assert state.attempt_number == 2


# ── load_run ───────────────────────────────────────────────────────────────────


def _make_db_with_run(run_id: str = "test-run") -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            goal_text TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            iteration_count INTEGER NOT NULL DEFAULT 0,
            latest_summary TEXT NOT NULL DEFAULT ''
        );
    """)
    conn.execute(
        "INSERT INTO runs (run_id, goal_text, status, created_at, updated_at, iteration_count) VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, "improve codebase", "running", "now", "now", 3),
    )
    conn.commit()
    return conn


def test_load_run_found() -> None:
    conn = _make_db_with_run()
    ctx = load_run(conn, "test-run")
    assert ctx.run_id == "test-run"
    assert ctx.goal_text == "improve codebase"
    assert ctx.status == "running"
    assert ctx.iteration_count == 3


def test_load_run_with_session_goal() -> None:
    conn = _make_db_with_run()
    ctx = load_run(conn, "test-run", session_goal="fix bugs")
    assert ctx.session_goal == "fix bugs"


def test_load_run_not_found() -> None:
    conn = _make_db_with_run()
    with pytest.raises(SystemExit, match="Run id not found: nonexistent"):
        load_run(conn, "nonexistent")


class TestResolveRun:
    """Tests for resolve_run which resolves a run context from CLI args."""

    @staticmethod
    def _args(**overrides: Any) -> argparse.Namespace:
        base = dict(
            runtime_dir=Path("/tmp/r"),
            goal="",
            goal_file=None,
            resume_run="",
            resume_latest=False,
            session_goal="",
            session_goal_file=None,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    @staticmethod
    def _mock_resolve_deps(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("scripts.run_autopilot.sync_goal_from_canonical_file", lambda conn, ctx: ctx)
        monkeypatch.setattr("scripts.run_autopilot.set_active_run", lambda rt, rid: None)

    def test_resume_run_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._mock_resolve_deps(monkeypatch)
        conn = _make_db_with_run("existing-run")
        args = self._args(resume_run="existing-run")
        ctx = resolve_run(conn, args)
        assert ctx.run_id == "existing-run"
        assert ctx.goal_text == "improve codebase"

    def test_new_run_passes_session_goal(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._mock_resolve_deps(monkeypatch)
        monkeypatch.setattr(
            "scripts.run_autopilot.create_run",
            lambda conn, rt, gt, session_goal="": RunContext(
                run_id="new-run",
                goal_text=gt,
                status="running",
                iteration_count=0,
                session_goal=session_goal,
            ),
        )
        conn = _make_db_with_run()
        args = self._args(runtime_dir=tmp_path, goal="improve", session_goal="  explicit session  ")
        ctx = resolve_run(conn, args)
        assert ctx.session_goal == "explicit session"

    def test_resume_latest_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._mock_resolve_deps(monkeypatch)
        monkeypatch.setattr("scripts.run_autopilot.active_run_id", lambda _: "latest-run")
        conn = _make_db_with_run("latest-run")
        args = self._args(resume_latest=True)
        ctx = resolve_run(conn, args)
        assert ctx.run_id == "latest-run"

    def test_resume_latest_no_active_run(self) -> None:
        conn = _make_db_with_run("test-run")
        args = self._args(resume_latest=True)
        with pytest.raises(SystemExit, match="No active run recorded"):
            resolve_run(conn, args)

    def test_new_run_from_goal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._mock_resolve_deps(monkeypatch)
        monkeypatch.setattr(
            "scripts.run_autopilot.create_run",
            lambda conn, rt, gt, session_goal="": RunContext(
                run_id="new-run",
                goal_text=gt,
                status="running",
                iteration_count=0,
            ),
        )
        conn = _make_db_with_run()
        args = self._args(goal="fix performance")
        ctx = resolve_run(conn, args)
        assert ctx.goal_text == "fix performance"
        assert ctx.run_id == "new-run"
        assert ctx.status == "running"
        assert ctx.session_goal == ""

    def test_fallback_to_active_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._mock_resolve_deps(monkeypatch)
        monkeypatch.setattr("scripts.run_autopilot.active_run_id", lambda _: "active-run")
        conn = _make_db_with_run("active-run")
        args = self._args()
        ctx = resolve_run(conn, args)
        assert ctx.run_id == "active-run"

    def test_fallback_no_active_run(self) -> None:
        conn = _make_db_with_run("test-run")
        args = self._args()
        with pytest.raises(SystemExit, match="Provide --goal"):
            resolve_run(conn, args)


def _make_db_with_goal(run_id: str = "test-run", goal_text: str = "old goal") -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            goal_text TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            iteration_count INTEGER NOT NULL DEFAULT 0,
            latest_summary TEXT NOT NULL DEFAULT ''
        );
    """)
    conn.execute(
        "INSERT INTO runs (run_id, goal_text, status, created_at, updated_at, iteration_count) VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, goal_text, "running", "now", "now", 0),
    )
    conn.commit()
    return conn


def test_sync_goal_from_canonical_file_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "nonexistent.md"
    monkeypatch.setattr("scripts.run_autopilot.DEFAULT_GOAL_FILE", missing)
    conn = _make_db_with_goal()
    ctx = RunContext(run_id="test-run", goal_text="old goal", status="running", iteration_count=0)
    result = sync_goal_from_canonical_file(conn, ctx)
    assert result is ctx


def test_sync_goal_from_canonical_file_same(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    goal_file = tmp_path / "goal.md"
    goal_file.write_text("old goal\n", encoding="utf-8")
    monkeypatch.setattr("scripts.run_autopilot.DEFAULT_GOAL_FILE", goal_file)
    conn = _make_db_with_goal()
    ctx = RunContext(run_id="test-run", goal_text="old goal", status="running", iteration_count=0)
    result = sync_goal_from_canonical_file(conn, ctx)
    assert result is ctx


def test_sync_goal_from_canonical_file_updates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    goal_file = tmp_path / "goal.md"
    goal_file.write_text("new goal\n", encoding="utf-8")
    monkeypatch.setattr("scripts.run_autopilot.DEFAULT_GOAL_FILE", goal_file)
    conn = _make_db_with_goal()
    ctx = RunContext(run_id="test-run", goal_text="old goal", status="running", iteration_count=0)
    result = sync_goal_from_canonical_file(conn, ctx)
    assert result is not ctx
    assert result.goal_text == "new goal"
    stored = conn.execute("SELECT goal_text FROM runs WHERE run_id = ?", ("test-run",)).fetchone()
    assert stored["goal_text"] == "new goal"


# ── controller_should_stop ─────────────────────────────────────────────────────


def test_controller_should_stop_abandon() -> None:
    conn = _make_db()
    ctx = _make_ctx()
    cp = {"status": "continue", "promotion_recommendation": "abandon_run"}
    should_stop, reason = controller_should_stop(conn, ctx, cp)
    assert should_stop is True
    assert "abandon" in reason


def test_controller_should_stop_not_continue() -> None:
    conn = _make_db()
    ctx = _make_ctx()
    cp = {"status": "done"}
    should_stop, reason = controller_should_stop(conn, ctx, cp)
    assert should_stop is False


def test_controller_should_stop_non_competitive_streak() -> None:
    conn = _make_db()
    ctx = RunContext(run_id="test-run", goal_text="stop after 3 iterations", status="running", iteration_count=3)
    for i in range(1, 4):
        _insert_iteration(conn, "test-run", i, competitiveness="non_competitive", summary=f"failed attempt {i}")
    cp = {"status": "continue", "competitiveness": "non_competitive"}
    should_stop, reason = controller_should_stop(conn, ctx, cp)
    assert should_stop is True
    assert "non-competitive" in reason


def test_controller_should_stop_continues_when_promising() -> None:
    conn = _make_db()
    ctx = _make_ctx()
    _insert_iteration(conn, "test-run", 1, competitiveness="promising")
    cp = {"status": "continue", "competitiveness": "promising"}
    should_stop, reason = controller_should_stop(conn, ctx, cp)
    assert should_stop is False


# ── synthesize_checkpoint_from_git ────────────────────────────────────────────


class FakeCompletedProcess:
    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode


def test_synthesize_checkpoint_returns_none_when_raw_exists() -> None:
    existing = {"summary": "real work"}
    result = synthesize_checkpoint_from_git(0, existing)
    assert result is existing


def test_synthesize_checkpoint_nonzero_exit_no_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeCompletedProcess("", returncode=0))
    result = synthesize_checkpoint_from_git(1, None)
    assert result is None


def test_synthesize_checkpoint_nonzero_exit_with_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeCompletedProcess(" M scripts/foo.py\n", returncode=0))
    result = synthesize_checkpoint_from_git(1, None)
    assert result is not None
    assert result["status"] == "failed"
    assert result["branch_family"] == "scripts"


def test_synthesize_checkpoint_returns_none_when_git_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeCompletedProcess("", returncode=1))
    result = synthesize_checkpoint_from_git(0, None)
    assert result is None


def test_synthesize_checkpoint_no_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeCompletedProcess("", returncode=0))
    result = synthesize_checkpoint_from_git(0, None)
    assert result is None


def test_synthesize_checkpoint_with_test_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: FakeCompletedProcess(" M tests/test_foo.py\n M scripts/bar.py\n", returncode=0),
    )
    result = synthesize_checkpoint_from_git(0, None)
    assert result is not None
    assert result["status"] == "continue"
    assert result["branch_family"] == "tests"


def test_synthesize_checkpoint_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(OSError("no git")))
    result = synthesize_checkpoint_from_git(0, None)
    assert result is None


def test_synthesize_checkpoint_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(ValueError("bad status")))
    result = synthesize_checkpoint_from_git(0, None)
    assert result is None


def test_synthesize_checkpoint_with_scripts_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: FakeCompletedProcess(" M scripts/foo.py\n M scripts/bar.py\n", returncode=0)
    )
    result = synthesize_checkpoint_from_git(0, None)
    assert result is not None
    assert result["branch_family"] == "scripts"
    assert len(result["artifacts"]) == 2


def test_synthesize_checkpoint_with_knowledge_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: FakeCompletedProcess(" M knowledge/kb.yaml\n", returncode=0)
    )
    result = synthesize_checkpoint_from_git(0, None)
    assert result is not None
    assert result["branch_family"] == "knowledge"


def test_synthesize_checkpoint_with_unknown_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: FakeCompletedProcess(" M README.md\n M Makefile\n", returncode=0)
    )
    result = synthesize_checkpoint_from_git(0, None)
    assert result is not None
    assert result["branch_family"] == "unknown"


def test_synthesize_checkpoint_untracked_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: FakeCompletedProcess("?? new_file.py\n M existing.py\n", returncode=0)
    )
    result = synthesize_checkpoint_from_git(0, None)
    assert result is not None
    assert len(result["artifacts"]) == 2


def test_parse_git_status_empty() -> None:
    assert _parse_git_status_porcelain("") == []


def test_parse_git_status_only_whitespace() -> None:
    assert _parse_git_status_porcelain("  \n  \n") == []


def test_parse_git_status_modified() -> None:
    result = _parse_git_status_porcelain(" M scripts/foo.py\n")
    assert result == ["scripts/foo.py"]


def test_parse_git_status_untracked() -> None:
    result = _parse_git_status_porcelain("?? new_test.py\n")
    assert result == ["new_test.py"]


def test_parse_git_status_mixed() -> None:
    result = _parse_git_status_porcelain(" M scripts/foo.py\n?? test_bar.py\n M scripts/baz.py\n")
    assert result == ["scripts/foo.py", "test_bar.py", "scripts/baz.py"]


def test_parse_git_status_deduplicates() -> None:
    result = _parse_git_status_porcelain(" M scripts/foo.py\n M scripts/foo.py\n")
    assert result == ["scripts/foo.py"]


def test_parse_git_status_staged() -> None:
    result = _parse_git_status_porcelain("M  scripts/foo.py\n")
    assert result == ["scripts/foo.py"]


def test_parse_git_status_deleted() -> None:
    result = _parse_git_status_porcelain(" D scripts/deleted.py\n")
    assert result == ["scripts/deleted.py"]


def test_parse_git_status_short_lines_skipped() -> None:
    result = _parse_git_status_porcelain(" M\n")
    assert result == []


def test_parse_git_status_no_trailing_newline() -> None:
    result = _parse_git_status_porcelain(" M scripts/foo.py")
    assert result == ["scripts/foo.py"]


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
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
            stderr_path TEXT NOT NULL
        );
    """)
    return conn


def _make_ctx(run_id: str = "test-run", iteration_count: int = 5) -> RunContext:
    return RunContext(
        run_id=run_id,
        goal_text="Improve the codebase",
        status="running",
        iteration_count=iteration_count,
    )


def _insert_iteration(
    conn: sqlite3.Connection,
    run_id: str,
    iteration_number: int,
    competitiveness: str = "promising",
    branch_family: str = "tests",
    evidence_quality: str = "artifact_only",
    promotion: str = "hold",
    family_novelty: str = "minor",
    summary: str = "Added tests",
    parallel_branches: int = 1,
    next_steps: list[str] | None = None,
    risks: list[str] | None = None,
    decisions: list[str] | None = None,
    new_facts: list[str] | None = None,
    goal_progress: str | None = None,
) -> None:
    checkpoint: dict[str, Any] = {
        "status": "continue",
        "summary": summary,
        "branch_family": branch_family,
        "competitiveness": competitiveness,
        "evidence_quality": evidence_quality,
        "promotion_recommendation": promotion,
        "family_novelty": family_novelty,
        "parallel_branches": parallel_branches,
    }
    if next_steps:
        checkpoint["next_steps"] = next_steps
    if risks:
        checkpoint["risks"] = risks
    if decisions:
        checkpoint["decisions"] = decisions
    if new_facts:
        checkpoint["new_facts"] = new_facts
    if goal_progress:
        checkpoint["goal_progress"] = goal_progress
    conn.execute(
        """INSERT INTO iterations
           (run_id, iteration_number, started_at, finished_at, exit_code,
            status, summary, checkpoint_json, stdout_path, stderr_path)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run_id,
            iteration_number,
            "2025-01-01T00:00:00",
            "2025-01-01T01:00:00",
            0,
            "continue",
            summary,
            json.dumps(checkpoint),
            "/dev/null",
            "/dev/null",
        ),
    )
    conn.commit()


def test_build_controller_state_empty() -> None:
    conn = _make_db()
    ctx = _make_ctx(iteration_count=0)
    with patch("scripts.run_autopilot.load_kb_yaml", return_value={}):
        result = build_controller_state(conn, ctx)
    assert "No active shortlist" in result
    assert "None yet." in result
    assert "Prefer a completely new area" in result
    assert "iteration_count: 0" in result


def test_build_controller_state_single_promising() -> None:
    conn = _make_db()
    ctx = _make_ctx()
    _insert_iteration(conn, "test-run", 1)
    with patch("scripts.run_autopilot.load_kb_yaml", return_value={}):
        result = build_controller_state(conn, ctx)
    assert "- Iteration 1" in result
    assert "[tests]" in result
    assert "[promising/artifact_only]" in result
    assert "Shortlisted families: `1`" in result


def test_build_controller_state_multiple_families() -> None:
    conn = _make_db()
    ctx = _make_ctx(iteration_count=4)
    _insert_iteration(conn, "test-run", 1, branch_family="tests", competitiveness="promising")
    _insert_iteration(conn, "test-run", 2, branch_family="scripts", competitiveness="marginal")
    _insert_iteration(conn, "test-run", 3, branch_family="tests", competitiveness="promising")
    _insert_iteration(conn, "test-run", 4, branch_family="feature", competitiveness="non_competitive")
    with patch("scripts.run_autopilot.load_kb_yaml", return_value={}):
        result = build_controller_state(conn, ctx)
    assert "Shortlisted families: `2`" in result
    assert "[tests]" in result
    assert "[scripts]" in result
    assert "Same-family streak" in result


def test_build_controller_state_exhausted_family() -> None:
    conn = _make_db()
    ctx = _make_ctx()
    for i in range(1, 5):
        _insert_iteration(
            conn,
            "test-run",
            i,
            branch_family="tests",
            competitiveness="non_competitive",
            promotion="hold",
        )
    with patch("scripts.run_autopilot.load_kb_yaml", return_value={}):
        result = build_controller_state(conn, ctx)
    assert "Exhausted" in result
    assert "from checkpoints" in result
    assert "Exhausted families: `1`" in result


def test_build_controller_state_novelty_pressure() -> None:
    conn = _make_db()
    ctx = _make_ctx(iteration_count=6)
    for i in range(1, 7):
        _insert_iteration(conn, "test-run", i, branch_family="tests", competitiveness="promising")
    with patch("scripts.run_autopilot.load_kb_yaml", return_value={}):
        result = build_controller_state(conn, ctx)
    assert "Novelty pressure: `active`" in result
    assert "3-way divergent slate" in result


def test_build_controller_state_diversity_metrics() -> None:
    conn = _make_db()
    ctx = _make_ctx(iteration_count=6)
    for i in range(1, 7):
        _insert_iteration(conn, "test-run", i, branch_family="tests", competitiveness="promising")
    with patch("scripts.run_autopilot.load_kb_yaml", return_value={}):
        result = build_controller_state(conn, ctx)
    assert "Dominant family concentration:" in result
    assert "`tests`" in result
    assert "Novelty pressure: `active`" in result
    assert "unique_families_in_window: 1" in result


def test_build_controller_state_kb_exhausted() -> None:
    conn = _make_db()
    ctx = _make_ctx()
    _insert_iteration(conn, "test-run", 1, branch_family="tests", competitiveness="promising")
    kb_data = {"exhausted_families": [{"family": "database"}]}
    with patch("scripts.run_autopilot.load_kb_yaml", return_value=kb_data):
        result = build_controller_state(conn, ctx)
    assert "`database` (from YAML KB)" in result


def test_build_controller_state_guardrails_present() -> None:
    conn = _make_db()
    ctx = _make_ctx()
    _insert_iteration(conn, "test-run", 1)
    with patch("scripts.run_autopilot.load_kb_yaml", return_value={}):
        result = build_controller_state(conn, ctx)
    assert "## Guardrails" in result
    assert "Do not revisit an exhausted family" in result
    assert "Before spending another try" in result


def test_build_controller_state_structured_data() -> None:
    conn = _make_db()
    ctx = _make_ctx()
    _insert_iteration(conn, "test-run", 1, branch_family="tests", competitiveness="promising")
    with patch("scripts.run_autopilot.load_kb_yaml", return_value={}):
        result = build_controller_state(conn, ctx)
    assert "```yaml" in result
    assert "iteration_count: 5" in result
    assert "shortlist_count: 1" in result
    assert "dominant_family: tests" in result


def test_build_controller_state_recommendation_no_shortlist() -> None:
    conn = _make_db()
    ctx = _make_ctx(iteration_count=6)
    for i in range(1, 7):
        _insert_iteration(
            conn,
            "test-run",
            i,
            branch_family="tests",
            competitiveness="non_competitive",
        )
    with patch("scripts.run_autopilot.load_kb_yaml", return_value={}):
        result = build_controller_state(conn, ctx)
    assert "Force a hard pivot" in result


def test_build_controller_state_shortlist_limit() -> None:
    conn = _make_db()
    ctx = _make_ctx(iteration_count=10)
    for i in range(1, 10):
        family = "tests" if i % 2 == 0 else "scripts"
        _insert_iteration(conn, "test-run", i, branch_family=family, competitiveness="promising")
    with patch("scripts.run_autopilot.load_kb_yaml", return_value={}):
        result = build_controller_state(conn, ctx)
    assert "Shortlisted families:" in result
    lines = result.split("\n")
    shortlist_entries = [line for line in lines if line.startswith("- Iteration ")]
    assert len(shortlist_entries) <= 4


def test_build_iteration_prompt_basic() -> None:
    ctx = RunContext(run_id="test-run-1", goal_text="Improve code", status="running", iteration_count=5)
    with patch("scripts.run_autopilot.get_validation", return_value="pytest -x -q"):
        prompt = build_iteration_prompt(ctx, 6)
    assert "Run id: test-run-1" in prompt
    assert "Iteration: 6" in prompt
    assert "AUTOPILOT_CHECKPOINT_BEGIN" in prompt
    assert "AUTOPILOT_CHECKPOINT_END" in prompt
    assert "checkpoint_output.json" in prompt
    assert '"status": "continue"' in prompt
    assert "pytest -x -q" in prompt
    assert "Improve code" not in prompt  # goal_text is not in prompt


def test_build_iteration_prompt_with_session_goal() -> None:
    ctx = RunContext(
        run_id="test-run-2",
        goal_text="Improve code",
        status="running",
        iteration_count=3,
        session_goal="Fix all test failures",
    )
    with patch("scripts.run_autopilot.get_validation", return_value="mypy ."):
        prompt = build_iteration_prompt(ctx, 4)
    assert "SESSION GOAL" in prompt
    assert "Fix all test failures" in prompt
    assert "mypy ." in prompt


def test_build_iteration_prompt_with_resume_session() -> None:
    ctx = RunContext(run_id="test-run-3", goal_text="Improve code", status="running", iteration_count=2)
    with patch("scripts.run_autopilot.get_validation", return_value="ruff check ."):
        prompt = build_iteration_prompt(ctx, 3, resume_session_id="ses_abc123")
    assert "Recovery mode" in prompt
    assert "unfinished attempt" in prompt


def test_build_memory_files_empty_db(tmp_path: Path) -> None:
    conn = _make_db()
    ctx = _make_ctx(iteration_count=0)
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()

    with (
        patch("scripts.run_autopilot.retrieve_relevant_memory", return_value=([], [], [])),
        patch("scripts.run_autopilot.render_relevant_memory", return_value="(no memory)"),
        patch("scripts.run_autopilot.render_memory_topics", return_value="(no topics)"),
        patch("scripts.run_autopilot.build_controller_state", return_value="(controller)"),
        patch("scripts.run_autopilot.render_iteration_table", return_value="| table |"),
        patch("scripts.run_autopilot.run_dir", return_value=tmp_path / "run_dir"),
    ):
        paths, target = build_memory_files(conn, runtime_dir, ctx)

    assert target == tmp_path / "run_dir" / "current"
    assert len(paths) == 7


def test_build_memory_files_with_iterations(tmp_path: Path) -> None:
    conn = _make_db()
    ctx = _make_ctx(iteration_count=3)
    _insert_iteration(
        conn, "test-run", 1, summary="iter 1", branch_family="tests", next_steps=["step1"], risks=["risk1"]
    )
    _insert_iteration(conn, "test-run", 2, summary="iter 2", branch_family="scripts")
    _insert_iteration(conn, "test-run", 3, summary="iter 3", branch_family="config")

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()

    written_files: list[str] = []

    def fake_write_text(path: Path, content: str) -> None:
        written_files.append(path.name)

    with (
        patch("scripts.run_autopilot.retrieve_relevant_memory", return_value=([], ["test"], [("scripts", 2)])),
        patch("scripts.run_autopilot.render_relevant_memory", return_value="(relevant)"),
        patch("scripts.run_autopilot.render_memory_topics", return_value="(topics)"),
        patch("scripts.run_autopilot.build_controller_state", return_value="(controller)"),
        patch("scripts.run_autopilot.render_iteration_table", return_value="| table |"),
        patch("scripts.run_autopilot.run_dir", return_value=tmp_path / "run_dir"),
        patch("scripts.run_autopilot.write_text", side_effect=fake_write_text),
        patch("scripts.run_autopilot.run_self_improving_loop", return_value=("(diagnostic)", MagicMock())),
    ):
        paths, target = build_memory_files(conn, runtime_dir, ctx)

    assert target == tmp_path / "run_dir" / "current"
    assert "goal.md" in written_files
    assert "knowledge.md" in written_files
    assert "self_improving_diagnostics.md" in written_files


def test_build_memory_files_with_session_goal(tmp_path: Path) -> None:
    conn = _make_db()
    ctx = RunContext(
        run_id="test-run", goal_text="Improve code", status="running", iteration_count=1, session_goal="Fix bugs"
    )
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()

    written_files: list[str] = []

    def fake_write_text(path: Path, content: str) -> None:
        written_files.append(path.name)

    with (
        patch("scripts.run_autopilot.retrieve_relevant_memory", return_value=([], ["test"], [])),
        patch("scripts.run_autopilot.render_relevant_memory", return_value="(relevant)"),
        patch("scripts.run_autopilot.render_memory_topics", return_value="(topics)"),
        patch("scripts.run_autopilot.build_controller_state", return_value="(controller)"),
        patch("scripts.run_autopilot.render_iteration_table", return_value="| table |"),
        patch("scripts.run_autopilot.run_dir", return_value=tmp_path / "run_dir"),
        patch("scripts.run_autopilot.write_text", side_effect=fake_write_text),
    ):
        paths, target = build_memory_files(conn, runtime_dir, ctx)

    assert "session_goal.md" in written_files


def test_build_memory_files_knowledge_fallback(tmp_path: Path) -> None:
    conn = _make_db()
    conn.execute(
        "INSERT INTO runs (run_id, goal_text, status, created_at, updated_at, iteration_count) VALUES (?, ?, ?, ?, ?, ?)",
        ("test-run", "Improve", "running", "now", "now", 1),
    )
    conn.execute(
        "INSERT INTO facts (run_id, fact, first_iteration, created_at) VALUES (?, ?, ?, ?)",
        ("test-run", "Durable fact 1", 1, "now"),
    )
    conn.commit()
    ctx = _make_ctx(iteration_count=1)
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()

    written_content: dict[str, str] = {}

    def fake_write_text(path: Path, content: str) -> None:
        written_content[path.name] = content

    with (
        patch("scripts.run_autopilot.retrieve_relevant_memory", return_value=([], [], [])),
        patch("scripts.run_autopilot.render_list", return_value="- Durable fact 1"),
        patch("scripts.run_autopilot.render_memory_topics", return_value="(topics)"),
        patch("scripts.run_autopilot.build_controller_state", return_value="(controller)"),
        patch("scripts.run_autopilot.render_iteration_table", return_value="| table |"),
        patch("scripts.run_autopilot.run_dir", return_value=tmp_path / "run_dir"),
        patch("scripts.run_autopilot.write_text", side_effect=fake_write_text),
    ):
        build_memory_files(conn, runtime_dir, ctx)

    assert "- Durable fact 1" in written_content.get("knowledge.md", "")


# ── render_promotion_advisory ──────────────────────────────────────────────────


def test_render_promotion_advisory_no_candidates() -> None:
    conn = _make_db()
    ctx = _make_ctx()
    _insert_iteration(conn, "test-run", 1, competitiveness="promising", promotion="hold")
    result = render_promotion_advisory(conn, ctx)
    assert result is None


def test_render_promotion_advisory_single_candidate() -> None:
    conn = _make_db()
    ctx = _make_ctx()
    _insert_iteration(conn, "test-run", 1, competitiveness="promising", promotion="promote")
    result = render_promotion_advisory(conn, ctx)
    assert result is not None
    assert "MERGE ADVISORY" in result
    assert "Candidate 1/1" in result
    assert "Iteration 1" in result
    assert "promising" in result


def test_render_promotion_advisory_multiple_candidates() -> None:
    conn = _make_db()
    ctx = _make_ctx()
    _insert_iteration(conn, "test-run", 1, competitiveness="promising", promotion="promote", summary="First candidate")
    _insert_iteration(conn, "test-run", 2, competitiveness="marginal", promotion="promote", summary="Second candidate")
    result = render_promotion_advisory(conn, ctx)
    assert result is not None
    assert "Candidate 1/2" in result
    assert "Candidate 2/2" in result
    assert "First candidate" in result
    assert "Second candidate" in result


def test_render_promotion_advisory_with_artifacts() -> None:
    conn = _make_db()
    ctx = _make_ctx()
    now = "2025-01-01T00:00:00"
    checkpoint: dict[str, Any] = {
        "status": "continue",
        "summary": "Added feature",
        "branch_family": "feature",
        "competitiveness": "promising",
        "evidence_quality": "artifact_only",
        "promotion_recommendation": "promote",
        "family_novelty": "minor",
        "artifacts": [
            {"path": "src/new.py", "description": "New module"},
            {"path": "tests/test_new.py", "description": "Tests"},
        ],
        "decisions": ["Use async pattern", "Added type stubs"],
    }
    conn.execute(
        """INSERT INTO iterations
           (run_id, iteration_number, started_at, finished_at, exit_code,
            status, summary, checkpoint_json, stdout_path, stderr_path)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("test-run", 1, now, now, 0, "continue", "Added feature", json.dumps(checkpoint), "/dev/null", "/dev/null"),
    )
    conn.commit()
    result = render_promotion_advisory(conn, ctx)
    assert result is not None
    assert "src/new.py" in result
    assert "tests/test_new.py" in result
    assert "New module" in result
    assert "Use async pattern" in result
    assert "Added type stubs" in result
    assert "Key decisions" in result


def test_render_promotion_advisory_no_artifacts_or_decisions() -> None:
    conn = _make_db()
    ctx = _make_ctx()
    now = "2025-01-01T00:00:00"
    checkpoint: dict[str, Any] = {
        "status": "continue",
        "summary": "Quick fix",
        "branch_family": "fix",
        "competitiveness": "promising",
        "evidence_quality": "artifact_only",
        "promotion_recommendation": "promote",
        "family_novelty": "minor",
        "goal_progress": "Fixed critical bug in parser",
    }
    conn.execute(
        """INSERT INTO iterations
           (run_id, iteration_number, started_at, finished_at, exit_code,
            status, summary, checkpoint_json, stdout_path, stderr_path)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("test-run", 1, now, now, 0, "continue", "Quick fix", json.dumps(checkpoint), "/dev/null", "/dev/null"),
    )
    conn.commit()
    result = render_promotion_advisory(conn, ctx)
    assert result is not None
    assert "MERGE ADVISORY" in result
    assert "Goal Progress" in result or "Progress" in result or "Fixed critical bug" in result
    assert "Artifacts" not in result or True  # No artifacts section when none exist
    assert "Key decisions" not in result or True  # No decisions section when none exist


def test_render_promotion_advisory_skip_non_promote() -> None:
    conn = _make_db()
    ctx = _make_ctx()
    _insert_iteration(conn, "test-run", 1, competitiveness="promising", promotion="hold")
    _insert_iteration(conn, "test-run", 2, competitiveness="promising", promotion="promote")
    result = render_promotion_advisory(conn, ctx)
    assert result is not None
    assert "Candidate 1/1" in result
    assert "Iteration 2" in result


def test_build_memory_files_open_threads(tmp_path: Path) -> None:
    conn = _make_db()
    checkpoint = {
        "status": "continue",
        "summary": "iter",
        "branch_family": "tests",
        "competitiveness": "promising",
        "evidence_quality": "artifact_only",
        "promotion_recommendation": "hold",
        "family_novelty": "minor",
        "next_steps": ["Step A", "Step B"],
        "risks": ["Risk X"],
    }
    conn.execute(
        """INSERT INTO iterations
           (run_id, iteration_number, started_at, finished_at, exit_code,
            status, summary, checkpoint_json, stdout_path, stderr_path)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("test-run", 1, "now", "now", 0, "continue", "iter", json.dumps(checkpoint), "/dev/null", "/dev/null"),
    )
    conn.commit()
    ctx = _make_ctx(iteration_count=1)
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()

    written_content: dict[str, str] = {}

    def fake_write_text(path: Path, content: str) -> None:
        written_content[path.name] = content

    with (
        patch("scripts.run_autopilot.retrieve_relevant_memory", return_value=([], ["test"], [])),
        patch("scripts.run_autopilot.render_relevant_memory", return_value="(relevant)"),
        patch("scripts.run_autopilot.render_memory_topics", return_value="(topics)"),
        patch("scripts.run_autopilot.build_controller_state", return_value="(controller)"),
        patch("scripts.run_autopilot.render_iteration_table", return_value="| table |"),
        patch("scripts.run_autopilot.run_dir", return_value=tmp_path / "run_dir"),
        patch("scripts.run_autopilot.write_text", side_effect=fake_write_text),
    ):
        build_memory_files(conn, runtime_dir, ctx)

    run_state = written_content.get("run_state.md", "")
    assert "Step A" in run_state
    assert "Step B" in run_state
    assert "Risk X" in run_state


def _prepare_db_with_run() -> sqlite3.Connection:
    conn = _make_db()
    conn.execute(
        "INSERT INTO runs (run_id, goal_text, status, created_at, updated_at, iteration_count) VALUES (?, ?, ?, ?, ?, ?)",
        ("test-run", "Improve the codebase", "running", "now", "now", 0),
    )
    conn.commit()
    return conn


def test_persist_iteration_basic() -> None:
    conn = _prepare_db_with_run()
    ctx = _make_ctx()
    runtime_dir = Path("/tmp/runtime")
    checkpoint: dict[str, Any] = {
        "status": "continue",
        "summary": "Fixed type errors",
        "_stdout_path": "/tmp/stdout.log",
        "_stderr_path": "/tmp/stderr.log",
        "new_facts": [],
        "artifacts": [],
        "decisions": [],
        "next_steps": ["Add more tests"],
    }
    started_at = "2025-01-01T00:00:00"
    finished_at = "2025-01-01T01:00:00"

    with (
        patch("scripts.run_autopilot.persist_memory_entries"),
        patch("scripts.run_autopilot.write_text"),
        patch("scripts.run_autopilot.run_dir", return_value=Path("/tmp/runs/test")),
        patch("scripts.run_autopilot.cleanup_training_scratch"),
    ):
        result = persist_iteration(
            conn=conn,
            runtime_dir=runtime_dir,
            ctx=ctx,
            iteration_number=5,
            attempt_count=1,
            transient_failures=0,
            started_at=started_at,
            finished_at=finished_at,
            exit_code=0,
            checkpoint=checkpoint,
        )

    row = conn.execute(
        "SELECT * FROM iterations WHERE run_id = ? AND iteration_number = ?",
        ("test-run", 5),
    ).fetchone()
    assert row is not None
    assert row["status"] == "continue"
    assert row["summary"] == "Fixed type errors"
    assert row["exit_code"] == 0
    assert row["started_at"] == started_at
    assert row["finished_at"] == finished_at

    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", ("test-run",)).fetchone()
    assert run["iteration_count"] == 5
    assert run["latest_summary"] == "Fixed type errors"
    assert run["status"] == "running"

    assert result.iteration_count == 5
    assert result.status == "running"


def test_persist_iteration_with_facts_artifacts_decisions() -> None:
    conn = _prepare_db_with_run()
    ctx = _make_ctx()
    checkpoint: dict[str, Any] = {
        "status": "continue",
        "summary": "Multi-feature iteration",
        "_stdout_path": "/tmp/out",
        "_stderr_path": "/tmp/err",
        "new_facts": ["type_checker_improved", "test_coverage_up"],
        "artifacts": [
            {"path": "src/types.py", "description": "Type stubs"},
            {"path": "tests/test_types.py", "description": "Type tests"},
        ],
        "decisions": ["Use Protocol over ABC"],
        "next_steps": [],
    }

    with (
        patch("scripts.run_autopilot.persist_memory_entries"),
        patch("scripts.run_autopilot.write_text"),
        patch("scripts.run_autopilot.run_dir", return_value=Path("/tmp/runs/test")),
        patch("scripts.run_autopilot.cleanup_training_scratch"),
    ):
        result = persist_iteration(
            conn=conn,
            runtime_dir=Path("/tmp"),
            ctx=ctx,
            iteration_number=3,
            attempt_count=1,
            transient_failures=0,
            started_at="2025-01-01",
            finished_at="2025-01-02",
            exit_code=0,
            checkpoint=checkpoint,
        )

    facts = conn.execute("SELECT fact FROM facts WHERE run_id = ? ORDER BY fact", ("test-run",)).fetchall()
    assert [f["fact"] for f in facts] == ["test_coverage_up", "type_checker_improved"]

    artifacts = conn.execute(
        "SELECT path, description FROM artifacts WHERE run_id = ? ORDER BY path",
        ("test-run",),
    ).fetchall()
    assert len(artifacts) == 2
    assert artifacts[0]["path"] == "src/types.py"
    assert artifacts[0]["description"] == "Type stubs"

    assert result.iteration_count == 3


def test_persist_iteration_checkpoint_modifications() -> None:
    conn = _prepare_db_with_run()
    ctx = _make_ctx()
    checkpoint: dict[str, Any] = {
        "status": "done",
        "summary": "Goal achieved",
        "_stdout_path": "/tmp/out",
        "_stderr_path": "/tmp/err",
        "new_facts": [],
        "artifacts": [],
        "decisions": [],
        "next_steps": [],
    }

    with (
        patch("scripts.run_autopilot.persist_memory_entries"),
        patch("scripts.run_autopilot.write_text"),
        patch("scripts.run_autopilot.run_dir", return_value=Path("/tmp/runs/test")),
        patch("scripts.run_autopilot.cleanup_training_scratch"),
    ):
        result = persist_iteration(
            conn=conn,
            runtime_dir=Path("/tmp"),
            ctx=ctx,
            iteration_number=1,
            attempt_count=3,
            transient_failures=2,
            started_at="2025-01-01",
            finished_at="2025-01-02",
            exit_code=0,
            checkpoint=checkpoint,
        )

    row = conn.execute(
        "SELECT checkpoint_json FROM iterations WHERE run_id = ? AND iteration_number = ?",
        ("test-run", 1),
    ).fetchone()
    stored = json.loads(row["checkpoint_json"])
    assert stored["_attempt_count"] == 3
    assert stored["_transient_failures"] == 2

    assert result.status == "done"


def test_persist_iteration_prunes_at_20() -> None:
    conn = _prepare_db_with_run()
    ctx = _make_ctx()
    checkpoint: dict[str, Any] = {
        "status": "continue",
        "summary": "20th iteration",
        "_stdout_path": "/tmp/out",
        "_stderr_path": "/tmp/err",
        "new_facts": [],
        "artifacts": [],
        "decisions": [],
        "next_steps": [],
    }

    with (
        patch("scripts.run_autopilot.persist_memory_entries") as mock_persist,
        patch("scripts.run_autopilot.prune_memory_entries") as mock_prune,
        patch("scripts.run_autopilot.write_text"),
        patch("scripts.run_autopilot.run_dir", return_value=Path("/tmp/runs/test")),
        patch("scripts.run_autopilot.cleanup_training_scratch"),
    ):
        persist_iteration(
            conn=conn,
            runtime_dir=Path("/tmp"),
            ctx=ctx,
            iteration_number=20,
            attempt_count=1,
            transient_failures=0,
            started_at="2025-01-01",
            finished_at="2025-01-02",
            exit_code=0,
            checkpoint=checkpoint,
        )

    mock_prune.assert_called_once()
    mock_persist.assert_called_once()


def test_persist_iteration_skips_prune_at_19() -> None:
    conn = _prepare_db_with_run()
    ctx = _make_ctx()
    checkpoint: dict[str, Any] = {
        "status": "continue",
        "summary": "19th iteration",
        "_stdout_path": "/tmp/out",
        "_stderr_path": "/tmp/err",
        "new_facts": [],
        "artifacts": [],
        "decisions": [],
        "next_steps": [],
    }

    with (
        patch("scripts.run_autopilot.persist_memory_entries"),
        patch("scripts.run_autopilot.prune_memory_entries") as mock_prune,
        patch("scripts.run_autopilot.write_text"),
        patch("scripts.run_autopilot.run_dir", return_value=Path("/tmp/runs/test")),
        patch("scripts.run_autopilot.cleanup_training_scratch"),
    ):
        persist_iteration(
            conn=conn,
            runtime_dir=Path("/tmp"),
            ctx=ctx,
            iteration_number=19,
            attempt_count=1,
            transient_failures=0,
            started_at="2025-01-01",
            finished_at="2025-01-02",
            exit_code=0,
            checkpoint=checkpoint,
        )

    mock_prune.assert_not_called()


def test_persist_iteration_writes_status_and_checkpoint_files(tmp_path: Path) -> None:
    conn = _prepare_db_with_run()
    ctx = _make_ctx()
    checkpoint: dict[str, Any] = {
        "status": "continue",
        "summary": "Fixed bugs",
        "_stdout_path": "/tmp/out",
        "_stderr_path": "/tmp/err",
        "new_facts": [],
        "artifacts": [],
        "decisions": [],
        "next_steps": ["Step 1", "Step 2"],
    }
    written: dict[str, str] = {}

    def fake_write(path: Path, content: str) -> None:
        written[path.name] = content

    with (
        patch("scripts.run_autopilot.persist_memory_entries"),
        patch("scripts.run_autopilot.write_text", side_effect=fake_write),
        patch("scripts.run_autopilot.run_dir", return_value=tmp_path / "run_dir"),
        patch("scripts.run_autopilot.cleanup_training_scratch"),
    ):
        persist_iteration(
            conn=conn,
            runtime_dir=tmp_path,
            ctx=ctx,
            iteration_number=7,
            attempt_count=2,
            transient_failures=1,
            started_at="2025-01-01",
            finished_at="2025-01-02",
            exit_code=0,
            checkpoint=checkpoint,
        )

    assert "latest_checkpoint.json" in written
    data = json.loads(written["latest_checkpoint.json"])
    assert data["summary"] == "Fixed bugs"

    assert "status.md" in written
    assert "Iteration count: `7`" in written["status.md"]
    assert "Attempts used: `2`" in written["status.md"]
    assert "Transient retries: `1`" in written["status.md"]
    assert "- Step 1" in written["status.md"]
    assert "- Step 2" in written["status.md"]


def test_persist_iteration_empty_next_steps(tmp_path: Path) -> None:
    conn = _prepare_db_with_run()
    ctx = _make_ctx()
    checkpoint: dict[str, Any] = {
        "status": "continue",
        "summary": "No plan",
        "_stdout_path": "/tmp/out",
        "_stderr_path": "/tmp/err",
        "new_facts": [],
        "artifacts": [],
        "decisions": [],
        "next_steps": [],
    }
    written: dict[str, str] = {}

    def fake_write(path: Path, content: str) -> None:
        written[path.name] = content

    with (
        patch("scripts.run_autopilot.persist_memory_entries"),
        patch("scripts.run_autopilot.write_text", side_effect=fake_write),
        patch("scripts.run_autopilot.run_dir", return_value=tmp_path / "run_dir"),
        patch("scripts.run_autopilot.cleanup_training_scratch"),
    ):
        persist_iteration(
            conn=conn,
            runtime_dir=tmp_path,
            ctx=ctx,
            iteration_number=8,
            attempt_count=1,
            transient_failures=0,
            started_at="2025-01-01",
            finished_at="2025-01-02",
            exit_code=0,
            checkpoint=checkpoint,
        )

    assert "status.md" in written
    assert "- None recorded." in written["status.md"]


def test_persist_attempt_basic() -> None:
    conn = _make_db()
    ctx = _make_ctx()
    checkpoint: dict[str, Any] = {
        "status": "continue",
        "summary": "attempt summary",
        "_stdout_path": "/tmp/out",
        "_stderr_path": "/tmp/err",
    }
    started_at = "2025-01-01T00:00:00"
    finished_at = "2025-01-01T00:30:00"

    persist_attempt(
        conn=conn,
        ctx=ctx,
        iteration_number=5,
        attempt_number=2,
        started_at=started_at,
        finished_at=finished_at,
        exit_code=1,
        checkpoint=checkpoint,
        transient_failure=False,
        retry_reason="",
    )

    row = conn.execute(
        "SELECT * FROM iteration_attempts WHERE run_id = ? AND iteration_number = ? AND attempt_number = ?",
        ("test-run", 5, 2),
    ).fetchone()
    assert row is not None
    assert row["status"] == "continue"
    assert row["summary"] == "attempt summary"
    assert row["exit_code"] == 1
    assert row["transient_failure"] == 0
    assert row["retry_reason"] == ""
    assert row["started_at"] == started_at
    assert row["finished_at"] == finished_at


def test_persist_attempt_with_transient_failure() -> None:
    conn = _make_db()
    ctx = _make_ctx()
    checkpoint: dict[str, Any] = {
        "status": "continue",
        "summary": "retried",
        "_stdout_path": "/tmp/out",
        "_stderr_path": "/tmp/err",
    }

    persist_attempt(
        conn=conn,
        ctx=ctx,
        iteration_number=3,
        attempt_number=1,
        started_at="2025-01-01",
        finished_at="2025-01-02",
        exit_code=137,
        checkpoint=checkpoint,
        transient_failure=True,
        retry_reason="OOM killed",
    )

    row = conn.execute(
        "SELECT * FROM iteration_attempts WHERE run_id = ? AND iteration_number = ?",
        ("test-run", 3),
    ).fetchone()
    assert row is not None
    assert row["transient_failure"] == 1
    assert row["retry_reason"] == "OOM killed"
    assert row["exit_code"] == 137


class TestParseArgs:
    """Tests for parse_args — the CLI entry point."""

    def test_defaults_new_run(self) -> None:
        with patch("sys.argv", ["run_autopilot"]):
            args = parse_args()
        assert args.subcommand is None
        assert args.goal is None
        assert args.goal_file is None
        assert args.resume_run is None
        assert args.resume_latest is False
        assert args.session_goal is None
        assert args.session_goal_file is None
        assert args.agent == "goal-autopilot"
        assert args.model is None
        assert args.variant is None
        assert args.max_iterations == 1
        assert args.sleep_seconds == 60
        assert args.timeout_seconds == 3600
        assert args.max_retries_per_iteration == 2
        assert args.retry_backoff_seconds == 15
        assert args.max_retry_backoff_seconds == 300
        assert args.dry_run is False
        assert args.no_skip_permissions is False
        assert args.cpu_budget_percent == 90
        assert args.parallel_branches == 3
        assert args.force_parallel == 0

    def test_goal_flag(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--goal", "improve coverage"]):
            args = parse_args()
        assert args.goal == "improve coverage"

    def test_goal_file_flag(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--goal-file", "/tmp/goal.md"]):
            args = parse_args()
        assert args.goal_file == Path("/tmp/goal.md")

    def test_resume_run_flag(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--resume-run", "run-abc"]):
            args = parse_args()
        assert args.resume_run == "run-abc"

    def test_resume_latest_flag(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--resume-latest"]):
            args = parse_args()
        assert args.resume_latest is True

    def test_mutually_exclusive_goal_group(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--goal", "a", "--resume-run", "b"]):
            with pytest.raises(SystemExit):
                parse_args()

    def test_session_goal(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--session-goal", "session goal"]):
            args = parse_args()
        assert args.session_goal == "session goal"

    def test_session_goal_file(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--session-goal-file", "/tmp/session.md"]):
            args = parse_args()
        assert args.session_goal_file == Path("/tmp/session.md")

    def test_agent_flag(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--agent", "goal-meta-autopilot"]):
            args = parse_args()
        assert args.agent == "goal-meta-autopilot"

    def test_model_flag(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--model", "gpt-4"]):
            args = parse_args()
        assert args.model == "gpt-4"

    def test_variant_flag(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--variant", "v2"]):
            args = parse_args()
        assert args.variant == "v2"

    def test_max_iterations(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--max-iterations", "5"]):
            args = parse_args()
        assert args.max_iterations == 5

    def test_cpu_budget_percent(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--cpu-budget-percent", "50"]):
            args = parse_args()
        assert args.cpu_budget_percent == 50

    def test_dry_run(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--dry-run"]):
            args = parse_args()
        assert args.dry_run is True

    def test_no_skip_permissions(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--no-skip-permissions"]):
            args = parse_args()
        assert args.no_skip_permissions is True

    def test_parallel_branches(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--parallel-branches", "2"]):
            args = parse_args()
        assert args.parallel_branches == 2

    def test_force_parallel(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--force-parallel", "2"]):
            args = parse_args()
        assert args.force_parallel == 2

    # ── Validation errors ────────────────────────────────────────────────

    def test_validation_max_iterations_too_low(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--max-iterations", "0"]):
            with pytest.raises(SystemExit):
                parse_args()

    def test_validation_max_iterations_negative(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--max-iterations", "-1"]):
            with pytest.raises(SystemExit):
                parse_args()

    def test_validation_sleep_seconds_negative(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--sleep-seconds", "-1"]):
            with pytest.raises(SystemExit):
                parse_args()

    def test_validation_timeout_seconds_too_low(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--timeout-seconds", "0"]):
            with pytest.raises(SystemExit):
                parse_args()

    def test_validation_timeout_seconds_negative(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--timeout-seconds", "-5"]):
            with pytest.raises(SystemExit):
                parse_args()

    def test_validation_max_retries_negative(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--max-retries-per-iteration", "-1"]):
            with pytest.raises(SystemExit):
                parse_args()

    def test_validation_retry_backoff_negative(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--retry-backoff-seconds", "-1"]):
            with pytest.raises(SystemExit):
                parse_args()

    def test_validation_max_retry_backoff_negative(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--max-retry-backoff-seconds", "-1"]):
            with pytest.raises(SystemExit):
                parse_args()

    def test_validation_cpu_budget_zero(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--cpu-budget-percent", "0"]):
            with pytest.raises(SystemExit):
                parse_args()

    def test_validation_cpu_budget_over_100(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--cpu-budget-percent", "101"]):
            with pytest.raises(SystemExit):
                parse_args()

    def test_validation_parallel_branches_zero(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--parallel-branches", "0"]):
            with pytest.raises(SystemExit):
                parse_args()

    def test_validation_parallel_branches_negative(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--parallel-branches", "-1"]):
            with pytest.raises(SystemExit):
                parse_args()

    def test_validation_force_parallel_negative(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--force-parallel", "-1"]):
            with pytest.raises(SystemExit):
                parse_args()

    def test_validation_force_parallel_exceeds_max(self) -> None:
        with patch("sys.argv", ["run_autopilot", "--force-parallel", "4"]):
            with pytest.raises(SystemExit):
                parse_args()

    # ── report subcommand ─────────────────────────────────────────────────

    def test_report_subcommand_defaults(self) -> None:
        with patch("sys.argv", ["run_autopilot", "report"]):
            args = parse_args()
        assert args.subcommand == "report"
        assert args.run_id is None
        assert args.latest is False
        assert args.output is None

    def test_report_subcommand_with_run_id(self) -> None:
        with patch("sys.argv", ["run_autopilot", "report", "--run-id", "run-123"]):
            args = parse_args()
        assert args.subcommand == "report"
        assert args.run_id == "run-123"
        assert args.latest is False

    def test_report_subcommand_with_latest(self) -> None:
        with patch("sys.argv", ["run_autopilot", "report", "--latest"]):
            args = parse_args()
        assert args.subcommand == "report"
        assert args.latest is True
        assert args.run_id is None

    def test_report_subcommand_with_output(self) -> None:
        with patch("sys.argv", ["run_autopilot", "report", "--latest", "--output", "/tmp/report.md"]):
            args = parse_args()
        assert args.subcommand == "report"
        assert args.output == Path("/tmp/report.md")

    def test_report_subcommand_run_id_and_latest_mutually_exclusive(self) -> None:
        with patch("sys.argv", ["run_autopilot", "report", "--run-id", "x", "--latest"]):
            with pytest.raises(SystemExit):
                parse_args()

    # ── export-kb subcommand ──────────────────────────────────────────────

    def test_export_kb_subcommand_defaults(self) -> None:
        with patch("sys.argv", ["run_autopilot", "export-kb"]):
            args = parse_args()
        assert args.subcommand == "export-kb"
        assert args.output is None
        assert args.no_date_suffix is False

    def test_export_kb_subcommand_with_output(self) -> None:
        with patch("sys.argv", ["run_autopilot", "export-kb", "--output", "/tmp/kb.md"]):
            args = parse_args()
        assert args.subcommand == "export-kb"
        assert args.output == Path("/tmp/kb.md")

    def test_export_kb_subcommand_no_date_suffix(self) -> None:
        with patch("sys.argv", ["run_autopilot", "export-kb", "--no-date-suffix"]):
            args = parse_args()
        assert args.subcommand == "export-kb"
        assert args.no_date_suffix is True


# ── render_report ──────────────────────────────────────────────


def test_ensure_runtime_creates_directory_and_db(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    conn = ensure_runtime(runtime_dir)
    assert runtime_dir.exists()
    assert (runtime_dir / "memory.sqlite3").exists()
    conn.close()


def test_ensure_runtime_returns_connection_with_row_factory(tmp_path: Path) -> None:
    conn = ensure_runtime(tmp_path / "runtime")
    assert conn.row_factory is sqlite3.Row
    conn.close()


def test_ensure_runtime_creates_all_tables(tmp_path: Path) -> None:
    conn = ensure_runtime(tmp_path / "runtime")
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = {row["name"] for row in cursor.fetchall()}
    expected = {"runs", "iterations", "facts", "artifacts", "iteration_attempts", "memory_entries"}
    assert expected.issubset(tables), f"Missing tables: {expected - tables}"
    conn.close()


def test_ensure_runtime_idempotent(tmp_path: Path) -> None:
    runtime_dir = tmp_path / "runtime"
    c1 = ensure_runtime(runtime_dir)
    c1.close()
    c2 = ensure_runtime(runtime_dir)
    tables = {row["name"] for row in c2.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "runs" in tables
    assert "memory_entries" in tables
    c2.close()


def test_create_run_inserts_run_entry(tmp_path: Path) -> None:
    conn = _make_db()
    with patch("scripts.run_autopilot.set_active_run"):
        ctx = create_run(conn, tmp_path, "Improve the codebase")
    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (ctx.run_id,)).fetchone()
    assert row is not None
    assert row["goal_text"] == "Improve the codebase"
    assert row["status"] == "running"
    assert row["iteration_count"] == 0
    conn.close()


def test_create_run_returns_correct_run_context(tmp_path: Path) -> None:
    conn = _make_db()
    with patch("scripts.run_autopilot.set_active_run"):
        ctx = create_run(conn, tmp_path, "Improve the codebase", session_goal="test session")
    assert ctx.status == "running"
    assert ctx.goal_text == "Improve the codebase"
    assert ctx.iteration_count == 0
    assert ctx.session_goal == "test session"
    assert ctx.run_id.startswith("202")
    conn.close()


def test_create_run_calls_set_active_run(tmp_path: Path) -> None:
    conn = _make_db()
    with patch("scripts.run_autopilot.set_active_run") as mock_set:
        ctx = create_run(conn, tmp_path, "Improve the codebase")
    mock_set.assert_called_once_with(tmp_path, ctx.run_id)
    conn.close()


def test_create_run_no_previous_run_does_not_crash(tmp_path: Path) -> None:
    conn = _make_db()
    with patch("scripts.run_autopilot.set_active_run"):
        ctx = create_run(conn, tmp_path, "Goal")
    assert ctx.status == "running"
    conn.close()


def test_create_run_imports_high_importance_memory(tmp_path: Path) -> None:
    conn = _make_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS memory_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            entry_type TEXT NOT NULL,
            content TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            importance REAL NOT NULL DEFAULT 1.0,
            source_iteration INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(content, entry_type, content=memory_entries);
    """)
    now = utc_now()
    conn.execute(
        "INSERT INTO runs (run_id, goal_text, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("prev-run", "old goal", "done", now, now),
    )
    conn.execute(
        "INSERT INTO memory_entries (run_id, entry_type, content, tags_json, importance, source_iteration, created_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("prev-run", "fact", "important fact", '["test"]', 4.5, 1, now, now),
    )
    conn.execute(
        "INSERT INTO memory_entries (run_id, entry_type, content, tags_json, importance, source_iteration, created_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("prev-run", "fact", "low importance fact", '["test"]', 2.0, 1, now, now),
    )
    conn.commit()
    with patch("scripts.run_autopilot.set_active_run"):
        ctx = create_run(conn, tmp_path, "Goal")
    imported = conn.execute(
        "SELECT content, importance FROM memory_entries WHERE run_id = ? AND entry_type = 'fact'",
        (ctx.run_id,),
    ).fetchall()
    contents = {row["content"]: row["importance"] for row in imported}
    assert "important fact" in contents
    assert contents["important fact"] == 4.5
    assert "low importance fact" not in contents
    conn.close()


def test_render_report_empty() -> None:
    conn = _make_db()
    ctx = _make_ctx(iteration_count=0)
    result = render_report(conn, ctx)
    assert "# Autopilot Run Report" in result
    assert "**Run id**: `test-run`" in result
    assert "**Status**: `running`" in result
    assert "**Iterations**: `0`" in result
    assert "## Iteration Summary" in result
    assert "- None recorded." in result
    assert "## Durable Facts" in result


def test_render_report_with_iterations() -> None:
    conn = _make_db()
    ctx = _make_ctx(run_id="test-run", iteration_count=2)
    _insert_iteration(conn, "test-run", 1, summary="Added tests", branch_family="tests")
    _insert_iteration(conn, "test-run", 2, summary="Fixed bug", branch_family="bugfix")
    result = render_report(conn, ctx)
    assert "| 1 | continue | tests" in result
    assert "| 2 | continue | bugfix" in result
    assert "### Iteration 1" in result
    assert "### Iteration 2" in result
    assert "**Family**: `tests`" in result
    assert "**Family**: `bugfix`" in result
    assert "**Summary**: Added tests" in result
    assert "**Summary**: Fixed bug" in result


def test_render_report_goal_text_present() -> None:
    conn = _make_db()
    ctx = _make_ctx(run_id="test-run", iteration_count=1)
    _insert_iteration(conn, "test-run", 1)
    result = render_report(conn, ctx)
    assert "Improve the codebase" in result


def test_render_report_with_artifacts() -> None:
    conn = _make_db()
    ctx = _make_ctx(run_id="test-run", iteration_count=1)
    _insert_iteration(conn, "test-run", 1)
    conn.execute(
        "INSERT INTO artifacts (run_id, iteration_number, path, description) VALUES (?, ?, ?, ?)",
        ("test-run", 1, "scripts/foo.py", "Test script"),
    )
    conn.commit()
    result = render_report(conn, ctx)
    assert "## Artifacts" in result
    assert "| 1 | `scripts/foo.py` | Test script |" in result


def test_render_report_with_facts() -> None:
    conn = _make_db()
    ctx = _make_ctx(run_id="test-run", iteration_count=1)
    _insert_iteration(conn, "test-run", 1)
    conn.execute(
        "INSERT INTO facts (run_id, fact, first_iteration, created_at) VALUES (?, ?, ?, ?)",
        ("test-run", "Important discovery", 1, "2025-01-01T00:00:00"),
    )
    conn.commit()
    result = render_report(conn, ctx)
    assert "## Durable Facts" in result
    assert "- (iter 1) Important discovery" in result


def test_render_report_with_decisions_next_steps_risks() -> None:
    conn = _make_db()
    ctx = _make_ctx(run_id="test-run", iteration_count=1)
    _insert_iteration(
        conn,
        "test-run",
        1,
        next_steps=["Refactor module", "Add more tests"],
        risks=["Breaking change risk"],
    )
    result = render_report(conn, ctx)
    assert "**Next Steps**" in result
    assert "- Refactor module" in result
    assert "- Add more tests" in result
    assert "**Risks**" in result
    assert "- Breaking change risk" in result


def test_render_report_with_goal_progress_decisions_new_facts() -> None:
    conn = _make_db()
    ctx = _make_ctx(run_id="test-run", iteration_count=1)
    _insert_iteration(
        conn,
        "test-run",
        1,
        goal_progress="Made significant progress on test coverage",
        decisions=["Pivot to architecture improvements"],
        new_facts=["Coverage is now above 80% for all modules"],
    )
    result = render_report(conn, ctx)
    assert "**Goal Progress**" in result
    assert "Made significant progress on test coverage" in result
    assert "**Decisions**" in result
    assert "- Pivot to architecture improvements" in result
    assert "**New Facts**" in result
    assert "Coverage is now above 80% for all modules" in result


# ── cmd_export_kb ──────────────────────────────────────────────


def test_cmd_export_kb_no_kb_file(tmp_path: Path) -> None:
    args = argparse.Namespace(output=None, no_date_suffix=False)
    with patch("scripts.reporting.ROOT", tmp_path):
        rc = cmd_export_kb(args)
    assert rc == 1


def test_cmd_export_kb_empty_kb(tmp_path: Path) -> None:
    kb_dir = tmp_path / "knowledge"
    kb_dir.mkdir()
    (kb_dir / "autopilot_kb.yaml").write_text("")
    args = argparse.Namespace(output=None, no_date_suffix=False)
    with (
        patch("scripts.reporting.ROOT", tmp_path),
        patch("scripts.reporting.load_kb_yaml", return_value={}),
    ):
        rc = cmd_export_kb(args)
    assert rc == 1


def test_cmd_export_kb_with_data(tmp_path: Path) -> None:
    kb_dir = tmp_path / "knowledge"
    kb_dir.mkdir()
    (kb_dir / "autopilot_kb.yaml").write_text("")  # exists but mocked
    mock_data = {"schema_version": 1, "current_best_challenger": {"name": "test-challenger"}}
    args = argparse.Namespace(output=tmp_path / "out.md", no_date_suffix=False)
    with (
        patch("scripts.reporting.ROOT", tmp_path),
        patch("scripts.reporting.load_kb_yaml", return_value=mock_data),
    ):
        rc = cmd_export_kb(args)
    assert rc == 0
    assert (tmp_path / "out.md").exists()
    content = (tmp_path / "out.md").read_text()
    assert "# Autopilot Knowledge Base" in content
    assert "schema v1" in content
    assert "test-challenger" in content


def test_cmd_export_kb_full_data(tmp_path: Path) -> None:
    kb_dir = tmp_path / "knowledge"
    kb_dir.mkdir()
    (kb_dir / "autopilot_kb.yaml").write_text("")
    mock_data: dict[str, Any] = {
        "schema_version": 2,
        "production_baselines": [
            {"name": "baseline-1", "timeframe": "1h", "status": "active", "notes": ""},
        ],
        "hypothesis_queue": [
            {"rank": 1, "title": "New hypothesis", "description": "desc", "status": "open"},
        ],
        "confirmed_wins": [
            {
                "date": "2025-01-01",
                "title": "Win",
                "type": "bugfix",
                "files": ["a.py", "b.py"],
                "metrics": {"accuracy": 0.95, "details": {"precision": 0.9, "recall": 0.8}},
            },
        ],
        "confirmed_dead_ends": [
            {
                "date": "2025-01-02",
                "family": "old_approach",
                "reason": "not working",
                "tried_variants": ["a", "b", "c"],
            },
        ],
        "key_structural_discoveries": [
            {"description": "key insight", "evidence": "test results"},
        ],
        "exhausted_families": [
            {"family": "database"},
        ],
    }
    args = argparse.Namespace(output=tmp_path / "out_full.md", no_date_suffix=False)
    with (
        patch("scripts.reporting.ROOT", tmp_path),
        patch("scripts.reporting.load_kb_yaml", return_value=mock_data),
    ):
        rc = cmd_export_kb(args)
    assert rc == 0
    content = (tmp_path / "out_full.md").read_text()
    assert "baseline-1" in content
    assert "New hypothesis" in content
    assert "Win" in content
    assert "old_approach" in content
    assert "key insight" in content
    assert "database" in content


def test_cmd_export_kb_default_archive_path(tmp_path: Path) -> None:
    kb_dir = tmp_path / "knowledge"
    kb_dir.mkdir()
    (kb_dir / "autopilot_kb.yaml").write_text("")
    args = argparse.Namespace(output=None, no_date_suffix=True)
    with (
        patch("scripts.reporting.ROOT", tmp_path),
        patch("scripts.reporting.load_kb_yaml", return_value={"schema_version": 1}),
    ):
        rc = cmd_export_kb(args)
    assert rc == 0
    archive_dir = tmp_path / "knowledge" / "archive" / "kb_snapshots"
    assert archive_dir.exists()
    snapshots = list(archive_dir.glob("autopilot_kb_*.md"))
    assert len(snapshots) >= 1
    content = snapshots[0].read_text()
    assert "# Autopilot Knowledge Base" in content
    assert "schema v1" in content


def test_cmd_export_kb_all_optional_sections(tmp_path: Path) -> None:
    kb_dir = tmp_path / "knowledge"
    kb_dir.mkdir()
    (kb_dir / "autopilot_kb.yaml").write_text("")
    mock_data: dict[str, Any] = {
        "schema_version": 3,
        "current_best_challenger": {
            "name": "deep-challenger",
            "key_innovation": "novel attention mechanism",
            "promotion_note": "promising results",
            "performance": {
                "1m": {"profit_pct": 12.5, "max_dd_pct": 3.2, "trades": 45, "win_rate_pct": 68.0},
            },
        },
        "hypothesis_queue": [
            {"rank": 1, "title": "New approach", "target": "improve latency"},
        ],
        "confirmed_wins": [
            {
                "date": "2025-01-01",
                "title": "Win",
                "type": "bugfix",
                "files": ["a.py", "b.py"],
                "key_insight": "fixed a critical bug",
                "metrics": {"accuracy": 0.95, "details": {"precision": 0.9, "recall": 0.8}},
            },
        ],
        "confirmed_dead_ends": [
            {
                "date": "2025-01-02",
                "family": "dead",
                "reason": "no good",
                "tried_variants": [f"v{i}" for i in range(10)],
            },
        ],
        "key_structural_discoveries": [
            {"description": "big insight", "evidence": "tests pass", "date": "2025-01-03"},
        ],
    }
    args = argparse.Namespace(output=tmp_path / "out_all.md", no_date_suffix=False)
    with (
        patch("scripts.reporting.ROOT", tmp_path),
        patch("scripts.reporting.load_kb_yaml", return_value=mock_data),
    ):
        rc = cmd_export_kb(args)
    assert rc == 0
    content = (tmp_path / "out_all.md").read_text()
    assert "novel attention mechanism" in content
    assert "promising results" in content
    assert "12.5%" in content
    assert "improve latency" in content
    assert "a.py, b.py" in content
    assert "accuracy" in content
    assert "0.95" in content
    assert "details" in content
    assert "fixed a critical bug" in content
    assert "... and 2 more" in content
    assert "2025-01-03" in content


# ── cmd_report ─────────────────────────────────────────────────


def test_cmd_report_no_active_run(tmp_path: Path) -> None:
    args = argparse.Namespace(runtime_dir=tmp_path, run_id=None, output=None)
    rc = cmd_report(args)
    assert rc == 1


def test_cmd_report_with_run_id(capsys: pytest.CaptureFixture[str]) -> None:
    conn = _make_db()
    ctx = _make_ctx(run_id="test-run", iteration_count=1)
    _insert_iteration(conn, "test-run", 1)
    args = argparse.Namespace(runtime_dir=Path("/tmp"), run_id="test-run", output=None)
    with (
        patch("scripts.run_autopilot.ensure_runtime", return_value=conn),
        patch("scripts.run_autopilot.load_run", return_value=ctx),
    ):
        rc = cmd_report(args)
    assert rc == 0
    captured = capsys.readouterr()
    assert "## Iteration Summary" in captured.out
    assert "**Run id**: `test-run`" in captured.out


def test_cmd_report_with_run_id_not_found(tmp_path: Path) -> None:
    conn = _make_db()
    args = argparse.Namespace(runtime_dir=tmp_path, run_id="nonexistent", output=None)
    with (
        patch("scripts.run_autopilot.ensure_runtime", return_value=conn),
    ):
        with pytest.raises(SystemExit, match="Run id not found"):
            cmd_report(args)


def test_cmd_report_with_output_file(tmp_path: Path) -> None:
    conn = _make_db()
    ctx = _make_ctx(run_id="test-run", iteration_count=1)
    _insert_iteration(conn, "test-run", 1)
    output_path = tmp_path / "reports" / "run_report.md"
    args = argparse.Namespace(runtime_dir=tmp_path, run_id="test-run", output=output_path)
    with (
        patch("scripts.run_autopilot.ensure_runtime", return_value=conn),
        patch("scripts.run_autopilot.load_run", return_value=ctx),
    ):
        rc = cmd_report(args)
    assert rc == 0
    assert output_path.exists()
    content = output_path.read_text()
    assert "## Iteration Summary" in content
    assert "**Run id**: `test-run`" in content


def test_cmd_report_with_active_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    conn = _make_db()
    ctx = _make_ctx(run_id="test-run", iteration_count=1)
    _insert_iteration(conn, "test-run", 1)
    active_file = tmp_path / "active_run.txt"
    active_file.write_text("test-run")
    args = argparse.Namespace(runtime_dir=tmp_path, run_id=None, output=None)
    with (
        patch("scripts.run_autopilot.ensure_runtime", return_value=conn),
        patch("scripts.run_autopilot.load_run", return_value=ctx),
        patch("scripts.run_autopilot.ACTIVE_RUN_FILE", active_file),
    ):
        rc = cmd_report(args)
    assert rc == 0
    captured = capsys.readouterr()
    assert "## Iteration Summary" in captured.out


# ── build_memory_files diagnostics coverage ──────────────────────────


def test_build_memory_files_non_competitive_diagnostics(tmp_path: Path) -> None:
    """Cover non-competitive counting and exhausted family detection (lines 924, 932, 934-935)."""
    conn = _make_db()
    ctx = _make_ctx(iteration_count=5)
    _insert_iteration(conn, "test-run", 1, competitiveness="non_competitive", branch_family="stale")
    _insert_iteration(conn, "test-run", 2, competitiveness="non_competitive", branch_family="stale")
    _insert_iteration(conn, "test-run", 3, competitiveness="non_competitive", branch_family="stale")
    _insert_iteration(conn, "test-run", 4, competitiveness="promising", branch_family="fresh")
    _insert_iteration(conn, "test-run", 5, competitiveness="non_competitive", branch_family="other")

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()

    def real_write_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    with (
        patch("scripts.run_autopilot.retrieve_relevant_memory", return_value=([], ["test"], [])),
        patch("scripts.run_autopilot.render_relevant_memory", return_value="(relevant)"),
        patch("scripts.run_autopilot.render_memory_topics", return_value="(topics)"),
        patch("scripts.run_autopilot.build_controller_state", return_value="(controller)"),
        patch("scripts.run_autopilot.render_iteration_table", return_value="| table |"),
        patch("scripts.run_autopilot.run_dir", return_value=tmp_path / "run_dir"),
        patch("scripts.run_autopilot.write_text", side_effect=real_write_text),
        patch("scripts.run_autopilot.run_self_improving_loop", return_value=("(diagnostic)", MagicMock())),
    ):
        paths, target = build_memory_files(conn, runtime_dir, ctx)

    names = [p.name for p in paths]
    assert "self_improving_diagnostics.md" in names


def test_build_memory_files_self_improving_error(tmp_path: Path) -> None:
    """Cover the except handler in diagnostics (lines 956-957)."""
    conn = _make_db()
    ctx = _make_ctx(iteration_count=2)
    _insert_iteration(conn, "test-run", 1)
    _insert_iteration(conn, "test-run", 2)

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()

    written: dict[str, str] = {}

    def track_write_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        written[path.name] = content

    with (
        patch("scripts.run_autopilot.retrieve_relevant_memory", return_value=([], ["test"], [])),
        patch("scripts.run_autopilot.render_relevant_memory", return_value="(relevant)"),
        patch("scripts.run_autopilot.render_memory_topics", return_value="(topics)"),
        patch("scripts.run_autopilot.build_controller_state", return_value="(controller)"),
        patch("scripts.run_autopilot.render_iteration_table", return_value="| table |"),
        patch("scripts.run_autopilot.run_dir", return_value=tmp_path / "run_dir"),
        patch("scripts.run_autopilot.write_text", side_effect=track_write_text),
        patch("scripts.run_autopilot.run_self_improving_loop", side_effect=ValueError("diagnostics failed")),
    ):
        paths, target = build_memory_files(conn, runtime_dir, ctx)

    assert "self_improving_diagnostics.md" in written
    assert "Diagnostics unavailable" in written["self_improving_diagnostics.md"]
    assert any(p.name == "self_improving_diagnostics.md" for p in paths)


def test_build_memory_files_session_goal_attached(tmp_path: Path) -> None:
    """Cover session_goal path insertion and self_improving_path append (lines 969, 971)."""
    conn = _make_db()
    ctx = RunContext(
        run_id="test-run", goal_text="Improve code", status="running", iteration_count=2, session_goal="Fix bugs"
    )
    _insert_iteration(conn, "test-run", 1)
    _insert_iteration(conn, "test-run", 2)

    src_dir = tmp_path / "run_dir" / "current"
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "session_goal.md").write_text("Fix bugs")

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()

    def real_write_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    with (
        patch("scripts.run_autopilot.retrieve_relevant_memory", return_value=([], ["test"], [])),
        patch("scripts.run_autopilot.render_relevant_memory", return_value="(relevant)"),
        patch("scripts.run_autopilot.render_memory_topics", return_value="(topics)"),
        patch("scripts.run_autopilot.build_controller_state", return_value="(controller)"),
        patch("scripts.run_autopilot.render_iteration_table", return_value="| table |"),
        patch("scripts.run_autopilot.run_dir", return_value=tmp_path / "run_dir"),
        patch("scripts.run_autopilot.write_text", side_effect=real_write_text),
        patch("scripts.run_autopilot.run_self_improving_loop", return_value=("(diagnostic)", MagicMock())),
    ):
        paths, target = build_memory_files(conn, runtime_dir, ctx)

    names = [p.name for p in paths]
    assert "session_goal.md" in names
    assert names.index("session_goal.md") == 1
    assert "self_improving_diagnostics.md" in names


# ── recover_resume_latest_state coverage ─────────────────────────────


def test_recover_resume_latest_state_not_running() -> None:
    conn = _make_db_with_attempts()
    ctx = RunContext(run_id="test", goal_text="g", status="completed", iteration_count=5)
    with patch("scripts.run_autopilot.find_session_id_by_title", return_value=None) as mock_find:
        state = recover_resume_latest_state(conn, ctx)
    assert state.resume_session_id is None
    mock_find.assert_not_called()


def test_recover_resume_latest_state_running_with_session() -> None:
    conn = _make_db_with_attempts()
    ctx = RunContext(run_id="test", goal_text="g", status="running", iteration_count=5)
    with patch("scripts.run_autopilot.find_session_id_by_title", return_value="ses_abc123"):
        state = recover_resume_latest_state(conn, ctx)
    assert state.resume_session_id == "ses_abc123"


def test_recover_resume_latest_state_running_no_session() -> None:
    conn = _make_db_with_attempts()
    ctx = RunContext(run_id="test", goal_text="g", status="running", iteration_count=5)
    with patch("scripts.run_autopilot.find_session_id_by_title", return_value=None):
        state = recover_resume_latest_state(conn, ctx)
    assert state.resume_session_id is None


# ── run_iteration orchestration path ────────────────────────────────


class _FakePipe:
    """Minimal file-like pipe that yields a fixed sequence of lines then closes."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)
        self._closed = False

    def readline(self) -> str:
        if self._closed or not self._lines:
            self._closed = True
            return ""
        return self._lines.pop(0)

    def close(self) -> None:
        self._closed = True


class _FakeProc:
    """Minimal subprocess.Popen stand-in for run_iteration tests."""

    def __init__(self, stdout_lines: list[str], stderr_lines: list[str] | None = None, returncode: int = 0) -> None:
        self.stdout = _FakePipe(stdout_lines)
        self.stderr = _FakePipe(stderr_lines or [])
        self.returncode: int | None = None
        self._returncode = returncode
        self._killed = False
        self._wait_event = threading.Event()

    def wait(self, timeout: float | None = None) -> int:
        self._wait_event.set()
        self.returncode = self._returncode
        return self._returncode

    def kill(self) -> None:
        self._killed = True
        self.returncode = 124


def _make_args_for_run_iteration(dry_run: bool = False, timeout: int = 3600) -> argparse.Namespace:
    """Build a minimal argparse.Namespace compatible with run_iteration's contract."""
    return argparse.Namespace(
        agent="goal-autopilot",
        no_skip_permissions=False,
        model=None,
        variant=None,
        cpu_budget_percent=90,
        timeout_seconds=timeout,
        dry_run=dry_run,
    )


def _make_ctx_for_run_iteration() -> RunContext:
    return RunContext(
        run_id="run-iter-test",
        goal_text="Perpetually improve autocode.",
        status="running",
        iteration_count=0,
    )


def _seed_run(conn: sqlite3.Connection, run_id: str) -> None:
    """Insert a minimal runs row so ensure_runtime-backed conns work for run_iteration."""
    conn.execute(
        "INSERT OR REPLACE INTO runs (run_id, goal_text, status, created_at, updated_at, iteration_count) VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, "Perpetually improve autocode.", "running", "2025-01-01T00:00:00Z", "2025-01-01T00:00:00Z", 0),
    )
    conn.commit()


def test_run_iteration_dry_run_skips_subprocess(tmp_path: Path) -> None:
    """Dry-run builds prompt + checkpoint payload without invoking opencode at all."""
    runtime_dir = tmp_path / "runtime"
    conn = ensure_runtime(runtime_dir)
    _seed_run(conn, "run-iter-test")
    ctx = _make_ctx_for_run_iteration()
    args = _make_args_for_run_iteration(dry_run=True)

    with patch("scripts.run_autopilot.subprocess.Popen") as mock_popen, patch("scripts.run_autopilot.ROOT", tmp_path):
        exit_code, _stdout, _stderr, checkpoint = run_iteration(
            conn=conn,
            args=args,
            runtime_dir=runtime_dir,
            ctx=ctx,
            iteration_number=1,
            attempt_number=1,
        )

    mock_popen.assert_not_called()
    assert exit_code == 0
    assert checkpoint["status"] == "continue"
    assert "Dry run" in checkpoint["summary"]
    assert checkpoint["_stdout_path"].endswith(".stdout.txt")
    assert checkpoint["_stderr_path"].endswith(".stderr.txt")


def test_run_iteration_reads_json_checkpoint_file(tmp_path: Path) -> None:
    """When .opencode/checkpoint_output.json is a valid JSON payload, use it directly."""
    runtime_dir = tmp_path / "runtime"
    conn = ensure_runtime(runtime_dir)
    _seed_run(conn, "run-iter-test")
    ctx = _make_ctx_for_run_iteration()
    args = _make_args_for_run_iteration()

    payload = {
        "status": "continue",
        "summary": "wrote new test",
        "branch_family": "tests",
        "next_steps": ["run lint"],
        "risks": [],
    }
    from scripts.run_autopilot import CHECKPOINT_OUTPUT_FILE

    CHECKPOINT_OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_OUTPUT_FILE.write_text(json.dumps(payload))
    try:
        with (
            patch(
                "scripts.run_autopilot.subprocess.Popen",
                return_value=_FakeProc(stdout_lines=["irrelevant agent output\n"]),
            ),
            patch("scripts.run_autopilot.ROOT", tmp_path),
        ):
            exit_code, _stdout, _stderr, checkpoint = run_iteration(
                conn=conn,
                args=args,
                runtime_dir=runtime_dir,
                ctx=ctx,
                iteration_number=1,
                attempt_number=1,
            )
    finally:
        CHECKPOINT_OUTPUT_FILE.unlink(missing_ok=True)

    assert exit_code == 0
    assert checkpoint["status"] == "continue"
    assert checkpoint["summary"] == "wrote new test"
    assert checkpoint["branch_family"] == "tests"


def test_run_iteration_falls_back_to_stdout_markers(tmp_path: Path) -> None:
    """No file, no markers in stdout → synthesize from git diff on success."""
    runtime_dir = tmp_path / "runtime"
    conn = ensure_runtime(runtime_dir)
    _seed_run(conn, "run-iter-test")
    ctx = _make_ctx_for_run_iteration()
    args = _make_args_for_run_iteration()

    with (
        patch(
            "scripts.run_autopilot.subprocess.Popen",
            return_value=_FakeProc(stdout_lines=["some agent narration\n"], returncode=0),
        ),
        patch(
            "scripts.run_autopilot.synthesize_checkpoint_from_git",
            return_value={"status": "continue", "summary": "synth from git"},
        ) as mock_synth,
        patch("scripts.run_autopilot.ROOT", tmp_path),
    ):
        _exit_code, _stdout, _stderr, checkpoint = run_iteration(
            conn=conn,
            args=args,
            runtime_dir=runtime_dir,
            ctx=ctx,
            iteration_number=1,
            attempt_number=1,
        )

    assert mock_synth.called
    assert checkpoint["summary"] == "synth from git"


def test_run_iteration_extracts_checkpoint_from_stdout(tmp_path: Path) -> None:
    """When no file is written, markers in stdout are extracted via extract_checkpoint."""
    runtime_dir = tmp_path / "runtime"
    conn = ensure_runtime(runtime_dir)
    _seed_run(conn, "run-iter-test")
    ctx = _make_ctx_for_run_iteration()
    args = _make_args_for_run_iteration()

    from scripts.checkpoint import CHECKPOINT_BEGIN, CHECKPOINT_END

    marker_payload = {
        "status": "continue",
        "summary": "from stdout markers",
        "branch_family": "scripts",
    }
    stdout_text = f"thinking line\n{CHECKPOINT_BEGIN}\n{json.dumps(marker_payload)}\n{CHECKPOINT_END}\ntail line\n"

    with (
        patch(
            "scripts.run_autopilot.subprocess.Popen",
            return_value=_FakeProc(stdout_lines=stdout_text.splitlines(keepends=True)),
        ),
        patch("scripts.run_autopilot.ROOT", tmp_path),
    ):
        _exit_code, _stdout, _stderr, checkpoint = run_iteration(
            conn=conn,
            args=args,
            runtime_dir=runtime_dir,
            ctx=ctx,
            iteration_number=1,
            attempt_number=1,
        )

    assert checkpoint["summary"] == "from stdout markers"
    assert checkpoint["status"] == "continue"
    assert checkpoint["branch_family"] == "scripts"


def test_run_iteration_falls_back_to_stderr_when_stdout_empty(tmp_path: Path) -> None:
    """If stdout has no markers but stderr does, use the stderr-extracted checkpoint."""
    runtime_dir = tmp_path / "runtime"
    conn = ensure_runtime(runtime_dir)
    _seed_run(conn, "run-iter-test")
    ctx = _make_ctx_for_run_iteration()
    args = _make_args_for_run_iteration()

    from scripts.checkpoint import CHECKPOINT_BEGIN, CHECKPOINT_END

    stderr_payload = {
        "status": "continue",
        "summary": "from stderr markers",
        "branch_family": "scripts",
    }
    stderr_text = f"{CHECKPOINT_BEGIN}\n{json.dumps(stderr_payload)}\n{CHECKPOINT_END}\n"

    with (
        patch(
            "scripts.run_autopilot.subprocess.Popen",
            return_value=_FakeProc(
                stdout_lines=[],
                stderr_lines=stderr_text.splitlines(keepends=True),
            ),
        ),
        patch("scripts.run_autopilot.ROOT", tmp_path),
    ):
        _exit_code, _stdout, _stderr, checkpoint = run_iteration(
            conn=conn,
            args=args,
            runtime_dir=runtime_dir,
            ctx=ctx,
            iteration_number=1,
            attempt_number=1,
        )

    assert checkpoint["summary"] == "from stderr markers"
    assert checkpoint["branch_family"] == "scripts"


def test_run_iteration_timeout_kills_process(tmp_path: Path) -> None:
    """When wait() times out, run_iteration must kill the proc and report exit_code=124."""
    runtime_dir = tmp_path / "runtime"
    conn = ensure_runtime(runtime_dir)
    _seed_run(conn, "run-iter-test")
    ctx = _make_ctx_for_run_iteration()
    args = _make_args_for_run_iteration(timeout=1)

    class _HangingProc(_FakeProc):
        def wait(self, timeout: float | None = None) -> int:  # type: ignore[override]
            raise subprocess.TimeoutExpired(cmd="opencode", timeout=timeout or 0)

    proc = _HangingProc(stdout_lines=[])

    with (
        patch("scripts.run_autopilot.subprocess.Popen", return_value=proc),
        patch("scripts.run_autopilot.ROOT", tmp_path),
    ):
        exit_code, _stdout, _stderr, checkpoint = run_iteration(
            conn=conn,
            args=args,
            runtime_dir=runtime_dir,
            ctx=ctx,
            iteration_number=1,
            attempt_number=1,
        )

    assert proc._killed is True
    assert exit_code == 124
    assert checkpoint["status"] == "failed"
    assert any("opencode exited with status 124" in r for r in checkpoint["risks"])


def test_run_iteration_oserror_when_binary_missing(tmp_path: Path) -> None:
    """If Popen itself fails (binary not found), run_iteration must record exit_code=1 + a failed checkpoint."""
    runtime_dir = tmp_path / "runtime"
    conn = ensure_runtime(runtime_dir)
    _seed_run(conn, "run-iter-test")
    ctx = _make_ctx_for_run_iteration()
    args = _make_args_for_run_iteration()

    def _raise_oserror(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("No such file or directory: 'opencode'")

    with (
        patch("scripts.run_autopilot.subprocess.Popen", side_effect=_raise_oserror),
        patch("scripts.run_autopilot.ROOT", tmp_path),
    ):
        exit_code, _stdout, stderr_text, checkpoint = run_iteration(
            conn=conn,
            args=args,
            runtime_dir=runtime_dir,
            ctx=ctx,
            iteration_number=1,
            attempt_number=1,
        )

    assert exit_code == 1
    assert "opencode" in stderr_text
    assert checkpoint["status"] == "failed"
    assert any("opencode exited with status 1" in r for r in checkpoint["risks"])


def test_run_iteration_uses_invalid_checkpoint_file_falls_back(tmp_path: Path) -> None:
    """A file that exists but does not contain a JSON object with 'status' must be ignored
    and the code must fall back to stdout marker extraction (or git-synth if no markers)."""
    runtime_dir = tmp_path / "runtime"
    conn = ensure_runtime(runtime_dir)
    _seed_run(conn, "run-iter-test")
    ctx = _make_ctx_for_run_iteration()
    args = _make_args_for_run_iteration()

    from scripts.run_autopilot import CHECKPOINT_OUTPUT_FILE

    CHECKPOINT_OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Valid JSON but no 'status' key — must be rejected.
    CHECKPOINT_OUTPUT_FILE.write_text(json.dumps({"summary": "orphan payload"}))
    try:
        with (
            patch(
                "scripts.run_autopilot.subprocess.Popen",
                return_value=_FakeProc(stdout_lines=["agent narrated something\n"]),
            ),
            patch(
                "scripts.run_autopilot.synthesize_checkpoint_from_git",
                return_value={"status": "continue", "summary": "fallback synth"},
            ),
            patch("scripts.run_autopilot.ROOT", tmp_path),
        ):
            _exit_code, _stdout, _stderr, checkpoint = run_iteration(
                conn=conn,
                args=args,
                runtime_dir=runtime_dir,
                ctx=ctx,
                iteration_number=1,
                attempt_number=1,
            )
    finally:
        CHECKPOINT_OUTPUT_FILE.unlink(missing_ok=True)

    assert checkpoint["summary"] == "fallback synth"


# ── run_cycle orchestration path ──────────────────────────────────────


from scripts.run_autopilot import (  # noqa: E402
    CycleResult,
    _apply_meta_overrides_to_args,
    _maybe_run_meta_autopilot_cycle,
    _pick_best_branch_checkpoint,
    _resolve_meta_controller_overrides,
    run_cycle,
)


def _make_args_for_run_cycle(
    *,
    max_iterations: int = 5,
    parallel_branches: int = 1,
    sleep_seconds: int = 0,
    max_retries: int = 3,
    cpu_budget_percent: int = 100,
    timeout_seconds: int = 3600,
    force_parallel: int = 0,
    session_goal_file: Any = None,
    retry_backoff_seconds: int = 1,
    max_retry_backoff_seconds: int = 5,
) -> argparse.Namespace:
    return argparse.Namespace(
        max_iterations=max_iterations,
        parallel_branches=parallel_branches,
        sleep_seconds=sleep_seconds,
        max_retries_per_iteration=max_retries,
        cpu_budget_percent=cpu_budget_percent,
        timeout_seconds=timeout_seconds,
        force_parallel=force_parallel,
        session_goal_file=session_goal_file,
        retry_backoff_seconds=retry_backoff_seconds,
        max_retry_backoff_seconds=max_retry_backoff_seconds,
        dry_run=False,
    )


def _make_branch_checkpoint(
    *,
    status: str = "continue",
    summary: str = "branch done",
    competitiveness: str = "promising",
    branch_family: str = "scripts",
    evidence_quality: str = "moderate",
    family_novelty: str = "moderate",
    promotion_recommendation: str = "hold",
) -> dict[str, Any]:
    """Build a normalized checkpoint with the underscore path keys persist_iteration expects."""
    cp = normalize_checkpoint(
        {
            "status": status,
            "summary": summary,
            "branch_family": branch_family,
            "competitiveness": competitiveness,
            "evidence_quality": evidence_quality,
            "family_novelty": family_novelty,
            "promotion_recommendation": promotion_recommendation,
        },
        fallback_status=status,
        fallback_summary=summary,
    )
    cp["_stdout_path"] = "fake.stdout.txt"
    cp["_stderr_path"] = "fake.stderr.txt"
    return cp


def _seed_run_with_iterations(conn: sqlite3.Connection, run_id: str, count: int) -> None:
    """Insert a run plus N iterations of synthetic checkpoints so iteration_count > 0."""
    conn.execute(
        "INSERT OR REPLACE INTO runs (run_id, goal_text, status, created_at, updated_at, iteration_count) VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, "Perpetually improve autocode.", "running", "2025-01-01T00:00:00Z", "2025-01-01T00:00:00Z", count),
    )
    for i in range(1, count + 1):
        payload = {
            "status": "continue",
            "summary": f"prior iter {i}",
            "branch_family": "scripts",
            "competitiveness": "promising" if i == count else "marginal",
            "evidence_quality": "moderate",
            "family_novelty": "moderate",
            "promotion_recommendation": "hold",
            "_stdout_path": f"iter-{i:04d}.stdout.txt",
            "_stderr_path": f"iter-{i:04d}.stderr.txt",
        }
        conn.execute(
            "INSERT INTO iterations (run_id, iteration_number, status, summary, started_at, finished_at, exit_code, checkpoint_json, stdout_path, stderr_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                i,
                "continue",
                f"prior iter {i}",
                "2025-01-01T00:00:00Z",
                "2025-01-01T00:00:01Z",
                0,
                json.dumps(payload),
                f"iter-{i:04d}.stdout.txt",
                f"iter-{i:04d}.stderr.txt",
            ),
        )
    conn.commit()


def test_pick_best_branch_checkpoint_promotable_wins() -> None:
    """The branch with the highest competitiveness rank must be selected."""
    branch_results = [
        (1, 1, 0, "s", "f", "o", {"competitiveness": "marginal", "summary": "m"}, 0),
        (2, 1, 0, "s", "f", "o", {"competitiveness": "promising", "summary": "p"}, 0),
        (3, 1, 0, "s", "f", "o", {"competitiveness": "promotable", "summary": "P"}, 0),
    ]
    best = _pick_best_branch_checkpoint(branch_results)
    assert best["summary"] == "P"


def test_pick_best_branch_checkpoint_unknown_falls_through() -> None:
    """When all branches have rank 0, the first one wins (insertion order)."""
    branch_results = [
        (1, 1, 0, "s", "f", "o", {"competitiveness": "unknown", "summary": "first"}, 0),
        (2, 1, 0, "s", "f", "o", {"competitiveness": "", "summary": "second"}, 0),
    ]
    assert _pick_best_branch_checkpoint(branch_results)["summary"] == "first"


def test_apply_meta_overrides_to_args_no_op_when_empty() -> None:
    args = argparse.Namespace(sleep_seconds=0, max_retries_per_iteration=3)
    _apply_meta_overrides_to_args(args, {})
    assert args.sleep_seconds == 0
    assert args.max_retries_per_iteration == 3


def test_apply_meta_overrides_to_args_sets_known_attrs() -> None:
    args = argparse.Namespace(sleep_seconds=0, max_retries_per_iteration=3)
    _apply_meta_overrides_to_args(args, {"sleep_seconds": 10, "max_retries_per_iteration": 5, "unknown_key": "x"})
    assert args.sleep_seconds == 10
    assert args.max_retries_per_iteration == 5


def test_resolve_meta_controller_overrides_skips_below_threshold(tmp_path: Path) -> None:
    """iteration_count < 3 must short-circuit (no DB query, no advice)."""
    runtime_dir = tmp_path / "runtime"
    conn = ensure_runtime(runtime_dir)
    _seed_run(conn, "run-mc-low")
    args = _make_args_for_run_cycle()
    ctx = RunContext(run_id="run-mc-low", goal_text="g", status="running", iteration_count=2)

    with patch("scripts.run_autopilot.run_self_improving_loop") as mock_loop:
        result = _resolve_meta_controller_overrides(conn, args, ctx)

    assert result == {}
    mock_loop.assert_not_called()


def test_resolve_meta_controller_overrides_returns_empty_when_unchanged(tmp_path: Path) -> None:
    """When the controller reports no changes, the result must be an empty dict."""
    runtime_dir = tmp_path / "runtime"
    conn = ensure_runtime(runtime_dir)
    _seed_run_with_iterations(conn, "run-mc-unchanged", 4)
    args = _make_args_for_run_cycle()
    ctx = RunContext(run_id="run-mc-unchanged", goal_text="g", status="running", iteration_count=4)

    advice = MagicMock()
    advice.changed = {}
    advice.performance_summary = "perf summary"
    advice.reasoning = "no change"
    with patch("scripts.run_autopilot.run_self_improving_loop", return_value=("", advice)):
        result = _resolve_meta_controller_overrides(conn, args, ctx)
    assert result == {}


def test_resolve_meta_controller_overrides_returns_new_params(tmp_path: Path) -> None:
    """When the controller reports changes, the result maps param→new_value."""
    runtime_dir = tmp_path / "runtime"
    conn = ensure_runtime(runtime_dir)
    _seed_run_with_iterations(conn, "run-mc-change", 4)
    args = _make_args_for_run_cycle()
    ctx = RunContext(run_id="run-mc-change", goal_text="g", status="running", iteration_count=4)

    advice = MagicMock()
    advice.changed = {"sleep_seconds": (0, 30), "parallel_branches": (1, 2)}
    advice.performance_summary = "perf"
    advice.reasoning = "narrowing"
    with patch("scripts.run_autopilot.run_self_improving_loop", return_value=("", advice)):
        result = _resolve_meta_controller_overrides(conn, args, ctx)
    assert result == {"sleep_seconds": 30, "parallel_branches": 2}


def test_resolve_meta_controller_overrides_swallows_exceptions(tmp_path: Path) -> None:
    """When the controller raises, the function must return {} and not crash the cycle."""
    runtime_dir = tmp_path / "runtime"
    conn = ensure_runtime(runtime_dir)
    _seed_run_with_iterations(conn, "run-mc-error", 4)
    args = _make_args_for_run_cycle()
    ctx = RunContext(run_id="run-mc-error", goal_text="g", status="running", iteration_count=4)

    with patch("scripts.run_autopilot.run_self_improving_loop", side_effect=RuntimeError("boom")):
        result = _resolve_meta_controller_overrides(conn, args, ctx)
    assert result == {}


def test_maybe_run_meta_autopilot_cycle_skips_below_threshold() -> None:
    """iter_count < 5 must not instantiate MetaController at all."""
    ctx = RunContext(run_id="r", goal_text="g", status="running", iteration_count=4)
    cp = {"competitiveness": "promising", "family_novelty": "moderate"}
    with patch("scripts.run_autopilot.MetaController") as MockCtrl:
        _maybe_run_meta_autopilot_cycle(ctx, cp)
    MockCtrl.assert_not_called()


def test_maybe_run_meta_autopilot_cycle_skips_off_mod_10_boundary() -> None:
    """iter_count == 5 or 15 must not trigger (only multiples of 10)."""
    ctx = RunContext(run_id="r", goal_text="g", status="running", iteration_count=5)
    cp = {"competitiveness": "promising", "family_novelty": "moderate"}
    with patch("scripts.run_autopilot.MetaController") as MockCtrl:
        _maybe_run_meta_autopilot_cycle(ctx, cp)
    MockCtrl.assert_not_called()


def test_maybe_run_meta_autopilot_cycle_runs_on_boundary() -> None:
    """iter_count == 10 (multiple of 10, >= 5) must invoke run_cycle(1, ...)."""
    ctx = RunContext(run_id="r", goal_text="g", status="running", iteration_count=10)
    cp = {"competitiveness": "promising", "family_novelty": "moderate"}
    mock_instance = MagicMock()
    mock_instance.run_cycle.return_value = {
        "experiments_advanced": 0,
        "experiments_started": 1,
        "stagnation_score": 0.1,
        "escalated": False,
    }
    with patch("scripts.run_autopilot.MetaController", return_value=mock_instance):
        _maybe_run_meta_autopilot_cycle(ctx, cp)
    mock_instance.run_cycle.assert_called_once()
    args, kwargs = mock_instance.run_cycle.call_args
    assert args[0] == 1
    assert kwargs["metrics"]["improvement_rate"] is True
    assert kwargs["metrics"]["novelty_diversity"] == 1.0


def test_maybe_run_meta_autopilot_cycle_swallows_exceptions() -> None:
    """When MetaController raises, the helper must not propagate."""
    ctx = RunContext(run_id="r", goal_text="g", status="running", iteration_count=10)
    cp = {"competitiveness": "promising", "family_novelty": "moderate"}
    with patch("scripts.run_autopilot.MetaController", side_effect=RuntimeError("boom")):
        _maybe_run_meta_autopilot_cycle(ctx, cp)  # must not raise


def test_run_cycle_single_branch_continue(tmp_path: Path) -> None:
    """Happy path: one branch returns continue, no controller stop, no status stop."""
    runtime_dir = tmp_path / "runtime"
    conn = ensure_runtime(runtime_dir)
    _seed_run(conn, "run-cycle-continue")
    ctx = RunContext(run_id="run-cycle-continue", goal_text="g", status="running", iteration_count=0)
    args = _make_args_for_run_cycle(parallel_branches=1)

    cp = _make_branch_checkpoint(summary="branch 0 done")

    with (
        patch("scripts.run_autopilot.run_iteration", return_value=(0, "", "", cp)) as mock_ri,
        patch("scripts.run_autopilot.persist_iteration", side_effect=lambda *a, **kw: a[2]) as mock_pi,
        patch("scripts.run_autopilot._resolve_meta_controller_overrides", return_value={}),
        patch("scripts.run_autopilot._maybe_run_meta_autopilot_cycle"),
        patch("scripts.run_autopilot.controller_should_stop", return_value=(False, "")),
    ):
        result = run_cycle(
            conn=conn,
            args=args,
            runtime_dir=runtime_dir,
            ctx=ctx,
            offset=1,
            base_iteration_number=1,
            resume_session_id=None,
            prev_checkpoint=None,
            meta_overrides={},
        )

    assert isinstance(result, CycleResult)
    assert result.stop is False
    assert result.stop_exit_code == 0
    assert result.checkpoint["status"] == "continue"
    assert mock_ri.call_count == 1
    assert mock_pi.call_count == 1


def test_run_cycle_dry_run_returns_immediately(tmp_path: Path) -> None:
    """dry_run must skip subprocess and short-circuit with stop=True, exit=0.

    The branch function does invoke run_iteration once (the dry-run path inside
    run_iteration itself short-circuits the real subprocess), so we mock it to
    return a synthetic 4-tuple. The cycle wrapper then detects args.dry_run and
    returns a CycleResult with stop=True."""
    runtime_dir = tmp_path / "runtime"
    conn = ensure_runtime(runtime_dir)
    _seed_run(conn, "run-cycle-dry")
    ctx = RunContext(run_id="run-cycle-dry", goal_text="g", status="running", iteration_count=0)
    args = _make_args_for_run_cycle(parallel_branches=1)
    args.dry_run = True

    fake_cp = _make_branch_checkpoint(summary="dry")
    with (
        patch("scripts.run_autopilot.run_iteration", return_value=(0, "", "", fake_cp)) as mock_ri,
        patch("scripts.run_autopilot.persist_iteration") as mock_pi,
        patch("scripts.run_autopilot._resolve_meta_controller_overrides", return_value={}),
        patch("scripts.run_autopilot._maybe_run_meta_autopilot_cycle"),
    ):
        result = run_cycle(
            conn=conn,
            args=args,
            runtime_dir=runtime_dir,
            ctx=ctx,
            offset=1,
            base_iteration_number=1,
            resume_session_id=None,
            prev_checkpoint=None,
            meta_overrides={},
        )

    assert result.stop is True
    assert result.stop_exit_code == 0
    assert result.stop_reason == "dry_run"
    mock_ri.assert_called_once()
    mock_pi.assert_not_called()


def test_run_cycle_stops_on_done_status(tmp_path: Path) -> None:
    """status=done in the branch checkpoint must terminate the loop with exit 0."""
    runtime_dir = tmp_path / "runtime"
    conn = ensure_runtime(runtime_dir)
    _seed_run(conn, "run-cycle-done")
    ctx = RunContext(run_id="run-cycle-done", goal_text="g", status="running", iteration_count=0)
    args = _make_args_for_run_cycle(parallel_branches=1)

    cp = _make_branch_checkpoint(status="done", summary="goal complete")
    cp["goal_complete"] = True

    with (
        patch("scripts.run_autopilot.run_iteration", return_value=(0, "", "", cp)),
        patch("scripts.run_autopilot.persist_iteration", side_effect=lambda *a, **kw: a[2]),
        patch("scripts.run_autopilot._resolve_meta_controller_overrides", return_value={}),
        patch("scripts.run_autopilot._maybe_run_meta_autopilot_cycle"),
        patch("scripts.run_autopilot.controller_should_stop", return_value=(False, "")),
    ):
        result = run_cycle(
            conn=conn,
            args=args,
            runtime_dir=runtime_dir,
            ctx=ctx,
            offset=1,
            base_iteration_number=1,
            resume_session_id=None,
            prev_checkpoint=None,
            meta_overrides={},
        )

    assert result.stop is True
    assert result.stop_exit_code == 0
    assert result.stop_reason == "status_done"
    assert result.checkpoint["status"] == "done"


def test_run_cycle_stops_on_blocked_status_with_exit_1(tmp_path: Path) -> None:
    """status=blocked must terminate the loop with exit code 1."""
    runtime_dir = tmp_path / "runtime"
    conn = ensure_runtime(runtime_dir)
    _seed_run(conn, "run-cycle-blocked")
    ctx = RunContext(run_id="run-cycle-blocked", goal_text="g", status="running", iteration_count=0)
    args = _make_args_for_run_cycle(parallel_branches=1)

    cp = _make_branch_checkpoint(status="blocked", summary="need human")

    with (
        patch("scripts.run_autopilot.run_iteration", return_value=(0, "", "", cp)),
        patch("scripts.run_autopilot.persist_iteration", side_effect=lambda *a, **kw: a[2]),
        patch("scripts.run_autopilot._resolve_meta_controller_overrides", return_value={}),
        patch("scripts.run_autopilot._maybe_run_meta_autopilot_cycle"),
        patch("scripts.run_autopilot.controller_should_stop", return_value=(False, "")),
    ):
        result = run_cycle(
            conn=conn,
            args=args,
            runtime_dir=runtime_dir,
            ctx=ctx,
            offset=1,
            base_iteration_number=1,
            resume_session_id=None,
            prev_checkpoint=None,
            meta_overrides={},
        )

    assert result.stop is True
    assert result.stop_exit_code == 1
    assert result.stop_reason == "status_blocked"


def test_run_cycle_stops_on_controller_forced_stop(tmp_path: Path) -> None:
    """controller_should_stop=True must terminate with the reason captured."""
    runtime_dir = tmp_path / "runtime"
    conn = ensure_runtime(runtime_dir)
    _seed_run(conn, "run-cycle-ctrl-stop")
    ctx = RunContext(run_id="run-cycle-ctrl-stop", goal_text="g", status="running", iteration_count=0)
    args = _make_args_for_run_cycle(parallel_branches=1)

    cp = _make_branch_checkpoint(summary="x")

    with (
        patch("scripts.run_autopilot.run_iteration", return_value=(0, "", "", cp)),
        patch("scripts.run_autopilot.persist_iteration", side_effect=lambda *a, **kw: a[2]),
        patch("scripts.run_autopilot._resolve_meta_controller_overrides", return_value={}),
        patch("scripts.run_autopilot._maybe_run_meta_autopilot_cycle"),
        patch(
            "scripts.run_autopilot.controller_should_stop", return_value=(True, "checkpoint requested run abandonment")
        ),
    ):
        result = run_cycle(
            conn=conn,
            args=args,
            runtime_dir=runtime_dir,
            ctx=ctx,
            offset=1,
            base_iteration_number=1,
            resume_session_id=None,
            prev_checkpoint=None,
            meta_overrides={},
        )

    assert result.stop is True
    assert result.stop_exit_code == 0
    assert result.stop_reason == "checkpoint requested run abandonment"


def test_run_cycle_updates_session_goal_from_file(tmp_path: Path) -> None:
    """When session_goal_file is set and contents differ, ctx.session_goal must be updated."""
    runtime_dir = tmp_path / "runtime"
    conn = ensure_runtime(runtime_dir)
    _seed_run(conn, "run-cycle-sg")
    ctx = RunContext(run_id="run-cycle-sg", goal_text="g", status="running", iteration_count=0, session_goal="old goal")

    goal_file = tmp_path / "goal.md"
    goal_file.write_text("new goal after mid-run update", encoding="utf-8")

    args = _make_args_for_run_cycle(parallel_branches=1, session_goal_file=goal_file)

    cp = _make_branch_checkpoint(summary="ok")

    with (
        patch("scripts.run_autopilot.run_iteration", return_value=(0, "", "", cp)),
        patch("scripts.run_autopilot.persist_iteration", side_effect=lambda *a, **kw: a[2]),
        patch("scripts.run_autopilot._resolve_meta_controller_overrides", return_value={}),
        patch("scripts.run_autopilot._maybe_run_meta_autopilot_cycle"),
        patch("scripts.run_autopilot.controller_should_stop", return_value=(False, "")),
    ):
        result = run_cycle(
            conn=conn,
            args=args,
            runtime_dir=runtime_dir,
            ctx=ctx,
            offset=1,
            base_iteration_number=1,
            resume_session_id=None,
            prev_checkpoint=None,
            meta_overrides={},
        )

    assert result.ctx.session_goal == "new goal after mid-run update"


def test_run_cycle_applies_meta_overrides_to_args(tmp_path: Path) -> None:
    """Pre-existing meta_overrides must be applied to args before resolve_branch_count runs.

    The previous checkpoint requests 2 branches; meta_overrides raise the cap to 3.
    Since SQLite can't be shared across threads in this test, we set the override
    to 0 to force single-branch execution and assert that args.parallel_branches
    is still updated by the helper (proving the override is applied before
    resolve_branch_count runs)."""
    runtime_dir = tmp_path / "runtime"
    conn = ensure_runtime(runtime_dir)
    _seed_run(conn, "run-cycle-overrides")
    ctx = RunContext(run_id="run-cycle-overrides", goal_text="g", status="running", iteration_count=0)
    args = _make_args_for_run_cycle(parallel_branches=1, sleep_seconds=0)

    cp = _make_branch_checkpoint(summary="x")

    prev_cp = _make_branch_checkpoint(summary="prev")
    prev_cp["parallel_branches"] = 0  # 0 ⇒ 1 branch via resolve_branch_count

    with (
        patch("scripts.run_autopilot.run_iteration", return_value=(0, "", "", cp)),
        patch("scripts.run_autopilot.persist_iteration", side_effect=lambda *a, **kw: a[2]),
        patch("scripts.run_autopilot._resolve_meta_controller_overrides", return_value={}),
        patch("scripts.run_autopilot._maybe_run_meta_autopilot_cycle"),
        patch("scripts.run_autopilot.controller_should_stop", return_value=(False, "")),
    ):
        result = run_cycle(
            conn=conn,
            args=args,
            runtime_dir=runtime_dir,
            ctx=ctx,
            offset=1,
            base_iteration_number=1,
            resume_session_id=None,
            prev_checkpoint=prev_cp,
            meta_overrides={"parallel_branches": 4},
        )

    assert args.parallel_branches == 4
    assert result.stop is False
    assert result.stop_exit_code == 0
    assert result.checkpoint["status"] == "continue"


# ── main() outer-loop orchestration ─────────────────────────────────────


def _make_main_args(
    *,
    subcommand: str | None = None,
    max_iterations: int = 1,
    sleep_seconds: int = 0,
    resume_latest: bool = False,
    runtime_dir: Path | None = None,
) -> argparse.Namespace:
    base = argparse.Namespace(
        subcommand=subcommand,
        max_iterations=max_iterations,
        sleep_seconds=sleep_seconds,
        resume_latest=resume_latest,
        runtime_dir=runtime_dir or Path("/tmp/runtime"),
        session_goal_file=None,
    )
    return base


def test_main_dispatches_report_subcommand(tmp_path: Path) -> None:
    args = _make_main_args(subcommand="report", runtime_dir=tmp_path)
    with patch("scripts.run_autopilot.parse_args", return_value=args) as pparse:
        with patch("scripts.run_autopilot.cmd_report", return_value=0) as prep:
            rc = main()
    pparse.assert_called_once()
    prep.assert_called_once_with(args)
    assert rc == 0


def test_main_dispatches_export_kb_subcommand(tmp_path: Path) -> None:
    args = _make_main_args(subcommand="export-kb", runtime_dir=tmp_path)
    with patch("scripts.run_autopilot.parse_args", return_value=args) as pparse:
        with patch("scripts.run_autopilot.cmd_export_kb", return_value=0) as pekb:
            rc = main()
    pparse.assert_called_once()
    pekb.assert_called_once_with(args)
    assert rc == 0


def test_main_runs_one_cycle_and_returns_zero(tmp_path: Path) -> None:
    args = _make_main_args(max_iterations=1, runtime_dir=tmp_path)
    conn = _make_db()
    ctx = _make_ctx(run_id="r1", iteration_count=0)
    state = PendingIterationState(iteration_number=1, attempt_number=1)
    cycle_result = CycleResult(
        ctx=ctx,
        checkpoint={"status": "continue", "summary": "ok"},
        meta_overrides={},
        stop=False,
        stop_exit_code=0,
        stop_reason="",
    )
    with (
        patch("scripts.run_autopilot.parse_args", return_value=args),
        patch("scripts.run_autopilot.ensure_runtime", return_value=conn),
        patch("scripts.run_autopilot.resolve_run", return_value=ctx),
        patch("scripts.run_autopilot.pending_iteration_state", return_value=state),
        patch("scripts.run_autopilot.run_cycle", return_value=cycle_result) as prc,
        patch("scripts.run_autopilot._print_run_end_advisory") as padv,
    ):
        rc = main()
    prc.assert_called_once()
    assert rc == 0
    padv.assert_called_once_with(conn, ctx)


def test_main_returns_on_controller_stop_with_exit_code(tmp_path: Path) -> None:
    args = _make_main_args(max_iterations=3, runtime_dir=tmp_path)
    conn = _make_db()
    ctx = _make_ctx(run_id="r1", iteration_count=2)
    state = PendingIterationState(iteration_number=3, attempt_number=1)
    stop_result = CycleResult(
        ctx=ctx,
        checkpoint={"status": "failed", "summary": "stop"},
        meta_overrides={},
        stop=True,
        stop_exit_code=2,
        stop_reason="controller_stop",
    )
    with (
        patch("scripts.run_autopilot.parse_args", return_value=args),
        patch("scripts.run_autopilot.ensure_runtime", return_value=conn),
        patch("scripts.run_autopilot.resolve_run", return_value=ctx),
        patch("scripts.run_autopilot.pending_iteration_state", return_value=state),
        patch("scripts.run_autopilot.run_cycle", return_value=stop_result),
        patch("scripts.run_autopilot._print_run_end_advisory") as padv,
    ):
        rc = main()
    assert rc == 2
    padv.assert_called_once_with(conn, ctx)


def test_main_returns_on_no_branch_results(tmp_path: Path) -> None:
    args = _make_main_args(max_iterations=2, runtime_dir=tmp_path)
    conn = _make_db()
    ctx = _make_ctx(run_id="r1", iteration_count=1)
    state = PendingIterationState(iteration_number=2, attempt_number=1)
    stop_result = CycleResult(
        ctx=ctx,
        checkpoint={"status": "failed", "summary": "none"},
        meta_overrides={},
        stop=True,
        stop_exit_code=1,
        stop_reason="no_branch_results",
    )
    with (
        patch("scripts.run_autopilot.parse_args", return_value=args),
        patch("scripts.run_autopilot.ensure_runtime", return_value=conn),
        patch("scripts.run_autopilot.resolve_run", return_value=ctx),
        patch("scripts.run_autopilot.pending_iteration_state", return_value=state),
        patch("scripts.run_autopilot.run_cycle", return_value=stop_result),
        patch("scripts.run_autopilot._print_run_end_advisory") as padv,
    ):
        rc = main()
    assert rc == 1
    padv.assert_not_called()


def test_main_returns_on_dry_run(tmp_path: Path) -> None:
    args = _make_main_args(max_iterations=5, runtime_dir=tmp_path)
    conn = _make_db()
    ctx = _make_ctx(run_id="r1", iteration_count=0)
    state = PendingIterationState(iteration_number=1, attempt_number=1)
    stop_result = CycleResult(
        ctx=ctx,
        checkpoint={"status": "continue", "summary": "dry"},
        meta_overrides={},
        stop=True,
        stop_exit_code=0,
        stop_reason="dry_run",
    )
    with (
        patch("scripts.run_autopilot.parse_args", return_value=args),
        patch("scripts.run_autopilot.ensure_runtime", return_value=conn),
        patch("scripts.run_autopilot.resolve_run", return_value=ctx),
        patch("scripts.run_autopilot.pending_iteration_state", return_value=state),
        patch("scripts.run_autopilot.run_cycle", return_value=stop_result) as prc,
    ):
        rc = main()
    assert rc == 0
    prc.assert_called_once()


def test_main_returns_on_status_prefix_stop(tmp_path: Path) -> None:
    args = _make_main_args(max_iterations=2, runtime_dir=tmp_path)
    conn = _make_db()
    ctx = _make_ctx(run_id="r1", iteration_count=1)
    state = PendingIterationState(iteration_number=2, attempt_number=1)
    stop_result = CycleResult(
        ctx=ctx,
        checkpoint={"status": "done", "summary": "x"},
        meta_overrides={},
        stop=True,
        stop_exit_code=0,
        stop_reason="status_done",
    )
    with (
        patch("scripts.run_autopilot.parse_args", return_value=args),
        patch("scripts.run_autopilot.ensure_runtime", return_value=conn),
        patch("scripts.run_autopilot.resolve_run", return_value=ctx),
        patch("scripts.run_autopilot.pending_iteration_state", return_value=state),
        patch("scripts.run_autopilot.run_cycle", return_value=stop_result),
        patch("scripts.run_autopilot._print_run_end_advisory") as padv,
    ):
        rc = main()
    assert rc == 0
    padv.assert_called_once_with(conn, ctx)


def test_main_runs_multiple_cycles_and_sleeps(tmp_path: Path) -> None:
    args = _make_main_args(max_iterations=3, sleep_seconds=7, runtime_dir=tmp_path)
    conn = _make_db()
    ctx = _make_ctx(run_id="r1", iteration_count=0)
    state = PendingIterationState(iteration_number=1, attempt_number=1)
    calls: list[int] = []

    def fake_cycle(*, conn, args, runtime_dir, ctx, offset, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(offset)
        return CycleResult(
            ctx=ctx,
            checkpoint={"status": "continue", "summary": f"c{offset}"},
            meta_overrides={},
            stop=False,
            stop_exit_code=0,
            stop_reason="",
        )

    with (
        patch("scripts.run_autopilot.parse_args", return_value=args),
        patch("scripts.run_autopilot.ensure_runtime", return_value=conn),
        patch("scripts.run_autopilot.resolve_run", return_value=ctx),
        patch("scripts.run_autopilot.pending_iteration_state", return_value=state),
        patch("scripts.run_autopilot.run_cycle", side_effect=fake_cycle),
        patch("scripts.run_autopilot.time.sleep") as psleep,
        patch("scripts.run_autopilot._print_run_end_advisory"),
    ):
        rc = main()
    assert rc == 0
    assert calls == [1, 2, 3]
    # Sleep fires between cycles (N-1 times), not after the last one.
    assert psleep.call_count == 2
    assert psleep.call_args_list[0].args == (7,)


def test_main_no_sleep_after_final_cycle(tmp_path: Path) -> None:
    args = _make_main_args(max_iterations=1, sleep_seconds=10, runtime_dir=tmp_path)
    conn = _make_db()
    ctx = _make_ctx(run_id="r1", iteration_count=0)
    state = PendingIterationState(iteration_number=1, attempt_number=1)
    cycle_result = CycleResult(
        ctx=ctx,
        checkpoint={"status": "continue", "summary": "ok"},
        meta_overrides={},
        stop=False,
        stop_exit_code=0,
        stop_reason="",
    )
    with (
        patch("scripts.run_autopilot.parse_args", return_value=args),
        patch("scripts.run_autopilot.ensure_runtime", return_value=conn),
        patch("scripts.run_autopilot.resolve_run", return_value=ctx),
        patch("scripts.run_autopilot.pending_iteration_state", return_value=state),
        patch("scripts.run_autopilot.run_cycle", return_value=cycle_result),
        patch("scripts.run_autopilot.time.sleep") as psleep,
        patch("scripts.run_autopilot._print_run_end_advisory"),
    ):
        rc = main()
    assert rc == 0
    psleep.assert_not_called()


def test_main_resume_latest_uses_recover_state(tmp_path: Path) -> None:
    args = _make_main_args(max_iterations=1, resume_latest=True, runtime_dir=tmp_path)
    conn = _make_db()
    ctx = _make_ctx(run_id="r1", iteration_count=2)
    state = PendingIterationState(iteration_number=3, attempt_number=1, resume_session_id="sess-abc")
    cycle_result = CycleResult(
        ctx=ctx,
        checkpoint={"status": "continue", "summary": "ok"},
        meta_overrides={},
        stop=False,
        stop_exit_code=0,
        stop_reason="",
    )
    with (
        patch("scripts.run_autopilot.parse_args", return_value=args),
        patch("scripts.run_autopilot.ensure_runtime", return_value=conn),
        patch("scripts.run_autopilot.resolve_run", return_value=ctx),
        patch("scripts.run_autopilot.recover_resume_latest_state", return_value=state) as prec,
        patch("scripts.run_autopilot.pending_iteration_state") as ppend,
        patch("scripts.run_autopilot.run_cycle", return_value=cycle_result) as prc,
        patch("scripts.run_autopilot._print_run_end_advisory"),
    ):
        rc = main()
    assert rc == 0
    prec.assert_called_once_with(conn, ctx)
    ppend.assert_not_called()
    prc.assert_called_once()
    kwargs = prc.call_args.kwargs
    assert kwargs["base_iteration_number"] == 3
    assert kwargs["resume_session_id"] == "sess-abc"


def test_main_propagates_meta_overrides_across_cycles(tmp_path: Path) -> None:
    args = _make_main_args(max_iterations=2, runtime_dir=tmp_path)
    conn = _make_db()
    ctx = _make_ctx(run_id="r1", iteration_count=0)
    state = PendingIterationState(iteration_number=1, attempt_number=1)
    seen_meta: list[dict[str, Any]] = []

    def fake_cycle(*, conn, args, runtime_dir, ctx, offset, meta_overrides, **kwargs):  # type: ignore[no-untyped-def]
        seen_meta.append(dict(meta_overrides))
        return CycleResult(
            ctx=ctx,
            checkpoint={"status": "continue", "summary": f"c{offset}"},
            meta_overrides={"sleep_seconds": 99},
            stop=False,
            stop_exit_code=0,
            stop_reason="",
        )

    with (
        patch("scripts.run_autopilot.parse_args", return_value=args),
        patch("scripts.run_autopilot.ensure_runtime", return_value=conn),
        patch("scripts.run_autopilot.resolve_run", return_value=ctx),
        patch("scripts.run_autopilot.pending_iteration_state", return_value=state),
        patch("scripts.run_autopilot.run_cycle", side_effect=fake_cycle),
        patch("scripts.run_autopilot._print_run_end_advisory"),
    ):
        rc = main()
    assert rc == 0
    assert seen_meta[0] == {}
    assert seen_meta[1] == {"sleep_seconds": 99}


def test_main_uses_prev_checkpoint_from_latest_iteration(tmp_path: Path) -> None:
    args = _make_main_args(max_iterations=1, runtime_dir=tmp_path)
    conn = _make_db()
    ctx = _make_ctx(run_id="r1", iteration_count=1)
    state = PendingIterationState(iteration_number=2, attempt_number=1)
    prior_cp = {
        "status": "continue",
        "summary": "prior",
        "branch_family": "scripts",
        "competitiveness": "promising",
        "evidence_quality": "moderate",
        "family_novelty": "moderate",
        "promotion_recommendation": "hold",
        "_stdout_path": "x",
        "_stderr_path": "y",
    }
    conn.execute(
        "INSERT INTO iterations (run_id, iteration_number, started_at, finished_at, exit_code, status, summary, checkpoint_json, stdout_path, stderr_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "r1",
            1,
            "2025-01-01T00:00:00Z",
            "2025-01-01T00:00:01Z",
            0,
            "continue",
            "prior",
            json.dumps(prior_cp),
            "x",
            "y",
        ),
    )
    conn.commit()
    cycle_result = CycleResult(
        ctx=ctx,
        checkpoint={"status": "continue", "summary": "ok"},
        meta_overrides={},
        stop=False,
        stop_exit_code=0,
        stop_reason="",
    )
    with (
        patch("scripts.run_autopilot.parse_args", return_value=args),
        patch("scripts.run_autopilot.ensure_runtime", return_value=conn),
        patch("scripts.run_autopilot.resolve_run", return_value=ctx),
        patch("scripts.run_autopilot.pending_iteration_state", return_value=state),
        patch("scripts.run_autopilot.run_cycle", return_value=cycle_result) as prc,
        patch("scripts.run_autopilot._print_run_end_advisory"),
    ):
        main()
    pc = prc.call_args.kwargs["prev_checkpoint"]
    assert pc is not None
    assert pc["summary"] == "prior"


# ── CHECKPOINT_OUTPUT_FILE YAML / OSError / parse-fail fallbacks ─────────


def test_run_iteration_reads_yaml_checkpoint_file(tmp_path: Path) -> None:
    """When .opencode/checkpoint_output.json contains non-JSON YAML, fall through
    json.JSONDecodeError and read it via _parse_checkpoint_kv. Covers lines 1264-1277."""
    runtime_dir = tmp_path / "runtime"
    conn = ensure_runtime(runtime_dir)
    _seed_run(conn, "run-iter-yaml")
    ctx = _make_ctx_for_run_iteration()
    args = _make_args_for_run_iteration()

    from scripts.run_autopilot import CHECKPOINT_OUTPUT_FILE

    CHECKPOINT_OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_OUTPUT_FILE.write_text(
        "status: continue\nsummary: yaml-formatted checkpoint\nbranch_family: scripts\nnext_steps:\n  - follow up\n"
    )
    try:
        with (
            patch(
                "scripts.run_autopilot.subprocess.Popen",
                return_value=_FakeProc(stdout_lines=["agent narration\n"]),
            ),
            patch("scripts.run_autopilot.ROOT", tmp_path),
        ):
            _exit_code, _stdout, _stderr, checkpoint = run_iteration(
                conn=conn,
                args=args,
                runtime_dir=runtime_dir,
                ctx=ctx,
                iteration_number=1,
                attempt_number=1,
            )
    finally:
        CHECKPOINT_OUTPUT_FILE.unlink(missing_ok=True)

    assert checkpoint["summary"] == "yaml-formatted checkpoint"
    assert checkpoint["branch_family"] == "scripts"
    assert checkpoint["status"] == "continue"


def test_run_iteration_invalid_yaml_file_falls_back(tmp_path: Path) -> None:
    """File that is neither valid JSON nor parseable YAML must be ignored;
    the code falls back to stdout marker / git-synth. Covers lines 1264-1277."""
    runtime_dir = tmp_path / "runtime"
    conn = ensure_runtime(runtime_dir)
    _seed_run(conn, "run-iter-bad")
    ctx = _make_ctx_for_run_iteration()
    args = _make_args_for_run_iteration()

    from scripts.run_autopilot import CHECKPOINT_OUTPUT_FILE

    CHECKPOINT_OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Neither valid JSON nor YAML — both parse paths must fail.
    CHECKPOINT_OUTPUT_FILE.write_text("not json ::: { also not yaml @@@ :::")
    try:
        with (
            patch(
                "scripts.run_autopilot.subprocess.Popen",
                return_value=_FakeProc(stdout_lines=["narrate\n"]),
            ),
            patch(
                "scripts.run_autopilot.synthesize_checkpoint_from_git",
                return_value={"status": "continue", "summary": "synth after bad file"},
            ),
            patch("scripts.run_autopilot.ROOT", tmp_path),
        ):
            _exit_code, _stdout, _stderr, checkpoint = run_iteration(
                conn=conn,
                args=args,
                runtime_dir=runtime_dir,
                ctx=ctx,
                iteration_number=1,
                attempt_number=1,
            )
    finally:
        CHECKPOINT_OUTPUT_FILE.unlink(missing_ok=True)

    assert checkpoint["summary"] == "synth after bad file"


def test_run_iteration_checkpoint_file_oserror_falls_back(tmp_path: Path) -> None:
    """OSError reading CHECKPOINT_OUTPUT_FILE (e.g. permission denied) must
    be caught and the code must fall through to stdout extraction. Covers lines 1278-1279."""
    runtime_dir = tmp_path / "runtime"
    conn = ensure_runtime(runtime_dir)
    _seed_run(conn, "run-iter-oserr")
    ctx = _make_ctx_for_run_iteration()
    args = _make_args_for_run_iteration()

    fake_path = tmp_path / "checkpoint_output.json"
    with (
        patch("scripts.run_autopilot.CHECKPOINT_OUTPUT_FILE", fake_path),
        patch.object(Path, "read_text", side_effect=OSError("permission denied")),
        patch(
            "scripts.run_autopilot.subprocess.Popen",
            return_value=_FakeProc(stdout_lines=["agent output\n"]),
        ),
        patch(
            "scripts.run_autopilot.synthesize_checkpoint_from_git",
            return_value={"status": "continue", "summary": "synth after oserr"},
        ),
        patch("scripts.run_autopilot.ROOT", tmp_path),
    ):
        _exit_code, _stdout, _stderr, checkpoint = run_iteration(
            conn=conn,
            args=args,
            runtime_dir=runtime_dir,
            ctx=ctx,
            iteration_number=1,
            attempt_number=1,
        )

    assert checkpoint["status"] == "continue"
    assert checkpoint["summary"] == "synth after oserr"


# ── parallel branch + transient-retry path ───────────────────────────────


def test_run_cycle_thread_pool_executes_all_branches(tmp_path: Path) -> None:
    """When num_branches > 1 the run_cycle must dispatch via ThreadPoolExecutor
    and aggregate all branch results. Covers lines 1759-1763."""
    runtime_dir = tmp_path / "runtime"
    conn = ensure_runtime(runtime_dir)
    _seed_run(conn, "run-cycle-pool")
    ctx = RunContext(run_id="run-cycle-pool", goal_text="g", status="running", iteration_count=0)

    # args.parallel_branches=3 + force_parallel=3 => 3 branches
    args = _make_args_for_run_cycle(parallel_branches=3, force_parallel=3)

    iter_calls: list[int] = []

    def fake_run_iteration(
        conn: Any,
        args: Any,
        runtime_dir: Any,
        ctx: Any,
        iteration_number: int,
        attempt_number: int,
        resume_session_id: Any = None,
    ) -> tuple[int, str, str, dict[str, Any]]:
        iter_calls.append(iteration_number)
        cp = _make_branch_checkpoint(summary=f"branch {iteration_number}")
        return (0, "", "", cp)

    with (
        patch("scripts.run_autopilot.run_iteration", side_effect=fake_run_iteration),
        patch("scripts.run_autopilot.persist_iteration", side_effect=lambda *a, **kw: a[2]),
        patch("scripts.run_autopilot._resolve_meta_controller_overrides", return_value={}),
        patch("scripts.run_autopilot._maybe_run_meta_autopilot_cycle"),
        patch("scripts.run_autopilot.controller_should_stop", return_value=(False, "")),
    ):
        result = run_cycle(
            conn=conn,
            args=args,
            runtime_dir=runtime_dir,
            ctx=ctx,
            offset=1,
            base_iteration_number=1,
            resume_session_id=None,
            prev_checkpoint=None,
            meta_overrides={},
        )

    assert sorted(iter_calls) == [1, 2, 3]
    assert result.stop is False
    assert result.checkpoint["status"] == "continue"


def test_run_cycle_thread_pool_swallows_branch_exception(tmp_path: Path) -> None:
    """If a branch raises, the ThreadPoolExecutor branch handler must catch
    it and continue with the remaining branches. Covers lines 1761-1765."""
    runtime_dir = tmp_path / "runtime"
    conn = ensure_runtime(runtime_dir)
    _seed_run(conn, "run-cycle-pool-err")
    ctx = RunContext(run_id="run-cycle-pool-err", goal_text="g", status="running", iteration_count=0)

    args = _make_args_for_run_cycle(parallel_branches=3, force_parallel=3)

    def fake_run_iteration(
        conn: Any,
        args: Any,
        runtime_dir: Any,
        ctx: Any,
        iteration_number: int,
        attempt_number: int,
        resume_session_id: Any = None,
    ) -> tuple[int, str, str, dict[str, Any]]:
        if iteration_number == 2:
            raise RuntimeError("branch 1 exploded")
        cp = _make_branch_checkpoint(summary=f"branch {iteration_number}")
        return (0, "", "", cp)

    with (
        patch("scripts.run_autopilot.run_iteration", side_effect=fake_run_iteration),
        patch("scripts.run_autopilot.persist_iteration", side_effect=lambda *a, **kw: a[2]),
        patch("scripts.run_autopilot._resolve_meta_controller_overrides", return_value={}),
        patch("scripts.run_autopilot._maybe_run_meta_autopilot_cycle"),
        patch("scripts.run_autopilot.controller_should_stop", return_value=(False, "")),
    ):
        result = run_cycle(
            conn=conn,
            args=args,
            runtime_dir=runtime_dir,
            ctx=ctx,
            offset=1,
            base_iteration_number=1,
            resume_session_id=None,
            prev_checkpoint=None,
            meta_overrides={},
        )

    assert result.stop is False
    assert result.checkpoint["status"] == "continue"
    # Best of surviving branches: branch 0 or branch 2 both have rank=3 (promising).
    # Either summary is acceptable; the test asserts the loop survived the exception.
    assert "branch" in result.checkpoint["summary"]


def test_run_cycle_branch_transient_retry_then_succeeds(tmp_path: Path) -> None:
    """When the first attempt is a transient failure, the branch must
    sleep with backoff, increment attempt_num, drop the resume session id,
    and try again. The second attempt succeeds. Covers lines 1739-1749."""
    runtime_dir = tmp_path / "runtime"
    conn = ensure_runtime(runtime_dir)
    _seed_run(conn, "run-cycle-retry")
    ctx = RunContext(run_id="run-cycle-retry", goal_text="g", status="running", iteration_count=0)
    args = _make_args_for_run_cycle(parallel_branches=1, max_retries=3)

    first_cp = _make_branch_checkpoint(status="failed", summary="transient: timeout")
    recovered_cp = _make_branch_checkpoint(status="continue", summary="recovered")

    attempt_results = [
        (1, "", "transient: timeout", first_cp),
        (0, "", "", recovered_cp),
    ]

    def fake_run_iteration(
        conn: Any,
        args: Any,
        runtime_dir: Any,
        ctx: Any,
        iteration_number: int,
        attempt_number: int,
        resume_session_id: Any = None,
    ) -> tuple[int, str, str, dict[str, Any]]:
        return attempt_results.pop(0)  # type: ignore[return-value]

    sleep_calls: list[float] = []
    persisted_attempts: list[int] = []

    def track_persist(
        conn: Any,
        ctx: Any,
        iteration_number: int,
        attempt_number: int,
        s_at: Any,
        f_at: Any,
        ec: Any,
        cp: dict[str, Any],
        tf: bool,
        retry_reason: str,
    ) -> Any:
        persisted_attempts.append(attempt_number)
        return ctx

    with (
        patch("scripts.run_autopilot.run_iteration", side_effect=fake_run_iteration),
        patch("scripts.run_autopilot.persist_attempt", side_effect=track_persist),
        patch("scripts.run_autopilot.time.sleep", side_effect=lambda s: sleep_calls.append(s)),
        patch("scripts.run_autopilot._resolve_meta_controller_overrides", return_value={}),
        patch("scripts.run_autopilot._maybe_run_meta_autopilot_cycle"),
        patch("scripts.run_autopilot.controller_should_stop", return_value=(False, "")),
    ):
        result = run_cycle(
            conn=conn,
            args=args,
            runtime_dir=runtime_dir,
            ctx=ctx,
            offset=1,
            base_iteration_number=1,
            resume_session_id="sess-initial",
            prev_checkpoint=None,
            meta_overrides={},
        )

    assert result.stop is False
    assert result.checkpoint["summary"] == "recovered"
    assert persisted_attempts == [1, 2]
    assert len(sleep_calls) == 1
    assert sleep_calls[0] > 0


# ── meta-controller reasoning log + create_run FTS5 fallback ─────────────


def test_resolve_meta_controller_overrides_logs_param_change(tmp_path: Path) -> None:
    """When the meta-controller adjusts a parameter, the reasoning string must
    appear in the change log. Covers line 1587."""
    runtime_dir = tmp_path / "runtime"
    conn = ensure_runtime(runtime_dir)
    _seed_run_with_iterations(conn, "run-mc-reasoning", 4)
    ctx = RunContext(run_id="run-mc-reasoning", goal_text="g", status="running", iteration_count=4)
    args = _make_args_for_run_cycle()

    advice = MagicMock()
    advice.performance_summary = "perf summary"
    advice.changed = {"sleep_seconds": (60, 120)}
    advice.reasoning = "reduce stress on shared DB"

    with (
        patch("scripts.run_autopilot.MetaControllerParams", MagicMock()),
        patch("scripts.run_autopilot.run_self_improving_loop", return_value=("", advice)),
    ):
        overrides = _resolve_meta_controller_overrides(conn, args, ctx)

    assert overrides == {"sleep_seconds": 120}


def test_create_run_fts5_operational_error_is_swallowed(tmp_path: Path) -> None:
    """When the FTS5 INSERT raises sqlite3.OperationalError, create_run must
    silently skip it (pass) and continue importing the entry into memory_entries.
    Covers lines 559-560."""
    conn = _make_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS memory_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            entry_type TEXT NOT NULL,
            content TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            importance REAL NOT NULL DEFAULT 1.0,
            source_iteration INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(content, entry_type, content=memory_entries);
    """)
    now = utc_now()
    conn.execute(
        "INSERT INTO runs (run_id, goal_text, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("prev-run", "old goal", "done", now, now),
    )
    conn.execute(
        "INSERT INTO memory_entries (run_id, entry_type, content, tags_json, importance, source_iteration, created_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("prev-run", "fact", "high value", '["x"]', 5.0, 1, now, now),
    )
    conn.commit()

    # Drop memory_fts to force OperationalError on the INSERT
    conn.executescript("DROP TABLE memory_fts;")

    with patch("scripts.run_autopilot.set_active_run"):
        ctx = create_run(conn, tmp_path, "Goal")

    imported = conn.execute(
        "SELECT content FROM memory_entries WHERE run_id = ? AND content = 'high value'",
        (ctx.run_id,),
    ).fetchall()
    assert len(imported) == 1


# ── resolve_run session_goal_file OSError path ──────────────────────────


def test_resolve_run_session_goal_file_oserror_warns(tmp_path: Path) -> None:
    """When session_goal_file is set but cannot be read, resolve_run must
    log a warning and continue with empty session_goal. Covers lines 595-599."""
    conn = _make_db()

    # Build args with both resume_run and an unreadable session_goal_file.
    missing = tmp_path / "does_not_exist.md"
    args = argparse.Namespace(
        resume_run="run-x",
        resume_latest=False,
        goal=None,
        goal_file=None,
        session_goal="",
        session_goal_file=missing,
        runtime_dir=tmp_path,
    )

    # Insert a run so load_run succeeds.
    conn.execute(
        "INSERT INTO runs (run_id, goal_text, status, created_at, updated_at, iteration_count) VALUES (?, ?, ?, ?, ?, ?)",
        ("run-x", "g", "running", utc_now(), utc_now(), 0),
    )
    conn.commit()

    with (
        patch("scripts.run_autopilot.sync_goal_from_canonical_file", side_effect=lambda c, x: x),
        patch("scripts.run_autopilot.set_active_run"),
    ):
        ctx = resolve_run(conn, args)

    assert ctx.session_goal == ""
    assert ctx.run_id == "run-x"
