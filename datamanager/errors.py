"""Structured error taxonomy.

Every domain error raised by stage runners, the download manager, etc. should
subclass DataManagerError so it carries a stable `error_type` (used later to
populate build_step.error_type/error_message) and survives being logged and
re-raised through RQ's failed-job registry.
"""


class DataManagerError(Exception):
    """Base class for all data-manager domain errors."""

    error_type = "DataManagerError"

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        return {
            "error_type": self.error_type,
            "message": self.message,
            "details": self.details,
        }


class DownloadError(DataManagerError):
    """A download failed or its content could not be validated."""

    error_type = "DownloadError"


class ToolBuildError(DataManagerError):
    """Compiling/building a third-party tool (Valhalla, Tippecanoe, ...) failed."""

    error_type = "ToolBuildError"


class SubprocessError(DataManagerError):
    """A subprocess invoked by a stage runner exited non-zero."""

    error_type = "SubprocessError"


class ValidationError(DataManagerError):
    """Input data or configuration failed validation."""

    error_type = "ValidationError"


class DependencyMissingError(DataManagerError):
    """A required asset, download, or tool dependency was not found."""

    error_type = "DependencyMissingError"


class StageFailedError(DataManagerError):
    """A stage ran to the end but reported failure (details in the run's report)."""

    error_type = "StageFailedError"


class CyclicDependencyError(DataManagerError):
    """The stage-runner dependency graph contains a cycle."""

    error_type = "CyclicDependencyError"


class UnresolvedDependencyError(DataManagerError):
    """A stage consumes an asset type that no registered stage produces."""

    error_type = "UnresolvedDependencyError"


class PackageError(DataManagerError):
    """Packaging or verifying a package failed (missing input, no space, hash mismatch)."""

    error_type = "PackageError"
