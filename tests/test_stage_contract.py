import pytest

from datamanager.errors import CyclicDependencyError, UnresolvedDependencyError
from datamanager.stages.contract import StageIO, StageResult, StageRunContext, StageRunner
from datamanager.stages.noop import NoOpStage
from datamanager.stages.registry import StageRegistry


class _StageA(StageRunner):
    key = "a"
    produces = (StageIO("x"),)

    def run(self, context):
        return StageResult(status="success")


class _StageB(StageRunner):
    key = "b"
    consumes = (StageIO("x"),)
    produces = (StageIO("y"),)

    def run(self, context):
        return StageResult(status="success")


class _StageC(StageRunner):
    key = "c"
    consumes = (StageIO("y"),)

    def run(self, context):
        return StageResult(status="success")


class _StageIndependent(StageRunner):
    key = "independent"

    def run(self, context):
        return StageResult(status="success")


class _StageMissingDependency(StageRunner):
    key = "missing-dep"
    consumes = (StageIO("nobody-produces-this"),)

    def run(self, context):
        return StageResult(status="success")


class _StageCyclicF(StageRunner):
    key = "f"
    consumes = (StageIO("g-output"),)
    produces = (StageIO("f-output"),)

    def run(self, context):
        return StageResult(status="success")


class _StageCyclicG(StageRunner):
    key = "g"
    consumes = (StageIO("f-output"),)
    produces = (StageIO("g-output"),)

    def run(self, context):
        return StageResult(status="success")


def _registry(*stage_classes) -> StageRegistry:
    registry = StageRegistry()
    for stage_cls in stage_classes:
        registry.register(stage_cls)
    return registry


def test_chain_resolves_in_producer_to_consumer_order():
    registry = _registry(_StageA, _StageB, _StageC)
    assert registry.resolve_order(["c"]) == ["a", "b", "c"]


def test_independent_stage_schedules_alongside_a_chain():
    registry = _registry(_StageA, _StageB, _StageC, _StageIndependent)
    order = registry.resolve_order(["c", "independent"])
    assert set(order) == {"a", "b", "c", "independent"}
    # "independent" has no dependency relationship, but the chain order holds.
    assert order.index("a") < order.index("b") < order.index("c")


def test_unmet_consumes_raises_unresolved_dependency_error():
    registry = _registry(_StageMissingDependency)
    with pytest.raises(UnresolvedDependencyError):
        registry.resolve_order(["missing-dep"])


def test_cycle_raises_cyclic_dependency_error():
    registry = _registry(_StageCyclicF, _StageCyclicG)
    with pytest.raises(CyclicDependencyError):
        registry.resolve_order(["f"])


def test_noop_stage_runs_directly_and_writes_marker(tmp_path):
    stage = NoOpStage()
    context = StageRunContext(run_id="test-run", work_dir=tmp_path / "test-run")
    result = stage.run(context)

    assert result.status == "success"
    marker_path = tmp_path / "test-run" / "noop.txt"
    assert marker_path.exists()
    assert "test-run" in marker_path.read_text()
