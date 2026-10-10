"""`deploy`: deploys a package to an environment, or rolls the environment back (`deploy/orchestrator.py`).
No review gate: the safety is the verified package, the plan shown before it starts, and the health check per class."""
from datamanager.db import SessionLocal
from datamanager.deploy import orchestrator
from datamanager.errors import DeployError
from datamanager.stages.contract import StageResult, StageRunContext, StageRunner


class DeployStage(StageRunner):
    key = "deploy"
    review_gate = False

    def run(self, context: StageRunContext) -> StageResult:
        session = SessionLocal()
        params = context.params
        try:
            if params.get("rollback"):
                deployment = orchestrator.rollback(
                    session, params["deploy_config"], params.get("classes") or None, triggered_by=params.get("triggered_by", "operator"),
                    build_run_id=int(context.run_id), progress=context.progress_cb, step=context.step_cb)
            else:
                deployment = orchestrator.run(
                    session, params["deploy_config"], params.get("tag"), params.get("classes") or None,
                    triggered_by=params.get("triggered_by", "operator"), build_run_id=int(context.run_id),
                    allow_unverified=bool(params.get("allow_unverified")), progress=context.progress_cb, step=context.step_cb,
                    drop_previous=bool(params.get("drop_previous")))
        except DeployError as exc:
            return StageResult("failed", report={"error": exc.message, "summary": {"config": params.get("deploy_config")}})
        detail = deployment.detail_json or {}
        summary = {"deployment": deployment.id, "config": params["deploy_config"], "tag": deployment.package_tag,
                   "rollback": bool(params.get("rollback")), "classes": detail.get("classes", {})}
        warnings = list(detail.get("warnings", [])) + [w for r in detail.get("classes", {}).values() for w in r.get("warnings", [])]
        if deployment.status != "succeeded":
            return StageResult("failed", report={"error": detail.get("error", "deploy failed"), "summary": summary, "warnings": warnings})
        return StageResult("success", report={"summary": summary, "warnings": warnings})
