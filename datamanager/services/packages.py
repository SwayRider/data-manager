"""The package repository: immutable, tagged copies of the approved build output (`RELEASE-CONTRACT.md` §2).

A package is a folder `PACKAGE_ROOT/<tag>/` holding the parts of one or more classes (tiles, valhalla,
pelias, geodata) and a `package.json` that is written last (its presence = complete). Parts are always
copied (the repository lives on another filesystem than DATA_ROOT) and hashed in the same pass. The
folder is self-describing: `reindex` rebuilds the DB rows from `package.json` + `labels.json`."""
import datetime
import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from datamanager.config import config
from datamanager.errors import PackageError
from datamanager.models import Asset, ConfigProfile, Package, PackageItem, PackageLabel
from datamanager.services import assets as asset_service
from datamanager.services import downloads, regions as region_service, resolve

CLASSES = ("tiles", "valhalla", "pelias", "geodata")
SCHEMA = 1
CHUNK = 8 * 1024 * 1024
FREE_SPACE_FACTOR = 1.05

# class -> [(asset type, destination relative to the class folder; `{region}` is filled in)] for per-region assets
REGION_PARTS = {
    "valhalla": [
        ("valhalla-tiles", "{region}/valhalla_tiles.tar"),
        ("valhalla-admin", "{region}/admin.sqlite"),
        ("valhalla-timezones", "{region}/tz_world.sqlite"),
    ],
    "pelias": [
        ("pelias-index-snapshot", "{region}/{region}.es-snapshot.tar"),
        ("pelias-config", "{region}/pelias.json"),
        ("pelias-wof", "{region}/wof.tar.gz"),
        ("pelias-interpolation-street-db", "{region}/interpolation/street.db"),
        ("pelias-interpolation-address-db", "{region}/interpolation/address.db"),
    ],
}
ProgressCb = Callable[[int, int, str], None]


@dataclass
class PlanItem:
    class_: str
    region: str | None
    rel_path: str  # relative to the package folder
    source: Path
    size: int
    expected_sha: str | None
    asset_id: int | None = None
    download_id: int | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class Plan:
    items: list[PlanItem]
    problems: list[str]
    regions: list[str]
    resolved_hashes: dict[str, str]

    @property
    def total_bytes(self) -> int:
        return sum(i.size for i in self.items)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


def root() -> Path:
    path = config.package_root
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---- planning ---------------------------------------------------------------------------------------------------

def _asset_item(session: Session, config_id: int, class_: str, region: str | None, asset_type: str, name: str,
                dest: str, problems: list[str], optional: bool = False) -> PlanItem | None:
    asset = asset_service.current(session, config_id, asset_type, name)
    if asset is None:
        if not optional:
            problems.append(f"{class_}: no approved {asset_type} asset '{name}'")
        return None
    source = asset_service.abs_path(asset)
    if not source.exists():
        problems.append(f"{class_}: file of {asset_type} '{name}' is gone ({asset.path}); rebuild the stage")
        return None
    return PlanItem(class_, region, f"{class_}/{dest}", source, source.stat().st_size, asset.content_hash,
                    asset_id=asset.id, meta={"asset_type": asset_type, "name": name, "run_id": asset.produced_by_run_id})


def plan(session: Session, config_id: int, classes: list[str] | None = None) -> Plan:
    """What a package of this configuration would contain, and what blocks it. Nothing is copied."""
    classes = list(classes or CLASSES)
    unknown = [c for c in classes if c not in CLASSES]
    if unknown:
        raise PackageError(f"Unknown class(es): {', '.join(unknown)}")
    resolved = resolve.resolve_config(session, config_id)
    regions = [r.name.lower() for r in resolved.regions]
    problems: list[str] = []
    items: list[PlanItem] = []
    if not regions:
        problems.append("The configuration has no regions")

    for class_ in classes:
        if class_ in REGION_PARTS:
            for region in regions:
                for asset_type, pattern in REGION_PARTS[class_]:
                    item = _asset_item(session, config_id, class_, region, asset_type, region,
                                       pattern.format(region=region), problems)
                    if item:
                        items.append(item)
        elif class_ == "geodata":
            for region in regions:
                for kind in ("core", "extended"):
                    item = _asset_item(session, config_id, class_, region, "region-outline", f"{region}-{kind}",
                                       f"contours/{region}-{kind}.geojson", problems)
                    if item:
                        items.append(item)
            for a, b in resolved.border_regions:
                a, b = a.lower(), b.lower()
                found = None
                for name in (f"{a}-{b}", f"{b}-{a}"):
                    found = _asset_item(session, config_id, class_, None, "border-crossings", name,
                                        f"border-crossings/{name}.csv", [], optional=True)
                    if found:
                        break
                if found:
                    items.append(found)
                else:
                    problems.append(f"geodata: no approved border-crossings for {a}/{b}")
        elif class_ == "tiles":
            record = downloads.resolve_version(session, "tiles:planet")
            if record is None or record.status != "approved":
                problems.append("tiles: no approved tiles:planet download")
            else:
                source = Path(config.DATA_ROOT) / record.local_path
                if not source.exists():
                    problems.append(f"tiles: planet file is gone ({record.local_path}); download it again")
                else:
                    items.append(PlanItem("tiles", None, "tiles/tiles.pmtiles", source, record.size_bytes,
                                          record.content_hash, download_id=record.id,
                                          meta={"source_key": record.source_key, "version": record.version_label,
                                                "data_timestamp": record.data_timestamp}))
            for name in ("style-light", "style-dark"):
                item = _asset_item(session, config_id, class_, None, "style", name, f"styles/{name}.json", problems)
                if item:
                    items.append(item)
    return Plan(items, problems, regions, {r.name.lower(): r.hash for r in resolved.regions})


# ---- creating ---------------------------------------------------------------------------------------------------

def _next_tag(session: Session) -> str:
    prefix = f"r-{_utcnow():%Y%m%d}-"
    taken = {t for (t,) in session.query(Package.tag).filter(Package.tag.like(prefix + "%"))}
    taken |= {p.name.removesuffix(".partial") for p in root().glob(prefix + "*")}
    n = 1
    while f"{prefix}{n}" in taken:
        n += 1
    return f"{prefix}{n}"


def _copy_hash(src: Path, dst: Path, on_bytes: Callable[[int], None]) -> str:
    digest = hashlib.sha256()
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        for chunk in iter(lambda: fin.read(CHUNK), b""):
            digest.update(chunk)
            fout.write(chunk)
            on_bytes(len(chunk))
        fout.flush()
        os.fsync(fout.fileno())
    return digest.hexdigest()


def _sha256_of(path: Path) -> str:
    return asset_service.sha256_of(path)


def create_package(session: Session, config_id: int, classes: list[str] | None = None, *, created_by: str = "operator",
                   labels: dict[str, str] | None = None, note: str = "", run_id: int | None = None,
                   progress: ProgressCb | None = None, step: Callable[[str], None] | None = None) -> Package:
    """Copy the approved inputs into `PACKAGE_ROOT/<tag>/`, hash them, write `package.json` last."""
    the_plan = plan(session, config_id, classes)
    if the_plan.problems:
        raise PackageError("Cannot package: " + "; ".join(the_plan.problems), problems=the_plan.problems)
    if not the_plan.items:
        raise PackageError("Nothing to package")
    repo = root()
    total = the_plan.total_bytes
    free = shutil.disk_usage(repo).free
    if free < total * FREE_SPACE_FACTOR:
        raise PackageError(f"Not enough space in {repo}: need {total * FREE_SPACE_FACTOR / 1e9:.1f} GB, free {free / 1e9:.1f} GB")

    config_row = session.get(ConfigProfile, config_id)
    tag = _next_tag(session)
    partial = repo / f"{tag}.partial"
    final = repo / tag
    package = Package(tag=tag, config_profile_id=config_id, status="building", path=str(final), note=note,
                      created_by=created_by, build_run_id=run_id, size_bytes=total)
    session.add(package)
    session.commit()

    done = 0
    parts: list[dict] = []
    try:
        for class_ in dict.fromkeys(i.class_ for i in the_plan.items):
            if step:
                step(f"Copy {class_}")
            for item in (i for i in the_plan.items if i.class_ == class_):
                def on_bytes(n: int, name=item.rel_path):
                    nonlocal done
                    done += n
                    if progress:
                        progress(done, total, name)
                sha = _copy_hash(item.source, partial / item.rel_path, on_bytes)
                if item.expected_sha and sha != item.expected_sha:
                    raise PackageError(f"{item.rel_path}: source changed on disk (hash {sha[:12]} != recorded {item.expected_sha[:12]})")
                parts.append({"class": class_, "region": item.region, "path": item.rel_path, "kind": "file",
                              "size": item.size, "sha256": sha,
                              "source": {k: v for k, v in (("asset_id", item.asset_id), ("download_id", item.download_id)) if v},
                              "meta": item.meta})
        created_at = _utcnow()
        tags = _auto_tags(config_row, the_plan, parts, created_by, created_at)
        document = {
            "schema": SCHEMA, "tag": tag, "created_at": created_at.isoformat() + "Z", "created_by": created_by,
            "config": {"name": config_row.name if config_row else None, "id": config_id,
                       "resolved_hashes": the_plan.resolved_hashes},
            "regions": the_plan.regions, "tags": tags,
            "classes": {c: {"parts": [p for p in parts if p["class"] == c]} for c in dict.fromkeys(p["class"] for p in parts)},
        }
        body = json.dumps(document, indent=2, sort_keys=True).encode()
        tmp = partial / "package.json.tmp"
        tmp.write_bytes(body)
        os.replace(tmp, partial / "package.json")  # last: its presence = complete
        os.replace(partial, final)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        package.status = "failed"
        session.commit()
        raise

    package.status = "complete"
    package.package_json_hash = hashlib.sha256(body).hexdigest()
    _store_rows(session, package, document)
    for key, value in (labels or {}).items():
        session.add(PackageLabel(package_id=package.id, key=key, value=value, origin="user"))
    session.commit()
    _write_labels_file(package)
    return package


def _auto_tags(config_row, the_plan: Plan, parts: list[dict], created_by: str, created_at: datetime.datetime) -> dict:
    tags = {
        "config": config_row.name if config_row else "",
        "regions": ",".join(the_plan.regions),
        "classes": ",".join(dict.fromkeys(p["class"] for p in parts)),
        "created": f"{created_at:%Y-%m-%d}",
        "created_by": created_by,
    }
    for region, digest in the_plan.resolved_hashes.items():
        tags[f"resolved_hash.{region}"] = digest[:16]
    runs = sorted({p["meta"]["run_id"] for p in parts if p["meta"].get("run_id")})
    if runs:
        tags["source_runs"] = ",".join(str(r) for r in runs)
    for p in parts:
        if p["class"] == "tiles" and p["meta"].get("version"):
            tags["tiles_build"] = p["meta"].get("data_timestamp") or p["meta"]["version"]
    return tags


def _store_rows(session: Session, package: Package, document: dict) -> None:
    session.query(PackageItem).filter_by(package_id=package.id).delete(synchronize_session="fetch")
    session.query(PackageLabel).filter_by(package_id=package.id, origin="auto").delete(synchronize_session="fetch")
    size = 0
    for class_, body in document["classes"].items():
        for p in body["parts"]:
            size += p["size"]
            session.add(PackageItem(
                package_id=package.id, class_=class_, region=p.get("region"), path=p["path"], kind=p.get("kind", "file"),
                size_bytes=p["size"], sha256=p["sha256"], asset_id=p.get("source", {}).get("asset_id"),
                download_id=p.get("source", {}).get("download_id"), meta_json=p.get("meta", {})))
    package.size_bytes = size
    for key, value in document.get("tags", {}).items():
        session.add(PackageLabel(package_id=package.id, key=key, value=str(value), origin="auto"))


# ---- labels, verify, delete, prune, reindex -----------------------------------------------------------------------

def _write_labels_file(package: Package) -> None:
    """Mutable user data next to (not inside) the hashed package content, so the folder stays self-describing."""
    data = {"note": package.note, "protected": package.protected,
            "labels": [{"key": l.key, "value": l.value} for l in package.labels if l.origin == "user"]}
    target = Path(package.path) / "labels.json"
    if target.parent.exists():
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, target)


def get(session: Session, tag: str) -> Package:
    package = session.query(Package).filter_by(tag=tag).first()
    if package is None:
        raise PackageError(f"No such package: {tag}")
    return package


def edit_labels(session: Session, tag: str, labels: dict[str, str] | None = None, note: str | None = None,
                protected: bool | None = None) -> Package:
    package = get(session, tag)
    if labels is not None:
        session.query(PackageLabel).filter_by(package_id=package.id, origin="user").delete(synchronize_session="fetch")
        for key, value in labels.items():
            session.add(PackageLabel(package_id=package.id, key=key, value=value, origin="user"))
    if note is not None:
        package.note = note[:500]
    if protected is not None:
        package.protected = protected
    session.commit()
    session.refresh(package)
    _write_labels_file(package)
    return package


def verify_package(session: Session, tag: str, progress: ProgressCb | None = None) -> list[str]:
    """Re-hash every file against package.json; returns the problems (empty = intact)."""
    package = get(session, tag)
    folder = Path(package.path)
    manifest = folder / "package.json"
    if not manifest.exists():
        return ["package.json is missing"]
    document = json.loads(manifest.read_text())
    problems: list[str] = []
    listed = {p["path"]: p for body in document["classes"].values() for p in body["parts"]}
    total = sum(p["size"] for p in listed.values())
    done = 0
    for rel, part in listed.items():
        file = folder / rel
        if not file.exists():
            problems.append(f"{rel}: missing")
            continue
        if file.stat().st_size != part["size"]:
            problems.append(f"{rel}: size {file.stat().st_size} != {part['size']}")
            continue
        if _sha256_of(file) != part["sha256"]:
            problems.append(f"{rel}: hash mismatch")
        done += part["size"]
        if progress:
            progress(done, total, rel)
    on_disk = {str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file()} - {"package.json", "labels.json"}
    problems += [f"{rel}: not listed in package.json" for rel in sorted(on_disk - set(listed))]
    return problems


def delete(session: Session, tag: str) -> None:
    package = get(session, tag)
    if package.protected:
        raise PackageError(f"{tag} is protected")
    shutil.rmtree(package.path, ignore_errors=True)
    session.delete(package)
    session.commit()


def prune(session: Session, keep: int, dry_run: bool = True) -> list[str]:
    """Delete the oldest complete, unprotected packages beyond the newest `keep`."""
    rows = session.query(Package).filter_by(status="complete").order_by(Package.id.desc()).all()
    victims = [p.tag for p in rows[keep:] if not p.protected]
    if not dry_run:
        for tag in victims:
            delete(session, tag)
    return victims


def reindex(session: Session) -> dict:
    """Rebuild the DB rows from the self-describing folders (after moving the repository or losing the DB)."""
    repo = root()
    added = updated = 0
    for manifest in sorted(repo.glob("*/package.json")):
        folder = manifest.parent
        body = manifest.read_bytes()
        document = json.loads(body)
        package = session.query(Package).filter_by(tag=document["tag"]).first()
        if package is None:
            config_id = document.get("config", {}).get("id")
            if config_id is not None and session.get(ConfigProfile, config_id) is None:
                config_id = None
            package = Package(tag=document["tag"], config_profile_id=config_id, status="complete", path=str(folder),
                              created_by=document.get("created_by", "operator"),
                              created_at=datetime.datetime.fromisoformat(document["created_at"].removesuffix("Z")))
            session.add(package)
            session.flush()
            added += 1
        else:
            updated += 1
        package.status, package.path = "complete", str(folder)
        package.package_json_hash = hashlib.sha256(body).hexdigest()
        _store_rows(session, package, document)
        labels_file = folder / "labels.json"
        if labels_file.exists():
            data = json.loads(labels_file.read_text())
            package.note, package.protected = data.get("note", ""), bool(data.get("protected"))
            session.query(PackageLabel).filter_by(package_id=package.id, origin="user").delete(synchronize_session="fetch")
            for label in data.get("labels", []):
                session.add(PackageLabel(package_id=package.id, key=label["key"], value=label["value"], origin="user"))
    swept = 0
    for partial in repo.glob("*.partial"):
        shutil.rmtree(partial, ignore_errors=True)
        swept += 1
    session.commit()
    return {"added": added, "updated": updated, "partials_removed": swept}
