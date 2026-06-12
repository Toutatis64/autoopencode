#!/usr/bin/env python3
"""
Meta-Autopilot — Hierarchical Self-Improvement Framework

A generic system that treats any part of the autopilot (prompts, algorithms,
parameters, code modules, architecture, LLM choices) as versioned *components*
and runs improvement experiments on them.  Works at any hierarchy level —
the same abstractions that improve the autopilot also improve the
meta-autopilot itself.

Architecture
────────────
ComponentRef       URI identifying a replaceable system part, e.g.
                   "autopilot:prompt:goal-autopilot" or "meta:algorithm:variant-generation"

Component[T]       A versioned, mutable component of type T with
                   performance history and variant genealogy.

MutationStrategy   Pluggable strategy for generating new component variants:
                   llm_rewrite, grid_search, bayesian, evolutionary, random.

Experiment         A/B test between control and treatment versions of a
                   component, run over N evaluation iterations.

HierarchyLevel     Scoped loop managing components at one level of the
                   improvement stack (0=autopilot, 1=meta, 2=meta.meta, …).

MetaController     Orchestrates all levels, detects stuck levels, and
                   decides when to promote an improvement to a higher level.

All state is stored in SQLite (component registry, experiments, performance)
with YAML checkpoints for human inspection.  No lock-in to any specific LLM
or algorithm — each mutation strategy is a self-contained callable.
"""

from __future__ import annotations

try:
    from autocode_config import ROOT, load_config, get_path
except ImportError:
    import sys
    from pathlib import Path
    _D = Path(__file__).resolve().parent
    if str(_D) not in sys.path:
        sys.path.insert(0, str(_D))
    from autocode_config import ROOT, load_config, get_path

_META_CONFIG = load_config()

import enum
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Generic, TypeVar

import yaml


# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

# ROOT imported from autocode_config
AUTOPILOT_DIR = Path(__file__).resolve().parent
META_DB = get_path("runtime_dir", _META_CONFIG) / "meta.sqlite3"
META_KB_PATH = get_path("meta_knowledge_base", _META_CONFIG)
COMPONENTS_DIR = get_path("components_dir", _META_CONFIG)

DEFAULT_COMPONENT_DIRS = {
    "prompts": COMPONENTS_DIR / "prompts",
    "algorithms": COMPONENTS_DIR / "algorithms",
    "parameters": COMPONENTS_DIR / "parameters",
    "models": COMPONENTS_DIR / "models",
}

# ──────────────────────────────────────────────────────────────────────────────
# Types
# ──────────────────────────────────────────────────────────────────────────────

T = TypeVar("T")

COMPETITIVENESS_RANK = {
    "promotable": 5, "promising": 4, "marginal": 3,
    "non_competitive": 1, "unknown": 2, "": 2,
}


# ──────────────────────────────────────────────────────────────────────────────
# Data Structures
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ComponentRef:
    """URI identifying a replaceable system part.

    Format:  <scope>:<type>:<name>
      scope   — hierarchy level path, e.g. "autopilot", "meta", "meta.meta"
      type    — "prompt", "algorithm", "parameter", "model", "module", "architecture"
      name    — snake_case identifier
    """
    scope: str
    component_type: str
    name: str

    def __str__(self) -> str:
        return f"{self.scope}:{self.component_type}:{self.name}"

    @classmethod
    def parse(cls, uri: str) -> ComponentRef:
        parts = uri.split(":", 2)
        if len(parts) != 3:
            raise ValueError(f"Invalid ComponentRef: {uri!r}")
        return cls(scope=parts[0], component_type=parts[1], name=parts[2])


@dataclass
class ComponentVariant(Generic[T]):
    """One concrete version of a component."""
    ref: ComponentRef
    version: str                      # semver-like, e.g. "1.0.0"
    content: T                        # the actual payload
    parent_version: str | None = None # what this was forked from
    mutation_strategy: str = "manual"
    mutation_params: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: _now())
    checksum: str = ""                 # auto-computed on first access

    def __post_init__(self) -> None:
        if not self.checksum:
            raw = json.dumps(asdict(self), sort_keys=True, default=str)
            self.checksum = hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class ComponentPerformance:
    """Performance metrics for a component version over a window."""
    ref: ComponentRef
    version: str
    start_iteration: int
    end_iteration: int
    metrics: dict[str, float]
    sample_count: int = 1
    evaluated_at: str = field(default_factory=lambda: _now())


@dataclass
class ComponentStatus:
    """Live status of a component in the registry."""
    ref: ComponentRef
    active_version: str
    exhausted: bool = False
    exhausted_reason: str = ""
    variant_count: int = 1
    last_evaluated: str = ""
    best_version: str = ""
    best_score: float = 0.0


class MutationType(str, enum.Enum):
    """Pluggable mutation strategy types."""
    LLM_REWRITE = "llm_rewrite"
    GRID_SEARCH = "grid_search"
    RANDOM_SEARCH = "random_search"
    BAYESIAN = "bayesian"
    EVOLUTIONARY = "evolutionary"
    PARAMETER_PERTURB = "parameter_perturb"
    ARCHITECTURE_SWAP = "architecture_swap"
    MANUAL = "manual"


@dataclass
class MutationStrategy:
    """How to generate a new variant of a component."""
    mutation_type: MutationType
    config: dict[str, Any] = field(default_factory=dict)
    applicable_types: list[str] = field(default_factory=lambda: ["prompt", "algorithm", "parameter"])

    def generate(self, component: ComponentVariant) -> ComponentVariant:
        """Apply this mutation strategy to produce a child variant.

        The default implementations are placeholders — in production each
        mutation_type dispatches to a registered generator (see STRATEGY_REGISTRY).
        """
        raise NotImplementedError(f"Strategy {self.mutation_type} not bound")


@dataclass
class OptimizationObjective:
    """What to optimize for at a given level."""
    name: str
    metric: str                        # key in the metrics dict
    weight: float = 1.0
    direction: str = "maximize"        # "maximize" | "minimize"

    def dominates(self, a: float, b: float) -> int:
        """Return 1 if a > b, -1 if b > a, 0 if equal (respecting direction)."""
        sign = 1 if self.direction == "maximize" else -1
        if a * sign > b * sign:
            return 1
        if b * sign > a * sign:
            return -1
        return 0


@dataclass
class ExperimentResult:
    """Result of comparing two component versions."""
    control_version: str
    treatment_version: str
    control_metrics: dict[str, float]
    treatment_metrics: dict[str, float]
    winner: str                       # "control" | "treatment" | "tie"
    effect_sizes: dict[str, float]
    sample_count: int
    concluded_at: str = field(default_factory=lambda: _now())


@dataclass
class Experiment:
    """An A/B test between two component versions."""
    ref: ComponentRef
    control_version: str
    treatment_version: str
    experiment_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    objectives: list[OptimizationObjective] = field(default_factory=list)
    eval_iterations: int = 10
    start_iteration: int = 0
    current_iteration: int = 0
    status: str = "pending"           # pending | running | completed | reverted | failed
    result: ExperimentResult | None = None
    created_at: str = field(default_factory=lambda: _now())

    @property
    def is_complete(self) -> bool:
        return self.status in ("completed", "reverted", "failed")


@dataclass
class HierarchyLevelConfig:
    """Configuration for one level of the hierarchy."""
    level: int = 0
    scope: str = "autopilot"
    objectives: list[OptimizationObjective] = field(default_factory=lambda: [
        OptimizationObjective("improvement_rate", "improvement_rate", 4.0),
        OptimizationObjective("iteration_efficiency", "iteration_efficiency", 2.0),
        OptimizationObjective("novelty_diversity", "novelty_diversity", 1.0),
    ])
    max_experiments_per_cycle: int = 1
    eval_window: int = 10
    min_variants_before_experiment: int = 1
    stagnation_threshold: int = 8      # consecutive iterations before forcing pivot
    exhaustion_threshold: int = 5      # failed experiments before exhausting a component


# ──────────────────────────────────────────────────────────────────────────────
# Strategy Registry — pluggable generators
# ──────────────────────────────────────────────────────────────────────────────

# Function signature for a strategy generator
MutationGenerator = Callable[[ComponentVariant, dict[str, Any]], ComponentVariant]
STRATEGY_REGISTRY: dict[MutationType, MutationGenerator] = {}


def register_strategy(mtype: MutationType) -> Callable:
    """Decorator to register a mutation strategy generator."""
    def wrapper(fn: MutationGenerator) -> MutationGenerator:
        STRATEGY_REGISTRY[mtype] = fn
        return fn
    return wrapper


def apply_mutation(
    variant: ComponentVariant,
    strategy: MutationType,
    params: dict[str, Any] | None = None,
) -> ComponentVariant:
    """Generate a child variant by applying the named mutation strategy."""
    generator = STRATEGY_REGISTRY.get(strategy)
    if generator is None:
        raise ValueError(f"No generator registered for mutation type: {strategy}")
    return generator(variant, params or {})


# ── Built-in mutation strategies ──────────────────────────────────────────

@register_strategy(MutationType.PARAMETER_PERTURB)
def _param_perturb(variant: ComponentVariant, params: dict[str, Any]) -> ComponentVariant:
    """Perturb numeric parameters by a random factor."""
    content = variant.content
    if isinstance(content, dict):
        new_content = dict(content)
        perturb_rate = params.get("rate", 0.3)
        perturb_magnitude = params.get("magnitude", 0.2)
        for key, value in new_content.items():
            if isinstance(value, (int, float)) and random.random() < perturb_rate:
                factor = 1.0 + random.uniform(-perturb_magnitude, perturb_magnitude)
                new_content[key] = type(value)(value * factor)
        version = _bump_version(variant.version, "patch")
        return ComponentVariant(
            ref=variant.ref,
            version=version,
            content=new_content,
            parent_version=variant.version,
            mutation_strategy="parameter_perturb",
            mutation_params=params,
        )
    raise TypeError(f"PARAMETER_PERTURB requires dict content, got {type(content)}")


@register_strategy(MutationType.RANDOM_SEARCH)
def _random_search(variant: ComponentVariant, params: dict[str, Any]) -> ComponentVariant:
    """Randomly sample parameter values from configured ranges."""
    content = variant.content
    if isinstance(content, dict):
        ranges = params.get("ranges", {})
        new_content = dict(content)
        for key, range_spec in ranges.items():
            lo, hi = range_spec[0], range_spec[1]
            if isinstance(lo, int) and isinstance(hi, int):
                new_content[key] = random.randint(lo, hi)
            else:
                new_content[key] = random.uniform(float(lo), float(hi))
        version = _bump_version(variant.version, "minor")
        return ComponentVariant(
            ref=variant.ref,
            version=version,
            content=new_content,
            parent_version=variant.version,
            mutation_strategy="random_search",
            mutation_params=params,
        )
    raise TypeError(f"RANDOM_SEARCH requires dict content, got {type(content)}")


@register_strategy(MutationType.EVOLUTIONARY)
def _evolutionary(variant: ComponentVariant, params: dict[str, Any]) -> ComponentVariant:
    """Cross-breed with another variant of the same component.

    Requires params['mate_version'] pointing to a sibling variant.
    For dict content: randomly picks values from either parent.
    For string content: interleaves segments.
    """
    mate_version = params.get("mate_version")
    if not mate_version:
        return _param_perturb(variant, params)  # fallback

    # mate content is resolved at call time by the caller
    mate_content = params.get("mate_content")
    if mate_content is None:
        return _param_perturb(variant, params)

    parent_a = variant.content
    parent_b = mate_content

    if isinstance(parent_a, dict) and isinstance(parent_b, dict):
        new_content = {}
        all_keys = set(parent_a) | set(parent_b)
        for key in all_keys:
            new_content[key] = random.choice([parent_a.get(key), parent_b.get(key)])
        version = _bump_version(variant.version, "minor")
        return ComponentVariant(
            ref=variant.ref,
            version=version,
            content=new_content,
            parent_version=variant.version,
            mutation_strategy="evolutionary",
            mutation_params=params,
        )
    raise TypeError(f"EVOLUTIONARY requires dict content, got {type(parent_a)}")


@register_strategy(MutationType.LLM_REWRITE)
def _llm_rewrite(variant: ComponentVariant, params: dict[str, Any]) -> ComponentVariant:
    """Generate a variant via LLM-driven rewrite scaffolding.

    This strategy creates variant scaffolding that embeds rewrite instructions
    for an LLM-powered agent to complete. It does NOT call an external API —
    instead it structures the variant so that when an agent selects it for use,
    the embedded meta-instructions guide the LLM to produce a semantically
    improved version.

    For string content (prompts, markdown, code):
      Appends an LLM-REWRITE comment block with the rewrite goal, instructions,
      and a placeholder for the rewritten content.

    For dict content (YAML/JSON configs):
      Wraps the existing config inside an '_llm_rewrite' envelope containing
      the rewrite goal, the original config, and instructions for the agent.

    Parameters:
      goal (str): Description of what the rewrite should achieve.
        Default: "Improve clarity, add structure, and enhance decision-guidance."
      target_sections (list[str], optional): Specific sections to rewrite.
      style (str, optional): Rewrite style — "elaborate", "condense", "restructure".
    """
    goal = params.get("goal", "Improve clarity, add structure, and enhance decision-guidance.")
    style = params.get("style", "restructure")
    target_sections = params.get("target_sections", None)

    if isinstance(variant.content, str):
        meta_block = (
            f"\n\n---\n"
            f"## LLM-REWRITE: {goal}\n"
            f"- **Style**: {style}\n"
            f"- **Target sections**: {target_sections or 'entire document'}\n"
            f"- **Instructions**: Rewrite the content above to better achieve the stated goal.\n"
            f"  Maintain all original semantic meaning. Improve clarity and actionability.\n"
            f"  Do not remove existing structural elements. Add new structure if needed.\n"
            f"- **Variant created**: {_now()}\n"
        )
        new_content = variant.content + meta_block
    elif isinstance(variant.content, dict):
        new_content = {
            "_llm_rewrite": {
                "goal": goal,
                "style": style,
                "target_sections": target_sections,
                "created_at": _now(),
                "instructions": (
                    "Review each parameter/section below. Adjust values, add new keys, "
                    "or restructure based on the rewrite goal."
                ),
            },
            "original": dict(variant.content),
        }
    else:
        raise TypeError(f"LLM_REWRITE requires str or dict content, got {type(variant.content)}")

    version = _bump_version(variant.version, "minor")
    return ComponentVariant(
        ref=variant.ref,
        version=version,
        content=new_content,
        parent_version=variant.version,
        mutation_strategy="llm_rewrite",
        mutation_params=params,
    )


# Component Registry (SQLite-backed)
# ──────────────────────────────────────────────────────────────────────────────

class ComponentRegistry:
    """Persistent store for component versions, performance, and status.

    Schema is intentionally generic: each component is identified by its
    ComponentRef URI, and all content is stored as JSONB (text).
    """

    def __init__(self, db_path: str | Path = META_DB) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS components (
                scope TEXT NOT NULL,
                type TEXT NOT NULL,
                name TEXT NOT NULL,
                active_version TEXT NOT NULL DEFAULT '',
                exhausted INTEGER NOT NULL DEFAULT 0,
                exhausted_reason TEXT NOT NULL DEFAULT '',
                variant_count INTEGER NOT NULL DEFAULT 1,
                last_evaluated TEXT NOT NULL DEFAULT '',
                best_version TEXT NOT NULL DEFAULT '',
                best_score REAL NOT NULL DEFAULT 0.0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(scope, type, name)
            );

            CREATE TABLE IF NOT EXISTS component_variants (
                scope TEXT NOT NULL,
                type TEXT NOT NULL,
                name TEXT NOT NULL,
                version TEXT NOT NULL,
                content_json TEXT NOT NULL,
                parent_version TEXT,
                checksum TEXT NOT NULL,
                mutation_strategy TEXT NOT NULL DEFAULT 'manual',
                mutation_params TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                UNIQUE(scope, type, name, version)
            );

            CREATE TABLE IF NOT EXISTS component_performance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                type TEXT NOT NULL,
                name TEXT NOT NULL,
                version TEXT NOT NULL,
                start_iteration INTEGER NOT NULL,
                end_iteration INTEGER NOT NULL,
                metrics_json TEXT NOT NULL,
                sample_count INTEGER NOT NULL DEFAULT 1,
                evaluated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS experiments (
                experiment_id TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                type TEXT NOT NULL,
                name TEXT NOT NULL,
                control_version TEXT NOT NULL,
                treatment_version TEXT NOT NULL,
                objectives_json TEXT NOT NULL,
                eval_iterations INTEGER NOT NULL DEFAULT 10,
                start_iteration INTEGER NOT NULL DEFAULT 0,
                current_iteration INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                result_json TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS hierarchy_levels (
                level INTEGER PRIMARY KEY,
                scope TEXT NOT NULL UNIQUE,
                config_json TEXT NOT NULL,
                parent_level INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_variants_ref
                ON component_variants(scope, type, name, version);
            CREATE INDEX IF NOT EXISTS idx_performance_ref
                ON component_performance(scope, type, name);
            CREATE INDEX IF NOT EXISTS idx_experiments_ref
                ON experiments(scope, type, name);
        """)
        self._conn.commit()

    # ── Component CRUD ────────────────────────────────────────────────────

    def register_component(
        self,
        ref: ComponentRef,
        initial_content: Any = None,
        version: str = "1.0.0",
    ) -> ComponentStatus:
        """Register a component (with optional initial variant)."""
        now = _now()
        self._conn.execute(
            """INSERT OR IGNORE INTO components
               (scope, type, name, active_version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (ref.scope, ref.component_type, ref.name, version, now, now),
        )
        if initial_content is not None:
            self.store_variant(ComponentVariant(
                ref=ref, version=version, content=initial_content,
            ))
            self._conn.execute(
                "UPDATE components SET active_version = ?, updated_at = ? WHERE scope=? AND type=? AND name=?",
                (version, now, ref.scope, ref.component_type, ref.name),
            )
        self._conn.commit()
        return self.get_component_status(ref)

    def get_component_status(self, ref: ComponentRef) -> ComponentStatus:
        row = self._conn.execute(
            "SELECT * FROM components WHERE scope=? AND type=? AND name=?",
            (ref.scope, ref.component_type, ref.name),
        ).fetchone()
        if row is None:
            return ComponentStatus(ref=ref, active_version="")
        return ComponentStatus(
            ref=ref,
            active_version=row["active_version"],
            exhausted=bool(row["exhausted"]),
            exhausted_reason=row["exhausted_reason"],
            variant_count=row["variant_count"],
            last_evaluated=row["last_evaluated"],
            best_version=row["best_version"],
            best_score=row["best_score"],
        )

    def list_components(self, scope: str | None = None) -> list[ComponentStatus]:
        if scope:
            rows = self._conn.execute(
                "SELECT * FROM components WHERE scope=? ORDER BY type, name",
                (scope,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM components ORDER BY scope, type, name",
            ).fetchall()
        return [
            ComponentStatus(
                ref=ComponentRef(r["scope"], r["type"], r["name"]),
                active_version=r["active_version"],
                exhausted=bool(r["exhausted"]),
                exhausted_reason=r["exhausted_reason"],
                variant_count=r["variant_count"],
                last_evaluated=r["last_evaluated"],
                best_version=r["best_version"],
                best_score=r["best_score"],
            )
            for r in rows
        ]

    def store_variant(self, variant: ComponentVariant) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO component_variants
               (scope, type, name, version, content_json, parent_version,
                checksum, mutation_strategy, mutation_params, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                variant.ref.scope, variant.ref.component_type, variant.ref.name,
                variant.version,
                json.dumps(asdict(variant), default=str),
                variant.parent_version,
                variant.checksum,
                variant.mutation_strategy,
                json.dumps(variant.mutation_params),
                variant.created_at,
            ),
        )
        # Update variant count
        count = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM component_variants WHERE scope=? AND type=? AND name=?",
            (variant.ref.scope, variant.ref.component_type, variant.ref.name),
        ).fetchone()["cnt"]
        self._conn.execute(
            "UPDATE components SET variant_count = ?, updated_at = ? WHERE scope=? AND type=? AND name=?",
            (count, _now(), variant.ref.scope, variant.ref.component_type, variant.ref.name),
        )
        self._conn.commit()

    def load_variant(
        self, ref: ComponentRef, version: str | None = None,
    ) -> ComponentVariant | None:
        if version is None:
            status = self.get_component_status(ref)
            version = status.active_version
            if not version:
                return None
        row = self._conn.execute(
            "SELECT * FROM component_variants WHERE scope=? AND type=? AND name=? AND version=?",
            (ref.scope, ref.component_type, ref.name, version),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_variant(row)

    def load_active_variant(self, ref: ComponentRef) -> ComponentVariant | None:
        return self.load_variant(ref, version=None)

    def list_variants(self, ref: ComponentRef) -> list[ComponentVariant]:
        rows = self._conn.execute(
            "SELECT * FROM component_variants WHERE scope=? AND type=? AND name=? ORDER BY version DESC",
            (ref.scope, ref.component_type, ref.name),
        ).fetchall()
        return [self._row_to_variant(r) for r in rows]

    def set_active_version(self, ref: ComponentRef, version: str) -> None:
        self._conn.execute(
            "UPDATE components SET active_version = ?, updated_at = ? WHERE scope=? AND type=? AND name=?",
            (version, _now(), ref.scope, ref.component_type, ref.name),
        )
        self._conn.commit()

    def mark_exhausted(self, ref: ComponentRef, reason: str) -> None:
        self._conn.execute(
            "UPDATE components SET exhausted = 1, exhausted_reason = ?, updated_at = ? WHERE scope=? AND type=? AND name=?",
            (reason, _now(), ref.scope, ref.component_type, ref.name),
        )
        self._conn.commit()

    def _row_to_variant(self, row: sqlite3.Row) -> ComponentVariant:
        content_json = json.loads(row["content_json"])
        ref = ComponentRef(row["scope"], row["type"], row["name"])
        return ComponentVariant(
            ref=ref,
            version=row["version"],
            content=content_json.get("content", content_json),
            parent_version=row["parent_version"],
            mutation_strategy=row["mutation_strategy"],
            mutation_params=json.loads(row["mutation_params"]) if row["mutation_params"] else {},
            created_at=row["created_at"],
            checksum=row["checksum"],
        )

    # ── Performance ───────────────────────────────────────────────────────

    def record_performance(self, perf: ComponentPerformance) -> None:
        self._conn.execute(
            """INSERT INTO component_performance
               (scope, type, name, version, start_iteration, end_iteration,
                metrics_json, sample_count, evaluated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                perf.ref.scope, perf.ref.component_type, perf.ref.name,
                perf.version, perf.start_iteration, perf.end_iteration,
                json.dumps(perf.metrics), perf.sample_count, perf.evaluated_at,
            ),
        )
        # Update best_version if this is the best so far
        total_score = sum(perf.metrics.values()) / max(len(perf.metrics), 1)
        current_best = self._conn.execute(
            "SELECT best_score FROM components WHERE scope=? AND type=? AND name=?",
            (perf.ref.scope, perf.ref.component_type, perf.ref.name),
        ).fetchone()
        if current_best and total_score > current_best["best_score"]:
            self._conn.execute(
                "UPDATE components SET best_version = ?, best_score = ?, last_evaluated = ? WHERE scope=? AND type=? AND name=?",
                (perf.version, total_score, _now(), perf.ref.scope, perf.ref.component_type, perf.ref.name),
            )
        self._conn.commit()

    def get_performance(
        self, ref: ComponentRef, version: str | None = None, limit: int = 20,
    ) -> list[ComponentPerformance]:
        if version:
            rows = self._conn.execute(
                "SELECT * FROM component_performance WHERE scope=? AND type=? AND name=? AND version=? ORDER BY end_iteration DESC LIMIT ?",
                (ref.scope, ref.component_type, ref.name, version, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM component_performance WHERE scope=? AND type=? AND name=? ORDER BY end_iteration DESC LIMIT ?",
                (ref.scope, ref.component_type, ref.name, limit),
            ).fetchall()
        return [
            ComponentPerformance(
                ref=ref,
                version=r["version"],
                start_iteration=r["start_iteration"],
                end_iteration=r["end_iteration"],
                metrics=json.loads(r["metrics_json"]),
                sample_count=r["sample_count"],
                evaluated_at=r["evaluated_at"],
            )
            for r in rows
        ]

    # ── Experiments ───────────────────────────────────────────────────────

    def create_experiment(self, exp: Experiment) -> None:
        self._conn.execute(
            """INSERT INTO experiments
               (experiment_id, scope, type, name, control_version, treatment_version,
                objectives_json, eval_iterations, start_iteration, current_iteration,
                status, result_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                exp.experiment_id,
                exp.ref.scope, exp.ref.component_type, exp.ref.name,
                exp.control_version, exp.treatment_version,
                json.dumps([asdict(o) for o in exp.objectives]),
                exp.eval_iterations, exp.start_iteration, exp.current_iteration,
                exp.status, json.dumps(exp.result) if exp.result else "",
                exp.created_at,
            ),
        )
        self._conn.commit()

    def update_experiment(self, exp: Experiment) -> None:
        self._conn.execute(
            """UPDATE experiments SET
               current_iteration = ?, status = ?, result_json = ?
               WHERE experiment_id = ?""",
            (
                exp.current_iteration, exp.status,
                json.dumps(asdict(exp.result)) if exp.result else "",
                exp.experiment_id,
            ),
        )
        self._conn.commit()

    def list_experiments(
        self, ref: ComponentRef | None = None, status: str | None = None,
    ) -> list[Experiment]:
        query = "SELECT * FROM experiments"
        params: list[Any] = []
        conditions: list[str] = []
        if ref:
            conditions.append("scope=? AND type=? AND name=?")
            params.extend([ref.scope, ref.component_type, ref.name])
        if status:
            conditions.append("status=?")
            params.append(status)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC"
        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_experiment(r) for r in rows]

    def _row_to_experiment(self, row: sqlite3.Row) -> Experiment:
        ref = ComponentRef(row["scope"], row["type"], row["name"])
        objectives = [
            OptimizationObjective(**o)
            for o in json.loads(row["objectives_json"])
        ]
        result_raw = row["result_json"]
        result = ExperimentResult(**json.loads(result_raw)) if result_raw else None
        return Experiment(
            experiment_id=row["experiment_id"],
            ref=ref,
            control_version=row["control_version"],
            treatment_version=row["treatment_version"],
            objectives=objectives,
            eval_iterations=row["eval_iterations"],
            start_iteration=row["start_iteration"],
            current_iteration=row["current_iteration"],
            status=row["status"],
            result=result,
            created_at=row["created_at"],
        )

    # ── Hierarchy Levels ──────────────────────────────────────────────────

    def register_level(
        self, level: int, scope: str, config: HierarchyLevelConfig,
        parent_level: int | None = None,
    ) -> None:
        now = _now()
        self._conn.execute(
            """INSERT OR REPLACE INTO hierarchy_levels
               (level, scope, config_json, parent_level, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (level, scope, json.dumps(asdict(config)), parent_level, now, now),
        )
        self._conn.commit()

    def get_level(self, level: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM hierarchy_levels WHERE level = ?",
            (level,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def list_levels(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM hierarchy_levels ORDER BY level ASC",
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()


# ──────────────────────────────────────────────────────────────────────────────
# Hierarchy Level — scoped meta-loop
# ──────────────────────────────────────────────────────────────────────────────

class HierarchyLevel:
    """One level of the meta-autopilot hierarchy.

    Each level manages components within its *scope* and runs experiments
    to improve them.  Level 0 = autopilot (trading strategies), Level 1 = meta
    (improving autopilot components), Level 2 = meta.meta (improving meta), etc.
    """

    def __init__(
        self,
        level: int,
        scope: str,
        registry: ComponentRegistry,
        config: HierarchyLevelConfig | None = None,
        parent_level: int | None = None,
    ) -> None:
        self.level = level
        self.scope = scope
        self.registry = registry
        self.config = config or HierarchyLevelConfig(level=level, scope=scope)
        self.parent_level = parent_level
        self._register()

    def _register(self) -> None:
        self.registry.register_level(self.level, self.scope, self.config, self.parent_level)
        _log(f"Level {self.level} registered | scope={self.scope} | "
             f"max_experiments={self.config.max_experiments_per_cycle} eval_window={self.config.eval_window}")

    @property
    def component_refs(self) -> list[ComponentRef]:
        """All components at this level's scope."""
        return [
            c.ref for c in self.registry.list_components(scope=self.scope)
        ]

    def discover_components(self) -> list[ComponentRef]:
        """Auto-discover components from the filesystem convention.

        Scans COMPONENTS_DIR / <scope> / <type> / <name>.{yaml|json|md|py}
        and registers any that aren't already tracked.
        """
        discovered: list[ComponentRef] = []
        base = COMPONENTS_DIR / self.scope
        if not base.exists():
            return discovered
        for comp_type_dir in base.iterdir():
            if not comp_type_dir.is_dir():
                continue
            comp_type = comp_type_dir.name
            for entry in comp_type_dir.iterdir():
                name = entry.stem
                ref = ComponentRef(self.scope, comp_type, name)
                status = self.registry.get_component_status(ref)
                if not status.active_version:
                    content = _load_file_content(entry)
                    self.registry.register_component(ref, content)
                    discovered.append(ref)
        if discovered:
            _log(f"Discovered {len(discovered)} new components at {self.scope}: "
                 f"{[str(d) for d in discovered]}")
        return discovered

    def create_variant(
        self,
        ref: ComponentRef,
        strategy: MutationType = MutationType.PARAMETER_PERTURB,
        params: dict[str, Any] | None = None,
    ) -> ComponentVariant | None:
        """Create a new variant of a component using a mutation strategy."""
        active = self.registry.load_active_variant(ref)
        if active is None:
            return None
        try:
            child = apply_mutation(active, strategy, params)
        except (NotImplementedError, TypeError, ValueError) as exc:
            return None
        self.registry.store_variant(child)
        return child

    def start_experiment(
        self,
        ref: ComponentRef,
        treatment_version: str,
        eval_iterations: int | None = None,
    ) -> Experiment | None:
        """Start an A/B experiment comparing active vs treatment."""
        status = self.registry.get_component_status(ref)
        if not status.active_version:
            return None
        if status.exhausted:
            return None

        # Check for overlapping running experiments
        running = self.registry.list_experiments(ref, status="running")
        if running:
            return None

        exp = Experiment(
            ref=ref,
            control_version=status.active_version,
            treatment_version=treatment_version,
            objectives=self.config.objectives,
            eval_iterations=eval_iterations or self.config.eval_window,
            start_iteration=0,
            status="running",
        )
        self.registry.create_experiment(exp)
        return exp

    def advance_experiment(self, exp: Experiment) -> None:
        """Advance experiment by one evaluation iteration."""
        exp.current_iteration += 1
        if exp.current_iteration >= exp.eval_iterations:
            self._conclude_experiment(exp)
        else:
            self.registry.update_experiment(exp)

    def _conclude_experiment(self, exp: Experiment) -> None:
        """Evaluate control vs treatment and decide winner."""
        control_perf = self.registry.get_performance(exp.ref, exp.control_version, limit=exp.eval_iterations)
        treatment_perf = self.registry.get_performance(exp.ref, exp.treatment_version, limit=exp.eval_iterations)

        control_metrics = self._aggregate_metrics(control_perf)
        treatment_metrics = self._aggregate_metrics(treatment_perf)

        winner = "tie"
        effect_sizes: dict[str, float] = {}
        for obj in exp.objectives:
            c_val = control_metrics.get(obj.metric, 0.0)
            t_val = treatment_metrics.get(obj.metric, 0.0)
            effect_sizes[obj.metric] = t_val - c_val if obj.direction == "maximize" else c_val - t_val

        # Simple majority vote across objectives (weighted)
        control_score = sum(
            control_metrics.get(obj.metric, 0.0) * obj.weight for obj in exp.objectives
        )
        treatment_score = sum(
            treatment_metrics.get(obj.metric, 0.0) * obj.weight for obj in exp.objectives
        )
        if treatment_score > control_score * 1.05:
            winner = "treatment"
        elif control_score > treatment_score * 1.05:
            winner = "control"

        n = max(len(control_perf), len(treatment_perf), 1)
        exp.result = ExperimentResult(
            control_version=exp.control_version,
            treatment_version=exp.treatment_version,
            control_metrics=control_metrics,
            treatment_metrics=treatment_metrics,
            winner=winner,
            effect_sizes=effect_sizes,
            sample_count=n,
        )
        exp.status = "completed"

        if winner == "treatment":
            self.registry.set_active_version(exp.ref, exp.treatment_version)
            _log(f"  ✓ Experiment {exp.experiment_id}: {exp.ref} — TREATMENT WINS "
                 f"(control={exp.control_version} → treatment={exp.treatment_version})")
        elif winner == "control":
            _log(f"  - Experiment {exp.experiment_id}: {exp.ref} — control retains "
                 f"(control={exp.control_version} beat treatment={exp.treatment_version})")
        else:
            _log(f"  ~ Experiment {exp.experiment_id}: {exp.ref} — TIE "
                 f"(control={exp.control_version} vs treatment={exp.treatment_version}, "
                 f"score {control_score:.2f} vs {treatment_score:.2f})")
        # Record performance for both versions
        for version, metrics in [(exp.control_version, control_metrics), (exp.treatment_version, treatment_metrics)]:
            self.registry.record_performance(ComponentPerformance(
                ref=exp.ref,
                version=version,
                start_iteration=max(0, exp.current_iteration - exp.eval_iterations),
                end_iteration=exp.current_iteration,
                metrics=metrics,
                sample_count=n,
            ))

        self.registry.update_experiment(exp)

    def _aggregate_metrics(
        self, performances: list[ComponentPerformance],
    ) -> dict[str, float]:
        if not performances:
            return {}
        aggregated: dict[str, float] = defaultdict(float)
        for p in performances:
            for k, v in p.metrics.items():
                aggregated[k] += v
        n = max(len(performances), 1)
        return {k: v / n for k, v in aggregated.items()}

    def get_experiment_candidates(self) -> list[tuple[ComponentRef, ComponentVariant]]:
        """Find components with multiple variants that could be A/B tested.

        Returns (ref, treatment_variant) pairs where the treatment is
        a variant not currently active and not already in an experiment.
        """
        candidates: list[tuple[ComponentRef, ComponentVariant]] = []
        for ref in self.component_refs:
            status = self.registry.get_component_status(ref)
            if status.exhausted or not status.active_version:
                continue
            if status.variant_count < 2:
                continue

            # Check no running experiment for this component
            running = self.registry.list_experiments(ref, status="running")
            if running:
                continue

            # Best variant that isn't the active one
            variants = self.registry.list_variants(ref)
            best_non_active: ComponentVariant | None = None
            best_score = -1e9
            for v in variants:
                if v.version == status.active_version:
                    continue
                perf = self.registry.get_performance(ref, v.version, limit=5)
                if perf:
                    score = sum(p.metrics.get("composite", 0.0) for p in perf) / len(perf)
                else:
                    # Untested variant — high priority for testing
                    score = 999.0
                if score > best_score:
                    best_score = score
                    best_non_active = v

            if best_non_active is not None:
                candidates.append((ref, best_non_active))

        candidates.sort(key=lambda x: self._variant_urgency(x[0], x[1]), reverse=True)
        return candidates

    def _variant_urgency(self, ref: ComponentRef, variant: ComponentVariant) -> float:
        """Score how urgently this variant should be tested (higher = sooner)."""
        perf = self.registry.get_performance(ref, variant.version, limit=3)
        if not perf:
            return 10.0  # untested variants get highest priority
        avg = sum(p.metrics.get("composite", 0.0) for p in perf) / len(perf)
        return 10.0 - avg  # lower performing variants get checked sooner


    def detect_stagnation(self) -> float:
        """Return stagnation score (0.0–1.0) for this level.

        Based on:
        - Consecutive failed or inconclusive experiments
        - Components stuck in exhausted state
        - No new variants created recently
        """
        experiments = self.registry.list_experiments()
        recent = [e for e in experiments if e.status == "completed"][:12]
        if not recent:
            return 0.0
        failed = sum(1 for e in recent if e.status in ("failed", "reverted"))
        return min(1.0, failed / max(len(recent), 1))

    def summary(self) -> dict[str, Any]:
        """Return a machine-readable summary of this level's state."""
        comps = self.registry.list_components(scope=self.scope)
        exps = self.registry.list_experiments()
        running_exps = [e for e in exps if e.status == "running"]
        return {
            "level": self.level,
            "scope": self.scope,
            "components": len(comps),
            "total_variants": sum(c.variant_count for c in comps),
            "exhausted_components": sum(1 for c in comps if c.exhausted),
            "experiments_total": len(exps),
            "experiments_running": len(running_exps),
            "stagnation_score": round(self.detect_stagnation(), 3),
            "config": asdict(self.config),
        }


# ──────────────────────────────────────────────────────────────────────────────
# Meta Controller — orchestrates all levels
# ──────────────────────────────────────────────────────────────────────────────

class MetaController:
    """Orchestrates the hierarchy of improvement levels.

    Responsibilities:
    - Coordinates between levels (promotes wins, cascades changes)
    - Detects when a level is stuck and triggers meta-improvement
    - Decides whether a Level N improvement should become a Level N+1 component
    - Provides the unified experiment/status API

    This is the entry point for external loops (run_meta_autopilot.py).
    """

    def __init__(self, registry: ComponentRegistry | None = None) -> None:
        self.registry = registry or ComponentRegistry()
        self.levels: dict[int, HierarchyLevel] = {}
        _log("MetaController initialized")

    def get_or_create_level(
        self, level: int, config: HierarchyLevelConfig | None = None,
    ) -> HierarchyLevel:
        if level not in self.levels:
            scope = self._scope_for_level(level)
            parent = level - 1 if level > 0 else None
            self.levels[level] = HierarchyLevel(
                level=level,
                scope=scope,
                registry=self.registry,
                config=config,
                parent_level=parent,
            )
        return self.levels[level]

    @staticmethod
    def _scope_for_level(level: int) -> str:
        if level == 0:
            return "autopilot"
        return "meta" + ".meta" * (level - 1)

    def level_for_scope(self, scope: str) -> int:
        if scope == "autopilot":
            return 0
        if scope == "meta":
            return 1
        if scope.startswith("meta."):
            return 1 + scope.count(".meta")
        return 0

    def _derive_cycle_metrics(self, level: int) -> dict[str, float]:
        """Derive objective system metrics from registry state.

        These auto-generated metrics provide the comparison data that A/B
        experiments need to evaluate control vs treatment performance.
        Without this, no metrics are recorded during cycle() and
        experiments always conclude as ties on empty data.
        """
        hl = self.get_or_create_level(level)
        refs = hl.component_refs
        total_variants = sum(
            len(self.registry.list_variants(ref)) for ref in refs
        ) if refs else 0

        running_exps = self.registry.list_experiments(status="running")
        stagnation = hl.detect_stagnation()

        return {
            "iteration_efficiency": 1.0,
            "improvement_rate": round(total_variants / max(len(refs), 1), 2) if refs else 0.0,
            "novelty_diversity": round(len(set(ref.component_type for ref in refs)), 2),
            "variant_accumulation": total_variants,
            "experiment_coverage": len(running_exps),
            "stagnation_score": round(stagnation, 3),
        }

    def register_component(
        self,
        ref: ComponentRef,
        initial_content: Any = None,
        version: str = "1.0.0",
    ) -> ComponentStatus:
        return self.registry.register_component(ref, initial_content, version)

    def auto_discover(self, level: int | None = None) -> list[ComponentRef]:
        """Auto-discover components from filesystem for one or all levels."""
        discovered: list[ComponentRef] = []
        if level is not None:
            hl = self.get_or_create_level(level)
            discovered.extend(hl.discover_components())
        else:
            # Discover from all registered levels
            for lev in list(self.levels):
                discovered.extend(self.levels[lev].discover_components())
            # Also scan the filesystem for any scope directories
            for scope_dir in COMPONENTS_DIR.iterdir():
                if scope_dir.is_dir():
                    scope = scope_dir.name
                    lev = self.level_for_scope(scope)
                    hl = self.get_or_create_level(lev)
                    discovered.extend(hl.discover_components())
        return discovered

    def run_cycle(self, level: int, metrics: dict[str, float] | None = None) -> dict[str, Any]:
        """Execute one improvement cycle at the given level.

        1. Check for running experiments → advance them
        2. Record current metrics against active components
        3. Find experiment candidates → start new experiments
        4. Check stagnation → auto-escalate if stuck
        5. Return cycle summary
        """
        hl = self.get_or_create_level(level)
        scope_name = hl.scope
        _log(f"Cycle start | level={level} scope={scope_name}")

        # Step 1: Advance running experiments
        running = self.registry.list_experiments(status="running")
        running_at_level = [e for e in running if self.level_for_scope(e.ref.scope) == level]
        _log(f"  Running experiments: {len(running_at_level)} at level={level} ({len(running)} total in DB)")

        for exp in running_at_level:
            _log(f"  Advancing experiment {exp.experiment_id}: {exp.ref} | "
                 f"control={exp.control_version} vs treatment={exp.treatment_version} "
                 f"({exp.current_iteration}/{exp.eval_iterations})")
            hl.advance_experiment(exp)

        # Step 2: Record auto-derived cycle metrics for experiment evaluation
        cycle_metrics = self._derive_cycle_metrics(level)
        if metrics:
            cycle_metrics.update(metrics)
        _log(f"  Cycle metrics: variant_accumulation={cycle_metrics.get('variant_accumulation', '?')} "
             f"experiment_coverage={cycle_metrics.get('experiment_coverage', '?')} "
             f"stagnation={cycle_metrics.get('stagnation_score', '?')}")

        # Build a map: ref -> iteration number from running experiments
        ref_iter_map: dict[str, int] = {}
        for exp in running_at_level:
            ref_iter_map[str(exp.ref)] = exp.current_iteration

        # Track which (ref, version) pairs we've already recorded
        recorded: set[tuple[str, str]] = set()

        for ref in hl.component_refs:
            active = self.registry.load_active_variant(ref)
            if active:
                it = ref_iter_map.get(str(ref), 1)
                hl.registry.record_performance(ComponentPerformance(
                    ref=ref,
                    version=active.version,
                    start_iteration=it - 1,
                    end_iteration=it,
                    metrics=cycle_metrics,
                ))
                recorded.add((str(ref), active.version))
                _log(f"  Recorded performance: {ref} version={active.version} iter={it}")

        # Also record metrics for treatment versions of running experiments
        # Without this, experiments always conclude as false control wins
        # because treatment has zero accumulated performance records.
        for exp in running_at_level:
            treatment = self.registry.load_variant(exp.ref, exp.treatment_version)
            if treatment and (str(exp.ref), exp.treatment_version) not in recorded:
                it = exp.current_iteration
                hl.registry.record_performance(ComponentPerformance(
                    ref=exp.ref,
                    version=exp.treatment_version,
                    start_iteration=it - 1,
                    end_iteration=it,
                    metrics=cycle_metrics,
                ))
                _log(f"  Recorded treatment performance: {exp.ref} version={exp.treatment_version} iter={it}")

        # Step 3: Start new experiments if possible
        candidates = hl.get_experiment_candidates()
        _log(f"  Experiment candidates found: {len(candidates)}")
        started = 0
        for ref, treatment in candidates:
            if started >= hl.config.max_experiments_per_cycle:
                _log(f"  Max experiments per cycle ({hl.config.max_experiments_per_cycle}) reached, stopping")
                break
            exp = hl.start_experiment(ref, treatment.version)
            if exp:
                started += 1
                _log(f"  Started experiment {exp.experiment_id}: {ref} | "
                     f"control={exp.control_version} treatment={exp.treatment_version} "
                     f"eval={exp.eval_iterations} iters")
            else:
                _log(f"  Could not start experiment for {ref} version={treatment.version} "
                     f"(may have running experiment or exhausted component)")

        # Step 4: Check stagnation → escalate
        stagnation = hl.detect_stagnation()
        _log(f"  Stagnation score: {stagnation:.3f} (threshold for escalation: 0.7)")
        escalation: dict[str, Any] = {"escalated": False}
        if stagnation > 0.7 and level < 5:  # don't escalate beyond depth 5
            meta_level = level + 1
            meta_scope = self._scope_for_level(meta_level)
            meta_ref = ComponentRef(meta_scope, "algorithm", f"level-{level}-controller")
            escalation = {
                "escalated": True,
                "meta_level": meta_level,
                "meta_ref": str(meta_ref),
                "reason": f"Level {level} stagnation score {stagnation:.2f} exceeds 0.7 threshold",
            }
            _log(f"  ⚠ ESCALATION: {escalation['reason']} — promoting to level {meta_level}")

        result = {
            "level": level,
            "scope": hl.scope,
            "experiments_advanced": len(running_at_level),
            "experiments_started": started,
            "stagnation_score": round(stagnation, 3),
            **escalation,
        }
        _log(f"Cycle complete | level={level} | advanced={len(running_at_level)} started={started} "
             f"stagnation={stagnation:.3f} escalated={escalation['escalated']}")
        return result

    def summary(self) -> dict[str, Any]:
        levels_summary = {
            str(lev): hl.summary()
            for lev, hl in sorted(self.levels.items())
        }
        return {
            "active_levels": list(self.levels),
            "levels": levels_summary,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Initialization — register built-in components
# ──────────────────────────────────────────────────────────────────────────────

def initialize_defaults(controller: MetaController | None = None) -> MetaController:
    """Register default components from the filesystem convention.

    Scans:
      .opencode/autopilot/components/<scope>/<type>/<name>.{yaml,json,md,py}
    for each level.

    Also registers the meta-autopilot's own components so it can self-improve.
    """
    ctrl = controller or MetaController()
    ctrl.get_or_create_level(0)   # autopilot
    ctrl.get_or_create_level(1)   # meta
    ctrl.auto_discover()
    return ctrl


# ──────────────────────────────────────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    """Print a timestamped progress line to stdout immediately.

    Uses the same format as run_autopilot.py's _log so that logs from
    meta-inner cycles are visually consistent with outer-loop logs.
    The '[meta]' prefix is deliberately omitted here because callers
    (run_meta_autopilot.sh, run_autopilot.py) already add their own
    context labels via shell piping or separate _log functions.
    """
    print(f"[{_now()}] {msg}", flush=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _bump_version(version: str, part: str = "patch") -> str:
    parts = version.split(".")
    try:
        if part == "patch" and len(parts) >= 3:
            parts[2] = str(int(parts[2]) + 1)
        elif part == "minor" and len(parts) >= 2:
            parts[1] = str(int(parts[1]) + 1)
            if len(parts) >= 3:
                parts[2] = "0"
        elif part == "major" and len(parts) >= 1:
            parts[0] = str(int(parts[0]) + 1)
            if len(parts) >= 2:
                parts[1] = "0"
            if len(parts) >= 3:
                parts[2] = "0"
    except ValueError:
        return version + ".1"
    return ".".join(parts)


def _load_file_content(path: Path) -> Any:
    """Load a component file from disk, returning parsed content."""
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    elif suffix == ".json":
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    elif suffix in (".md", ".txt"):
        return path.read_text(encoding="utf-8")
    elif suffix == ".py":
        return path.read_text(encoding="utf-8")
    return path.read_text(encoding="utf-8")


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def print_status(controller: MetaController) -> None:
    """Print a human-readable status of the meta-autopilot."""
    summary = controller.summary()
    print("=" * 60)
    print("  Meta-Autopilot Status")
    print("=" * 60)
    for scope_key, level_data in summary.get("levels", {}).items():
        print(f"\n  Level {level_data['level']}: {level_data['scope']}")
        print(f"    Components: {level_data['components']} | "
              f"Variants: {level_data['total_variants']} | "
              f"Exhausted: {level_data['exhausted_components']}")
        print(f"    Experiments: {level_data['experiments_total']} total, "
              f"{level_data['experiments_running']} running")
        stag = level_data.get("stagnation_score", 0)
        stag_mark = "⚠" if stag > 0.5 else "✓"
        print(f"    Stagnation: {stag_mark} {stag:.1%}")

    print(f"\n  Active levels: {summary.get('active_levels', [])}")
    print("=" * 60)


cli_doc = """
Meta-Autopilot CLI

Usage:
  python3 meta_autopilot.py init       Initialize default components
  python3 meta_autopilot.py status     Print current status
  python3 meta_autopilot.py cycle --level N [--metrics '{"k": v}']
                                       Run one improvement cycle
  python3 meta_autopilot.py discover   Auto-discover components from filesystem
"""


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Meta-Autopilot CLI")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="Initialize default components")
    sub.add_parser("status", help="Print current status")
    sub.add_parser("discover", help="Auto-discover components from filesystem")

    cycle = sub.add_parser("cycle", help="Run one improvement cycle")
    cycle.add_argument("--level", type=int, default=1, help="Hierarchy level")
    cycle.add_argument("--metrics", type=str, help="JSON metrics dict")

    args = parser.parse_args()

    ctrl = initialize_defaults()

    if args.command == "init":
        count = len(ctrl.auto_discover())
        print(f"Initialized {count} components.")
        return 0
    elif args.command == "status":
        print_status(ctrl)
        return 0
    elif args.command == "discover":
        found = ctrl.auto_discover()
        print(f"Discovered {len(found)} components:")
        for ref in found:
            print(f"  {ref}")
        return 0
    elif args.command == "cycle":
        metrics = json.loads(args.metrics) if args.metrics else None
        result = ctrl.run_cycle(args.level, metrics)
        print(json.dumps(result, indent=2))
        return 0
    else:
        print(cli_doc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
