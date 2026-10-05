"""Cleanup after packaging (`RELEASE-CONTRACT.md` §8): free SSD space once a package holds the results.

`plan` lists, per category, what would go (and what is skipped, with the reason); `apply` re-plans and executes.
Approved assets and downloads are *purged* (file removed, row kept with its hash and provenance, so stage fingerprints
and statuses stay as they are); rejected leftovers and old download versions are deleted outright. Things that are
part of a package class are only removed when the given, verified package holds the same bytes."""
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from datamanager.config import config
from datamanager.errors import PackageError
from datamanager.models import Asset, BuildRun, DownloadRecord, Package, PackageItem
from datamanager.services import assets as asset_service
from datamanager.services import downloads
from datamanager.services import settings as settings_service
from datamanager.services.cleanup_categories import BY_KEY, CATEGORIES

# category -> (asset types, the subset that is part of a package and so needs coverage when it is the current one)
ASSET_CATEGORIES = {
    "country_pbf": ({"country-pbf"}, set()),
    "region_pbf": ({"osm-pbf", "osm-core-pbf"}, set()),
    "valhalla": ({"valhalla-tiles", "valhalla-admin", "valhalla-timezones", "valhalla-polylines"},
                 {"valhalla-tiles", "valhalla-admin", "valhalla-timezones"}),
    "pelias_snapshots": ({"pelias-index-snapshot"}, {"pelias-index-snapshot"}),
    "pelias_wof": ({"pelias-wof", "wof-patched-sqlite"}, {"pelias-wof"}),
    "interpolation": ({"pelias-interpolation-street-db", "pelias-interpolation-address-db"},
                      {"pelias-interpolation-street-db", "pelias-interpolation-address-db"}),
    "small_assets": ({"core-polygon", "overlap-polygon", "border-polygon", "border-crossings", "region-outline",
                      "pelias-config", "style"}, {"border-crossings", "region-outline", "pelias-config", "style"}),
}  # `carve-polygon` is in no category on purpose: it is user input
# category -> (source key prefixes, needs coverage when it is the version stages use)
DOWNLOAD_CATEGORIES = {
    "planet": (("planet:",), False),
    "tiles": (("tiles:",), True),
    "srtm_downloads": (("srtm:",), False),
    "pelias_sources": (("wof:", "geonames:", "openaddresses:", "placeholder:"), False),
    "overture_gtfs": (("overture:", "gtfs:"), False),
}
IDLE_STAGES = ("package", "package-verify", "cleanup")  # runs of these never conflict with a cleanup


@dataclass
class Item:
    category: str
    kind: str  # purge-asset | purge-download | delete-download | delete-asset | rmdir
    ref: int | str
    label: str
    bytes: int
    skip: str | None = None


@dataclass
class CleanupPlan:
    package: str
    items: list[Item] = field(default_factory=list)
    busy: list[int] = field(default_factory=list)  # runs that are queued or running: a cleanup must wait for them

    def by_category(self) -> dict[str, dict]:
        out = {c.key: {"bytes": 0, "count": 0, "skipped": [], "items": []} for c in CATEGORIES}
        for item in self.items:
            entry = out[item.category]
            entry["items"].append(item)
            if item.skip:
                entry["skipped"].append(item)
            else:
                entry["bytes"] += item.bytes
                entry["count"] += 1
        return out


def defaults(session: Session) -> dict[str, bool]:
    """category key -> ticked by default (the setting says `delete`)."""
    return {c.key: settings_service.get(session, f"cleanup.default.{c.key}") == "delete" for c in CATEGORIES}


def _package(session: Session, tag: str) -> Package:
    package = session.query(Package).filter_by(tag=tag).first()
    if package is None:
        raise PackageError(f"No such package: {tag}")
    if package.status != "complete" or package.verified_at is None:
        raise PackageError(f"{tag} is not verified yet: run Verify first, the cleanup only follows a clean verify")
    return package


def _covered(session: Session, package: Package, **match) -> bool:
    return session.query(PackageItem).filter_by(package_id=package.id, **match).first() is not None


def _size(files) -> int:
    return sum(f.stat().st_size for f in files if f.exists())


def _asset_items(session: Session, package: Package, key: str) -> list[Item]:
    types, covered_types = ASSET_CATEGORIES[key]
    items = []
    rows = session.query(Asset).filter(Asset.asset_type.in_(types), Asset.status == "approved").order_by(Asset.id).all()
    for asset in rows:
        if asset_service.is_purged(asset):
            continue
        files = asset_service._files(asset)
        size = _size(files)
        if not size and not any(f.exists() for f in files):
            continue
        item = Item(key, "purge-asset", asset.id, f"{asset.asset_type} {asset.name} (run {asset.produced_by_run_id})", size)
        current = asset_service.current(session, asset.config_profile_id, asset.asset_type, asset.name)
        if asset.asset_type in covered_types and current is not None and current.id == asset.id:
            if not _covered(session, package, asset_id=asset.id, sha256=asset.content_hash):
                item.skip = f"not in {package.tag}"
        items.append(item)
    return items


def _download_items(session: Session, package: Package, key: str) -> list[Item]:
    prefixes, needs_coverage = DOWNLOAD_CATEGORIES[key]
    items = []
    rows = session.query(DownloadRecord).filter(DownloadRecord.status != "rejected", DownloadRecord.purged_at.is_(None)).order_by(DownloadRecord.id).all()
    for record in rows:
        if not record.source_key.startswith(prefixes):
            continue
        path = downloads.abs_path(record)
        if not path.exists() or not downloads.is_managed(path):
            continue
        item = Item(key, "purge-download", record.id, f"{record.source_key} {record.version_label}", record.size_bytes or path.stat().st_size)
        selected = downloads.resolve_version(session, record.source_key)
        if record.pinned:
            item.skip = "pinned"
        elif needs_coverage and selected is not None and selected.id == record.id \
                and not _covered(session, package, download_id=record.id, sha256=record.content_hash):
            item.skip = f"not in {package.tag}"
        items.append(item)
    return items


def _dir_size(path: Path) -> int:
    """Bytes a removal frees: hard-linked files (still reachable elsewhere) do not count."""
    total = 0
    for f in path.rglob("*"):
        if f.is_file() and f.stat().st_nlink == 1:
            total += f.stat().st_size
    return total


def _srtm_items() -> list[Item]:
    base = Path(config.DATA_ROOT) / "library" / "srtm"
    if not base.exists():
        return []
    return [Item("srtm_unpacked", "rmdir", str(d.relative_to(config.DATA_ROOT)), f"library/srtm/{d.name}", _dir_size(d))
            for d in sorted(base.iterdir()) if d.is_dir()]


def _leftover_items(session: Session, busy: list[int]) -> list[Item]:
    active = set(busy) | {r for (r,) in session.query(BuildRun.id).filter(BuildRun.status.in_(("queued", "running", "awaiting_review")))}
    items = []
    for asset in session.query(Asset).filter(Asset.status.in_(("produced", "rejected"))).all():
        if asset.produced_by_run_id in active:
            continue
        files = asset_service._files(asset)
        items.append(Item("leftovers", "delete-asset", asset.id,
                          f"{asset.asset_type} {asset.name} ({asset.status}, run {asset.produced_by_run_id})", _size(files)))
    work = Path(config.DATA_ROOT) / "work"
    if work.exists():
        for d in sorted(work.iterdir()):
            if d.is_dir() and not (d.name.isdigit() and int(d.name) in active):
                items.append(Item("leftovers", "rmdir", str(d.relative_to(config.DATA_ROOT)), f"work/{d.name}", _dir_size(d)))
    return items


def _old_version_items(session: Session, taken: set) -> list[Item]:
    items = []
    for record in downloads.cleanup_candidates(session):
        if record.purged_at is not None or ("download", record.id) in taken:
            continue
        path = downloads.abs_path(record)
        items.append(Item("old_versions", "delete-download", record.id, f"{record.source_key} {record.version_label}",
                          record.size_bytes if path.exists() else 0))
    return items


def plan(session: Session, package_tag: str, categories: list[str] | None = None) -> CleanupPlan:
    """What a cleanup after `package_tag` would remove. `categories` None = all (for the sizes in the dialog)."""
    package = _package(session, package_tag)
    wanted = list(categories) if categories is not None else [c.key for c in CATEGORIES]
    unknown = [c for c in wanted if c not in BY_KEY]
    if unknown:
        raise PackageError(f"Unknown cleanup categories: {', '.join(unknown)}")
    result = CleanupPlan(package_tag)
    result.busy = [r for (r,) in session.query(BuildRun.id).filter(BuildRun.status.in_(("queued", "running")),
                                                                    BuildRun.stage_key.notin_(IDLE_STAGES))]
    taken: set = set()
    for key in wanted:
        if key in ASSET_CATEGORIES:
            found = _asset_items(session, package, key)
            taken |= {("asset", i.ref) for i in found}
        elif key in DOWNLOAD_CATEGORIES:
            found = _download_items(session, package, key)
            taken |= {("download", i.ref) for i in found}
        elif key == "srtm_unpacked":
            found = _srtm_items()
        elif key == "leftovers":
            found = _leftover_items(session, result.busy)
        else:
            continue
        result.items += found
    if "old_versions" in wanted:
        result.items += _old_version_items(session, taken)
    return result


@dataclass
class CleanupResult:
    freed: int = 0
    purged: int = 0
    deleted: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def apply(session: Session, package_tag: str, categories: list[str]) -> CleanupResult:
    """Re-plan (never trust what a form posted) and execute. Refused while a run is queued or running."""
    the_plan = plan(session, package_tag, categories)
    if the_plan.busy:
        raise PackageError(f"Run(s) {', '.join(str(r) for r in the_plan.busy)} are queued or running: wait for them before cleaning up")
    result = CleanupResult()
    together = frozenset(i.ref for i in the_plan.items if i.kind == "purge-download" and not i.skip)
    for item in the_plan.items:
        if item.skip:
            result.skipped.append(f"{item.label}: {item.skip}")
            continue
        try:
            if item.kind == "purge-asset":
                result.freed += asset_service.purge(session, session.get(Asset, item.ref), package_tag)
                result.purged += 1
            elif item.kind == "purge-download":
                result.freed += downloads.purge(session, session.get(DownloadRecord, item.ref), package_tag, together)
                result.purged += 1
            elif item.kind == "delete-download":
                downloads._remove(session, session.get(DownloadRecord, item.ref))
                result.freed += item.bytes
                result.deleted += 1
            elif item.kind == "delete-asset":
                asset = session.get(Asset, item.ref)
                asset_service._remove_files(asset)
                session.delete(asset)
                result.freed += item.bytes
                result.deleted += 1
            elif item.kind == "rmdir":
                target = (Path(config.DATA_ROOT) / str(item.ref)).resolve()
                allowed = [(Path(config.DATA_ROOT) / "library" / "srtm").resolve(), (Path(config.DATA_ROOT) / "work").resolve()]
                if not any(a in target.parents for a in allowed):
                    raise PackageError(f"refusing to remove {target}")
                shutil.rmtree(target)
                result.freed += item.bytes
                result.deleted += 1
        except Exception as exc:  # one failing item must not stop the rest
            result.errors.append(f"{item.label}: {exc}")
    session.commit()
    return result
