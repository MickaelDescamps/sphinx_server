"""Application models"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional

from sqlalchemy.orm import relationship as sa_relationship
from sqlmodel import Field, Relationship, SQLModel


class ProviderType(str, Enum):
    """Type of code repo provider"""
    github = "github"
    gitlab = "gitlab"
    generic = "generic"


class RefType(str, Enum):
    """Type of target followed to build documentation"""
    branch = "branch"
    tag = "tag"


class BuildStatus(str, Enum):
    """Build status possible for build work"""
    queued = "queued"
    running = "running"
    success = "success"
    failed = "failed"


class Repository(SQLModel, table=True):
    """Repository model"""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    provider: ProviderType = Field(default=ProviderType.github)
    url: str
    description: Optional[str] = None
    default_branch: Optional[str] = None
    docs_path: str = "docs"
    auth_token: Optional[str] = Field(default=None, description="Personal access token if required")
    deploy_key: Optional[str] = Field(default=None, description="SSH deploy key for private clones")
    verify_ssl: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    project_name: Optional[str] = None
    project_version: Optional[str] = None
    project_summary: Optional[str] = None
    project_homepage: Optional[str] = None
    primary_target_id: Optional[int] = Field(default=None, foreign_key="trackedtarget.id")

    tracked_targets: List["TrackedTarget"] = Relationship(
        sa_relationship=sa_relationship(
            "TrackedTarget",
            back_populates="repository",
            foreign_keys="TrackedTarget.repository_id",
        )
    )
    builds: List["Build"] = Relationship(
        sa_relationship=sa_relationship(
            "Build",
            back_populates="repository",
            foreign_keys="Build.repository_id",
        )
    )


class TrackedTarget(SQLModel, table=True):
    """Target to track like an branch or a tags"""
    id: Optional[int] = Field(default=None, primary_key=True)
    repository_id: int = Field(foreign_key="repository.id")
    ref_type: RefType
    ref_name: str
    auto_build: bool = True
    last_sha: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

    repository: Repository | None = Relationship(
        sa_relationship=sa_relationship(
            "Repository",
            back_populates="tracked_targets",
            foreign_keys="TrackedTarget.repository_id",
        )
    )
    builds: List["Build"] = Relationship(
        sa_relationship=sa_relationship(
            "Build",
            back_populates="target",
            foreign_keys="Build.target_id",
        )
    )

    def slug(self) -> str:
        sanitized = self.ref_name.replace("/", "_").replace(" ", "-")
        return f"{self.ref_type}-{sanitized}"


class Build(SQLModel, table=True):
    """Build work model"""
    id: Optional[int] = Field(default=None, primary_key=True)
    repository_id: int = Field(foreign_key="repository.id")
    target_id: int = Field(foreign_key="trackedtarget.id")
    status: BuildStatus = Field(default=BuildStatus.queued)
    ref_name: str
    sha: Optional[str] = None
    log_path: Optional[str] = None
    artifact_path: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    triggered_by: Optional[str] = Field(default="manual")  # manual or auto

    repository: Repository | None = Relationship(
        sa_relationship=sa_relationship(
            "Repository",
            back_populates="builds",
            foreign_keys="Build.repository_id",
        )
    )
    target: TrackedTarget | None = Relationship(
        sa_relationship=sa_relationship(
            "TrackedTarget",
            back_populates="builds",
            foreign_keys="Build.target_id",
        )
    )
