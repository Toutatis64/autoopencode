from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts.autocode_config import (
    BASE_DEFAULTS,
    PRESETS,
    ROOT,
    _build_defaults,
    _deep_merge,
    get_conventions,
    get_module_keywords,
    get_modules,
    get_path,
    get_project_name,
    get_project_type,
    get_type_approaches,
    get_type_architectures,
    get_type_novelty_angles,
    get_type_objectives,
    get_validation,
    load_config,
    reload,
)


def test_build_defaults_python() -> None:
    defaults = _build_defaults("python")
    assert defaults["project"]["type"] == "generic"  # base default, not overridden by preset
    assert defaults["validation"]["default"] == "pytest"
    assert defaults["validation"]["typecheck"] == "mypy ."
    modules = defaults.get("modules", [])
    assert any(m["name"] == "src" for m in modules)


def test_build_defaults_node_ts() -> None:
    defaults = _build_defaults("node_ts")
    assert defaults["validation"]["default"] == "npm test"
    modules = defaults.get("modules", [])
    assert any(m["name"] == "api" for m in modules)


def test_build_defaults_rust() -> None:
    defaults = _build_defaults("rust")
    assert defaults["validation"]["default"] == "cargo test"
    assert any(m["name"] == "benches" for m in defaults.get("modules", []))


def test_build_defaults_go() -> None:
    defaults = _build_defaults("go")
    assert defaults["validation"]["default"] == "go test ./..."
    modules = defaults.get("modules", [])
    assert any(m["name"] == "cmd" for m in modules)


def test_build_defaults_generic() -> None:
    defaults = _build_defaults("generic")
    assert defaults["validation"]["default"] == "make test"
    assert defaults["validation"]["typecheck"] == "make check"


def test_build_defaults_unknown_type_falls_back_to_generic() -> None:
    defaults = _build_defaults("nonexistent")
    assert defaults["validation"]["default"] == "make test"
    modules = defaults.get("modules", [])
    assert any(m["name"] == "core" for m in modules)


def test_build_defaults_base_defaults_preserved() -> None:
    defaults = _build_defaults("python")
    # Base defaults should be preserved where preset doesn't override
    assert defaults["autopilot"]["inference"]["exhaust_family_after"] == 3
    assert defaults["conventions"] == BASE_DEFAULTS["conventions"]


def test_deep_merge_basic() -> None:
    base = {"a": 1, "b": 2}
    override = {"b": 3, "c": 4}
    result = _deep_merge(base, override)
    assert result == {"a": 1, "b": 3, "c": 4}


def test_deep_merge_nested() -> None:
    base = {"outer": {"inner": 1, "keep": 2}, "other": 3}
    override = {"outer": {"inner": 99, "new": 4}}
    result = _deep_merge(base, override)
    assert result["outer"]["inner"] == 99
    assert result["outer"]["keep"] == 2
    assert result["outer"]["new"] == 4
    assert result["other"] == 3


def test_deep_merge_empty_override() -> None:
    base = {"a": 1, "b": {"c": 2}}
    result = _deep_merge(base, {})
    assert result == base


def test_deep_merge_none_value() -> None:
    base = {"a": 1}
    override = {"a": None}
    result = _deep_merge(base, override)
    assert result["a"] is None


def test_load_config_returns_dict() -> None:
    cfg = load_config()
    assert isinstance(cfg, dict)
    assert cfg["project"]["type"] == "python"


def test_load_config_cache() -> None:
    reload()
    cfg1 = load_config()
    cfg2 = load_config()
    assert cfg1 is cfg2  # same object from cache


def test_reload_clears_cache() -> None:
    cfg1 = load_config()
    reload()
    cfg2 = load_config()
    # After reload, should be a fresh load (potentially same content but new dict)
    assert cfg1 is not cfg2


def test_get_path_knowledge_base() -> None:
    path = get_path("knowledge_base")
    assert isinstance(path, Path)
    assert path == ROOT / "knowledge" / "autopilot_kb.yaml"


def test_get_path_with_custom_config() -> None:
    config = {"paths": {"custom_key": "some/path"}}
    path = get_path("custom_key", config)
    assert path == ROOT / "some" / "path"


def test_get_path_empty_key() -> None:
    path = get_path("", {"paths": {"": "foo"}})
    assert path == ROOT / "foo"


def test_get_path_missing_key() -> None:
    path = get_path("nonexistent", {"paths": {"present": "val"}})
    assert path == ROOT / ""


def test_get_validation_default() -> None:
    val = get_validation("default")
    assert val == "pytest -x -q" or val == "pytest"


def test_get_validation_typecheck() -> None:
    val = get_validation("typecheck")
    assert val == "mypy ."


def test_get_validation_lint() -> None:
    val = get_validation("lint")
    assert val == "ruff check ."


def test_get_validation_with_custom_config() -> None:
    config = {"validation": {"default": "custom_cmd"}}
    val = get_validation("default", config)
    assert val == "custom_cmd"


def test_get_validation_fallback_to_preset() -> None:
    config = {"project": {"type": "node_ts"}}
    val = get_validation("default", config)
    assert val == "npm test"


def test_get_validation_fallback_to_base_default() -> None:
    config = {"project": {"type": "unknown_type"}}
    val = get_validation("default", config)
    # Unknown type falls back to generic preset, not BASE_DEFAULTS echo command
    assert val == "make test"


def test_get_validation_build() -> None:
    val = get_validation("build")
    assert val is not None and len(val) > 0


def test_get_project_name_default() -> None:
    name = get_project_name()
    assert name == "AutoOpencode"


def test_get_project_name_with_config() -> None:
    config = {"project": {"name": "Custom"}}
    assert get_project_name(config) == "Custom"


def test_get_project_name_missing() -> None:
    config: dict[str, Any] = {}
    assert get_project_name(config) == "Unnamed Project"


def test_get_project_type_default() -> None:
    pt = get_project_type()
    assert pt == "python"


def test_get_project_type_with_config() -> None:
    config = {"project": {"type": "rust"}}
    assert get_project_type(config) == "rust"


def test_get_project_type_invalid_falls_back_to_generic() -> None:
    config = {"project": {"type": "invalid_type"}}
    assert get_project_type(config) == "generic"


def test_get_project_type_missing() -> None:
    config: dict[str, Any] = {}
    assert get_project_type(config) == "generic"


def test_get_modules_default() -> None:
    modules = get_modules()
    assert isinstance(modules, list)
    assert len(modules) > 0
    names = [m["name"] for m in modules]
    assert "scripts" in names
    assert "tests" in names


def test_get_modules_with_custom_config() -> None:
    custom = [{"name": "custom_mod", "keywords": ["custom"]}]
    config = {"modules": custom}
    result = get_modules(config)
    assert result == custom


def test_get_modules_cache() -> None:
    reload()
    m1 = get_modules()
    m2 = get_modules()
    assert m1 is m2


def test_get_module_keywords() -> None:
    keywords = get_module_keywords()
    assert isinstance(keywords, list)
    assert all(isinstance(k, tuple) and len(k) == 3 for k in keywords)


def test_get_module_keywords_with_custom_config() -> None:
    config: dict[str, Any] = {"modules": [{"name": "test", "description": "desc", "keywords": ["a", "b"]}]}
    custom_modules = get_modules(config)
    assert custom_modules == [{"name": "test", "description": "desc", "keywords": ["a", "b"]}]


def test_get_conventions_default() -> None:
    convs = get_conventions()
    assert isinstance(convs, list)
    assert len(convs) > 0


def test_get_conventions_with_config() -> None:
    custom = ["custom convention"]
    config = {"conventions": custom}
    assert get_conventions(config) == custom


def test_get_conventions_empty() -> None:
    config: dict[str, Any] = {"conventions": []}
    assert get_conventions(config) == BASE_DEFAULTS["conventions"]


def test_get_conventions_missing() -> None:
    config: dict[str, Any] = {}
    assert get_conventions(config) == BASE_DEFAULTS["conventions"]


def test_presets_all_have_validation() -> None:
    for pt, preset in PRESETS.items():
        assert "validation" in preset, f"Preset {pt} missing validation"
        assert "default" in preset["validation"], f"Preset {pt} missing default validation"
        assert "typecheck" in preset["validation"], f"Preset {pt} missing typecheck"
        assert "lint" in preset["validation"], f"Preset {pt} missing lint"


def test_presets_all_have_modules() -> None:
    for pt, preset in PRESETS.items():
        assert "modules" in preset, f"Preset {pt} missing modules"
        assert len(preset["modules"]) > 0, f"Preset {pt} has empty modules"


def test_presets_module_keywords_are_lists() -> None:
    for pt, preset in PRESETS.items():
        for m in preset.get("modules", []):
            assert isinstance(m.get("keywords", []), list), f"Preset {pt} module {m['name']} keywords not a list"


def test_get_validation_lint_ruff() -> None:
    val = get_validation("lint")
    assert "ruff" in val or "lint" in val


def test_get_validation_fallback_to_preset_with_explicit_validation() -> None:
    config = {"validation": {}, "project": {"type": "node_ts"}}
    # When validation dict is present but empty, get_validation should still find the key
    val = get_validation("default", config)
    assert val == "npm test"


def test_reload_then_load_produces_valid_config() -> None:
    reload()
    cfg = load_config()
    assert isinstance(cfg, dict)
    assert "project" in cfg
    assert "validation" in cfg


def test_root_is_absolute_path() -> None:
    assert isinstance(ROOT, Path)
    assert ROOT.is_absolute()
    assert (ROOT / "autocode.yaml").exists()


def test_get_type_approaches_default() -> None:
    approaches = get_type_approaches()
    assert isinstance(approaches, list)
    assert len(approaches) > 0


def test_get_type_objectives_default() -> None:
    objectives = get_type_objectives()
    assert isinstance(objectives, list)
    assert len(objectives) > 0


def test_get_type_architectures_default() -> None:
    archs = get_type_architectures()
    assert isinstance(archs, list)
    assert len(archs) > 0


def test_get_type_novelty_angles_default() -> None:
    angles = get_type_novelty_angles()
    assert isinstance(angles, list)
    assert len(angles) >= 4
    assert all(isinstance(a, str) and len(a) > 0 for a in angles)


def test_get_type_novelty_angles_python_omits_node_topics() -> None:
    """The Python preset must not surface Node/React/MongoDB boilerplate."""
    angles = get_type_novelty_angles("python")
    text = " ".join(angles).lower()
    assert "mongodb" not in text
    assert "websocket" not in text
    assert "bundle splitting" not in text
    assert "graphql federation" not in text
    assert "lambda cold-start" not in text


def test_get_type_novelty_angles_python_contains_python_relevant_topics() -> None:
    angles = get_type_novelty_angles("python")
    text = " ".join(angles).lower()
    assert "mypy" in text or "pytest" in text or "asyncio" in text or "structlog" in text


def test_get_type_novelty_angles_node_ts_keeps_legacy_suggestions() -> None:
    """Regression: the original Node/React-flavoured suggestions are still the
    default for node_ts projects (this was the only list before iter 4)."""
    angles = get_type_novelty_angles("node_ts")
    text = " ".join(angles).lower()
    assert "backend" in text
    assert "mongodb" in text
    assert "websocket" in text


def test_get_type_novelty_angles_rust_surfaces_rust_specific_topics() -> None:
    angles = get_type_novelty_angles("rust")
    text = " ".join(angles).lower()
    assert "tokio" in text or "cargo" in text or "rayon" in text or "rust" in text


def test_get_type_novelty_angles_go_surfaces_go_specific_topics() -> None:
    angles = get_type_novelty_angles("go")
    text = " ".join(angles).lower()
    assert "errgroup" in text or "context cancellation" in text or "pprof" in text or "go " in text


def test_get_type_novelty_angles_unknown_type_falls_back_to_generic() -> None:
    angles = get_type_novelty_angles("nonexistent_type")
    generic = get_type_novelty_angles("generic")
    assert angles == generic


def test_get_type_novelty_angles_returns_independent_copies() -> None:
    """Two calls must return equal-but-distinct lists so callers can mutate one
    without affecting the preset."""
    a = get_type_novelty_angles("python")
    b = get_type_novelty_angles("python")
    assert a == b
    assert a is not b
    a.append("mutation")
    assert "mutation" not in get_type_novelty_angles("python")


def test_get_path_default() -> None:
    p = get_path("test")
    assert isinstance(p, Path)


def test_get_validation_fallback_missing_key() -> None:
    config: dict[str, Any] = {"validation": {"default": "custom_cmd"}, "project": {"type": "rust"}}
    # When config has validation but missing the requested key, fall back to preset
    val = get_validation("typecheck", config)
    assert isinstance(val, str)
    assert len(val) > 0


def test_get_modules_fallback_no_config_modules() -> None:
    config: dict[str, Any] = {"project": {"type": "node_ts"}}
    modules = get_modules(config)
    assert isinstance(modules, list)
    assert len(modules) > 0


def test_load_config_yaml_exception_falls_back_to_defaults() -> None:
    reload()
    with patch.object(Path, "exists", return_value=True):
        with patch("builtins.open", side_effect=OSError("read error")):
            cfg = load_config()
    reload()
    assert isinstance(cfg, dict)
    assert "project" in cfg
    assert "validation" in cfg


def test_load_config_invalid_project_type_falls_back_to_generic() -> None:
    reload()
    import yaml

    original_load = yaml.safe_load
    try:

        def bad_yaml(*args: Any, **kwargs: Any) -> dict[str, Any]:
            return {"project": {"type": "nonexistent_type"}}

        yaml.safe_load = bad_yaml  # type: ignore[assignment]
        with patch.object(Path, "exists", return_value=True):
            cfg = load_config()
        reload()
        assert isinstance(cfg, dict)
        assert "validation" in cfg
    finally:
        yaml.safe_load = original_load
