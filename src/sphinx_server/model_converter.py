from sphinx_server.models import Build
from sphinx_server.time_utils import format_local_datetime
from sphinx_server.ui_models import BuildElement


def convert_build_to_ui_model(build: Build) -> BuildElement:
    """Convert a Build model to a BuildElement UI model."""
    return BuildElement(
        id=build.id,
        status=build.status,
        log_path=build.log_path,
        artifact_path=build.artifact_path,
        duration_seconds=build.duration_seconds,
        triggered_by=build.triggered_by,
        ref_name=build.ref_name,
        created_at=format_local_datetime(build.created_at),
        started_at=format_local_datetime(build.started_at) if build.started_at else None,
        finished_at=format_local_datetime(build.finished_at) if build.finished_at else None,
    )