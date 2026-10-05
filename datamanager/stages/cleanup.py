"""`cleanup`: frees SSD space after packaging, per category (`services/cleanup.py`, `RELEASE-CONTRACT.md` §8).
No review gate: the safety is the verified package it needs and the dry run in the modal that starts it."""
from datamanager.db import SessionLocal
from datamanager.services import cleanup
from datamanager.services.cleanup_categories import BY_KEY
from datamanager.stages.contract import StageResult, StageRunContext, StageRunner


class CleanupStage(StageRunner):
    key = "cleanup"
    review_gate = False

    def run(self, context: StageRunContext) -> StageResult:
        session = SessionLocal()
        tag, categories = context.params["tag"], [c for c in context.params.get("categories", []) if c in BY_KEY]
        if not categories:
            return StageResult("failed", report={"error": "No categories selected."})
        rows, skipped, errors = [], [], []
        freed = purged = deleted = 0
        for number, key in enumerate(categories, 1):
            context.step_cb(f"Clean up: {BY_KEY[key].label}")
            context.progress_cb(number - 1, len(categories), BY_KEY[key].label)
            result = cleanup.apply(session, tag, [key])  # raises while another run is queued or running
            rows.append({"category": key, "label": BY_KEY[key].label, "freed": result.freed, "purged": result.purged,
                         "deleted": result.deleted, "skipped": len(result.skipped)})
            freed, purged, deleted = freed + result.freed, purged + result.purged, deleted + result.deleted
            skipped += result.skipped
            errors += result.errors
            context.progress_cb(number, len(categories), BY_KEY[key].label)
        return StageResult("success", report={
            "summary": {"tag": tag, "freed": freed, "purged": purged, "deleted": deleted, "categories": rows},
            "skipped": skipped, "errors": errors, "warnings": errors,
        })
