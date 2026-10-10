"""Deploy a package to an environment: configurations, plan, run, rollback (`RELEASE-CONTRACT.md` §3 and §4).

The driver does the work per class (`deploy/compose_single_machine.py`); this module owns the database side: the
`deploy_config` rows, one `deployment` row per deploy or rollback with the per-class result, the rule that only one
deploy per configuration runs at a time, the order of the classes and the `live` label on the packages."""
import datetime
from sqlalchemy.orm import Session

from datamanager.deploy.base import DeployDriver, PackageView, ProgressCb, StepCb
from datamanager.deploy.registry import get_driver
from datamanager.errors import DeployError
from datamanager.models import BuildRun, DeployConfig, Deployment, Package, PackageLabel
from datamanager.services import packages, settings as settings_service

DEFAULT_ORDER = ("geodata", "valhalla", "pelias", "tiles")  # region names must agree across classes: geodata first
LIVE_ORIGIN = "deploy"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


# ---- configurations -----------------------------------------------------------------------------------------------

def save_config(session: Session, key: str, config: dict, description: str = "") -> DeployConfig:
    driver_key = config.get("driver", "compose-single-machine")
    problems = get_driver(driver_key).validate_config(config)
    if problems:
        raise DeployError("Invalid deploy configuration: " + "; ".join(problems), problems=problems)
    if not key or key == "new" or not key.replace("-", "").replace("_", "").isalnum():
        raise DeployError("The configuration key may only contain letters, digits, dashes and underscores")
    row = session.query(DeployConfig).filter_by(key=key).first()
    if row is None:
        row = DeployConfig(key=key, driver=driver_key)
        session.add(row)
    row.driver, row.config_json, row.description = driver_key, config, description[:300]
    session.commit()
    return row


def get_config(session: Session, key: str) -> DeployConfig:
    row = session.query(DeployConfig).filter_by(key=key).first()
    if row is None:
        raise DeployError(f"No deploy configuration named {key!r}")
    return row


def make_driver(session: Session, config: DeployConfig) -> DeployDriver:
    options = {"verify": settings_service.get(session, "deploy.verify"),
               "health_timeout": settings_service.get(session, "deploy.health_timeout")}
    return get_driver(config.driver)(config.config_json, options)


def class_order(config: DeployConfig, classes: list[str] | None = None) -> list[str]:
    configured = config.config_json.get("activation_order") or [c for c in DEFAULT_ORDER if c in config.config_json["classes"]]
    configured = [c for c in configured if c in config.config_json["classes"]]
    if classes:
        unknown = sorted(set(classes) - set(configured))
        if unknown:
            raise DeployError(f"Not configured in {config.key}: {', '.join(unknown)}")
        return [c for c in configured if c in classes]
    return configured


# ---- packages -----------------------------------------------------------------------------------------------------

def resolve_package(session: Session, ref: str | None) -> Package:
    """A tag, or a bare label that names exactly one package; no reference = the newest verified complete package."""
    if not ref:
        found = (session.query(Package).filter(Package.status == "complete", Package.verified_at.isnot(None))
                 .order_by(Package.id.desc()).first())
        if found is None:
            raise DeployError("There is no verified package yet: package and verify first")
        return found
    package = session.query(Package).filter_by(tag=ref).first()
    if package is not None:
        return package
    labelled = (session.query(Package).join(PackageLabel).filter(PackageLabel.key == ref, PackageLabel.value == "",
                                                                 PackageLabel.origin == "user").all())
    if len(labelled) == 1:
        return labelled[0]
    raise DeployError(f"No package with tag or label {ref!r}" if not labelled else f"The label {ref!r} is on several packages")


def _check_package(package: Package, allow_unverified: bool) -> None:
    if package.status != "complete":
        raise DeployError(f"{package.tag} is {package.status}, not complete")
    if package.verified_at is None and not allow_unverified:
        raise DeployError(f"{package.tag} has not been verified: verify it first (Repo → Verify, or `flask package-verify`)")


# ---- plan ---------------------------------------------------------------------------------------------------------

def plan(session: Session, config_key: str, package_ref: str | None, classes: list[str] | None = None,
         allow_unverified: bool = False, drop_previous: bool = False) -> dict:
    config = get_config(session, config_key)
    package = resolve_package(session, package_ref)
    view = PackageView.from_package(package)
    driver = make_driver(session, config)
    problems, warnings, entries = [], [], []
    try:
        _check_package(package, allow_unverified)
    except DeployError as exc:
        problems.append(exc.message)
    order = class_order(config, classes)
    for name in order:
        entry = driver.plan_class(view, name, drop_previous)
        problems += [f"{name}: {p}" for p in entry["problems"]]
        entries.append(entry)
    state = driver.describe_state(order)
    rest = [c for c in driver.class_names() if c not in order]
    after = {name: package.tag for name in order}
    after.update({name: entry["current"] for name, entry in (driver.describe_state(rest) if rest else {}).items() if entry["current"]})
    if len(set(after.values())) > 1:
        warnings.append("After this deploy the classes carry different packages: " + ", ".join(f"{n}={t}" for n, t in after.items()))
    return {"config": config.key, "package": package.tag, "classes": entries, "state": state, "problems": problems,
            "warnings": warnings, "bytes_to_copy": sum(e["bytes_to_copy"] for e in entries)}


# ---- run ----------------------------------------------------------------------------------------------------------

def active_deployment(session: Session, config: DeployConfig) -> Deployment | None:
    """The deploy of this configuration that is running now. A `running` row whose run is gone (worker died) is marked failed."""
    active = None
    for row in session.query(Deployment).filter_by(deploy_config_id=config.id, status="running").all():
        run = session.get(BuildRun, row.build_run_id) if row.build_run_id else None
        if run is not None and run.status in ("queued", "running"):
            active = row
            continue
        row.status, row.finished_at = "failed", _now()
        row.detail_json = {**row.detail_json, "error": "interrupted (the run is no longer active)"}
    session.commit()
    return active


def _guard_single_run(session: Session, config: DeployConfig) -> None:
    """One running deploy per configuration."""
    row = active_deployment(session, config)
    if row is not None:
        raise DeployError(f"Deploy {row.id} of {row.package_tag} to {config.key} is still running")


def _refresh_live_labels(session: Session, config: DeployConfig, driver: DeployDriver) -> None:
    """`live=<config key>` marks the packages that are current or previous on this target (they cannot be deleted)."""
    tags = set()
    for name, entry in driver.describe_state().items():
        tags |= {entry.get("current"), entry.get("previous")} - {None}
    session.query(PackageLabel).filter_by(key=packages.LIVE_LABEL, value=config.key).delete()
    for tag in tags:
        package = session.query(Package).filter_by(tag=tag).first()
        if package is not None:
            session.add(PackageLabel(package_id=package.id, key=packages.LIVE_LABEL, value=config.key, origin=LIVE_ORIGIN))
    session.commit()


def _package_by_tag(session: Session, tag: str | None) -> Package | None:
    return session.query(Package).filter_by(tag=tag).first() if tag else None


def run(session: Session, config_key: str, package_ref: str | None, classes: list[str] | None = None,
        triggered_by: str = "operator", build_run_id: int | None = None, allow_unverified: bool = False,
        progress: ProgressCb | None = None, step: StepCb | None = None, drop_previous: bool = False) -> Deployment:
    """Deploy the classes in order. A failed class stops the sequence (the ones before it stay live); the returned
    deployment says which. Running it again resumes: classes that are current are skipped. `drop_previous` removes the
    previous release of each class before its copy starts (saves space; no rollback target until the deploy is healthy)."""
    config = get_config(session, config_key)
    package = resolve_package(session, package_ref)
    _check_package(package, allow_unverified)
    order = class_order(config, classes)
    missing = [c for c in order if c not in {i.class_ for i in package.items}]
    if missing:
        raise DeployError(f"{package.tag} has no {', '.join(missing)} class")
    driver = make_driver(session, config)
    _guard_single_run(session, config)
    view = PackageView.from_package(package)
    before = driver.describe_state(order)
    replaced = next((before[c]["current"] for c in order if before[c]["current"] not in (None, package.tag)), None)
    old = _package_by_tag(session, replaced)
    deployment = Deployment(package_id=package.id, package_tag=package.tag, deploy_config_id=config.id,
                            classes_json=order, status="running", triggered_by=triggered_by, build_run_id=build_run_id,
                            detail_json={"classes": {}}, previous_package_id=old.id if old else None)
    session.add(deployment)
    session.commit()
    detail = {"classes": {}}
    base_warnings = driver.ensure_base(step)
    if base_warnings:
        detail["warnings"] = base_warnings
    try:
        for name in order:
            if step:
                step(f"Deploy {name}")
            result = driver.deploy_class(view, name, progress, step, drop_previous)
            detail["classes"][name] = result
            deployment.detail_json = dict(detail)
            session.commit()
        deployment.status = "succeeded"
    except DeployError as exc:
        detail["classes"][name] = {"class": name, "status": "failed", "error": exc.message}
        detail["error"] = exc.message
        deployment.status = "failed"
    except Exception as exc:  # never leave a deployment row `running`
        detail["error"] = f"{type(exc).__name__}: {exc}"
        deployment.status = "failed"
    deployment.detail_json, deployment.finished_at = dict(detail), _now()
    session.commit()
    try:
        _refresh_live_labels(session, config, driver)
    except Exception as exc:  # the labels follow the state on the next deploy
        detail.setdefault("warnings", []).append(f"live labels not updated: {exc}")
        deployment.detail_json = dict(detail)
        session.commit()
    return deployment


def rollback(session: Session, config_key: str, classes: list[str] | None = None, triggered_by: str = "operator",
             build_run_id: int | None = None, progress: ProgressCb | None = None, step: StepCb | None = None) -> Deployment:
    """`current` back to `previous` per class; the release that was rolled back is removed from the target."""
    config = get_config(session, config_key)
    order = class_order(config, classes)
    driver = make_driver(session, config)
    _guard_single_run(session, config)
    state = driver.describe_state(order)
    nothing = [c for c in order if not state[c]["previous"]]
    if nothing:
        raise DeployError(f"No previous release to roll back to for: {', '.join(nothing)}")
    reverted = (session.query(Deployment).filter(Deployment.deploy_config_id == config.id, Deployment.status == "succeeded",
                                                 Deployment.rolled_back_from_id.is_(None))
                .order_by(Deployment.id.desc()).first())
    target_tag = state[order[0]]["previous"]
    deployment = Deployment(package_id=getattr(_package_by_tag(session, target_tag), "id", None), package_tag=target_tag,
                            deploy_config_id=config.id, classes_json=order, status="running", triggered_by=triggered_by,
                            build_run_id=build_run_id, rolled_back_from_id=reverted.id if reverted else None,
                            previous_package_id=getattr(_package_by_tag(session, state[order[0]]["current"]), "id", None),
                            detail_json={"classes": {}, "rollback": True})
    session.add(deployment)
    session.commit()
    detail = {"classes": {}, "rollback": True}
    try:
        for name in order:
            if step:
                step(f"Roll back {name}")
            detail["classes"][name] = driver.rollback_class(name, step)
            deployment.detail_json = dict(detail)
            session.commit()
        deployment.status = "succeeded"
        if reverted is not None and set(reverted.classes_json) <= set(order):
            reverted.status = "rolled_back"
    except DeployError as exc:
        detail["classes"][name] = {"class": name, "status": "failed", "error": exc.message}
        detail["error"] = exc.message
        deployment.status = "failed"
    except Exception as exc:
        detail["error"] = f"{type(exc).__name__}: {exc}"
        deployment.status = "failed"
    deployment.detail_json, deployment.finished_at = dict(detail), _now()
    session.commit()
    try:
        _refresh_live_labels(session, config, driver)
    except Exception as exc:
        detail.setdefault("warnings", []).append(f"live labels not updated: {exc}")
        deployment.detail_json = dict(detail)
        session.commit()
    return deployment


def state(session: Session, config_key: str) -> dict:
    config = get_config(session, config_key)
    return make_driver(session, config).describe_state()


def history(session: Session, config_key: str | None = None, limit: int = 30) -> list[Deployment]:
    query = session.query(Deployment).order_by(Deployment.id.desc())
    if config_key:
        query = query.filter(Deployment.deploy_config_id == get_config(session, config_key).id)
    return query.limit(limit).all()
