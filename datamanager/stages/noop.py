from datamanager.stages.contract import StageIO, StageResult, StageRunContext, StageRunner


class NoOpStage(StageRunner):
    """Synthetic stage used to prove the stage-runner contract, the
    work/<run_id>/ filesystem convention, and background-job execution
    end-to-end, before any real stage exists.
    """

    key = "noop"
    produces = (StageIO("noop-output"),)
    consumes = ()

    def run(self, context: StageRunContext) -> StageResult:
        context.progress_cb(0, 2, "starting")
        context.work_dir.mkdir(parents=True, exist_ok=True)
        marker_path = context.work_dir / "noop.txt"
        marker_path.write_text(f"noop stage ran for run_id={context.run_id}\n")
        context.progress_cb(1, 2, "wrote marker file")
        context.progress_cb(2, 2, "done")
        return StageResult(status="success", produced_paths={"noop-output": str(marker_path)})
