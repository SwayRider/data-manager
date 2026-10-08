"""The tiles class goes to the object store (Garage), not to a directory: `releases/<tag>/…` objects, then the pointer
object `current.json` (`previous.json` keeps the release before it). Same procedure as the file classes: upload ->
verify -> switch pointer -> activate -> health check -> prune, switch back on failure; the store keeps only the
`current` and `previous` release prefixes. Contract: `SERVICES.md` ("Contract: the release in the object store")
and `RELEASE-CONTRACT.md` §3.3.

Credentials come from the environment variables named in the configuration, never from the configuration itself."""
import datetime
import hashlib
import json
import os
import time

from datamanager.deploy.base import ACTIVATORS, ActivationContext, PackageView, ProgressCb, StepCb
from datamanager.errors import DeployError

PART_SIZE = 64 * 1024 * 1024  # a 140 GB planet is ~2100 parts, far below the 10000-part limit
SINGLE_PUT_MAX = 8 * 1024 * 1024
PROGRESS_EVERY = 1.0
CURRENT, PREVIOUS = "current.json", "previous.json"


def validate_tiles_block(block: dict) -> list[str]:
    problems = []
    if block.get("transport") != "s3":
        problems.append("classes.tiles.transport: must be 's3'")
    if not str(block.get("endpoint") or "").startswith(("http://", "https://")):
        problems.append("classes.tiles.endpoint: an http(s) URL is required")
    if not block.get("bucket"):
        problems.append("classes.tiles.bucket: required")
    credentials = block.get("credentials") or {}
    for key in ("access_key_env", "secret_key_env"):
        if not credentials.get(key):
            problems.append(f"classes.tiles.credentials.{key}: the name of an environment variable is required")
    kind = (block.get("activate") or {"type": "none"}).get("type")
    if kind not in ACTIVATORS:
        problems.append(f"classes.tiles.activate.type: unknown '{kind}'")
    return problems


def make_client(block: dict):
    """boto3 client for the configured store. Replaced by a fake in tests."""
    import boto3
    from botocore.config import Config

    credentials = block["credentials"]
    access, secret = os.environ.get(credentials["access_key_env"]), os.environ.get(credentials["secret_key_env"])
    if not access or not secret:
        raise DeployError(f"The environment variables {credentials['access_key_env']} / {credentials['secret_key_env']} "
                          "are not set for the data-manager process")
    return boto3.client(
        "s3", endpoint_url=block["endpoint"], region_name=block.get("region", "garage"), aws_access_key_id=access,
        aws_secret_access_key=secret,
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 5, "mode": "standard"},
                      request_checksum_calculation="when_required", response_checksum_validation="when_required"))


def _key(tag: str, part_path: str) -> str:
    return f"releases/{tag}/{part_path.removeprefix('tiles/')}"


def _pointer(tag: str) -> bytes:
    return json.dumps({"schema": 1, "release": tag, "prefix": f"releases/{tag}/",
                       "updated_at": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}, indent=1).encode()


class TilesTransport:
    def __init__(self, block: dict, options: dict):
        self.block, self.options = block, options
        self.bucket = block["bucket"]
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = make_client(self.block)
        return self._client

    # ---- objects ------------------------------------------------------------------------------------------------

    @staticmethod
    def _missing(exc: Exception) -> bool:
        code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
        return code in ("404", "NoSuchKey", "NotFound")

    def _head(self, key: str) -> dict | None:
        try:
            return self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if self._missing(exc):
                return None
            raise DeployError(f"object store: HEAD {key}: {exc}") from exc

    def _read_pointer(self, name: str) -> str | None:
        try:
            body = self.client.get_object(Bucket=self.bucket, Key=name)["Body"].read()
        except Exception as exc:
            if self._missing(exc):
                return None
            raise DeployError(f"object store: GET {name}: {exc}") from exc
        try:
            return json.loads(body)["release"]
        except (ValueError, KeyError) as exc:
            raise DeployError(f"object store: {name} is not a release pointer") from exc

    def _write_pointer(self, name: str, tag: str | None) -> None:
        try:
            if tag is None:
                self.client.delete_object(Bucket=self.bucket, Key=name)
            else:
                self.client.put_object(Bucket=self.bucket, Key=name, Body=_pointer(tag), ContentType="application/json")
        except Exception as exc:
            raise DeployError(f"object store: writing {name}: {exc}") from exc

    def _release_prefixes(self) -> list[str]:
        tags, token = set(), None
        while True:
            args = {"Bucket": self.bucket, "Prefix": "releases/", "Delimiter": "/"}
            if token:
                args["ContinuationToken"] = token
            page = self.client.list_objects_v2(**args)
            tags |= {p["Prefix"].split("/")[1] for p in page.get("CommonPrefixes", [])}
            token = page.get("NextContinuationToken")
            if not token:
                return sorted(tags)

    # ---- state / plan -------------------------------------------------------------------------------------------

    def describe_state(self) -> dict:
        return {"root": f"s3://{self.bucket}", "current": self._read_pointer(CURRENT),
                "previous": self._read_pointer(PREVIOUS), "releases": self._release_prefixes(), "partial": []}

    def _stored(self, tag: str, part) -> bool:
        head = self._head(_key(tag, part.path))
        return bool(head and head["ContentLength"] == part.size and (head.get("Metadata") or {}).get("sha256") == part.sha256)

    def plan_class(self, package: PackageView) -> dict:
        parts = package.of_class("tiles")
        problems, skip = [], None
        if not parts:
            problems.append("the package has no tiles class")
        for part in parts:
            if not (package.path / part.path).is_file():
                problems.append(f"{part.path}: missing in the package")
        current = None
        todo = parts
        try:
            current = self._read_pointer(CURRENT)
            if current == package.tag:
                skip = "already current"
            todo = [p for p in parts if not self._stored(package.tag, p)]
        except DeployError as exc:
            problems.append(str(exc))
        return {"class": "tiles", "tag": package.tag, "parts": len(parts), "bytes": sum(p.size for p in parts),
                "bytes_to_copy": sum(p.size for p in todo), "present": bool(parts) and not todo, "current": current,
                "previous": None, "skip": skip, "problems": problems}

    # ---- upload -------------------------------------------------------------------------------------------------

    def _upload(self, source, key: str, part, on_bytes) -> None:
        metadata = {"sha256": part.sha256}
        digest = hashlib.sha256()
        if part.size <= SINGLE_PUT_MAX:
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != part.sha256:
                raise DeployError(f"{part.path}: the package file changed or is corrupt (sha256 does not match)")
            on_bytes(len(data))
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data, Metadata=metadata)
            return
        upload = self.client.create_multipart_upload(Bucket=self.bucket, Key=key, Metadata=metadata)
        upload_id, parts = upload["UploadId"], []
        try:
            with open(source, "rb") as f:
                number = 0
                for chunk in iter(lambda: f.read(PART_SIZE), b""):
                    number += 1
                    digest.update(chunk)
                    answer = self.client.upload_part(Bucket=self.bucket, Key=key, UploadId=upload_id, PartNumber=number, Body=chunk)
                    parts.append({"ETag": answer["ETag"], "PartNumber": number})
                    on_bytes(len(chunk))
            if digest.hexdigest() != part.sha256:
                raise DeployError(f"{part.path}: the package file changed or is corrupt (sha256 does not match)")
            self.client.complete_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts})
        except BaseException:
            try:
                self.client.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)
            except Exception:
                pass
            raise

    def _transfer(self, package: PackageView, progress: ProgressCb | None, step: StepCb | None) -> None:
        parts = sorted(package.of_class("tiles"), key=lambda p: (p.kind == "manifest", p.path))  # the manifest goes last
        todo = [p for p in parts if not self._stored(package.tag, p)]
        total, state = sum(p.size for p in todo), {"done": 0, "last": 0.0}
        for index, part in enumerate(todo, 1):
            label = f"tiles {index}/{len(todo)}: {part.path}"
            if step:
                step(f"Upload {part.path}")

            def on_bytes(n, label=label):
                state["done"] += n
                if progress and time.monotonic() - state["last"] >= PROGRESS_EVERY:
                    state["last"] = time.monotonic()
                    progress(state["done"], total, label)
            try:
                self._upload(package.path / part.path, _key(package.tag, part.path), part, on_bytes)
            except DeployError:
                raise
            except Exception as exc:
                raise DeployError(f"{part.path}: upload failed: {exc}") from exc
            if progress:
                progress(state["done"], total, label)

    def _verify(self, package: PackageView) -> None:
        """Every object is there with the right size and the sha256 recorded when it was uploaded (the content hash was
        checked against the source while the bytes went out; the 140 GB are not read back)."""
        problems = []
        for part in package.of_class("tiles"):
            head = self._head(_key(package.tag, part.path))
            if head is None:
                problems.append(f"{part.path}: missing")
            elif head["ContentLength"] != part.size:
                problems.append(f"{part.path}: size {head['ContentLength']} != {part.size}")
            elif self.options.get("verify", "full") == "full" and (head.get("Metadata") or {}).get("sha256") != part.sha256:
                problems.append(f"{part.path}: sha256 metadata does not match")
        if problems:
            raise DeployError(f"tiles: verification of {package.tag} failed: " + "; ".join(problems[:5]), problems=problems)

    # ---- activate -----------------------------------------------------------------------------------------------

    def _context(self, tag: str) -> ActivationContext:
        from pathlib import Path

        settings = self.block.get("activate") or {"type": "none"}
        kept = tuple(sorted({self._read_pointer(CURRENT), self._read_pointer(PREVIOUS)} - {None}))
        root = Path(settings.get("env_file", "/")).parent
        return ActivationContext("tiles", tag, root, (), settings, {**self.block, **self.options}, kept)

    def _activator(self):
        return ACTIVATORS[(self.block.get("activate") or {"type": "none"})["type"]]()

    def _activate(self, tag: str, step: StepCb | None) -> None:
        activator, ctx = self._activator(), self._context(tag)
        if step:
            step("Activate tiles")
        activator.activate(ctx)
        if step:
            step("Health check tiles")
        activator.check_health(ctx)

    # ---- the whole class ----------------------------------------------------------------------------------------

    def prune_class(self, dry_run: bool = False) -> list[str]:
        keep = {self._read_pointer(CURRENT), self._read_pointer(PREVIOUS)} - {None}
        doomed = [t for t in self._release_prefixes() if t not in keep]
        if dry_run:
            return doomed
        for tag in doomed:
            self._activator().release_removed(self._context(tag))
            token = None
            while True:
                args = {"Bucket": self.bucket, "Prefix": f"releases/{tag}/"}
                if token:
                    args["ContinuationToken"] = token
                page = self.client.list_objects_v2(**args)
                keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if keys:
                    self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": keys, "Quiet": True})
                token = page.get("NextContinuationToken")
                if not token:
                    break
        return doomed

    def _switch_back(self, before: dict) -> None:
        self._write_pointer(CURRENT, before["current"])
        self._write_pointer(PREVIOUS, before["previous"])

    def deploy_class(self, package: PackageView, progress: ProgressCb | None = None, step: StepCb | None = None) -> dict:
        plan = self.plan_class(package)
        if plan["problems"]:
            raise DeployError("tiles: " + "; ".join(plan["problems"]), problems=plan["problems"])
        if plan["skip"]:
            return {"class": "tiles", "status": "skipped", "reason": plan["skip"], "current": package.tag}
        tag = package.tag
        self._transfer(package, progress, step)
        if step:
            step("Verify tiles")
        self._verify(package)
        before = {"current": self._read_pointer(CURRENT), "previous": self._read_pointer(PREVIOUS)}
        self._write_pointer(PREVIOUS, before["current"])
        self._write_pointer(CURRENT, tag)  # last: the release is complete
        try:
            self._activate(tag, step)
        except Exception as exc:
            self._switch_back(before)
            if before["current"]:
                try:
                    self._activate(before["current"], None)
                except Exception as again:
                    raise DeployError(f"tiles: activation of {tag} failed ({exc}); switched back to "
                                      f"{before['current']}, which did not come up either ({again})") from exc
            else:
                try:
                    self._activator().abandon(self._context(tag))
                except Exception:
                    pass
            raise DeployError(f"tiles: activation of {tag} failed ({exc}); "
                              + (f"switched back to {before['current']}" if before["current"] else "nothing was live before")) from exc
        from datamanager.deploy import activators

        warnings = activators.ensure_after(self.block.get("activate") or {})
        try:
            removed = self.prune_class()
        except Exception as exc:
            removed, warnings = [], warnings + [f"cleanup of old releases failed: {exc}"]
        return {"class": "tiles", "status": "ok", "current": tag, "previous": before["current"], "removed": removed,
                "warnings": warnings}

    def rollback_class(self, step: StepCb | None = None) -> dict:
        current, previous = self._read_pointer(CURRENT), self._read_pointer(PREVIOUS)
        if not previous or not self._head(f"releases/{previous}/tiles.pmtiles"):
            raise DeployError("tiles: there is no previous release to roll back to")
        before = {"current": current, "previous": previous}
        self._write_pointer(CURRENT, previous)
        self._write_pointer(PREVIOUS, None)
        try:
            self._activate(previous, step)
        except Exception as exc:
            self._switch_back(before)
            try:
                self._activate(current, None)
            except Exception:
                pass
            raise DeployError(f"tiles: rollback to {previous} failed ({exc}); {current} is current again") from exc
        removed = self.prune_class()
        return {"class": "tiles", "status": "rolled_back", "current": previous, "rolled_back": current, "removed": removed}
