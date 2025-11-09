"""Configuration and settings helpers for Sphinx Server."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="SPHINX_SERVER_",
        env_file=(".env",),
        extra="ignore",
    )

    environment: Literal["dev", "prod"] = "dev"
    host: str = "127.0.0.1"
    port: int = 8000
    reload: bool = False

    data_dir: Path = Field(default_factory=lambda: Path.cwd() / ".sphinx_server")
    repo_cache_subdir: str = "repos"  # legacy cache, no longer used for builds
    build_subdir: str = "builds"
    log_subdir: str = "logs"
    env_subdir: str = "envs"
    workspace_subdir: str = "workspaces"

    database_url: str | None = None

    git_default_timeout: int = 120
    sphinx_timeout: int = 600
    build_processes: int = 5
    auto_build_interval_seconds: int = 60

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.data_dir / 'sphinx_server.db'}"

    @property
    def repo_cache_dir(self) -> Path:
        return self.data_dir / self.repo_cache_subdir

    @property
    def build_output_dir(self) -> Path:
        return self.data_dir / self.build_subdir

    @property
    def log_dir(self) -> Path:
        return self.data_dir / self.log_subdir

    @property
    def env_root_dir(self) -> Path:
        return self.data_dir / self.env_subdir

    @property
    def workspace_root(self) -> Path:
        return self.data_dir / self.workspace_subdir

    def ensure_dirs(self) -> None:
        """Create all filesystem directories required by the service."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.repo_cache_dir.mkdir(parents=True, exist_ok=True)
        self.build_output_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.env_root_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_root.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    """Load and cache the :class:`Settings` instance."""
    settings = Settings()
    settings.ensure_dirs()
    return settings


settings = get_settings()
