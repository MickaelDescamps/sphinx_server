from pydantic import BaseModel


class TrackedTargetElement(BaseModel):
    id: int
    repository_id: int
    ref_type: str
    ref_name: str
    auto_build: bool
    last_sha: str | None = None
    created_at: str
    environment_manager: str | None = None


class RepositoryElement(BaseModel):
    id: int
    name: str
    provider: str
    url: str
    description: str | None = None
    default_branch: str | None = None
    docs_path: str
    created_at: str
    tracked_targets: list[TrackedTargetElement] = []


class BuildElement(BaseModel):
    id: int
    status: str
    log_path: str | None = None
    artifact_path: str | None = None
    duration_seconds: float | None = None
    triggered_by: str | None = None
    ref_name: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    repository: RepositoryElement | None = None
