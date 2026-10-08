"""Driver and activator interfaces, plus the package view a driver works on (`RELEASE-CONTRACT.md` §3.1)."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from datamanager.models import Package

ProgressCb = Callable[[int, int, str], None]
StepCb = Callable[[str], None]


@dataclass(frozen=True)
class PartView:
    path: str  # relative to the package folder, e.g. pelias/benelux/pelias.json
    class_: str
    region: str | None
    kind: str
    size: int
    sha256: str
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PackageView:
    tag: str
    path: Path
    parts: tuple[PartView, ...]

    @classmethod
    def from_package(cls, package: Package) -> "PackageView":
        parts = tuple(PartView(i.path, i.class_, i.region, i.kind, i.size_bytes, i.sha256, dict(i.meta_json or {}))
                      for i in package.items)
        return cls(package.tag, Path(package.path), parts)

    def of_class(self, class_: str) -> list[PartView]:
        return [p for p in self.parts if p.class_ == class_]

    @property
    def classes(self) -> list[str]:
        return sorted({p.class_ for p in self.parts})


@dataclass(frozen=True)
class ActivationContext:
    class_: str
    tag: str
    root: Path  # the class root on the target (releases/, current, previous)
    regions: tuple[str, ...]  # regions of this release
    settings: dict  # the `activate` block of the deploy configuration
    options: dict  # the class block and the deploy settings (health timeout, ...)


class Activator(ABC):
    """What makes a copied release live: restart containers, restore indices, ... One implementation per `activate.type`."""

    @abstractmethod
    def activate(self, ctx: ActivationContext) -> None:
        """Called after `current` points at `ctx.tag`. Raises DeployError when the services cannot be started."""

    def check_health(self, ctx: ActivationContext) -> None:
        """Raises DeployError when the services are not healthy within the timeout."""

    def release_removed(self, ctx: ActivationContext) -> None:
        """Called before the files of an old release are removed (e.g. drop its Elasticsearch indices)."""


class NoActivator(Activator):
    """Switch only: for classes whose services read the symlink themselves, and for tests."""

    def activate(self, ctx: ActivationContext) -> None:
        return None


ACTIVATORS: dict[str, type[Activator]] = {"none": NoActivator}


class DeployDriver(ABC):
    key: str

    @classmethod
    @abstractmethod
    def validate_config(cls, config: dict) -> list[str]:
        """Problems with a deploy configuration (empty = fine). Never contains secrets."""

    @abstractmethod
    def describe_state(self, classes: list[str] | None = None) -> dict[str, dict]:
        """Truth from the target: per class `current`, `previous` and the releases present."""

    @abstractmethod
    def plan_class(self, package: PackageView, class_: str) -> dict:
        """What deploying this class would do: bytes, free space, problems, skip reason."""

    @abstractmethod
    def deploy_class(self, package: PackageView, class_: str, progress: ProgressCb | None = None,
                     step: StepCb | None = None) -> dict:
        """Copy, verify, switch, activate, health check, prune. Switches back on failure."""

    @abstractmethod
    def rollback_class(self, class_: str, step: StepCb | None = None) -> dict:
        """`current` back to `previous`; the release that was rolled back is removed afterwards."""
