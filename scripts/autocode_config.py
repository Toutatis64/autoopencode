#!/usr/bin/env python3
"""
AutoOpencode config loader — reads autocode.yaml from the project root.

All Python scripts import this instead of hardcoding paths or domain values.
Supports multiple project types (python, node_ts, rust, go, generic) with
language-appropriate defaults.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


# ── Discover project root ────────────────────────────────────────────────────


def _find_root() -> Path:
    start = Path(__file__).resolve()
    for parent in [start] + list(start.parents):
        if (parent / "autocode.yaml").exists():
            return parent
        if (parent / ".git").exists():
            return parent
    return start.parents[2]


ROOT = Path(os.environ.get("AUTOPILOT_ROOT", str(_find_root()))).resolve()
CONFIG_PATH = ROOT / "autocode.yaml"


# ── Project-type presets ─────────────────────────────────────────────────────

# ── Type-specific divergence lists ─────────────────────────────────────────────

TYPE_APPROACHES: dict[str, list[str]] = {
    "python": [
        "Refactor existing (improve structure, preserve behavior)",
        "New feature (add capability with tests)",
        "Bug fix (diagnose + regression test)",
        "Test coverage (add tests for untested code paths)",
        "Performance (measurable speed/memory improvement)",
        "Type safety (replace Any, add proper interfaces)",
        "Documentation (architecture decisions, README, docstrings)",
        "Code removal (delete dead/unused code/files)",
    ],
    "node_ts": [
        "Refactor existing (improve structure, preserve behavior)",
        "New feature (add capability with tests)",
        "Bug fix (diagnose + regression test)",
        "Test coverage (add tests for untested code paths)",
        "Type safety (strict TypeScript, replace any)",
        "Performance (bundle size, lazy loading, caching)",
        "Documentation (README, JSDoc, ADRs)",
        "Code removal (delete dead/unused code/files)",
    ],
    "rust": [
        "Refactor existing (improve structure, preserve behavior)",
        "New feature (add capability with tests)",
        "Bug fix (diagnose + regression test)",
        "Test coverage (add tests for untested code paths, benchmarks)",
        "Performance (zero-cost abstractions, memory optimization)",
        "Type safety (leverage type system, eliminate unwrap)",
        "Documentation (doc comments, module-level docs, examples)",
        "Code removal (delete dead/unused code/files)",
    ],
    "go": [
        "Refactor existing (improve structure, preserve behavior)",
        "New feature (add capability with tests)",
        "Bug fix (diagnose + regression test)",
        "Test coverage (add tests for untested code paths)",
        "Performance (concurrency, memory optimization)",
        "Type safety (reduce interface{}, add proper types)",
        "Documentation (godoc, architecture decisions)",
        "Code removal (delete dead/unused code/files)",
    ],
    "generic": [
        "Refactor existing (improve structure, preserve behavior)",
        "New feature (add capability with tests)",
        "Bug fix (diagnose + regression test)",
        "Test coverage (add tests for untested code paths)",
        "Performance (measurable improvement)",
        "Documentation (architecture decisions, README)",
        "Code removal (delete dead/unused code/files)",
    ],
    "config": [
        "Validate config correctness (schema validation)",
        "Improve config consistency (formatting, naming, structure)",
        "Remove unused or redundant config values",
        "Add documentation (README per config area, usage examples)",
        "Security hardening (secrets detection, permissions, auditing)",
        "Parameter tuning and optimization",
        "Add schema definitions (JSON Schema, YAML schema)",
        "Organize config structure (split monolithic files, logical grouping)",
        "Add CI validation pipeline for configs",
    ],
}

TYPE_OBJECTIVES: dict[str, list[str]] = {
    "python": [
        "Reduce code duplication",
        "Improve error handling and edge cases",
        "Add input validation and sanitization",
        "Reduce coupling / increase cohesion",
        "Increase test coverage (line/branch)",
        "Remove dead code and unused dependencies",
        "Improve type safety (strict mode, no Any)",
        "Add logging / observability / monitoring",
    ],
    "node_ts": [
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
    ],
    "rust": [
        "Reduce code duplication",
        "Improve error handling (Result/Option patterns)",
        "Add input validation and sanitization",
        "Reduce coupling / increase cohesion",
        "Improve memory efficiency / reduce allocations",
        "Increase test coverage (unit + integration + benchmarks)",
        "Remove dead code and unused dependencies",
        "Improve type safety (leverage type states, newtypes)",
        "Add logging / observability (tracing)",
        "Improve concurrency correctness (Send + Sync bounds)",
    ],
    "go": [
        "Reduce code duplication",
        "Improve error handling and edge cases",
        "Add input validation and sanitization",
        "Reduce coupling / increase cohesion",
        "Improve concurrency / parallelism",
        "Increase test coverage (unit + integration)",
        "Remove dead code and unused dependencies",
        "Improve type safety (reduce interface{}, avoid empty interface)",
        "Add logging / observability / monitoring",
    ],
    "generic": [
        "Reduce code duplication",
        "Improve error handling and edge cases",
        "Add input validation and sanitization",
        "Reduce coupling / increase cohesion",
        "Increase test coverage (line/branch)",
        "Remove dead code and unused dependencies",
        "Add logging / observability / monitoring",
    ],
    "config": [
        "Remove duplicate or redundant config values",
        "Improve schema validation coverage",
        "Add input validation (pre-commit hooks, schema checks)",
        "Normalize config structure across files",
        "Increase config test coverage (validation tests)",
        "Remove unused config keys and dead references",
        "Add validation / linting pipeline",
        "Improve security posture (secrets detection, RBAC)",
    ],
}

TYPE_ARCHITECTURES: dict[str, list[str]] = {
    "python": [
        "Module decomposition (split/merge modules)",
        "Shared contract extraction (common types/utils to shared)",
        "Error boundary and global error handling",
        "State management rationalization",
        "Event-driven decoupling (queues/events)",
        "Caching layer (memoization/memory/in-process)",
        "CLI structure improvement (argparse to click/typer)",
        "Plugin/extensibility architecture",
    ],
    "node_ts": [
        "Backend module decomposition (split/merge services/controllers)",
        "Frontend component composition (extract/shared components)",
        "Shared contract extraction (common types/utils to shared)",
        "Middleware pipeline improvement",
        "Error boundary and global error handling",
        "State management rationalization",
        "API versioning and deprecation strategy",
        "Event-driven decoupling (queues/events)",
        "Caching layer (Redis/memoization/CDN)",
    ],
    "rust": [
        "Crate decomposition (split/merge crates)",
        "Shared types/contracts in common crate",
        "Error handling architecture (thiserror/anyhow layers)",
        "Concurrency architecture (channels/arcs/locks strategy)",
        "Builder/constructor pattern standardization",
        "Plugin/trait-object architecture",
        "Zero-cost abstraction refactoring",
    ],
    "go": [
        "Package decomposition (split/merge packages)",
        "Shared types/contracts in common package",
        "Middleware/interceptor pipeline",
        "Error handling architecture (sentinel errors/wrapping)",
        "Concurrency architecture (goroutines/channels)",
        "Interface-based decoupling",
        "Configuration management pattern",
    ],
    "generic": [
        "Module decomposition (split/merge modules)",
        "Shared contract extraction (common types/utils to shared)",
        "Error handling improvement",
        "Configuration management pattern",
        "Plugin/extensibility architecture",
    ],
    "config": [
        "Config file decomposition (split monolithic files into focused files)",
        "Schema extraction (extract inline schemas to shared schema files)",
        "Default value rationalization (consistent defaults across configs)",
        "Environment-specific config layering",
        "Template-driven config generation",
        "Include/reference mechanism for shared config blocks",
    ],
}

TYPE_NOVELTY_ANGLES: dict[str, list[str]] = {
    "python": [
        "Test architecture: contract testing, property-based testing with Hypothesis, snapshot testing, or mutation testing.",
        "Type safety: enable strict mypy flags, eliminate Any usage, add Protocol classes for structural typing, or introduce NewType for domain primitives.",
        "Concurrency: switch CPU-bound work to multiprocessing, async I/O refactor, or asyncio.TaskGroup for structured concurrency.",
        "Performance: profile with cProfile/py-spy, optimize hot paths with functools.lru_cache, use orjson for JSON, or introduce Cython/Numba for numerics.",
        "Developer experience: pre-commit hooks (ruff/mypy), editable install with uv/pip, dependency groups, or rich tracebacks in dev mode.",
        "Architecture: extract a public API in a dedicated package, split scripts/ into subpackages by domain, or introduce a settings layer (pydantic-settings).",
        "Observability: structured logging with structlog, OpenTelemetry tracing, or Prometheus metrics for hot paths.",
        "Resilience: tenacity retries, circuit breakers, graceful shutdown handlers, or typed exception hierarchies.",
    ],
    "node_ts": [
        "Backend architecture: modular decomposition, event-driven design, CQRS, queue-based decoupling, or GraphQL federation.",
        "Frontend innovation: server components, streaming SSR, optimistic updates, edge caching, or WebSocket real-time sync.",
        "Database optimization: read replicas, materialized views, denormalization, TTL indexes, or MongoDB aggregation pipelines.",
        "Testing strategy: contract testing, property-based testing, snapshot testing, visual regression, or load/stress testing.",
        "DevOps and deployment: zero-downtime migrations, Blue/Green deploy, canary releases, or Lambda cold-start mitigation.",
        "Security hardening: rate limiting, WAF rules, input sanitization, audit logging, or secrets rotation.",
        "Performance optimization: Redis caching, CDN tuning, image optimization, lazy loading, or bundle splitting.",
        "Developer experience: monorepo tooling, code generation, API documentation, or automated migration pipelines.",
    ],
    "rust": [
        "Concurrency: tokio task spawning, rayon data parallelism, crossbeam channels, or async-std runtime tuning.",
        "Performance: cargo bench, flamegraph profiling, mimalloc/jemalloc allocator swap, or SIMD intrinsics for hot paths.",
        "Type safety: replace Box<dyn Trait> with generics, newtype wrappers for domain units, or typestate for state machines.",
        "Architecture: split into a workspace of crates, extract a public API crate, or introduce a feature-flag matrix.",
        "Memory: arena allocation, smallvec for small collections, or cow clone-on-write for parsing hot paths.",
        "Tooling: cargo-nextest for parallel tests, cargo-mutants for mutation testing, or cargo-deny for license/audit.",
        "FFI: cbindgen for C exports, pyo3/maturin for Python bindings, or wasm-bindgen for browser targets.",
        "Error handling: thiserror/anyhow layering, error context with snafu, or structured error types for the public API.",
    ],
    "go": [
        "Concurrency: errgroup fan-out, worker pools with bounded semaphore, or context cancellation trees.",
        "Performance: pprof CPU/heap profiling, escape analysis tuning, sync.Pool for hot allocations, or generic specialization.",
        "Type safety: replace interface{} with generics (Go 1.18+), typed constants, or sum-type patterns via sealed interfaces.",
        "Architecture: hexagonal/clean architecture, dependency injection with wire, or cmd/internal split for binary libraries.",
        "Testing: table-driven tests, fuzz testing (go test -fuzz), testify suites, or testcontainers for integration.",
        "Tooling: golangci-lint with strict rule set, go vet with shadow, or staticcheck for deeper analysis.",
        "Observability: OpenTelemetry traces/metrics, prometheus client_golang, or slog structured logging.",
        "Resilience: circuit breakers (sony/gobreaker), retries with backoff, graceful shutdown via signal.Notify.",
    ],
    "generic": [
        "Testing strategy: contract testing, property-based testing, snapshot testing, or load/stress testing.",
        "Performance optimization: profile the hot path, replace hot allocations, introduce caching, or use a faster parser.",
        "Architecture: extract a shared module, split a monolithic file, or introduce a plugin/extension point.",
        "Security hardening: input validation, rate limiting, audit logging, or secrets rotation policy.",
        "Developer experience: pre-commit hooks, dependency grouping, generated docs, or migration scripts.",
        "Resilience: retry with backoff, circuit breakers, graceful shutdown, or typed exception hierarchy.",
        "Observability: structured logging, tracing, or metrics on hot paths.",
        "Type safety: replace dynamic types with strict types, add validation, or introduce domain primitives.",
    ],
    "config": [
        "Config validation: add schemas (JSON Schema, KCL, Cue), run validation in CI, or use conftest/OPA policies.",
        "Secrets management: detect hardcoded secrets, migrate to vault/secret-store references, or add audit logging.",
        "Idempotency: ensure configs are safe to re-apply (no drift, no side-effects on re-apply).",
        "Parameter tuning: review and adjust numerical parameters (timeouts, limits, weights) based on observed behavior.",
        "Diff hygiene: reduce noisy diffs via consistent formatting, sorted keys, and trailing-whitespace removal.",
        "Environment parity: detect config drift between dev/staging/prod, or template for environment-specific values.",
        "Security posture: review RBAC permissions, network policies, and resource limits against least-privilege principle.",
        "Documentation generation: auto-generate README tables, schema docs, or dependency diagrams from config.",
    ],
}


PRESETS: dict[str, dict[str, Any]] = {
    "python": {
        "validation": {
            "default": "pytest",
            "typecheck": "mypy .",
            "lint": "ruff check .",
            "build": "python -m build",
            "test_one": "pytest",
            "test_all": "pytest",
            "full": "pytest && mypy . && ruff check .",
        },
        "modules": [
            {"name": "src", "description": "Main source code", "keywords": ["src", "module", "package", "lib"]},
            {"name": "tests", "description": "Test suite", "keywords": ["test", "spec", "pytest", "unittest"]},
            {
                "name": "scripts",
                "description": "Utility scripts and tools",
                "keywords": ["script", "cli", "tool", "bin"],
            },
            {"name": "docs", "description": "Documentation", "keywords": ["doc", "readme", "guide", "md"]},
            {
                "name": "data",
                "description": "Data files, configs, migrations",
                "keywords": ["data", "config", "migration", "schema"],
            },
            {
                "name": "ci_cd",
                "description": "CI/CD pipelines and infra config",
                "keywords": ["ci", "cd", "docker", "deploy", "action"],
            },
        ],
    },
    "node_ts": {
        "validation": {
            "default": "npm test",
            "typecheck": "tsc --noEmit",
            "lint": "npm run lint",
            "build": "npm run build",
            "test_one": "npm test --",
            "test_all": "npm test",
            "full": "npm run build && npm test",
        },
        "modules": [
            {
                "name": "api",
                "description": "API endpoints and middleware",
                "keywords": ["api", "route", "endpoint", "controller", "middleware"],
            },
            {
                "name": "web",
                "description": "Web UI components and pages",
                "keywords": ["component", "page", "ui", "hook", "view"],
            },
            {
                "name": "shared",
                "description": "Shared types, utilities, contracts",
                "keywords": ["shared", "common", "util", "type", "dto"],
            },
            {
                "name": "database",
                "description": "Database schemas and queries",
                "keywords": ["database", "migration", "schema", "model", "query"],
            },
            {"name": "tests", "description": "Test suite", "keywords": ["test", "spec", "e2e", "integration"]},
            {
                "name": "infra",
                "description": "Infrastructure and deployment",
                "keywords": ["deploy", "ci", "docker", "config", "infra"],
            },
        ],
    },
    "rust": {
        "validation": {
            "default": "cargo test",
            "typecheck": "cargo check",
            "lint": "cargo clippy -- -D warnings",
            "build": "cargo build",
            "test_one": "cargo test",
            "test_all": "cargo test --workspace",
            "full": "cargo build && cargo test --workspace && cargo clippy -- -D warnings",
        },
        "modules": [
            {"name": "src", "description": "Main crate source", "keywords": ["src", "lib", "main", "module", "crate"]},
            {"name": "tests", "description": "Integration tests", "keywords": ["test", "integration", "bench"]},
            {"name": "benches", "description": "Benchmarks", "keywords": ["bench", "criterion", "perf"]},
            {"name": "examples", "description": "Usage examples", "keywords": ["example", "demo"]},
            {"name": "scripts", "description": "Build scripts and tooling", "keywords": ["script", "build", "tool"]},
        ],
    },
    "go": {
        "validation": {
            "default": "go test ./...",
            "typecheck": "go vet ./...",
            "lint": "golangci-lint run",
            "build": "go build ./...",
            "test_one": "go test -run",
            "test_all": "go test ./...",
            "full": "go build ./... && go test ./... && go vet ./...",
        },
        "modules": [
            {"name": "cmd", "description": "CLI entrypoints", "keywords": ["cmd", "main", "cli", "entrypoint"]},
            {"name": "pkg", "description": "Library packages", "keywords": ["pkg", "lib", "package", "module"]},
            {"name": "internal", "description": "Internal packages", "keywords": ["internal", "private"]},
            {
                "name": "api",
                "description": "API handlers and middleware",
                "keywords": ["api", "handler", "route", "middleware"],
            },
            {"name": "tests", "description": "Test suite", "keywords": ["test", "integration", "e2e"]},
        ],
    },
    "generic": {
        "validation": {
            "default": "make test",
            "typecheck": "make check",
            "lint": "make lint",
            "build": "make build",
            "test_one": "make test",
            "test_all": "make test-all",
            "full": "make build && make test",
        },
        "modules": [
            {"name": "core", "description": "Core library / main code", "keywords": ["core", "main", "lib", "src"]},
            {"name": "tests", "description": "Test suite", "keywords": ["test", "spec", "check"]},
            {"name": "docs", "description": "Documentation", "keywords": ["doc", "readme", "guide"]},
            {"name": "scripts", "description": "Utility scripts", "keywords": ["script", "tool", "util", "bin"]},
            {
                "name": "config",
                "description": "Configuration and deployment",
                "keywords": ["config", "deploy", "ci", "docker"],
            },
        ],
    },
    "config": {
        "validation": {
            "default": "yamllint .",
            "typecheck": "prettier --check .",
            "lint": "yamllint .",
            "build": "echo 'Config-only project — no build needed'",
            "test_one": "yamllint",
            "test_all": "yamllint .",
            "full": "yamllint . && prettier --check .",
        },
        "modules": [
            {
                "name": "configs",
                "description": "Configuration files (YAML, JSON, TOML, etc.)",
                "keywords": ["config", "yaml", "json", "toml", "ini", "hcl"],
            },
            {
                "name": "schemas",
                "description": "Schema definitions for config validation",
                "keywords": ["schema", "jsonschema", "validation", "spec"],
            },
            {
                "name": "ci",
                "description": "CI/CD pipeline configs",
                "keywords": ["ci", "cd", "pipeline", "action", "workflow"],
            },
            {
                "name": "docs",
                "description": "Documentation and usage guides",
                "keywords": ["doc", "readme", "guide", "example"],
            },
            {
                "name": "templates",
                "description": "Config templates and scaffolding",
                "keywords": ["template", "scaffold", "example", "sample"],
            },
        ],
    },
}


# ── Defaults (used when autocode.yaml is missing or partial) ─────────────────

BASE_DEFAULTS: dict[str, Any] = {
    "project": {
        "name": "Unnamed Project",
        "description": "",
        "type": "generic",
    },
    "validation": {
        "default": "echo 'No test command configured'",
        "typecheck": "echo 'No typecheck configured'",
        "lint": "echo 'No lint configured'",
        "build": "echo 'No build configured'",
        "test_one": "echo",
        "test_all": "echo 'No test command configured'",
        "full": "echo 'No full validation configured'",
    },
    "paths": {
        "knowledge_base": "knowledge/autopilot_kb.yaml",
        "meta_knowledge_base": "knowledge/meta_kb.yaml",
        "goal_file": ".opencode/autopilot/goal.md",
        "runtime_dir": ".opencode/autopilot/runtime",
        "components_dir": ".opencode/autopilot/components",
    },
    "project_goals": {
        "description": "Improve the codebase continuously across all areas.",
        "principles": [
            "Improve quality, coverage, performance, and architecture",
            "Add tests for uncovered code",
            "Fix bugs with regression tests",
            "Reduce duplication and technical debt",
        ],
    },
    "conventions": [
        "Tests for every fix: regression test that fails before the fix",
        "One focused validation per edit (test, typecheck, or lint)",
        "Preserve existing code patterns unless explicitly changing them",
    ],
    "skills": {
        "paths": [".opencode/skills"],
    },
    "autopilot": {
        "loop": {
            "sleep_seconds": 60,
            "max_retries_per_iteration": 2,
            "retry_backoff_seconds": 15,
            "max_retry_backoff_seconds": 300,
            "cpu_budget_percent": 90,
            "parallel_branches_max": 3,
            "timeout_seconds": 3600,
        },
        "inference": {
            "exhaust_family_after": 3,
            "hard_pivot_after": 5,
            "diversity_window": 6,
            "repeat_family_streak": 3,
            "dominant_family_share": 0.6,
            "default_stop_after": 100,
        },
        "meta": {
            "eval_window": 8,
            "exhaustion_threshold": 5,
            "experiment_min_sample": 3,
            "max_experiments_per_cycle": 2,
            "min_variants_before_experiment": 2,
            "promotion_effect_threshold": 1.05,
            "stagnation_threshold": 6,
        },
    },
}


# ── Build effective defaults based on project type ───────────────────────────


def _build_defaults(project_type: str) -> dict[str, Any]:
    """Merge the base defaults with the preset for the given project type."""
    preset = PRESETS.get(project_type, PRESETS["generic"])
    merged = dict(BASE_DEFAULTS)
    # Override validation if preset has it
    if "validation" in preset:
        merged["validation"] = dict(BASE_DEFAULTS["validation"])
        merged["validation"].update(preset["validation"])
    # Override modules if preset has them
    if "modules" in preset:
        merged["modules"] = list(preset["modules"])
    return merged


# ── Loader ───────────────────────────────────────────────────────────────────

_CONFIG_CACHE: dict[str, Any] | None = None


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config() -> dict[str, Any]:
    """Load autocode.yaml from the project root, falling back to type-aware defaults."""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE

    import yaml

    # Read user config
    user_config: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user_config = yaml.safe_load(f) or {}
        except Exception:
            user_config = {}

    # Determine project type
    project_type = user_config.get("project", {}).get("type") or BASE_DEFAULTS["project"]["type"]
    if project_type not in PRESETS:
        project_type = "generic"

    # Build defaults from the preset
    defaults = _build_defaults(project_type)

    # If user has explicit modules, preserve them (don't merge with preset)
    if "modules" in user_config and user_config["modules"]:
        pass  # user modules take precedence
    else:
        # Use preset modules as the base, but don't put them in the merged result
        # if the user didn't specify any — the config system already has them from _build_defaults
        pass

    # Merge: user config overrides defaults
    result = _deep_merge(defaults, user_config)

    _CONFIG_CACHE = result
    return result


def get_path(key: str, config: dict | None = None) -> Path:
    if config is None:
        config = load_config()
    rel = config.get("paths", {}).get(key, "")
    return ROOT / rel


def get_validation(key: str, config: dict | None = None) -> str:
    if config is None:
        config = load_config()
    val = config.get("validation", {}).get(key)
    if val:
        return val
    # Fall back to base default (may be from preset)
    pt = config.get("project", {}).get("type", "generic")
    preset = PRESETS.get(pt, PRESETS["generic"])
    return preset.get("validation", {}).get(key, BASE_DEFAULTS["validation"][key])


def get_project_name(config: dict | None = None) -> str:
    if config is None:
        config = load_config()
    return config.get("project", {}).get("name", "Unnamed Project")


def get_project_type(config: dict | None = None) -> str:
    if config is None:
        config = load_config()
    pt = config.get("project", {}).get("type", "generic")
    return pt if pt in PRESETS else "generic"


def get_project_goals(config: dict | None = None) -> dict:
    if config is None:
        config = load_config()
    return config.get("project_goals", BASE_DEFAULTS["project_goals"])


_MODULES_CACHE: list[dict] | None = None


def get_modules(config: dict | None = None) -> list[dict]:
    global _MODULES_CACHE
    cache_result = config is None
    if config is None:
        if _MODULES_CACHE is not None:
            return _MODULES_CACHE
        config = load_config()
    modules = config.get("modules")
    if modules:
        if cache_result:
            _MODULES_CACHE = modules
        return modules
    # Fall back to preset modules for the project type
    pt = get_project_type(config)
    preset = PRESETS.get(pt, PRESETS["generic"])
    preset_modules = preset.get("modules", PRESETS["generic"]["modules"])
    if cache_result:
        _MODULES_CACHE = preset_modules
    return preset_modules


def get_module_keywords() -> list[tuple[str, str, list[str]]]:
    return [(m["name"], m.get("description", ""), m.get("keywords", [])) for m in get_modules()]


def get_conventions(config: dict | None = None) -> list[str]:
    if config is None:
        config = load_config()
    return config.get("conventions") or BASE_DEFAULTS["conventions"]


def get_type_approaches(pt: str | None = None) -> list[str]:
    """Return approaches list for the given project type (or current project)."""
    if pt is None:
        pt = get_project_type()
    return list(TYPE_APPROACHES.get(pt, TYPE_APPROACHES["generic"]))


def get_type_objectives(pt: str | None = None) -> list[str]:
    """Return objectives list for the given project type (or current project)."""
    if pt is None:
        pt = get_project_type()
    return list(TYPE_OBJECTIVES.get(pt, TYPE_OBJECTIVES["generic"]))


def get_type_architectures(pt: str | None = None) -> list[str]:
    """Return architectures list for the given project type (or current project)."""
    if pt is None:
        pt = get_project_type()
    return list(TYPE_ARCHITECTURES.get(pt, TYPE_ARCHITECTURES["generic"]))


def get_type_novelty_angles(pt: str | None = None) -> list[str]:
    """Return novelty-angle suggestions for the given project type (or current project).

    These are surfaced when the loop is stuck (low diversity, exhausted families) and
    the controller is asking for a fresh angle. The list is project-type-aware so
    Python repos do not get told to 'optimize bundle splitting' or 'add WebSocket
    real-time sync'.
    """
    if pt is None:
        pt = get_project_type()
    return list(TYPE_NOVELTY_ANGLES.get(pt, TYPE_NOVELTY_ANGLES["generic"]))


def reload() -> None:
    global _CONFIG_CACHE, _MODULES_CACHE
    _CONFIG_CACHE = None
    _MODULES_CACHE = None
