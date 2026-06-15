from __future__ import annotations

import json
import re
from typing import Any

try:
    from scripts.memory import clean_text  # noqa: F401
except ImportError:
    from memory import clean_text  # type: ignore[no-redef]

CHECKPOINT_BEGIN = "AUTOPILOT_CHECKPOINT_BEGIN"
CHECKPOINT_END = "AUTOPILOT_CHECKPOINT_END"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
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


def normalize_checkpoint(
    raw: dict[str, Any] | None,
    fallback_status: str,
    fallback_summary: str,
) -> dict[str, Any]:
    payload = raw or {}
    status = clean_text(payload.get("status")).lower()
    if status not in STATUS_MAP:
        status = fallback_status

    raw_gc = payload.get("goal_complete")
    if isinstance(raw_gc, bool):
        goal_complete = raw_gc
    elif isinstance(raw_gc, str) and raw_gc.strip().lower() in ("true", "1", "yes"):
        goal_complete = True
    else:
        goal_complete = False

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
    if not checkpoint["branch_family"]:
        checkpoint["branch_family"] = infer_branch_family(checkpoint)
    return checkpoint


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


_CHECKPOINT_KEY_MAP: dict[str, str] = {
    "family": "branch_family",
    "evidence": "evidence_quality",
}


def _parse_checkpoint_kv(text: str) -> dict[str, Any] | None:
    lines = text.strip().split("\n")
    result: dict[str, Any] = {}

    active_list_key: str | None = None
    active_list: list[Any] = []
    active_dict: dict[str, str] | None = None

    def _flush_list() -> None:
        nonlocal active_list_key, active_list, active_dict
        if active_dict is not None:
            active_list.append(active_dict)
            active_dict = None
        if active_list_key is not None and active_list:
            result[active_list_key] = active_list
        active_list_key = None
        active_list = []

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            continue
        indent = len(raw_line) - len(raw_line.lstrip())

        if indent < 2 and stripped.endswith(":") and not stripped.startswith("- "):
            _flush_list()
            key = stripped[:-1].strip().lower().replace("-", "_")
            mapped = _CHECKPOINT_KEY_MAP.get(key, key)
            active_list_key = mapped
            active_list = []
            active_dict = None

        elif indent >= 2 and stripped.startswith("- "):
            item_text = stripped[2:].strip()
            if active_dict is not None:
                active_list.append(active_dict)
                active_dict = None
            if ": " in item_text:
                k, v = item_text.split(": ", 1)
                active_dict = {k.strip().lower().replace("-", "_"): v.strip()}
            else:
                active_list.append(item_text)

        elif indent >= 4 and ": " in stripped and active_dict is not None:
            k, v = stripped.split(": ", 1)
            active_dict[k.strip().lower().replace("-", "_")] = v.strip()

        elif ": " in stripped:
            _flush_list()
            key, value = stripped.split(": ", 1)
            key = key.strip().lower().replace("-", "_")
            mapped = _CHECKPOINT_KEY_MAP.get(key, key)
            result[mapped] = value.strip()

        elif stripped.startswith("  ") and active_dict is not None and not stripped.startswith("- "):
            pass

    _flush_list()

    if not result or "status" not in result:
        return None
    return result


def _is_checkpoint_like(obj: Any) -> bool:
    return isinstance(obj, dict) and isinstance(obj.get("status"), str)


def _find_last_code_fenced_json(text: str) -> dict[str, Any] | None:
    pattern = re.compile(
        r"^```(?:json)?\s*\n(.*?)\n?^```",
        re.MULTILINE | re.DOTALL,
    )
    candidates: list[dict[str, Any]] = []
    for match in pattern.finditer(text):
        raw = match.group(1).strip()
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not json_match:
            continue
        try:
            obj = json.loads(json_match.group(0))
        except json.JSONDecodeError:
            continue
        if _is_checkpoint_like(obj):
            candidates.append(obj)
    return candidates[-1] if candidates else None


def extract_checkpoint(stdout_text: str) -> tuple[dict[str, Any] | None, str | None]:
    clean_stdout = strip_ansi(stdout_text)
    pattern = re.compile(
        rf"{CHECKPOINT_BEGIN}(.*?){CHECKPOINT_END}",
        re.DOTALL,
    )
    match = pattern.search(clean_stdout)
    if match:
        inner = match.group(1).strip()
        inner = re.sub(r"^```(?:json)?\s*\n?", "", inner)
        inner = re.sub(r"\n?```$", "", inner)
        inner = inner.strip()
        json_match = re.search(r"\{.*\}", inner, re.DOTALL)
        if json_match:
            inner = json_match.group(0)
            try:
                return json.loads(inner), None
            except json.JSONDecodeError as exc:
                return None, f"Checkpoint JSON was invalid: {exc}"

        kv_result = _parse_checkpoint_kv(inner)
        if kv_result is not None:
            return kv_result, None

    fallback = _find_last_code_fenced_json(clean_stdout)
    if fallback is not None:
        return fallback, None

    return None, "No checkpoint markers found in opencode output."


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
        from scripts.autocode_config import get_module_keywords

        families = get_module_keywords()
    except Exception:
        families = []
    if not families:
        families = [
            ("core", "Main code", ["src", "core", "main", "lib"]),
            ("tests", "Tests", [r"\btests?\b", r"\bspec\b", r"\bpytest\b", r"\bunittest\b", r"\btypecheck\b"]),
            ("docs", "Documentation", [r"\bdoc\b", r"\breadme\b", r"\bguide\b"]),
            ("scripts", "Scripts/tools", [r"\bscript\b", r"\btool\b", r"\bcli\b"]),
            ("config", "Config/deploy", [r"\bconfig\b", r"\bdeploy\b", r"\bci\b", r"\bdocker\b"]),
        ]
    text = checkpoint_text(checkpoint)
    for family, _desc, hints in families:
        if any(re.search(hint, text) for hint in hints):
            return family
    return "unknown"


def infer_family_novelty(checkpoint: dict[str, Any]) -> str:
    text = checkpoint_text(checkpoint)
    if any(
        phrase in text
        for phrase in (
            "pivoted hard",
            "hard structural pivot",
            "materially different",
            "new module",
            "new architecture",
        )
    ):
        return "major"
    if any(
        phrase in text
        for phrase in ("new family", "new approach", "new pattern", "real structural pivot", "new technique")
    ):
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
    if any(
        phrase in text
        for phrase in ("promising enough", "deployable", "outperform", "promotable", "production-ready", "merge-ready")
    ):
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
    if any(
        phrase in text
        for phrase in ("abandon", "should not be deployed", "should not be merged", "pivot away", "do not revisit")
    ):
        return "abandon_family"
    if competitiveness in {"promising", "promotable"}:
        return "promote" if checkpoint.get("evidence_quality") == "strategy_level" else "hold"
    return "hold"
