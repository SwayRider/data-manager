"""Assets: files produced by stage runs, tracked with a hash and gated by run approval.

An asset is `produced` when its run ends, `approved`/`rejected` with the run's review. Later stages
use `current(...)`: the newest approved asset for a (configuration, type, name)."""
import datetime
import hashlib
from pathlib import Path

from sqlalchemy.orm import Session

from datamanager.config import config
from datamanager.errors import ValidationError
from datamanager.models import Asset, BuildRun


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def asset_dir(kind: str, run_id: int | str) -> Path:
    """Where a run's files of one kind live; created on demand, kept after the run (unlike work/)."""
    path = Path(config.DATA_ROOT) / "library" / "assets" / kind / str(run_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def abs_path(asset: Asset) -> Path:
    return Path(config.DATA_ROOT) / asset.path


def create(
    session: Session,
    run_id: int,
    config_id: int | None,
    asset_type: str,
    name: str,
    file: Path,
    meta: dict | None = None,
    source_download_ids: list[int] | None = None,
) -> Asset:
    asset = Asset(
        asset_type=asset_type, name=name, config_profile_id=config_id, produced_by_run_id=run_id,
        path=str(Path(file).resolve().relative_to(Path(config.DATA_ROOT).resolve())),
        content_hash=sha256_of(file), size_bytes=Path(file).stat().st_size,
        meta_json=meta or {}, source_download_ids=source_download_ids or [],
    )
    session.add(asset)
    session.commit()
    return asset


def current(session: Session, config_id: int | None, asset_type: str, name: str) -> Asset | None:
    """Newest approved asset; `config_id=None` selects configuration-independent assets (country PBFs)."""
    owner = Asset.config_profile_id.is_(None) if config_id is None else Asset.config_profile_id == config_id
    return (
        session.query(Asset)
        .filter(owner, Asset.asset_type == asset_type, Asset.name == name, Asset.status == "approved")
        .order_by(Asset.id.desc())
        .first()
    )


def find_extract(session: Session, asset_type: str, name: str, planet_record_id: int, poly_hash: str) -> Asset | None:
    """A non-rejected configuration-independent asset built from exactly this planet version and polygon."""
    candidates = (
        session.query(Asset)
        .filter(Asset.config_profile_id.is_(None), Asset.asset_type == asset_type, Asset.name == name, Asset.status != "rejected")
        .order_by(Asset.id.desc())
        .all()
    )
    return next(
        (a for a in candidates if a.meta_json.get("planet_record_id") == planet_record_id
         and a.meta_json.get("poly_hash") == poly_hash and abs_path(a).exists()),
        None,
    )


def for_run(session: Session, run_id: int) -> list[Asset]:
    return session.query(Asset).filter(Asset.produced_by_run_id == run_id).order_by(Asset.id).all()


def _files(asset: Asset) -> list[Path]:
    files = [abs_path(asset)]
    if asset.meta_json.get("geojson"):
        files.append(Path(config.DATA_ROOT) / asset.meta_json["geojson"])
    return files


def _remove_files(asset: Asset) -> None:
    for file in _files(asset):
        file.unlink(missing_ok=True)


def is_purged(asset: Asset) -> bool:
    return bool((asset.meta_json or {}).get("purged"))


def purge(session: Session, asset: Asset, package_tag: str) -> int:
    """Cleanup after packaging: remove the files, keep the row (hash, provenance, fingerprint) so stages still
    count as up to date. Returns the bytes that were on disk."""
    freed = sum(f.stat().st_size for f in _files(asset) if f.exists())
    extra = {}
    geojson = Path(config.DATA_ROOT) / asset.meta_json["geojson"] if asset.meta_json.get("geojson") else None
    if geojson is not None and geojson.exists():  # polygons: later stages only need the bounding box for their fingerprint
        try:
            import json

            from shapely.geometry import shape

            extra["bbox"] = list(shape(json.loads(geojson.read_text())).bounds)
        except (OSError, ValueError, KeyError):
            pass
    _remove_files(asset)
    asset.meta_json = {**asset.meta_json, **extra, "purged": {"package": package_tag, "at": datetime.datetime.now(datetime.UTC).isoformat()}}
    session.flush()
    return freed


def usable(asset: Asset) -> bool:
    """Present on disk, or knowingly purged (skip checks: a purged result is not a reason to rebuild)."""
    return is_purged(asset) or abs_path(asset).exists()


def input_path(asset: Asset) -> Path:
    """The file a stage reads; a clear error instead of a failure halfway when cleanup removed it."""
    path = abs_path(asset)
    if path.exists():
        return path
    purged = (asset.meta_json or {}).get("purged")
    reason = f"was purged by the cleanup (packaged in {purged['package']})" if purged else "is missing on disk"
    raise ValidationError(f"{asset.asset_type} '{asset.name}' {reason}: run the stage that produces it again.")


KEEP_ASSETS = {"country-pbf": 2}  # asset types that only keep their newest N approved versions per name


def apply_review(session: Session, run: BuildRun, approved: bool) -> None:
    """Approve or reject what the run produced; rejected assets lose their files (the row stays as a record).
    Approving also approves the still-`produced` assets the run reused (`report.asset_ids`)."""
    if approved:
        reused = [i for i in (run.report_json or {}).get("asset_ids", [])]
        if reused:
            session.query(Asset).filter(Asset.id.in_(reused), Asset.status == "produced").update({"status": "approved"})
    produced = session.query(Asset).filter(Asset.produced_by_run_id == run.id, Asset.status == "produced").all()
    for asset in produced:
        asset.status = "approved" if approved else "rejected"
        if not approved:
            _remove_files(asset)
    session.flush()
    if approved:
        for asset_type, keep in KEEP_ASSETS.items():
            prune(session, asset_type, keep)


def prune(session: Session, asset_type: str, keep: int) -> int:
    """Per (configuration, name) keep the newest `keep` approved assets; older approved ones lose files and rows."""
    rows = session.query(Asset).filter(Asset.asset_type == asset_type, Asset.status == "approved").order_by(Asset.id.desc()).all()
    seen: dict[tuple, int] = {}
    removed = 0
    for asset in rows:
        key = (asset.config_profile_id, asset.name)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > keep:
            _remove_files(asset)
            session.delete(asset)
            removed += 1
    return removed


def discard(session: Session, run_id: int) -> None:
    """Delete a run's assets entirely (files and rows), e.g. when the run failed."""
    for asset in for_run(session, run_id):
        _remove_files(asset)
        session.delete(asset)
    session.commit()
