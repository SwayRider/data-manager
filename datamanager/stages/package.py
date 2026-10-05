"""`package`: copies the approved output of a configuration into a tagged package in the repository
(`services/packages.py`). No review gate: nothing downstream consumes a package, `verify` is its check."""
from datamanager.db import SessionLocal
from datamanager.services import packages
from datamanager.stages.contract import StageResult, StageRunContext, StageRunner


class PackageStage(StageRunner):
    key = "package"
    review_gate = False

    def run(self, context: StageRunContext) -> StageResult:
        session = SessionLocal()
        params = context.params
        context.step_cb("Plan")
        package = packages.create_package(
            session, context.config_id, params.get("classes") or None,
            created_by=params.get("created_by", "operator"), labels=params.get("labels") or {},
            note=params.get("note", ""), run_id=int(context.run_id), progress=context.progress_cb,
            step=context.step_cb, check_status=not params.get("force", False),
        )
        by_class: dict[str, dict] = {}
        for item in package.items:
            entry = by_class.setdefault(item.class_, {"files": 0, "bytes": 0})
            entry["files"] += 1
            entry["bytes"] += item.size_bytes
        return StageResult("success", report={
            "summary": {"tag": package.tag, "path": package.path, "size_bytes": package.size_bytes,
                        "files": len(package.items), "classes": by_class},
            "tags": {l.key: l.value for l in package.labels if l.origin == "auto"},
            "warnings": [],
        })


class PackageVerifyStage(StageRunner):
    """Re-hashes every file of a package against its package.json (params: `tag`). A clean result unlocks the cleanup."""

    key = "package-verify"
    review_gate = False

    def run(self, context: StageRunContext) -> StageResult:
        tag = context.params["tag"]
        context.step_cb(f"Verify {tag}")
        problems = packages.verify_package(SessionLocal(), tag, progress=context.progress_cb)
        if problems:
            return StageResult("failed", report={"error": f"{len(problems)} problem(s): " + "; ".join(problems[:5]),
                                                 "summary": {"tag": tag, "problems": problems}})
        return StageResult("success", report={"summary": {"tag": tag, "problems": []}})
