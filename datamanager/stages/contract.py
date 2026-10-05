"""The stage-runner contract.

Every real stage (osm, border, valhalla, pelias, tiles — added from Phase 2
onward) implements StageRunner and declares what it produces/consumes, so the
dependency order between stages is resolved structurally by StageRegistry
instead of by comments in shell scripts (today's data-pipeline convention).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal


@dataclass(frozen=True)
class StageIO:
    """An asset type a stage produces or consumes, e.g. StageIO("osm-pbf")."""

    asset_type: str


@dataclass
class StageRunContext:
    run_id: str
    work_dir: Path
    config_resolved: dict | None = None
    config_id: int | None = None
    params: dict = field(default_factory=dict)
    input_versions: dict = field(default_factory=dict)  # source_key -> pinned download_record id
    step_cb: Callable[[str], None] = lambda name: None  # starts a new named step
    progress_cb: Callable[[int, int, str], None] = lambda current, total, message: None


@dataclass
class StageResult:
    status: Literal["success", "failed"]
    produced_paths: dict[str, str] = field(default_factory=dict)
    report: dict = field(default_factory=dict)  # validation report shown when the run is reviewed


class StageRunner(ABC):
    key: str
    produces: tuple[StageIO, ...] = ()
    consumes: tuple[StageIO, ...] = ()
    consumes_downloads: tuple[str, ...] = ()
    review_gate: bool = True  # False: a clean run is approved automatically (e.g. packaging: nothing downstream consumes it)

    @abstractmethod
    def run(self, context: StageRunContext) -> StageResult: ...
