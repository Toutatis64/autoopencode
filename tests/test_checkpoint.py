from __future__ import annotations

import json
from unittest.mock import patch

from scripts.checkpoint import (
    CHECKPOINT_BEGIN,
    CHECKPOINT_END,
    STATUS_MAP,
    clean_text,
    checkpoint_text,
    extract_checkpoint,
    normalize_artifacts,
    normalize_checkpoint,
    normalize_string_list,
    strip_ansi,
    unique_preserve,
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
    assert clean_text("  ") == ""


def test_clean_text_multi_line() -> None:
    assert clean_text("  hello\nworld  ") == "hello\nworld"


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
    payload = {"status": "continue", "summary": "Fixed checkpoint extraction"}
    inner = json.dumps(payload, indent=2)
    text = f"some text\n```json\n{inner}\n```\n"
    result, error = extract_checkpoint(text)
    assert error is None
    assert result == payload


def test_extract_checkpoint_fallback_code_fence_no_lang() -> None:
    payload = {"status": "continue", "summary": "work"}
    inner = json.dumps(payload)
    text = f"```\n{inner}\n```"
    result, error = extract_checkpoint(text)
    assert error is None
    assert result == payload


def test_extract_checkpoint_fallback_picks_last_valid() -> None:
    first = {"status": "continue", "summary": "first"}
    last = {"status": "continue", "summary": "last"}
    text = f"```json\n{json.dumps(first)}\n```\nsome text\n```json\n{json.dumps(last)}\n```\n"
    result, error = extract_checkpoint(text)
    assert error is None
    assert result == last


def test_extract_checkpoint_fallback_skips_non_checkpoint_json() -> None:
    text = '```json\n{"name": "foo", "value": 42}\n```'
    result, error = extract_checkpoint(text)
    assert result is None
    assert error is not None and "No checkpoint markers found" in error


def test_extract_checkpoint_fallback_code_fence_no_braces() -> None:
    """Code fence content has no {…} — covers _find_last_code_fenced_json continue when no JSON match."""
    text = "```\nplain text without braces\n```"
    result, error = extract_checkpoint(text)
    assert result is None
    assert error is not None and "No checkpoint markers found" in error


def test_extract_checkpoint_fallback_code_fence_invalid_json() -> None:
    """Code fence with invalid JSON — covers _find_last_code_fenced_json JSONDecodeError path."""
    text = "```json\n{invalid}\n```"
    result, error = extract_checkpoint(text)
    assert result is None
    assert error is not None and "No checkpoint markers found" in error


def test_extract_checkpoint_fallback_markers_still_take_priority() -> None:
    payload_marker = {"status": "done", "summary": "from markers"}
    payload_fallback = {"status": "continue", "summary": "from fallback"}
    text = (
        f"{CHECKPOINT_BEGIN} {json.dumps(payload_marker)} {CHECKPOINT_END}\n"
        f"```json\n{json.dumps(payload_fallback)}\n```"
    )
    result, error = extract_checkpoint(text)
    assert error is None
    assert result == payload_marker


def test_extract_checkpoint_empty_markers_falls_to_fallback() -> None:
    payload = {"status": "continue", "summary": "found via fallback"}
    text = f"{CHECKPOINT_BEGIN}\n{CHECKPOINT_END}\n```json\n{json.dumps(payload)}\n```"
    result, error = extract_checkpoint(text)
    assert error is None
    assert result == payload


def test_extract_checkpoint_markers_whitespace_only_falls_to_fallback() -> None:
    payload = {"status": "continue", "summary": "whitespace markers"}
    text = f"{CHECKPOINT_BEGIN}   \n  \n{CHECKPOINT_END}\n```json\n{json.dumps(payload)}\n```"
    result, error = extract_checkpoint(text)
    assert error is None
    assert result == payload


def test_extract_checkpoint_markers_no_braces_falls_to_fallback() -> None:
    payload = {"status": "continue", "summary": "text between markers"}
    text = (
        f"{CHECKPOINT_BEGIN} just some explanatory text without json {CHECKPOINT_END}\n"
        f"```json\n{json.dumps(payload)}\n```"
    )
    result, error = extract_checkpoint(text)
    assert error is None
    assert result == payload


def test_extract_checkpoint_both_empty_and_no_fallback() -> None:
    text = f"{CHECKPOINT_BEGIN}\n{CHECKPOINT_END}"
    result, error = extract_checkpoint(text)
    assert result is None
    assert error is not None and "No checkpoint markers found" in error


def test_extract_checkpoint_from_stderr_content() -> None:
    payload = {"status": "continue", "summary": "found in stderr"}
    text = f"[INFO] processing\n{CHECKPOINT_BEGIN}\n{json.dumps(payload)}\n{CHECKPOINT_END}\n"
    result, error = extract_checkpoint(text)
    assert error is None
    assert result == payload


def test_extract_checkpoint_stdout_empty_stderr_has_checkpoint() -> None:
    stdout_text = ""
    payload = {"status": "continue", "summary": "stderr only"}
    stderr_text = f"some processing messages\n{CHECKPOINT_BEGIN}\n{json.dumps(payload)}\n{CHECKPOINT_END}\n"
    stdout_result, stdout_error = extract_checkpoint(stdout_text)
    assert stdout_result is None
    assert stdout_error is not None

    stderr_result, stderr_error = extract_checkpoint(stderr_text)
    assert stderr_error is None
    assert stderr_result == payload


class TestNormalizeCheckpoint:
    def test_empty_raw(self) -> None:
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
    assert normalize_artifacts("a.py") == []


def test_normalize_artifacts_empty_list() -> None:
    assert normalize_artifacts([]) == []


def test_normalize_artifacts_missing_path() -> None:
    result = normalize_artifacts([{"description": "no path"}])
    assert result == []


def test_normalize_artifacts_not_dict() -> None:
    result = normalize_artifacts(["string", 42])
    assert result == []


def test_checkpoint_text_concatenates_fields() -> None:
    cp = {"summary": "fixed bug", "branch_family": "core", "decisions": ["use regex"], "files_touched": ["parser.py"]}
    text = checkpoint_text(cp)
    assert "fixed bug" in text
    assert "core" in text
    assert "use regex" in text
    assert "parser.py" in text


def test_checkpoint_text_empty() -> None:
    assert checkpoint_text({}) == ""


def test_checkpoint_text_includes_artifacts() -> None:
    cp = {"artifacts": [{"path": "src/main.py", "description": "main file"}]}
    text = checkpoint_text(cp)
    assert "src/main.py" in text


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


def test_infer_branch_family_get_module_keywords_exception() -> None:
    """When get_module_keywords raises, infer_branch_family falls back to default keyword list."""
    with patch("scripts.autocode_config.get_module_keywords", side_effect=Exception("mock")):
        cp = normalize_checkpoint({"summary": "added pytest tests", "branch_family": ""}, "continue", "")
    assert cp["branch_family"] == "tests"


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


def test_infer_family_novelty_moderate() -> None:
    cp = normalize_checkpoint(
        {"summary": "new approach to testing pipeline", "family_novelty": "invalid"}, "continue", ""
    )
    assert cp["family_novelty"] == "moderate"


def test_infer_family_novelty_minor() -> None:
    cp = normalize_checkpoint({"summary": "fixed a small formatting bug", "family_novelty": "invalid"}, "continue", "")
    assert cp["family_novelty"] == "minor"


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


def test_infer_evidence_quality_strategy_level() -> None:
    cp = normalize_checkpoint(
        {"summary": "e2e tests passing in ci", "evidence_quality": "invalid"},
        "continue",
        "",
    )
    assert cp["evidence_quality"] == "strategy_level"


def test_infer_evidence_quality_artifact_only() -> None:
    cp = normalize_checkpoint(
        {"summary": "just wrote some code without running any checks", "evidence_quality": "invalid"},
        "continue",
        "",
    )
    assert cp["evidence_quality"] == "artifact_only"


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


def test_infer_promotion_recommendation_non_promising() -> None:
    """When competitiveness is not promising/promotable, should fall back to 'hold'."""
    cp = normalize_checkpoint(
        {
            "summary": "exploratory work",
            "competitiveness": "non_competitive",
            "promotion_recommendation": "invalid",
        },
        "continue",
        "",
    )
    assert cp["promotion_recommendation"] == "hold"


def test_extract_tags_basic() -> None:
    from scripts.checkpoint import extract_tags

    tags = extract_tags("fixed bug in parser module")
    assert "fixed" in tags
    assert "bug" in tags
    assert "parser" in tags
    assert "module" in tags


def test_extract_tags_excludes_stopwords() -> None:
    from scripts.checkpoint import extract_tags

    tags = extract_tags("the and of in to a")
    assert all(t not in ("the", "and", "of", "in", "to", "a") for t in tags)


def test_extract_tags_empty() -> None:
    from scripts.checkpoint import extract_tags

    assert extract_tags(None) == []
    assert extract_tags("") == []


def test_extract_tags_limit() -> None:
    from scripts.checkpoint import extract_tags

    tags = extract_tags(
        "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen",
        limit=10,
    )
    assert len(tags) <= 10


def test_extract_tags_bigrams() -> None:
    from scripts.checkpoint import extract_tags

    tags = extract_tags("machine learning model training")
    assert any("machine_learning" in t for t in tags)


def test_checkpoint_text_lowercases() -> None:
    cp = {"summary": "FIXED Bug"}
    text = checkpoint_text(cp)
    assert text == "fixed bug"


def test_parse_checkpoint_kv_simple() -> None:
    from scripts.checkpoint import _parse_checkpoint_kv

    text = "status: continue\nsummary: Simple test\nevidence: artifact_only"
    result = _parse_checkpoint_kv(text)
    assert result is not None
    assert result["status"] == "continue"
    assert result["summary"] == "Simple test"
    assert result["evidence_quality"] == "artifact_only"


def test_parse_checkpoint_kv_with_list() -> None:
    from scripts.checkpoint import _parse_checkpoint_kv

    text = """status: continue
summary: Test with list
new_facts:
  - fact one
  - fact two
decisions:
  - decision one
risks:
  - risk one
  - risk two"""
    result = _parse_checkpoint_kv(text)
    assert result is not None
    assert result["status"] == "continue"
    assert result["new_facts"] == ["fact one", "fact two"]
    assert result["decisions"] == ["decision one"]
    assert result["risks"] == ["risk one", "risk two"]


def test_parse_checkpoint_kv_with_artifacts() -> None:
    from scripts.checkpoint import _parse_checkpoint_kv

    text = """status: continue
summary: Artifact test
artifacts:
  - path: scripts/foo.py
    description: added foo
  - path: tests/test_foo.py
    description: tests for foo"""
    result = _parse_checkpoint_kv(text)
    assert result is not None
    assert result["status"] == "continue"
    assert len(result["artifacts"]) == 2
    assert result["artifacts"][0]["path"] == "scripts/foo.py"
    assert result["artifacts"][0]["description"] == "added foo"
    assert result["artifacts"][1]["path"] == "tests/test_foo.py"


def test_parse_checkpoint_kv_no_status() -> None:
    from scripts.checkpoint import _parse_checkpoint_kv

    result = _parse_checkpoint_kv("foo: bar\nbaz: qux")
    assert result is None


def test_parse_checkpoint_kv_empty() -> None:
    from scripts.checkpoint import _parse_checkpoint_kv

    assert _parse_checkpoint_kv("") is None
    assert _parse_checkpoint_kv("  ") is None


def test_parse_checkpoint_kv_key_mapping() -> None:
    from scripts.checkpoint import _parse_checkpoint_kv

    text = "status: continue\nfamily: test_coverage\nevidence: model_level"
    result = _parse_checkpoint_kv(text)
    assert result is not None
    assert result["branch_family"] == "test_coverage"
    assert result["evidence_quality"] == "model_level"


def test_extract_checkpoint_yaml_like_format() -> None:
    from scripts.checkpoint import extract_checkpoint

    text = """some preamble
AUTOPILOT_CHECKPOINT_BEGIN
status: continue
summary: YAML-like checkpoint
family: bug_fix
family_novelty: moderate
competitiveness: promising
evidence: model_level
goal_progress: Fixed the issue
new_facts:
  - Fixed edge case in parser
decisions:
  - Chose fix A over fix B
artifacts:
  - path: src/parser.py
    description: Fixed edge case
next_steps:
  - Add integration tests
AUTOPILOT_CHECKPOINT_END
some trailing text"""
    result, error = extract_checkpoint(text)
    assert error is None, f"Unexpected error: {error}"
    assert result is not None
    assert result["status"] == "continue"
    assert result["summary"] == "YAML-like checkpoint"
    assert result["branch_family"] == "bug_fix"
    assert result["family_novelty"] == "moderate"
    assert result["competitiveness"] == "promising"
    assert result["evidence_quality"] == "model_level"
    assert result["goal_progress"] == "Fixed the issue"
    assert result["new_facts"] == ["Fixed edge case in parser"]
    assert result["decisions"] == ["Chose fix A over fix B"]
    assert len(result["artifacts"]) == 1
    assert result["artifacts"][0]["path"] == "src/parser.py"


def test_extract_checkpoint_yaml_in_code_fence() -> None:
    from scripts.checkpoint import extract_checkpoint

    text = """text here
```
AUTOPILOT_CHECKPOINT_BEGIN
status: continue
summary: YAML in fence
AUTOPILOT_CHECKPOINT_END
```
more text"""
    result, error = extract_checkpoint(text)
    assert error is None, f"Unexpected error: {error}"
    assert result is not None
    assert result["summary"] == "YAML in fence"


def test_extract_checkpoint_yaml_with_colons_in_value() -> None:
    from scripts.checkpoint import extract_checkpoint

    text = """AUTOPILOT_CHECKPOINT_BEGIN
status: continue
summary: Fixed the foo:bar issue in parser: now handles edge case
goal_progress: Completed the refactor story: critique now feeds into controller
AUTOPILOT_CHECKPOINT_END"""
    result, error = extract_checkpoint(text)
    assert error is None, f"Unexpected error: {error}"
    assert result is not None
    assert "foo:bar" in result["summary"]
    assert "refactor story" in result["goal_progress"]
