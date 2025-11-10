"""Configuration and settings helpers for Sphinx Server."""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


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
    environment_manager: Literal["uv", "pyenv"] = "uv"
    pyenv_default_python_version: str = "3.11.8"
    secret_key: str = "change-me"

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
        for label, path in {
            "data": self.data_dir,
            "repo_cache": self.repo_cache_dir,
            "builds": self.build_output_dir,
            "logs": self.log_dir,
            "envs": self.env_root_dir,
            "workspaces": self.workspace_root,
        }.items():
            path.mkdir(parents=True, exist_ok=True)
            logger.debug("Ensured %s directory exists at %s", label, path)


_ENV_FILES = Settings.model_config.get("env_file") or ()
if isinstance(_ENV_FILES, str):
    _ENV_FILES = (_ENV_FILES,)
_ENV_FILE_PATH = Path(_ENV_FILES[0]) if _ENV_FILES else Path(".env")
if not _ENV_FILE_PATH.is_absolute():
    _ENV_FILE_PATH = Path.cwd() / _ENV_FILE_PATH
ENV_FILE_PATH = _ENV_FILE_PATH


def get_env_file_path() -> Path:
    """Return the absolute path to the configured .env file."""
    return ENV_FILE_PATH


def _parse_env_assignment(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, value = stripped.split("=", 1)
    return key.strip(), value


def _serialize_env_value(value: str) -> str:
    if value == "":
        return '""'
    needs_quotes = bool(re.search(r"\s|#|['\"]", value))
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"' if needs_quotes else value


def persist_env_settings(updates: dict[str, str]) -> None:
    """Update the .env file with the provided key/value pairs."""
    if not updates:
        return
    env_path = get_env_file_path()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    key_to_index: dict[str, int] = {}
    if env_path.exists():
        content = env_path.read_text(encoding="utf-8").splitlines()
        lines = content.copy()
        for idx, line in enumerate(lines):
            parsed = _parse_env_assignment(line)
            if parsed:
                key_to_index[parsed[0]] = idx
    for key, value in updates.items():
        serialized = f"{key}={_serialize_env_value(value)}"
        if key in key_to_index:
            lines[key_to_index[key]] = serialized
        else:
            lines.append(serialized)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    """Load and cache the :class:`Settings` instance."""
    settings = Settings()
    settings.ensure_dirs()
    logger.debug("Settings loaded (environment=%s)", settings.environment)
    return settings


settings = get_settings()


def apply_settings_overrides(overrides: dict[str, Any]) -> None:
    """Mutate the global settings instance with freshly saved values."""
    if not overrides:
        return
    data_dir_changed = False
    for key, value in overrides.items():
        setattr(settings, key, value)
        if key == "data_dir":
            data_dir_changed = True
    if data_dir_changed:
        settings.ensure_dirs()
