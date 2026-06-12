from __future__ import annotations

import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from scripts.meta_autopilot import (
    STRATEGY_REGISTRY,
    ComponentPerformance,
    ComponentRef,
    ComponentRegistry,
    ComponentVariant,
    Experiment,
    ExperimentResult,
    HierarchyLevel,
    HierarchyLevelConfig,
    MetaController,
    MutationStrategy,
    MutationType,
    OptimizationObjective,
    _bump_version,
    _evolutionary,
    _llm_rewrite,
    _load_file_content,
    _param_perturb,
    _random_search,
    apply_mutation,
    initialize_defaults,
    main,
    print_status,
    register_strategy,
)


class TestComponentRef:
    def test_str(self) -> None:
        ref = ComponentRef(scope="autopilot", component_type="prompt", name="goal")
        assert str(ref) == "autopilot:prompt:goal"

    def test_parse_valid(self) -> None:
        ref = ComponentRef.parse("meta:algorithm:variant-generation")
        assert ref.scope == "meta"
        assert ref.component_type == "algorithm"
        assert ref.name == "variant-generation"

    def test_parse_too_few_parts(self) -> None:
        try:
            ComponentRef.parse("too:few")
            assert False, "expected ValueError"
        except ValueError:
            pass

    def test_parse_too_many_parts(self) -> None:
        ref = ComponentRef.parse("a:b:c:d")
        assert ref.scope == "a"
        assert ref.component_type == "b"
        assert ref.name == "c:d"

    def test_parse_empty_string(self) -> None:
        try:
            ComponentRef.parse("")
            assert False, "expected ValueError"
        except ValueError:
            pass

    def test_equality(self) -> None:
        a = ComponentRef("s", "t", "n")
        b = ComponentRef("s", "t", "n")
        c = ComponentRef("s", "t", "other")
        assert a == b
        assert a != c

    def test_hashable(self) -> None:
        refs = {ComponentRef("s", "t", "n"), ComponentRef("s", "t", "n")}
        assert len(refs) == 1


class TestComponentVariant:
    def test_default_parent_version(self) -> None:
        ref = ComponentRef("s", "t", "n")
        v = ComponentVariant(ref=ref, version="1.0.0", content="hello")
        assert v.parent_version is None
        assert v.mutation_strategy == "manual"
        assert v.mutation_params == {}

    def test_content_types(self) -> None:
        ref = ComponentRef("s", "t", "n")
        for content in ["hello", {"key": "val"}, 42, [1, 2, 3], None]:
            v = ComponentVariant(ref=ref, version="1.0.0", content=content)
            assert v.content == content

    def test_created_at_set(self) -> None:
        ref = ComponentRef("s", "t", "n")
        v = ComponentVariant(ref=ref, version="1.0.0", content="x")
        assert isinstance(v.created_at, str)
        assert "T" in v.created_at


class TestMutationType:
    def test_enum_values(self) -> None:
        assert MutationType.LLM_REWRITE.value == "llm_rewrite"
        assert MutationType.PARAMETER_PERTURB.value == "parameter_perturb"
        assert MutationType.RANDOM_SEARCH.value == "random_search"
        assert MutationType.EVOLUTIONARY.value == "evolutionary"
        assert MutationType.GRID_SEARCH.value == "grid_search"
        assert MutationType.BAYESIAN.value == "bayesian"

    def test_members_defined(self) -> None:
        expected = {
            "LLM_REWRITE",
            "PARAMETER_PERTURB",
            "RANDOM_SEARCH",
            "EVOLUTIONARY",
            "GRID_SEARCH",
            "BAYESIAN",
            "ARCHITECTURE_SWAP",
            "MANUAL",
        }
        assert {m.name for m in MutationType} == expected


class TestMutationStrategy:
    def test_default_applicable_types(self) -> None:
        s = MutationStrategy(mutation_type=MutationType.RANDOM_SEARCH)
        assert s.applicable_types == ["prompt", "algorithm", "parameter"]

    def test_generate_raises_not_implemented(self) -> None:
        s = MutationStrategy(mutation_type=MutationType.GRID_SEARCH)
        ref = ComponentRef("s", "t", "n")
        v = ComponentVariant(ref=ref, version="1.0.0", content="x")
        try:
            s.generate(v)
            assert False, "expected NotImplementedError"
        except NotImplementedError:
            pass


class TestOptimizationObjective:
    def test_dominates_maximize_a_greater(self) -> None:
        obj = OptimizationObjective(name="score", metric="score", direction="maximize")
        assert obj.dominates(10.0, 5.0) == 1

    def test_dominates_maximize_b_greater(self) -> None:
        obj = OptimizationObjective(name="score", metric="score", direction="maximize")
        assert obj.dominates(5.0, 10.0) == -1

    def test_dominates_maximize_equal(self) -> None:
        obj = OptimizationObjective(name="score", metric="score", direction="maximize")
        assert obj.dominates(7.0, 7.0) == 0

    def test_dominates_minimize_a_smaller(self) -> None:
        obj = OptimizationObjective(name="loss", metric="loss", direction="minimize")
        assert obj.dominates(5.0, 10.0) == 1

    def test_dominates_minimize_b_smaller(self) -> None:
        obj = OptimizationObjective(name="loss", metric="loss", direction="minimize")
        assert obj.dominates(10.0, 5.0) == -1

    def test_dominates_minimize_equal(self) -> None:
        obj = OptimizationObjective(name="loss", metric="loss", direction="minimize")
        assert obj.dominates(7.0, 7.0) == 0

    def test_dominates_default_weight(self) -> None:
        obj = OptimizationObjective(name="score", metric="score")
        assert obj.weight == 1.0
        assert obj.direction == "maximize"


class TestRegisterStrategy:
    def test_registration_and_dispatch(self) -> None:
        assert MutationType.PARAMETER_PERTURB in STRATEGY_REGISTRY
        assert MutationType.RANDOM_SEARCH in STRATEGY_REGISTRY
        assert MutationType.EVOLUTIONARY in STRATEGY_REGISTRY

    def test_decorator_registers(self) -> None:
        seen_before = MutationType.GRID_SEARCH in STRATEGY_REGISTRY
        custom_type = MutationType.GRID_SEARCH

        @register_strategy(custom_type)
        def _custom_strategy(variant: ComponentVariant, params: dict[str, str]) -> ComponentVariant:
            return ComponentVariant(
                ref=variant.ref,
                version="0.0.0",
                content="custom",
                parent_version=variant.version,
                mutation_strategy="custom",
            )

        assert custom_type in STRATEGY_REGISTRY
        assert STRATEGY_REGISTRY[custom_type] is _custom_strategy

        if not seen_before:
            del STRATEGY_REGISTRY[custom_type]


class TestApplyMutation:
    def test_applies_registered_strategy(self) -> None:
        ref = ComponentRef("s", "t", "n")
        variant = ComponentVariant(ref=ref, version="1.0.0", content={"x": 1.0})
        result = apply_mutation(variant, MutationType.PARAMETER_PERTURB, {"rate": 0.0})
        assert result.ref == ref
        assert result.parent_version == "1.0.0"
        assert result.mutation_strategy == "parameter_perturb"

    def test_raises_for_unregistered_strategy(self) -> None:
        ref = ComponentRef("s", "t", "n")
        variant = ComponentVariant(ref=ref, version="1.0.0", content="x")
        try:
            apply_mutation(variant, MutationType.GRID_SEARCH)
            assert False, "expected ValueError"
        except ValueError:
            pass


class TestParamPerturb:
    def test_perturbs_numeric_values(self) -> None:
        ref = ComponentRef("s", "t", "n")
        variant = ComponentVariant(ref=ref, version="1.0.0", content={"lr": 0.01, "bs": 32})
        # force ALL numeric values to be perturbed (rate=1.0, magnitude=0.0)
        result = _param_perturb(variant, {"rate": 1.0, "magnitude": 0.0})
        assert result.content["lr"] == 0.01
        assert result.content["bs"] == 32
        assert result.version == "1.0.1"
        assert result.parent_version == "1.0.0"
        assert result.mutation_strategy == "parameter_perturb"

    def test_raises_for_non_dict_content(self) -> None:
        ref = ComponentRef("s", "t", "n")
        variant = ComponentVariant(ref=ref, version="1.0.0", content="string")
        try:
            _param_perturb(variant, {})
            assert False, "expected TypeError"
        except TypeError:
            pass

    def test_preserves_non_numeric_values(self) -> None:
        ref = ComponentRef("s", "t", "n")
        variant = ComponentVariant(ref=ref, version="1.0.0", content={"lr": 0.01, "optimizer": "adam", "dropout": 0.5})
        result = _param_perturb(variant, {"rate": 1.0, "magnitude": 0.0})
        assert result.content["lr"] == 0.01
        assert result.content["optimizer"] == "adam"
        assert result.content["dropout"] == 0.5

    def test_default_params(self) -> None:
        ref = ComponentRef("s", "t", "n")
        variant = ComponentVariant(ref=ref, version="1.0.0", content={"lr": 0.01})
        with patch("random.random", return_value=0.1):
            with patch("random.uniform", return_value=0.0):
                result = _param_perturb(variant, {})
        assert result.content["lr"] == 0.01


class TestRandomSearch:
    def test_random_search_applies_ranges(self) -> None:
        ref = ComponentRef("s", "t", "n")
        variant = ComponentVariant(ref=ref, version="1.0.0", content={"lr": 0.0, "bs": 0})
        with patch("random.randint", return_value=64):
            with patch("random.uniform", return_value=0.001):
                result = _random_search(variant, {"ranges": {"lr": [0.0001, 0.01], "bs": [16, 128]}})
        assert result.content["lr"] == 0.001
        assert result.content["bs"] == 64
        assert result.version == "1.1.0"

    def test_raises_for_non_dict_content(self) -> None:
        ref = ComponentRef("s", "t", "n")
        variant = ComponentVariant(ref=ref, version="1.0.0", content="string")
        try:
            _random_search(variant, {})
            assert False, "expected TypeError"
        except TypeError:
            pass

    def test_preserves_unlisted_keys(self) -> None:
        ref = ComponentRef("s", "t", "n")
        variant = ComponentVariant(ref=ref, version="1.0.0", content={"a": 1, "b": "fixed"})
        result = _random_search(variant, {"ranges": {}})
        assert result.content["a"] == 1
        assert result.content["b"] == "fixed"


class TestEvolutionary:
    def test_crossover_with_mate(self) -> None:
        ref = ComponentRef("s", "t", "n")
        parent = ComponentVariant(ref=ref, version="1.0.0", content={"x": 1.0, "y": 2.0})
        with patch("random.choice", side_effect=lambda pair: pair[0]):
            result = _evolutionary(
                parent,
                {"mate_version": "1.1.0", "mate_content": {"x": 3.0, "y": 4.0}},
            )
        assert result.parent_version == "1.0.0"
        assert result.mutation_strategy == "evolutionary"
        assert result.content == {"x": 1.0, "y": 2.0}

    def test_crossover_with_mate_picks_second(self) -> None:
        ref = ComponentRef("s", "t", "n")
        parent = ComponentVariant(ref=ref, version="1.0.0", content={"x": 1.0, "y": 2.0})
        with patch("random.choice", side_effect=lambda pair: pair[1]):
            result = _evolutionary(
                parent,
                {"mate_version": "1.1.0", "mate_content": {"x": 3.0, "y": 4.0}},
            )
        assert result.content == {"x": 3.0, "y": 4.0}

    def test_raises_for_non_dict_content(self) -> None:
        ref = ComponentRef("s", "t", "n")
        variant = ComponentVariant(ref=ref, version="1.0.0", content="string")
        try:
            _evolutionary(variant, {})
            assert False, "expected TypeError"
        except TypeError:
            pass

    def test_mate_without_content_falls_to_param_perturb(self) -> None:
        ref = ComponentRef("s", "t", "n")
        variant = ComponentVariant(ref=ref, version="1.0.0", content={"x": 1.0, "y": 2.0})
        result = _evolutionary(variant, {"mate_version": "2.0.0"})
        assert isinstance(result, ComponentVariant)
        assert result.ref == ref
        assert result.parent_version == "1.0.0"

    def test_raises_for_non_dict_content_with_mate(self) -> None:
        ref = ComponentRef("s", "t", "n")
        variant = ComponentVariant(ref=ref, version="1.0.0", content="string")
        try:
            _evolutionary(variant, {"mate_version": "2.0.0", "mate_content": "other"})
            assert False, "expected TypeError"
        except TypeError:
            pass


class TestLlmRewrite:
    def test_string_content_appends_meta_block(self) -> None:
        ref = ComponentRef("s", "t", "n")
        variant = ComponentVariant(ref=ref, version="1.0.0", content="original prompt")
        result = _llm_rewrite(variant, {"goal": "Improve clarity"})
        assert result.ref == ref
        assert result.parent_version == "1.0.0"
        assert result.mutation_strategy == "llm_rewrite"
        assert result.version == "1.1.0"
        assert "original prompt" in result.content
        assert "LLM-REWRITE: Improve clarity" in result.content

    def test_dict_content_wraps_in_envelope(self) -> None:
        ref = ComponentRef("s", "t", "n")
        variant = ComponentVariant(ref=ref, version="1.0.0", content={"lr": 0.01, "bs": 32})
        result = _llm_rewrite(variant, {"goal": "Optimize params"})
        assert "_llm_rewrite" in result.content
        assert result.content["_llm_rewrite"]["goal"] == "Optimize params"
        assert result.content["original"] == {"lr": 0.01, "bs": 32}
        assert result.mutation_strategy == "llm_rewrite"
        assert result.version == "1.1.0"

    def test_default_goal(self) -> None:
        ref = ComponentRef("s", "t", "n")
        variant = ComponentVariant(ref=ref, version="1.0.0", content="test")
        result = _llm_rewrite(variant, {})
        assert "LLM-REWRITE: Improve clarity, add structure" in result.content

    def test_raises_for_non_string_non_dict_content(self) -> None:
        ref = ComponentRef("s", "t", "n")
        variant = ComponentVariant(ref=ref, version="1.0.0", content=42)
        try:
            _llm_rewrite(variant, {})
            assert False, "expected TypeError"
        except TypeError:
            pass

    def test_dict_content_preserves_original(self) -> None:
        ref = ComponentRef("s", "t", "n")
        original = {"a": 1, "b": 2}
        variant = ComponentVariant(ref=ref, version="1.0.0", content=dict(original))
        result = _llm_rewrite(variant, {})
        assert result.content["original"] == original
        # Ensure original is a copy, not a reference
        original["a"] = 99
        assert result.content["original"]["a"] == 1


class TestExperiment:
    def test_is_complete_true_for_completed(self) -> None:
        exp = Experiment(
            ref=ComponentRef("s", "t", "n"),
            control_version="1.0.0",
            treatment_version="1.1.0",
            status="completed",
        )
        assert exp.is_complete

    def test_is_complete_true_for_reverted(self) -> None:
        exp = Experiment(
            ref=ComponentRef("s", "t", "n"),
            control_version="1.0.0",
            treatment_version="1.1.0",
            status="reverted",
        )
        assert exp.is_complete

    def test_is_complete_true_for_failed(self) -> None:
        exp = Experiment(
            ref=ComponentRef("s", "t", "n"),
            control_version="1.0.0",
            treatment_version="1.1.0",
            status="failed",
        )
        assert exp.is_complete

    def test_is_complete_false_for_pending(self) -> None:
        exp = Experiment(
            ref=ComponentRef("s", "t", "n"),
            control_version="1.0.0",
            treatment_version="1.1.0",
            status="pending",
        )
        assert not exp.is_complete

    def test_is_complete_false_for_running(self) -> None:
        exp = Experiment(
            ref=ComponentRef("s", "t", "n"),
            control_version="1.0.0",
            treatment_version="1.1.0",
            status="running",
        )
        assert not exp.is_complete


class TestBumpVersion:
    def test_patch_bump(self) -> None:
        assert _bump_version("1.0.0", "patch") == "1.0.1"
        assert _bump_version("0.0.0", "patch") == "0.0.1"

    def test_minor_bump(self) -> None:
        assert _bump_version("1.0.0", "minor") == "1.1.0"
        assert _bump_version("2.5.3", "minor") == "2.6.0"

    def test_major_bump(self) -> None:
        assert _bump_version("1.0.0", "major") == "2.0.0"
        assert _bump_version("0.9.9", "major") == "1.0.0"

    def test_invalid_version_part_falls_back(self) -> None:
        result = _bump_version("1.0.abc", "patch")
        assert result == "1.0.abc.1"

    def test_default_is_patch(self) -> None:
        assert _bump_version("1.0.0") == "1.0.1"

    def test_two_part_version(self) -> None:
        assert _bump_version("1.0", "minor") == "1.1"
        assert _bump_version("1.0", "patch") == "1.0"
        assert _bump_version("1", "major") == "2"


class TestComponentRegistry:
    """ComponentRegistry with in-memory SQLite."""

    def _make_reg(self) -> ComponentRegistry:
        return ComponentRegistry(":memory:")

    def _ref(self, scope: str = "test", typ: str = "prompt", name: str = "foo") -> ComponentRef:
        return ComponentRef(scope=scope, component_type=typ, name=name)

    def test_init_creates_schema(self) -> None:
        reg = self._make_reg()
        tables = reg._conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        names = [r["name"] for r in tables]
        assert "components" in names
        assert "component_variants" in names
        assert "component_performance" in names
        assert "experiments" in names
        assert "hierarchy_levels" in names
        reg.close()

    def test_register_component_minimal(self) -> None:
        reg = self._make_reg()
        ref = self._ref()
        status = reg.register_component(ref)
        assert status.ref == ref
        assert status.active_version == "1.0.0"
        assert not status.exhausted
        reg.close()

    def test_register_component_with_content(self) -> None:
        reg = self._make_reg()
        ref = self._ref()
        status = reg.register_component(ref, initial_content={"x": 1}, version="2.0.0")
        assert status.active_version == "2.0.0"
        variant = reg.load_variant(ref)
        assert variant is not None
        assert variant.content == {"x": 1}
        reg.close()

    def test_register_component_duplicate_is_idempotent(self) -> None:
        reg = self._make_reg()
        ref = self._ref()
        reg.register_component(ref)
        reg.register_component(ref)  # should not raise
        reg.close()

    def test_get_component_status_nonexistent(self) -> None:
        reg = self._make_reg()
        ref = self._ref()
        status = reg.get_component_status(ref)
        assert status.active_version == ""
        assert not status.exhausted
        reg.close()

    def test_get_component_status_existing(self) -> None:
        reg = self._make_reg()
        ref = self._ref()
        reg.register_component(ref, initial_content="x")
        status = reg.get_component_status(ref)
        assert status.active_version == "1.0.0"
        assert status.ref == ref
        reg.close()

    def test_list_components_empty(self) -> None:
        reg = self._make_reg()
        assert reg.list_components() == []
        reg.close()

    def test_list_components_all(self) -> None:
        reg = self._make_reg()
        reg.register_component(self._ref(name="a"))
        reg.register_component(self._ref(name="b"))
        assert len(reg.list_components()) == 2
        reg.close()

    def test_list_components_by_scope(self) -> None:
        reg = self._make_reg()
        reg.register_component(self._ref(scope="s1", name="a"))
        reg.register_component(self._ref(scope="s2", name="b"))
        components = reg.list_components(scope="s1")
        assert len(components) == 1
        assert components[0].ref.scope == "s1"
        reg.close()

    def test_store_and_load_variant(self) -> None:
        reg = self._make_reg()
        ref = self._ref()
        reg.register_component(ref, initial_content="original", version="1.0.0")
        v2 = ComponentVariant(ref=ref, version="1.0.1", content="updated", parent_version="1.0.0")
        reg.store_variant(v2)
        loaded = reg.load_variant(ref, version="1.0.1")
        assert loaded is not None
        assert loaded.version == "1.0.1"
        assert loaded.content == "updated"
        assert loaded.parent_version == "1.0.0"
        reg.close()

    def test_load_variant_nonexistent_version(self) -> None:
        reg = self._make_reg()
        ref = self._ref()
        assert reg.load_variant(ref, version="9.9.9") is None
        reg.close()

    def test_load_variant_no_version_uses_active(self) -> None:
        reg = self._make_reg()
        ref = self._ref()
        reg.register_component(ref, initial_content="active", version="2.0.0")
        loaded = reg.load_variant(ref)
        assert loaded is not None
        assert loaded.version == "2.0.0"
        reg.close()

    def test_load_active_variant(self) -> None:
        reg = self._make_reg()
        ref = self._ref()
        reg.register_component(ref, initial_content="v1", version="3.0.0")
        loaded = reg.load_active_variant(ref)
        assert loaded is not None
        assert loaded.version == "3.0.0"
        reg.close()

    def test_load_active_variant_none_when_no_variant_stored(self) -> None:
        reg = self._make_reg()
        ref = self._ref()
        reg.register_component(ref)  # no initial_content → no variant row
        loaded = reg.load_active_variant(ref)
        assert loaded is None
        reg.close()

    def test_list_variants_empty(self) -> None:
        reg = self._make_reg()
        ref = self._ref()
        assert reg.list_variants(ref) == []
        reg.close()

    def test_list_variants_ordered_by_created_at(self) -> None:
        reg = self._make_reg()
        ref = self._ref()
        reg.register_component(ref, initial_content="v1", version="1.0.0")
        reg.store_variant(ComponentVariant(ref=ref, version="2.0.0", content="v2"))
        reg.store_variant(ComponentVariant(ref=ref, version="1.5.0", content="v3"))
        variants = reg.list_variants(ref)
        versions = {v.version for v in variants}
        assert versions == {"1.0.0", "2.0.0", "1.5.0"}
        reg.close()

    def test_set_active_version(self) -> None:
        reg = self._make_reg()
        ref = self._ref()
        reg.register_component(ref, initial_content="v1", version="1.0.0")
        reg.store_variant(ComponentVariant(ref=ref, version="2.0.0", content="v2"))
        reg.set_active_version(ref, "2.0.0")
        assert reg.get_component_status(ref).active_version == "2.0.0"
        reg.close()

    def test_mark_exhausted(self) -> None:
        reg = self._make_reg()
        ref = self._ref()
        reg.register_component(ref)
        reg.mark_exhausted(ref, "perf regression")
        status = reg.get_component_status(ref)
        assert status.exhausted
        assert status.exhausted_reason == "perf regression"
        reg.close()

    def test_record_performance_and_get(self) -> None:
        reg = self._make_reg()
        ref = self._ref()
        reg.register_component(ref, initial_content="v1")
        perf = ComponentPerformance(
            ref=ref,
            version="1.0.0",
            start_iteration=0,
            end_iteration=10,
            metrics={"score": 0.95},
            sample_count=100,
        )
        reg.record_performance(perf)
        perfs = reg.get_performance(ref)
        assert len(perfs) == 1
        assert perfs[0].metrics["score"] == 0.95
        reg.close()

    def test_get_performance_with_version_filter(self) -> None:
        reg = self._make_reg()
        ref = self._ref()
        reg.register_component(ref, initial_content="v1")
        reg.record_performance(
            ComponentPerformance(
                ref=ref,
                version="1.0.0",
                start_iteration=0,
                end_iteration=5,
                metrics={"s": 1.0},
                sample_count=10,
            )
        )
        reg.record_performance(
            ComponentPerformance(
                ref=ref,
                version="2.0.0",
                start_iteration=6,
                end_iteration=10,
                metrics={"s": 2.0},
                sample_count=10,
            )
        )
        perfs = reg.get_performance(ref, version="1.0.0")
        assert len(perfs) == 1
        assert perfs[0].version == "1.0.0"
        reg.close()

    def test_get_performance_limit(self) -> None:
        reg = self._make_reg()
        ref = self._ref()
        reg.register_component(ref, initial_content="v1")
        for i in range(5):
            reg.record_performance(
                ComponentPerformance(
                    ref=ref,
                    version="1.0.0",
                    start_iteration=i,
                    end_iteration=i + 1,
                    metrics={"s": float(i)},
                    sample_count=1,
                )
            )
        assert len(reg.get_performance(ref, limit=3)) == 3
        reg.close()

    def test_create_experiment_and_list(self) -> None:
        reg = self._make_reg()
        ref = self._ref()
        reg.register_component(ref, initial_content="v1")
        exp = Experiment(ref=ref, control_version="1.0.0", treatment_version="2.0.0")
        reg.create_experiment(exp)
        exps = reg.list_experiments()
        assert len(exps) == 1
        assert exps[0].experiment_id == exp.experiment_id
        assert exps[0].control_version == "1.0.0"
        reg.close()

    def test_list_experiments_filtered_by_status(self) -> None:
        reg = self._make_reg()
        ref = self._ref()
        reg.register_component(ref, initial_content="v1")
        reg.create_experiment(Experiment(ref=ref, control_version="1.0.0", treatment_version="1.0.1", status="running"))
        reg.create_experiment(
            Experiment(ref=ref, control_version="1.0.0", treatment_version="2.0.0", status="completed")
        )
        running = reg.list_experiments(status="running")
        assert len(running) == 1
        assert running[0].treatment_version == "1.0.1"
        reg.close()

    def test_list_experiments_filtered_by_ref(self) -> None:
        reg = self._make_reg()
        ref_a = self._ref(name="a")
        ref_b = self._ref(name="b")
        reg.register_component(ref_a, initial_content="v1")
        reg.register_component(ref_b, initial_content="v1")
        reg.create_experiment(Experiment(ref=ref_a, control_version="1.0.0", treatment_version="1.0.1"))
        reg.create_experiment(Experiment(ref=ref_b, control_version="1.0.0", treatment_version="1.0.1"))
        exps = reg.list_experiments(ref=ref_a)
        assert len(exps) == 1
        exps = reg.list_experiments(ref=ref_b)
        assert len(exps) == 1
        reg.close()

    def test_update_experiment(self) -> None:
        reg = self._make_reg()
        ref = self._ref()
        reg.register_component(ref, initial_content="v1")
        exp = Experiment(ref=ref, control_version="1.0.0", treatment_version="2.0.0")
        reg.create_experiment(exp)
        exp.status = "running"
        exp.current_iteration = 5
        reg.update_experiment(exp)
        exps = reg.list_experiments()
        assert exps[0].status == "running"
        assert exps[0].current_iteration == 5
        reg.close()

    def test_update_experiment_with_result(self) -> None:
        reg = self._make_reg()
        ref = self._ref()
        reg.register_component(ref, initial_content="v1")
        exp = Experiment(ref=ref, control_version="1.0.0", treatment_version="2.0.0", status="running")
        reg.create_experiment(exp)
        result = ExperimentResult(
            control_version="1.0.0",
            treatment_version="2.0.0",
            control_metrics={"s": 0.9},
            treatment_metrics={"s": 1.0},
            winner="treatment",
            effect_sizes={"s": 0.5},
            sample_count=50,
        )
        exp.result = result
        exp.status = "completed"
        reg.update_experiment(exp)
        exps = reg.list_experiments()
        assert exps[0].status == "completed"
        assert exps[0].result is not None
        assert exps[0].result.winner == "treatment"
        reg.close()

    def test_register_level_and_get_level(self) -> None:
        reg = self._make_reg()
        config = HierarchyLevelConfig(level=0, scope="autopilot")
        reg.register_level(0, "autopilot", config)
        level = reg.get_level(0)
        assert level is not None
        assert level["level"] == 0
        assert level["scope"] == "autopilot"
        reg.close()

    def test_get_level_nonexistent(self) -> None:
        reg = self._make_reg()
        assert reg.get_level(99) is None
        reg.close()

    def test_list_levels(self) -> None:
        reg = self._make_reg()
        reg.register_level(0, "autopilot", HierarchyLevelConfig(level=0, scope="autopilot"))
        reg.register_level(1, "meta", HierarchyLevelConfig(level=1, scope="meta"))
        assert len(reg.list_levels()) == 2
        reg.close()

    def test_close(self) -> None:
        reg = self._make_reg()
        reg.close()
        import sqlite3

        try:
            reg._conn.execute("SELECT 1")
            assert False, "expected ProgrammingError"
        except sqlite3.ProgrammingError:
            pass


class TestHierarchyLevel:
    """HierarchyLevel with in-memory ComponentRegistry."""

    def _make_reg(self) -> ComponentRegistry:
        return ComponentRegistry(":memory:")

    def _ref(self, scope: str = "test", typ: str = "prompt", name: str = "foo") -> ComponentRef:
        return ComponentRef(scope=scope, component_type=typ, name=name)

    def test_init_registers_level(self) -> None:
        reg = self._make_reg()
        HierarchyLevel(level=0, scope="autopilot", registry=reg)
        level_info = reg.get_level(0)
        assert level_info is not None
        assert level_info["level"] == 0
        assert level_info["scope"] == "autopilot"
        reg.close()

    def test_component_refs_empty(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(level=0, scope="test", registry=reg)
        assert hl.component_refs == []
        reg.close()

    def test_component_refs_returns_scoped_refs(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(level=0, scope="test", registry=reg)
        ref = self._ref()
        reg.register_component(ref, initial_content="v1")
        assert hl.component_refs == [ref]
        reg.close()

    def test_create_variant_returns_variant(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(level=0, scope="test", registry=reg)
        ref = self._ref()
        reg.register_component(ref, initial_content={"x": 1.0})
        variant = hl.create_variant(ref, MutationType.PARAMETER_PERTURB)
        assert variant is not None
        assert variant.ref == ref
        assert variant.parent_version == "1.0.0"
        reg.close()

    def test_create_variant_none_when_no_active(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(level=0, scope="test", registry=reg)
        ref = self._ref()
        reg.register_component(ref)  # no initial_content → no variant row
        variant = hl.create_variant(ref)
        assert variant is None
        reg.close()

    def test_start_experiment_creates_experiment(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(level=0, scope="test", registry=reg)
        ref = self._ref()
        reg.register_component(ref, initial_content="v1")
        reg.store_variant(ComponentVariant(ref=ref, version="2.0.0", content="v2"))
        exp = hl.start_experiment(ref, "2.0.0")
        assert exp is not None
        assert exp.ref == ref
        assert exp.control_version == "1.0.0"
        assert exp.treatment_version == "2.0.0"
        assert exp.status == "running"
        reg.close()

    def test_start_experiment_none_when_exhausted(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(level=0, scope="test", registry=reg)
        ref = self._ref()
        reg.register_component(ref, initial_content="v1")
        reg.mark_exhausted(ref, "test")
        exp = hl.start_experiment(ref, "2.0.0")
        assert exp is None
        reg.close()

    def test_start_experiment_none_when_running_exists(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(level=0, scope="test", registry=reg)
        ref = self._ref()
        reg.register_component(ref, initial_content="v1")
        reg.store_variant(ComponentVariant(ref=ref, version="2.0.0", content="v2"))
        hl.start_experiment(ref, "2.0.0")
        exp2 = hl.start_experiment(ref, "2.0.0")
        assert exp2 is None
        reg.close()

    def test_advance_experiment_increments(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(level=0, scope="test", registry=reg)
        ref = self._ref()
        reg.register_component(ref, initial_content="v1")
        reg.store_variant(ComponentVariant(ref=ref, version="2.0.0", content="v2"))
        exp = hl.start_experiment(ref, "2.0.0")
        assert exp is not None
        hl.advance_experiment(exp)
        assert exp.current_iteration == 1
        reg.close()

    def test_advance_experiment_concludes_when_done(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(level=0, scope="test", registry=reg)
        ref = self._ref()
        reg.register_component(ref, initial_content="v1")
        reg.store_variant(ComponentVariant(ref=ref, version="2.0.0", content="v2"))
        exp = hl.start_experiment(ref, "2.0.0", eval_iterations=1)
        assert exp is not None
        hl.advance_experiment(exp)
        assert exp.status == "completed"
        assert exp.result is not None
        assert exp.result.winner in ("treatment", "control", "tie")
        reg.close()

    def test_get_experiment_candidates_empty(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(level=0, scope="test", registry=reg)
        assert hl.get_experiment_candidates() == []
        reg.close()

    def test_get_experiment_candidates_returns_untested_variant(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(level=0, scope="test", registry=reg)
        ref = self._ref()
        reg.register_component(ref, initial_content="v1")
        reg.store_variant(ComponentVariant(ref=ref, version="2.0.0", content="v2"))
        candidates = hl.get_experiment_candidates()
        assert len(candidates) == 1
        assert candidates[0][0] == ref
        assert candidates[0][1].version == "2.0.0"
        reg.close()

    def test_detect_stagnation_no_experiments(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(level=0, scope="test", registry=reg)
        assert hl.detect_stagnation() == 0.0
        reg.close()

    def test_summary_returns_dict(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(level=0, scope="test", registry=reg)
        ref = self._ref()
        reg.register_component(ref, initial_content="v1")
        result = hl.summary()
        assert result["level"] == 0
        assert result["scope"] == "test"
        assert result["components"] == 1
        assert result["total_variants"] >= 1
        assert "stagnation_score" in result
        reg.close()

    def test_create_variant_no_active(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(level=0, scope="test", registry=reg)
        ref = self._ref()
        result = hl.create_variant(ref)
        assert result is None
        reg.close()

    def test_create_variant_exception(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(level=0, scope="test", registry=reg)
        ref = self._ref()
        reg.register_component(ref, initial_content={"k": 1.0})
        result = hl.create_variant(ref, strategy=MutationType.GRID_SEARCH)
        assert result is None
        reg.close()

    def test_start_experiment_no_active_version(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(level=0, scope="test", registry=reg)
        ref = self._ref()
        exp = hl.start_experiment(ref, "2.0.0")
        assert exp is None
        reg.close()

    def test_aggregate_metrics_empty(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(level=0, scope="test", registry=reg)
        result = hl._aggregate_metrics([])
        assert result == {}
        reg.close()

    def test_aggregate_metrics_single(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(level=0, scope="test", registry=reg)
        ref = self._ref()
        perfs = [
            ComponentPerformance(
                ref=ref, version="1.0.0", start_iteration=0, end_iteration=1, metrics={"a": 2.0, "b": 4.0}
            )
        ]
        result = hl._aggregate_metrics(perfs)
        assert result == {"a": 2.0, "b": 4.0}
        reg.close()

    def test_aggregate_metrics_multiple(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(level=0, scope="test", registry=reg)
        ref = self._ref()
        perfs = [
            ComponentPerformance(
                ref=ref, version="1.0.0", start_iteration=0, end_iteration=1, metrics={"a": 1.0, "b": 3.0}
            ),
            ComponentPerformance(
                ref=ref, version="1.0.0", start_iteration=1, end_iteration=2, metrics={"a": 3.0, "b": 5.0}
            ),
        ]
        result = hl._aggregate_metrics(perfs)
        assert result == {"a": 2.0, "b": 4.0}
        reg.close()

    def test_variant_urgency_no_perf(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(level=0, scope="test", registry=reg)
        ref = self._ref()
        reg.register_component(ref, initial_content="v1")
        variant = reg.load_variant(ref, "1.0.0")
        assert variant is not None
        urgency = hl._variant_urgency(ref, variant)
        assert urgency == 10.0
        reg.close()

    def test_variant_urgency_with_perf(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(level=0, scope="test", registry=reg)
        ref = self._ref()
        reg.register_component(ref, initial_content="v1")
        reg.record_performance(
            ComponentPerformance(
                ref=ref, version="1.0.0", start_iteration=0, end_iteration=1, metrics={"composite": 7.0}
            )
        )
        variant = reg.load_variant(ref, "1.0.0")
        assert variant is not None
        urgency = hl._variant_urgency(ref, variant)
        assert urgency == 3.0
        reg.close()

    def test_discover_components_with_existing_dir(self) -> None:
        import tempfile

        reg = self._make_reg()
        with tempfile.TemporaryDirectory() as tmp:
            with patch("scripts.meta_autopilot.COMPONENTS_DIR", Path(tmp)):
                hl = HierarchyLevel(level=0, scope="test_scope", registry=reg)
                scope_dir = Path(tmp) / "test_scope"
                scope_dir.mkdir(parents=True)
                prompt_dir = scope_dir / "prompt"
                prompt_dir.mkdir()
                (prompt_dir / "greeting.yaml").write_text("hello: world\n")
                (prompt_dir / "farewell.yaml").write_text("bye: world\n")
                discovered = hl.discover_components()
                assert len(discovered) == 2
                refs = {(d.scope, d.component_type, d.name) for d in discovered}
                assert ("test_scope", "prompt", "greeting") in refs
                assert ("test_scope", "prompt", "farewell") in refs
        reg.close()

    def test_evaluate_treatment_wins(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(
            level=0,
            scope="test",
            registry=reg,
            config=HierarchyLevelConfig(
                level=0, scope="test", eval_window=1, objectives=[OptimizationObjective("quality", "quality", 1.0)]
            ),
        )
        ref = self._ref()
        reg.register_component(ref, initial_content={"x": 1.0})
        treatment = hl.create_variant(ref, MutationType.PARAMETER_PERTURB)
        assert treatment is not None
        reg.record_performance(
            ComponentPerformance(ref=ref, version="1.0.0", start_iteration=0, end_iteration=1, metrics={"quality": 1.0})
        )
        reg.record_performance(
            ComponentPerformance(
                ref=ref, version=treatment.version, start_iteration=0, end_iteration=1, metrics={"quality": 3.0}
            )
        )
        exp = hl.start_experiment(ref, treatment.version)
        assert exp is not None
        hl.advance_experiment(exp)
        assert exp.status == "completed"
        assert exp.result is not None
        assert exp.result.winner == "treatment"
        reg.close()

    def test_evaluate_control_wins(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(
            level=0,
            scope="test",
            registry=reg,
            config=HierarchyLevelConfig(
                level=0, scope="test", eval_window=1, objectives=[OptimizationObjective("quality", "quality", 1.0)]
            ),
        )
        ref = self._ref()
        reg.register_component(ref, initial_content={"x": 1.0})
        treatment = hl.create_variant(ref, MutationType.PARAMETER_PERTURB)
        assert treatment is not None
        reg.record_performance(
            ComponentPerformance(ref=ref, version="1.0.0", start_iteration=0, end_iteration=1, metrics={"quality": 3.0})
        )
        reg.record_performance(
            ComponentPerformance(
                ref=ref, version=treatment.version, start_iteration=0, end_iteration=1, metrics={"quality": 1.0}
            )
        )
        exp = hl.start_experiment(ref, treatment.version)
        assert exp is not None
        hl.advance_experiment(exp)
        assert exp.status == "completed"
        assert exp.result is not None
        assert exp.result.winner == "control"
        reg.close()

    def test_evaluate_tie(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(
            level=0,
            scope="test",
            registry=reg,
            config=HierarchyLevelConfig(
                level=0, scope="test", eval_window=1, objectives=[OptimizationObjective("quality", "quality", 1.0)]
            ),
        )
        ref = self._ref()
        reg.register_component(ref, initial_content={"x": 1.0})
        treatment = hl.create_variant(ref, MutationType.PARAMETER_PERTURB)
        assert treatment is not None
        reg.record_performance(
            ComponentPerformance(ref=ref, version="1.0.0", start_iteration=0, end_iteration=1, metrics={"quality": 1.0})
        )
        reg.record_performance(
            ComponentPerformance(
                ref=ref, version=treatment.version, start_iteration=0, end_iteration=1, metrics={"quality": 1.02}
            )
        )
        exp = hl.start_experiment(ref, treatment.version)
        assert exp is not None
        hl.advance_experiment(exp)
        assert exp.status == "completed"
        assert exp.result is not None
        assert exp.result.winner == "tie"
        reg.close()

    def test_get_experiment_candidates_skips_exhausted(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(
            level=0,
            scope="test",
            registry=reg,
            config=HierarchyLevelConfig(
                level=0, scope="test", eval_window=1, objectives=[OptimizationObjective("q", "q", 1.0)]
            ),
        )
        ref = ComponentRef(scope="test", component_type="prompt", name="exhausted-test")
        reg.register_component(ref, initial_content={"x": 1.0})
        _ = hl.create_variant(ref, MutationType.PARAMETER_PERTURB)
        reg.mark_exhausted(ref, "test exhaustion")
        candidates = hl.get_experiment_candidates()
        assert all(c[0] != ref for c in candidates)
        reg.close()

    def test_get_experiment_candidates_skips_single_variant(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(
            level=0,
            scope="test",
            registry=reg,
            config=HierarchyLevelConfig(
                level=0, scope="test", eval_window=1, objectives=[OptimizationObjective("q", "q", 1.0)]
            ),
        )
        ref = ComponentRef(scope="test", component_type="prompt", name="single-variant-test")
        reg.register_component(ref, initial_content={"x": 1.0})
        candidates = hl.get_experiment_candidates()
        assert all(c[0] != ref for c in candidates)
        reg.close()

    def test_get_experiment_candidates_skips_running_experiment(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(
            level=0,
            scope="test",
            registry=reg,
            config=HierarchyLevelConfig(
                level=0, scope="test", eval_window=1, objectives=[OptimizationObjective("q", "q", 1.0)]
            ),
        )
        ref = ComponentRef(scope="test", component_type="prompt", name="running-exp-test")
        reg.register_component(ref, initial_content={"x": 1.0})
        treatment = hl.create_variant(ref, MutationType.PARAMETER_PERTURB)
        assert treatment is not None
        _ = hl.start_experiment(ref, treatment.version)
        candidates = hl.get_experiment_candidates()
        assert all(c[0] != ref for c in candidates)
        reg.close()

    def test_get_experiment_candidates_with_performance(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(
            level=0,
            scope="test",
            registry=reg,
            config=HierarchyLevelConfig(
                level=0, scope="test", eval_window=1, objectives=[OptimizationObjective("q", "q", 1.0)]
            ),
        )
        ref = ComponentRef(scope="test", component_type="prompt", name="perf-candidate-test")
        reg.register_component(ref, initial_content={"x": 1.0})
        treatment = hl.create_variant(ref, MutationType.PARAMETER_PERTURB)
        assert treatment is not None
        reg.record_performance(
            ComponentPerformance(
                ref=ref, version=treatment.version, start_iteration=0, end_iteration=1, metrics={"composite": 0.85}
            )
        )
        candidates = hl.get_experiment_candidates()
        assert any(c[0] == ref for c in candidates)
        for c_ref, variant in candidates:
            if c_ref == ref:
                assert variant.version == treatment.version
        reg.close()


class TestMetaController:
    """MetaController with in-memory ComponentRegistry."""

    def _make_reg(self) -> ComponentRegistry:
        return ComponentRegistry(":memory:")

    def test_init_creates_registry(self) -> None:
        reg = self._make_reg()
        mc = MetaController(registry=reg)
        assert mc.registry is reg
        assert mc.levels == {}

    def test_get_or_create_level_creates_new(self) -> None:
        reg = self._make_reg()
        mc = MetaController(registry=reg)
        hl = mc.get_or_create_level(0)
        assert isinstance(hl, HierarchyLevel)
        assert hl.level == 0
        assert hl.scope == "autopilot"
        assert 0 in mc.levels

    def test_get_or_create_level_returns_cached(self) -> None:
        reg = self._make_reg()
        mc = MetaController(registry=reg)
        hl1 = mc.get_or_create_level(0)
        hl2 = mc.get_or_create_level(0)
        assert hl1 is hl2

    def test_get_or_create_level_with_config(self) -> None:
        reg = self._make_reg()
        mc = MetaController(registry=reg)
        config = HierarchyLevelConfig(level=1, scope="meta")
        hl = mc.get_or_create_level(1, config=config)
        assert hl.level == 1
        assert hl.scope == "meta"

    def test_scope_for_level(self) -> None:
        assert MetaController._scope_for_level(0) == "autopilot"
        assert MetaController._scope_for_level(1) == "meta"
        assert MetaController._scope_for_level(2) == "meta.meta"
        assert MetaController._scope_for_level(3) == "meta.meta.meta"

    def test_level_for_scope(self) -> None:
        mc = MetaController(registry=self._make_reg())
        assert mc.level_for_scope("autopilot") == 0
        assert mc.level_for_scope("meta") == 1
        assert mc.level_for_scope("meta.meta") == 2
        assert mc.level_for_scope("unknown") == 0

    def test_register_component_delegates(self) -> None:
        reg = self._make_reg()
        mc = MetaController(registry=reg)
        ref = ComponentRef(scope="test", component_type="prompt", name="bar")
        status = mc.register_component(ref, initial_content="test")
        assert status.ref == ref
        assert status.active_version == "1.0.0"

    def test_summary_empty(self) -> None:
        reg = self._make_reg()
        mc = MetaController(registry=reg)
        result = mc.summary()
        assert result["active_levels"] == []

    def test_summary_with_levels(self) -> None:
        reg = self._make_reg()
        mc = MetaController(registry=reg)
        mc.get_or_create_level(0)
        result = mc.summary()
        assert result["active_levels"] == [0]
        assert "0" in result["levels"]
        assert result["levels"]["0"]["level"] == 0

    def test_detect_stagnation_with_failed_experiments(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(
            level=0,
            scope="test",
            registry=reg,
            config=HierarchyLevelConfig(
                level=0, scope="test", eval_window=1, objectives=[OptimizationObjective("quality", "quality", 1.0)]
            ),
        )
        ref = ComponentRef(scope="test", component_type="prompt", name="stagnation-test")
        reg.register_component(ref, initial_content={"x": 1.0})
        treatment = hl.create_variant(ref, MutationType.PARAMETER_PERTURB)
        assert treatment is not None
        exp = hl.start_experiment(ref, treatment.version)
        assert exp is not None
        exp.status = "failed"
        with patch.object(reg, "list_experiments", return_value=[exp]):
            score = hl.detect_stagnation()
        assert score > 0.0
        reg.close()

    def test_detect_stagnation_reverted_experiments(self) -> None:
        reg = self._make_reg()
        hl = HierarchyLevel(
            level=0,
            scope="test",
            registry=reg,
            config=HierarchyLevelConfig(
                level=0, scope="test", eval_window=1, objectives=[OptimizationObjective("quality", "quality", 1.0)]
            ),
        )
        ref = ComponentRef(scope="test", component_type="prompt", name="stagnation-reverted")
        reg.register_component(ref, initial_content={"x": 1.0})
        treatment = hl.create_variant(ref, MutationType.PARAMETER_PERTURB)
        assert treatment is not None
        exp = hl.start_experiment(ref, treatment.version)
        assert exp is not None
        exp.status = "reverted"
        with patch.object(reg, "list_experiments", return_value=[exp]):
            score = hl.detect_stagnation()
        assert score > 0.0
        reg.close()

    def test_run_cycle_no_experiments(self) -> None:
        reg = self._make_reg()
        mc = MetaController(registry=reg)
        mc.get_or_create_level(0)
        result = mc.run_cycle(0)
        assert result["level"] == 0
        assert result["scope"] == "autopilot"
        assert result["experiments_advanced"] == 0
        assert result["experiments_started"] == 0
        assert "stagnation_score" in result
        assert not result.get("escalated", False)

    def test_run_cycle_with_metrics(self) -> None:
        reg = self._make_reg()
        mc = MetaController(registry=reg)
        mc.get_or_create_level(0)
        result = mc.run_cycle(0, metrics={"quality": 0.95, "latency": 0.3})
        assert result["level"] == 0
        assert result["experiments_advanced"] == 0
        assert result["experiments_started"] == 0
        assert "stagnation_score" in result
        reg.close()

    def test_run_cycle_with_running_experiment(self) -> None:
        reg = self._make_reg()
        mc = MetaController(registry=reg)
        mc.get_or_create_level(0)
        ref = ComponentRef(scope="autopilot", component_type="prompt", name="cycle-test")
        reg.register_component(ref, initial_content={"param": 0.5})
        treatment = mc.levels[0].create_variant(ref, MutationType.PARAMETER_PERTURB)
        assert treatment is not None
        exp = mc.levels[0].start_experiment(ref, treatment.version)
        assert exp is not None
        assert exp.current_iteration == 0
        result = mc.run_cycle(0)
        assert result["experiments_advanced"] == 1
        running = reg.list_experiments(ref, status="running")
        assert len(running) == 1
        assert running[0].current_iteration == 1
        reg.close()

    def test_run_cycle_stagnation_escalates_to_next_level(self) -> None:
        """When stagnation > 0.7, run_cycle escalates by promoting to level+1."""
        reg = self._make_reg()
        mc = MetaController(registry=reg)
        mc.get_or_create_level(0)
        with patch.object(HierarchyLevel, "detect_stagnation", return_value=0.85):
            result = mc.run_cycle(0)
        assert result["escalated"] is True
        assert result["meta_level"] == 1
        assert "level-0-controller" in result["meta_ref"]
        assert "0.85" in result["reason"]
        assert "0.70" in result["reason"] or "0.7" in result["reason"]
        reg.close()

    def test_run_cycle_no_escalation_at_max_depth(self) -> None:
        """At level >= 5 with high stagnation, no escalation occurs (depth cap)."""
        reg = self._make_reg()
        mc = MetaController(registry=reg)
        config = HierarchyLevelConfig(
            level=5,
            scope="meta.meta.meta.meta.meta",
            eval_window=1,
            objectives=[OptimizationObjective("quality", "quality", 1.0)],
        )
        mc.get_or_create_level(5, config=config)
        with patch.object(HierarchyLevel, "detect_stagnation", return_value=0.95):
            result = mc.run_cycle(5)
        assert result["escalated"] is False
        assert "meta_level" not in result
        reg.close()

    def test_run_cycle_below_stagnation_threshold_no_escalation(self) -> None:
        """Stagnation at exactly 0.7 should NOT escalate (threshold is strict >)."""
        reg = self._make_reg()
        mc = MetaController(registry=reg)
        mc.get_or_create_level(0)
        with patch.object(HierarchyLevel, "detect_stagnation", return_value=0.7):
            result = mc.run_cycle(0)
        assert result["escalated"] is False
        reg.close()

    def test_run_cycle_respects_max_experiments_per_cycle(self) -> None:
        """When many candidates exist, only max_experiments_per_cycle are started."""
        reg = self._make_reg()
        mc = MetaController(registry=reg)
        config = HierarchyLevelConfig(
            level=0,
            scope="autopilot",
            eval_window=1,
            max_experiments_per_cycle=1,
            objectives=[OptimizationObjective("quality", "quality", 1.0)],
        )
        mc.get_or_create_level(0, config=config)
        for i in range(3):
            ref = ComponentRef(scope="autopilot", component_type="prompt", name=f"max-exp-{i}")
            reg.register_component(ref, initial_content={"p": 0.5})
            mc.levels[0].create_variant(ref, MutationType.PARAMETER_PERTURB)
        result = mc.run_cycle(0)
        assert result["experiments_started"] == 1
        assert result["experiments_advanced"] == 0
        running_total = sum(
            len(reg.list_experiments(ComponentRef("autopilot", "prompt", f"max-exp-{i}"), status="running"))
            for i in range(3)
        )
        assert running_total == 1
        reg.close()

    def test_auto_discover_level_without_components_dir(self) -> None:
        """auto_discover returns empty when the scope dir does not exist."""
        reg = self._make_reg()
        mc = MetaController(registry=reg)
        hl = mc.get_or_create_level(2)
        from scripts.meta_autopilot import COMPONENTS_DIR

        base = COMPONENTS_DIR / hl.scope
        if not base.exists():
            result = mc.auto_discover(level=2)
            assert result == []

    def test_initialize_defaults_with_registry(self) -> None:
        reg = self._make_reg()
        mc = MetaController(registry=reg)

        with patch("scripts.meta_autopilot.COMPONENTS_DIR") as mock_cd:
            mock_cd.exists.return_value = False
            result = initialize_defaults(controller=mc)
        assert result is mc
        assert 0 in result.levels
        assert 1 in result.levels

    def test_check_deploy_drift_when_in_sync(self) -> None:
        """When scripts/ matches the deploy mirror, drift is empty."""
        mc = MetaController(registry=self._make_reg())
        with patch("scripts.meta_autopilot.detect_drift", return_value=[]):
            result = mc.check_deploy_drift()
        assert result["available"] is True
        assert result["drift_count"] == 0
        assert result["missing"] == 0
        assert result["modified"] == 0
        assert result["extra"] == 0
        assert result["names"] == []

    def test_check_deploy_drift_flags_modifications(self) -> None:
        """A modified file in the deploy mirror is reported."""
        from scripts.sync import DriftEntry

        mc = MetaController(registry=self._make_reg())
        fake = [DriftEntry(name="a.py", status="modified", source_path=Path("/x/a.py"), deploy_path=Path("/y/a.py"))]
        with patch("scripts.meta_autopilot.detect_drift", return_value=fake):
            result = mc.check_deploy_drift()
        assert result["available"] is True
        assert result["drift_count"] == 1
        assert result["modified"] == 1
        assert result["missing"] == 0
        assert result["names"] == ["a.py:modified"]

    def test_check_deploy_drift_counts_missing_and_extra(self) -> None:
        """Missing-in-deploy and extra-in-deploy are counted separately."""
        from scripts.sync import DriftEntry

        mc = MetaController(registry=self._make_reg())
        fake = [
            DriftEntry(name="new.py", status="missing", source_path=Path("/x/new.py"), deploy_path=Path("/y/new.py")),
            DriftEntry(
                name="ghost.py", status="extra", source_path=Path("/x/ghost.py"), deploy_path=Path("/y/ghost.py")
            ),
        ]
        with patch("scripts.meta_autopilot.detect_drift", return_value=fake):
            result = mc.check_deploy_drift()
        assert result["available"] is True
        assert result["drift_count"] == 2
        assert result["missing"] == 1
        assert result["extra"] == 1
        assert result["modified"] == 0
        assert "new.py:missing" in result["names"]
        assert "ghost.py:extra" in result["names"]

    def test_check_deploy_drift_handles_missing_dirs(self) -> None:
        """Missing source/deploy directories return available=False, not crash."""
        mc = MetaController(registry=self._make_reg())
        with patch(
            "scripts.meta_autopilot.detect_drift",
            side_effect=FileNotFoundError("Source directory not found: /nonexistent-source-xyz"),
        ):
            result = mc.check_deploy_drift()
        assert result["available"] is False
        assert "reason" in result
        assert "nonexistent-source-xyz" in result["reason"]

    def test_check_deploy_drift_handles_import_failure(self) -> None:
        """If the sync module cannot be imported, return available=False."""
        mc = MetaController(registry=self._make_reg())
        with patch("scripts.meta_autopilot.detect_drift", None):
            result = mc.check_deploy_drift()
        assert result["available"] is False
        assert "not importable" in result["reason"]

    def test_run_cycle_includes_deploy_drift(self) -> None:
        """run_cycle result always carries a deploy_drift field."""
        reg = self._make_reg()
        mc = MetaController(registry=reg)
        mc.get_or_create_level(0)
        with patch("scripts.meta_autopilot.detect_drift", return_value=[]):
            result = mc.run_cycle(0)
        assert "deploy_drift" in result
        assert result["deploy_drift"]["available"] is True
        assert result["deploy_drift"]["drift_count"] == 0
        reg.close()

    def test_run_cycle_surfaces_drift_to_result(self) -> None:
        """run_cycle surfaces a non-zero drift count without crashing."""
        from scripts.sync import DriftEntry

        reg = self._make_reg()
        mc = MetaController(registry=reg)
        mc.get_or_create_level(0)
        fake = [DriftEntry(name="a.py", status="modified", source_path=Path("/x/a.py"), deploy_path=Path("/y/a.py"))]
        with patch("scripts.meta_autopilot.detect_drift", return_value=fake):
            result = mc.run_cycle(0)
        assert result["deploy_drift"]["drift_count"] == 1
        assert result["deploy_drift"]["modified"] == 1
        reg.close()


class TestLoadFileContent:
    def test_load_yaml(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("key: value\nnested: {a: 1}\n")
            path = Path(f.name)
        try:
            result = _load_file_content(path)
            assert result == {"key": "value", "nested": {"a": 1}}
        finally:
            path.unlink()

    def test_load_json(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"key": "value", "num": 42}, f)
            path = Path(f.name)
        try:
            result = _load_file_content(path)
            assert result == {"key": "value", "num": 42}
        finally:
            path.unlink()

    def test_load_md(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("# Hello\nWorld\n")
            path = Path(f.name)
        try:
            result = _load_file_content(path)
            assert result == "# Hello\nWorld\n"
        finally:
            path.unlink()

    def test_load_txt(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("plain text content")
            path = Path(f.name)
        try:
            result = _load_file_content(path)
            assert result == "plain text content"
        finally:
            path.unlink()

    def test_load_py(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("x = 1\n")
            path = Path(f.name)
        try:
            result = _load_file_content(path)
            assert result == "x = 1\n"
        finally:
            path.unlink()

    def test_load_unknown_suffix(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".unknown", delete=False) as f:
            f.write("fallback content")
            path = Path(f.name)
        try:
            result = _load_file_content(path)
            assert result == "fallback content"
        finally:
            path.unlink()


class TestPrintStatus:
    def test_print_status_empty(self) -> None:
        reg = ComponentRegistry(":memory:")
        mc = MetaController(registry=reg)
        out = io.StringIO()
        with patch("sys.stdout", out):
            print_status(mc)
        output = out.getvalue()
        assert "Meta-Autopilot Status" in output
        assert "Active levels" in output

    def test_print_status_with_levels(self) -> None:
        reg = ComponentRegistry(":memory:")
        mc = MetaController(registry=reg)
        mc.get_or_create_level(0)
        out = io.StringIO()
        with patch("sys.stdout", out):
            print_status(mc)
        output = out.getvalue()
        assert "Level 0" in output
        assert "autopilot" in output


class TestMain:
    def test_main_init(self) -> None:
        reg = ComponentRegistry(":memory:")
        mc = MetaController(registry=reg)
        mc.get_or_create_level(0)
        with (
            patch("scripts.meta_autopilot.initialize_defaults", return_value=mc),
            patch("sys.argv", ["meta_autopilot.py", "init"]),
        ):
            rc = main()
        assert rc == 0

    def test_main_status(self) -> None:
        reg = ComponentRegistry(":memory:")
        mc = MetaController(registry=reg)
        out = io.StringIO()
        with (
            patch("scripts.meta_autopilot.initialize_defaults", return_value=mc),
            patch("sys.argv", ["meta_autopilot.py", "status"]),
            patch("sys.stdout", out),
        ):
            rc = main()
        assert rc == 0
        assert "Meta-Autopilot Status" in out.getvalue()

    def test_main_discover(self) -> None:
        reg = ComponentRegistry(":memory:")
        mc = MetaController(registry=reg)
        mc.get_or_create_level(0)
        with (
            patch("scripts.meta_autopilot.initialize_defaults", return_value=mc),
            patch("sys.argv", ["meta_autopilot.py", "discover"]),
        ):
            rc = main()
        assert rc == 0

    def test_main_cycle(self) -> None:
        reg = ComponentRegistry(":memory:")
        mc = MetaController(registry=reg)
        mc.get_or_create_level(0)
        with (
            patch("scripts.meta_autopilot.initialize_defaults", return_value=mc),
            patch("sys.argv", ["meta_autopilot.py", "cycle", "--level", "0"]),
        ):
            rc = main()
        assert rc == 0

    def test_main_unknown_command(self) -> None:
        reg = ComponentRegistry(":memory:")
        mc = MetaController(registry=reg)
        with (
            patch("scripts.meta_autopilot.initialize_defaults", return_value=mc),
            patch("sys.argv", ["meta_autopilot.py"]),
        ):
            rc = main()
        assert rc == 1
