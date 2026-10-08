"""Driver `compose-single-machine`: the target is this machine (or, later, one reached over ssh), every artifact class has its
own root with `releases/<tag>`, and a relative `current` / `previous` symlink (`RELEASE-CONTRACT.md` §3.2).

Procedure per class: copy into `releases/<tag>.partial` (resumable, sha256 checked while copying) -> verify -> rename ->
switch `current` (the old one becomes `previous`) -> activate -> health check -> prune. A failed activation switches
back. The target keeps only `current` and `previous`: everything else is removed after a healthy deploy.

Tiles go to the object store (a later transport), not through this module."""
import gzip
import hashlib
import json
import os
import shutil
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

from datamanager.deploy.base import (ACTIVATORS, ActivationContext, Activator, DeployDriver, PackageView, PartView,
                                     ProgressCb, StepCb)
from datamanager.deploy import activators  # noqa: F401  (registers the activator types)
from datamanager.deploy.registry import register
from datamanager.errors import DeployError

CHUNK = 8 * 1024 * 1024
PROGRESS_EVERY = 1.0
FREE_SPACE_FACTOR = 1.1
STATE_FILE = ".deploy.json"
FILE_CLASSES = ("geodata", "valhalla", "pelias")
NOT_REGIONS = {"placeholder", "contours", "border-crossings"}


@dataclass(frozen=True)
class Target:
    """Where a package part lands: `file` is copied, `wof` and `snapshot` are tar files that are unpacked,
    `gunzip` is a gzip file that is unpacked. `dest` is relative to the release (or to the Elasticsearch snapshot
    directory for `snapshot`)."""

    kind: str
    dest: str


def target_of(class_: str, part: PartView) -> Target:
    """Package layout -> target layout (`RELEASE-CONTRACT.md` §2.1 and the infra layout)."""
    rel = part.path.removeprefix(f"{class_}/")
    if class_ == "pelias":
        if rel.endswith("/wof.tar.gz"):
            return Target("wof", rel.removesuffix("/wof.tar.gz") + "/wof/sqlite")
        if rel.endswith(".es-snapshot.tar"):
            return Target("snapshot", rel.split("/")[0])
        if rel == "placeholder/store.sqlite3.gz":
            return Target("gunzip", "placeholder/data/store.sqlite3")
    return Target("file", rel)


class _HashingReader:
    """File-like wrapper that hashes what is read and reports the bytes."""

    def __init__(self, raw, on_bytes):
        self.raw, self.on_bytes, self.digest = raw, on_bytes, hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        block = self.raw.read(size)
        self.digest.update(block)
        self.on_bytes(len(block))
        return block

    def drain(self) -> None:
        while self.read(CHUNK):
            pass


class _Progress:
    def __init__(self, progress: ProgressCb | None, total: int):
        self.progress, self.total, self.done, self.last = progress, total, 0, 0.0

    def add(self, n: int, label: str) -> None:
        self.done += n
        if self.progress and time.monotonic() - self.last >= PROGRESS_EVERY:
            self.last = time.monotonic()
            self.progress(self.done, self.total, label)

    def flush(self, label: str) -> None:
        if self.progress:
            self.progress(self.done, self.total, label)


def _sha256_file(path: Path, on_bytes=lambda n: None) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            digest.update(block)
            on_bytes(len(block))
    return digest.hexdigest()


@register
class ComposeSingleMachineDriver(DeployDriver):
    key = "compose-single-machine"

    def __init__(self, config: dict, options: dict | None = None):
        problems = self.validate_config(config)
        if problems:
            raise DeployError("Invalid deploy configuration: " + "; ".join(problems), problems=problems)
        self.config = config
        self.options = {"verify": "full", "health_timeout": 300, **(options or {})}
        self._tiles_transport = None

    # ---- configuration ------------------------------------------------------------------------------------------

    def ensure_base(self, step: StepCb | None = None) -> list[str]:
        """Services no release depends on (auth, mail, ...): started when missing, never recreated. Warnings, not errors."""
        block = self.config.get("ensure") or {}
        if not block.get("services"):
            return []
        if step:
            step("Ensure base services")
        return activators.ensure_services(block.get("compose_file"), block["services"])

    @classmethod
    def validate_config(cls, config: dict) -> list[str]:
        problems = []
        if config.get("host"):
            problems.append("host: only the local machine (host = null) is supported so far")
        classes = config.get("classes")
        if not isinstance(classes, dict) or not classes:
            return problems + ["classes: at least one class is required"]
        for name, block in classes.items():
            if name == "tiles":
                from datamanager.deploy.s3_tiles import validate_tiles_block

                problems += validate_tiles_block(block or {})
                continue
            if name not in FILE_CLASSES:
                problems.append(f"classes.{name}: unknown class")
                continue
            root = (block or {}).get("root")
            if not isinstance(root, str) or not os.path.isabs(root):
                problems.append(f"classes.{name}.root: an absolute path is required")
            if name == "pelias" and not os.path.isabs(str((block or {}).get("es_snapshots") or "")):
                problems.append("classes.pelias.es_snapshots: an absolute path is required")
            activate = (block or {}).get("activate") or {"type": "none"}
            if activate.get("type") not in ACTIVATORS:
                problems.append(f"classes.{name}.activate.type: unknown '{activate.get('type')}'")
            after = activate.get("ensure_after")
            if after is not None and not (isinstance(after, dict) and isinstance(after.get("services"), list)):
                problems.append(f"classes.{name}.activate.ensure_after: {{\"compose_file\": ..., \"services\": [...]}} is required")
        ensure = config.get("ensure")
        if ensure is not None and not (isinstance(ensure, dict) and isinstance(ensure.get("services"), list)
                                       and (not ensure["services"] or os.path.isabs(str(ensure.get("compose_file") or "")))):
            problems.append("ensure: {\"compose_file\": <absolute path>, \"services\": [...]} is required")
        order = config.get("activation_order")
        if order is not None and (not isinstance(order, list) or not set(order) <= set(classes)):
            problems.append("activation_order: must list configured classes only")
        return problems

    def _tiles(self):
        from datamanager.deploy.s3_tiles import TilesTransport

        if "tiles" not in self.config["classes"]:
            raise DeployError("Class tiles is not configured")
        if self._tiles_transport is None:
            self._tiles_transport = TilesTransport(self.config["classes"]["tiles"], self.options)
        return self._tiles_transport

    def class_names(self) -> list[str]:
        order = self.config.get("activation_order") or list(self.config["classes"])
        return [c for c in order if c in self.config["classes"]]

    def _block(self, class_: str) -> dict:
        if class_ not in self.config["classes"] or class_ not in FILE_CLASSES:
            raise DeployError(f"Class {class_} is not handled by this transport")
        return self.config["classes"][class_]

    def root(self, class_: str) -> Path:
        return Path(self._block(class_)["root"])

    def _activator(self, class_: str) -> tuple[Activator, dict]:
        settings = self._block(class_).get("activate") or {"type": "none"}
        return ACTIVATORS[settings["type"]](), settings

    def _context(self, class_: str, tag: str) -> ActivationContext:
        settings = self._activator(class_)[1]
        root = self.root(class_)
        kept = tuple(sorted({self._pointer(root, "current"), self._pointer(root, "previous")} - {None}))
        return ActivationContext(class_, tag, root, self.regions_of(class_, tag), settings,
                                 {**self._block(class_), **self.options}, kept)

    # ---- state on the target ------------------------------------------------------------------------------------

    @staticmethod
    def _pointer(root: Path, name: str) -> str | None:
        link = root / name
        return os.path.basename(os.readlink(link)) if link.is_symlink() else None

    @staticmethod
    def _releases(root: Path) -> list[str]:
        folder = root / "releases"
        if not folder.is_dir():
            return []
        return sorted(p.name for p in folder.iterdir() if p.is_dir() and not p.name.endswith(".partial"))

    def regions_of(self, class_: str, tag: str) -> tuple[str, ...]:
        folder = self.root(class_) / "releases" / tag
        if not folder.is_dir():
            return ()
        return tuple(sorted(p.name for p in folder.iterdir() if p.is_dir() and p.name not in NOT_REGIONS))

    def describe_state(self, classes: list[str] | None = None) -> dict[str, dict]:
        state = {}
        for name in classes or self.class_names():
            if name == "tiles":
                state[name] = self._tiles().describe_state()
                continue
            root = self.root(name)
            state[name] = {"root": str(root), "current": self._pointer(root, "current"),
                           "previous": self._pointer(root, "previous"), "releases": self._releases(root),
                           "partial": sorted(p.name for p in (root / "releases").glob("*.partial"))}
        return state

    def _es_snapshots(self) -> Path:
        return Path(self._block("pelias")["es_snapshots"])

    # ---- plan ---------------------------------------------------------------------------------------------------

    def plan_class(self, package: PackageView, class_: str) -> dict:
        if class_ == "tiles":
            return self._tiles().plan_class(package)
        parts = package.of_class(class_)
        root = self.root(class_)
        problems, skip = [], None
        if not parts:
            problems.append(f"the package has no {class_} class")
        for p in parts:
            if not (package.path / p.path).is_file():
                problems.append(f"{p.path}: missing in the package")
        current = self._pointer(root, "current")
        if current == package.tag:
            skip = "already current"
        done = self._done(root / "releases" / f"{package.tag}.partial")
        todo = [p for p in parts if done.get(p.path) != p.sha256]
        present = (root / "releases" / package.tag).is_dir()
        need_root = 0 if present else sum(p.size for p in todo if target_of(class_, p).kind != "snapshot")
        need_snap = 0 if present else sum(p.size for p in todo if target_of(class_, p).kind == "snapshot")
        free_root = shutil.disk_usage(_existing_parent(root)).free
        if need_root * FREE_SPACE_FACTOR > free_root and not skip:
            problems.append(f"not enough free space on {root}: need {need_root * FREE_SPACE_FACTOR / 1e9:.1f} GB, "
                            f"free {free_root / 1e9:.1f} GB")
        if need_snap and not skip:
            snap = self._es_snapshots()
            free_snap = shutil.disk_usage(_existing_parent(snap)).free
            if need_snap * FREE_SPACE_FACTOR > free_snap:
                problems.append(f"not enough free space on {snap}: need {need_snap * FREE_SPACE_FACTOR / 1e9:.1f} GB, "
                                f"free {free_snap / 1e9:.1f} GB")
        return {"class": class_, "tag": package.tag, "parts": len(parts), "bytes": sum(p.size for p in parts),
                "bytes_to_copy": need_root + need_snap, "present": present, "current": current,
                "previous": self._pointer(root, "previous"), "skip": skip, "problems": problems}

    # ---- transfer -----------------------------------------------------------------------------------------------

    @staticmethod
    def _done(partial: Path) -> dict[str, str]:
        state = partial / STATE_FILE
        try:
            return json.loads(state.read_text()).get("done", {}) if state.exists() else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _record(folder: Path, tag: str, class_: str, done: dict[str, str]) -> None:
        tmp = folder / (STATE_FILE + ".tmp")
        tmp.write_text(json.dumps({"tag": tag, "class": class_, "done": done}, indent=1))
        os.replace(tmp, folder / STATE_FILE)

    def _transfer(self, package: PackageView, class_: str, partial: Path, progress: ProgressCb | None,
                  step: StepCb | None) -> None:
        parts = package.of_class(class_)
        # the generated manifest is the last file written into a release
        parts.sort(key=lambda p: (p.kind == "manifest", p.path))
        done = self._done(partial)
        todo = [p for p in parts if done.get(p.path) != p.sha256]
        meter = _Progress(progress, sum(p.size for p in todo))
        partial.mkdir(parents=True, exist_ok=True)
        for index, part in enumerate(todo, 1):
            label = f"{class_} {index}/{len(todo)}: {part.path}"
            if step:
                step(f"Copy {part.path}")
            target = target_of(class_, part)
            source = package.path / part.path
            try:
                self._copy_part(source, part, target, partial, package.tag, lambda n: meter.add(n, label))
            except OSError as exc:
                raise DeployError(f"{part.path}: {exc}") from exc
            done[part.path] = part.sha256
            self._record(partial, package.tag, class_, done)
            meter.flush(label)

    def _copy_part(self, source: Path, part: PartView, target: Target, partial: Path, tag: str, on_bytes) -> None:
        kind = target.kind
        if kind == "snapshot":
            dest = self._es_snapshots() / tag / target.dest
        else:
            dest = partial / target.dest
        if kind == "file":
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".tmp")
            digest = hashlib.sha256()
            with open(source, "rb") as fin, open(tmp, "wb") as fout:
                for block in iter(lambda: fin.read(CHUNK), b""):
                    digest.update(block)
                    fout.write(block)
                    on_bytes(len(block))
            if digest.hexdigest() != part.sha256:
                tmp.unlink()
                raise DeployError(f"{part.path}: the package file changed or is corrupt (sha256 does not match)")
            os.replace(tmp, dest)
            return
        # unpacked kinds: hash the stream while unpacking, one read of the source
        if dest.exists():
            shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(source, "rb") as raw:
            reader = _HashingReader(raw, on_bytes)
            if kind == "gunzip":
                with gzip.GzipFile(fileobj=reader) as unzipped, open(dest, "wb") as out:
                    shutil.copyfileobj(unzipped, out, CHUNK)
            else:
                dest.mkdir(parents=True)
                with tarfile.open(fileobj=reader, mode="r|*") as tar:
                    tar.extractall(dest, filter="data")
            reader.drain()
        if reader.digest.hexdigest() != part.sha256:
            shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
            raise DeployError(f"{part.path}: the package file changed or is corrupt (sha256 does not match)")

    # ---- verify -------------------------------------------------------------------------------------------------

    def _verify(self, package: PackageView, class_: str, folder: Path, progress: ProgressCb | None,
                step: StepCb | None, fresh: bool = True) -> None:
        mode = self.options["verify"]
        parts = package.of_class(class_)
        files = [(p, target_of(class_, p)) for p in parts]
        meter = _Progress(progress, sum(p.size for p, t in files if t.kind == "file") if mode == "full" else 0)
        problems = []
        for part, target in files:
            if target.kind == "file":
                path = folder / target.dest
                if not path.is_file():
                    problems.append(f"{target.dest}: missing")
                elif path.stat().st_size != part.size:
                    problems.append(f"{target.dest}: size {path.stat().st_size} != {part.size}")
                elif mode == "full":
                    if step:
                        step(f"Verify {target.dest}")
                    if _sha256_file(path, lambda n: meter.add(n, f"{class_}: {target.dest}")) != part.sha256:
                        problems.append(f"{target.dest}: sha256 mismatch")
            else:
                # unpacked content has no hash of its own: the source stream was hashed while unpacking
                if target.kind == "snapshot" and not fresh:
                    continue  # unpacked snapshots are removed once the release is live (the index lives in Elasticsearch)
                where = (self._es_snapshots() / package.tag / target.dest) if target.kind == "snapshot" else folder / target.dest
                if not where.exists():
                    problems.append(f"{target.dest}: missing")
        if problems:
            raise DeployError(f"{class_}: verification of {package.tag} failed: " + "; ".join(problems[:5]),
                              problems=problems)

    # ---- switch -------------------------------------------------------------------------------------------------

    @staticmethod
    def _switch(root: Path, name: str, tag: str | None) -> None:
        link = root / name
        if link.exists() and not link.is_symlink():
            raise DeployError(f"{link} exists and is not a symlink; remove or move it first")
        if tag is None:
            if link.is_symlink():
                link.unlink()
            return
        tmp = root / f"{name}.tmp"
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        os.symlink(f"releases/{tag}", tmp)  # relative: the root can move
        os.replace(tmp, link)

    def _flip(self, class_: str, tag: str, previous: str | None) -> dict:
        """current -> tag, previous -> `previous`; returns what was there before so it can be restored."""
        root = self.root(class_)
        before = {"current": self._pointer(root, "current"), "previous": self._pointer(root, "previous")}
        self._switch(root, "previous", previous)
        self._switch(root, "current", tag)
        return before

    def _restore(self, class_: str, before: dict) -> None:
        root = self.root(class_)
        self._switch(root, "current", before["current"])
        self._switch(root, "previous", before["previous"])

    # ---- activate ------------------------------------------------------------------------------------------------

    def _activate(self, class_: str, tag: str, step: StepCb | None) -> None:
        activator, _ = self._activator(class_)
        ctx = self._context(class_, tag)
        if step:
            step(f"Activate {class_}")
        activator.activate(ctx)
        if step:
            step(f"Health check {class_}")
        activator.check_health(ctx)

    # ---- prune -------------------------------------------------------------------------------------------------

    def prune_class(self, class_: str, dry_run: bool = False) -> list[str]:
        """Remove every release except `current` and `previous`, and leftover .partial folders."""
        if class_ == "tiles":
            return self._tiles().prune_class(dry_run)
        root = self.root(class_)
        keep = {self._pointer(root, "current"), self._pointer(root, "previous")} - {None}
        doomed = [t for t in self._releases(root) if t not in keep]
        stale = sorted(p.name for p in (root / "releases").glob("*.partial")) if (root / "releases").is_dir() else []
        if dry_run:
            return doomed + stale
        activator, _ = self._activator(class_)
        for tag in doomed:
            activator.release_removed(self._context(class_, tag))
            shutil.rmtree(root / "releases" / tag)
            if class_ == "pelias":
                shutil.rmtree(self._es_snapshots() / tag, ignore_errors=True)
        for name in stale:
            shutil.rmtree(root / "releases" / name, ignore_errors=True)
            if class_ == "pelias":
                shutil.rmtree(self._es_snapshots() / name.removesuffix(".partial"), ignore_errors=True)
        return doomed + stale

    # ---- the whole class ------------------------------------------------------------------------------------------

    def deploy_class(self, package: PackageView, class_: str, progress: ProgressCb | None = None,
                     step: StepCb | None = None) -> dict:
        if class_ == "tiles":
            return self._tiles().deploy_class(package, progress, step)
        plan = self.plan_class(package, class_)
        if plan["problems"]:
            raise DeployError(f"{class_}: " + "; ".join(plan["problems"]), problems=plan["problems"])
        if plan["skip"]:  # nothing to copy, but the services around the class still get their chance to start
            return {"class": class_, "status": "skipped", "reason": plan["skip"], "current": package.tag,
                    "warnings": activators.ensure_after(self._block(class_).get("activate") or {})}
        root, tag = self.root(class_), package.tag
        final, partial = root / "releases" / tag, root / "releases" / f"{tag}.partial"
        root.mkdir(parents=True, exist_ok=True)
        if not final.is_dir():
            self._transfer(package, class_, partial, progress, step)
            self._verify(package, class_, partial, progress, step)
            if step:
                step(f"Finalize {class_}")
            partial.rename(final)
        else:
            self._verify(package, class_, final, progress, step, fresh=False)
        old_current = self._pointer(root, "current")
        before = self._flip(class_, tag, previous=old_current)
        try:
            self._activate(class_, tag, step)
        except Exception as exc:
            self._restore(class_, before)
            if before["current"]:  # bring the services back on the release that worked
                try:
                    self._activate(class_, before["current"], None)
                except Exception as again:  # report both; the symlink is already back
                    raise DeployError(f"{class_}: activation of {tag} failed ({exc}); switched back to "
                                      f"{before['current']}, which did not come up either ({again})") from exc
            if not before["current"]:
                try:
                    self._activator(class_)[0].abandon(self._context(class_, tag))
                except Exception:
                    pass
            raise DeployError(f"{class_}: activation of {tag} failed ({exc}); "
                              + (f"switched back to {before['current']}" if before["current"] else "nothing was live before")) from exc
        warnings = activators.ensure_after(self._block(class_).get("activate") or {})
        try:
            self._activator(class_)[0].finalize(self._context(class_, tag))
        except Exception as exc:  # the release is live; leftovers are only wasted space
            warnings.append(f"cleanup after activation failed: {exc}")
        try:
            removed = self.prune_class(class_)
        except Exception as exc:  # the deploy itself succeeded
            removed, warnings = [], warnings + [f"cleanup of old releases failed: {exc}"]
        return {"class": class_, "status": "ok", "current": tag, "previous": old_current, "removed": removed,
                "warnings": warnings}

    def rollback_class(self, class_: str, step: StepCb | None = None) -> dict:
        if class_ == "tiles":
            return self._tiles().rollback_class(step)
        root = self.root(class_)
        current, previous = self._pointer(root, "current"), self._pointer(root, "previous")
        if not previous or not (root / "releases" / previous).is_dir():
            raise DeployError(f"{class_}: there is no previous release to roll back to")
        before = self._flip(class_, previous, previous=None)
        try:
            self._activate(class_, previous, step)
        except Exception as exc:
            self._restore(class_, before)
            try:
                self._activate(class_, current, None)
            except Exception:
                pass
            raise DeployError(f"{class_}: rollback to {previous} failed ({exc}); {current} is current again") from exc
        removed = self.prune_class(class_)  # the release that was rolled back is gone from the target
        return {"class": class_, "status": "rolled_back", "current": previous, "rolled_back": current, "removed": removed}


def _existing_parent(path: Path) -> Path:
    while not path.exists() and path.parent != path:
        path = path.parent
    return path
