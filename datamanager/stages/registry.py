"""StageRegistry: resolves a requested set of stages into a valid run order.

Each requested stage's `consumes` entries are matched against every
registered stage's `produces`, transitively pulling in producers the caller
didn't explicitly ask for (so requesting "pelias" also schedules "osm" and
"valhalla" ahead of it). This replaces today's comment-only ordering
constraints in data-pipeline with a structural DAG resolver.
"""

from datamanager.errors import CyclicDependencyError, UnresolvedDependencyError
from datamanager.stages.contract import StageRunner


class StageRegistry:
    def __init__(self):
        self._stages: dict[str, type[StageRunner]] = {}

    def register(self, stage_cls: type[StageRunner]) -> type[StageRunner]:
        self._stages[stage_cls.key] = stage_cls
        return stage_cls

    def get(self, key: str) -> type[StageRunner]:
        return self._stages[key]

    def _producer_of(self, asset_type: str) -> type[StageRunner] | None:
        for stage_cls in self._stages.values():
            if any(io.asset_type == asset_type for io in stage_cls.produces):
                return stage_cls
        return None

    def resolve_order(self, requested_keys: list[str]) -> list[str]:
        """Returns requested_keys plus any transitively-required producer
        stages, topologically sorted. Raises UnresolvedDependencyError if a
        consumed asset type has no registered producer anywhere, or
        CyclicDependencyError if the resulting graph has a cycle.
        """
        nodes: set[str] = set()
        edges: dict[str, set[str]] = {}  # producer -> {consumers}

        def visit(key: str):
            if key in nodes:
                return
            nodes.add(key)
            edges.setdefault(key, set())
            stage_cls = self._stages[key]
            for io in stage_cls.consumes:
                producer = self._producer_of(io.asset_type)
                if producer is None:
                    raise UnresolvedDependencyError(
                        f"stage '{key}' consumes '{io.asset_type}' but no "
                        f"registered stage produces it",
                        stage=key,
                        asset_type=io.asset_type,
                    )
                edges.setdefault(producer.key, set()).add(key)
                visit(producer.key)

        for key in requested_keys:
            visit(key)

        in_degree = {n: 0 for n in nodes}
        for producer, consumers in edges.items():
            for consumer in consumers:
                in_degree[consumer] += 1

        ready = sorted(n for n, deg in in_degree.items() if deg == 0)
        order: list[str] = []
        while ready:
            node = ready.pop(0)
            order.append(node)
            for consumer in sorted(edges.get(node, ())):
                in_degree[consumer] -= 1
                if in_degree[consumer] == 0:
                    ready.append(consumer)

        if len(order) != len(nodes):
            remaining = nodes - set(order)
            raise CyclicDependencyError(
                f"cyclic dependency detected among stages: {sorted(remaining)}",
                stages=sorted(remaining),
            )
        return order


default_registry = StageRegistry()
