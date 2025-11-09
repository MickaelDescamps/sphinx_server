"""Facilities for queuing, executing, and recording documentation builds."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor
from contextlib import suppress
from datetime import datetime
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore

from sqlmodel import Session, select

from .config import get_settings, settings
from .database import engine
from .git_utils import clone_or_fetch, run_git
from .models import Build, BuildStatus, RefType, Repository, TrackedTarget


class BuildExecutor:
    """Runs build jobs inside a process pool with isolated workspaces."""

    def __init__(self) -> None:
        """Instantiate the underlying :class:`~concurrent.futures.ProcessPoolExecutor`."""
        self.pool = ProcessPoolExecutor(max_workers=settings.build_processes)

    async def run_build(self, build_id: int) -> None:
        """Delegate a build job to the process pool.

        :param build_id: Primary key of the :class:`sphinx_server.models.Build`.
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self.pool, _process_build, build_id)

    async def shutdown(self) -> None:
        """Tear down the executor without waiting for running jobs."""
        self.pool.shutdown(wait=False)


class BuildQueue:
    """Simple asyncio queue consuming build jobs sequentially."""

    def __init__(self, executor: BuildExecutor | None = None) -> None:
        """Create the queue wrapper around the provided executor."""
        self.executor = executor or BuildExecutor()
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self.worker_task: asyncio.Task[None] | None = None

    async def startup(self) -> None:
        """Spin up the background worker task once."""
        if self.worker_task:
            return
        self.worker_task = asyncio.create_task(self._worker())

    async def shutdown(self) -> None:
        """Cancel the worker and close the executor."""
        if self.worker_task:
            self.worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.worker_task
            self.worker_task = None
        await self.executor.shutdown()

    async def enqueue(self, build_id: int) -> None:
        """Queue a build identifier for background processing."""
        await self.queue.put(build_id)

    async def _worker(self) -> None:
        """Continuously pull build ids from the queue and execute them."""
        while True:
            build_id = await self.queue.get()
            try:
                await self.executor.run_build(build_id)
            finally:
                self.queue.task_done()


def _process_build(build_id: int) -> None:
    """Run every build step synchronously inside the worker process.

    :param build_id: Identifier of the build row to load and update.
    """
    local_settings = get_settings()
    workspace = local_settings.workspace_root / f"build_{build_id}"
    workspace_repo = workspace / "repo"
    env_dir = workspace / "venv"

    with Session(engine) as session:
        build = session.get(Build, build_id)
        if not build:
            return
        repo = session.get(Repository, build.repository_id)
        target = session.get(TrackedTarget, build.target_id)
        if not repo or not target:
            return

        log_path = local_settings.log_dir / f"build_{build.id}.log"
        build.log_path = str(log_path)
        build.status = BuildStatus.running
        build.started_at = datetime.utcnow()
        session.add(build)
        session.commit()

        try:
            _prepare_workspace(workspace)
            time.sleep(5)
            clone_or_fetch(
                repo.url,
                repo.auth_token,
                workspace_repo,
                log_path,
                timeout=settings.git_default_timeout,
                retries=2,
                deploy_key=repo.deploy_key,
                ssh_workdir=workspace,
            )
            checkout_ref = target.ref_name
            run_git(["checkout", checkout_ref], cwd=workspace_repo, log_file=log_path)
            if target.ref_type == RefType.branch:
                run_git(["pull"], cwd=workspace_repo, log_file=log_path)

            session.refresh(build)
            build.sha = _current_sha(workspace_repo, log_path)

            artifact_dir = local_settings.build_output_dir / str(repo.id) / target.slug()
            env_bin = _prepare_repo_environment(env_dir, workspace_repo, log_path)
            _build_sphinx(workspace_repo / repo.docs_path, artifact_dir, log_path, env_bin)

            metadata = _extract_project_metadata(workspace_repo)
            _maybe_update_repo_metadata(session, repo, target, metadata)

            version_hint = (
                (metadata.get("version") if metadata else None)
                or repo.project_version
                or "unknown"
            )
            _inject_navigation_links(artifact_dir, repo.id, target, version_hint)

            build.status = BuildStatus.success
            build.finished_at = datetime.utcnow()
            if build.started_at and build.finished_at:
                build.duration_seconds = (build.finished_at - build.started_at).total_seconds()
            build.artifact_path = str(artifact_dir)
            target.last_sha = build.sha
            session.add(target)
        except Exception as exc:  # pragma: no cover - best effort logging
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"\nBuild failed: {exc}\n")
            build.status = BuildStatus.failed
            build.finished_at = datetime.utcnow()
            if build.started_at and build.finished_at:
                build.duration_seconds = (build.finished_at - build.started_at).total_seconds()
        finally:
            session.add(build)
            session.commit()
            shutil.rmtree(workspace, ignore_errors=True)


def _prepare_workspace(workspace: Path) -> None:
    """Create an empty workspace directory for the build.

    :param workspace: Directory that will hold repo checkout and venv.
    """
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)


def _current_sha(repo_path: Path, log_path: Path) -> str:
    """Return the SHA of ``HEAD`` for the cloned repository.

    :param repo_path: Local checkout path.
    :param log_path: Build log file receiving git output.
    :returns: 40-character SHA or ``\"unknown\"`` on error.
    """
    cmd = ["git", "rev-parse", "HEAD"]
    with log_path.open("a", encoding="utf-8") as log:
        proc = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            log.write(proc.stdout)
            return "unknown"
        return proc.stdout.strip()


def _prepare_repo_environment(env_dir: Path, repo_path: Path, log_path: Path) -> Path:
    """Provision a uv-managed virtual environment with project dependencies.

    :param env_dir: Directory where the venv will live.
    :param repo_path: Local repository clone used to read dependency files.
    :param log_path: Log file to append command output.
    :returns: Path to the environment ``bin`` directory.
    """
    if env_dir.exists():
        shutil.rmtree(env_dir)
    env_dir.parent.mkdir(parents=True, exist_ok=True)

    _run_command(["uv", "venv", str(env_dir)], log_path)
    bin_dir = env_dir / ("Scripts" if os.name == "nt" else "bin")
    python_bin = bin_dir / ("python.exe" if os.name == "nt" else "python")

    _run_command(["uv", "pip", "install", "--python", str(python_bin), "sphinx"], log_path)
    _install_repo_dependencies(repo_path, python_bin, log_path)
    return bin_dir


def _install_repo_dependencies(repo_path: Path, python_bin: Path, log_path: Path) -> None:
    """Install optional extras plus common requirements files for the repo.

    :param repo_path: Repository checkout location.
    :param python_bin: Python interpreter inside the temporary environment.
    :param log_path: Build log path for recording installer output.
    """
    pyproject = repo_path / "pyproject.toml"
    if pyproject.exists():
        extras = _detect_optional_extras(pyproject)
        spec = "."
        if extras:
            spec = f".[{','.join(extras)}]"
        _run_command(
            ["uv", "pip", "install", "--python", str(python_bin), spec],
            log_path,
            cwd=repo_path,
        )

    req_candidates = [
        repo_path / "requirements.txt",
        repo_path / "docs" / "requirements.txt",
        repo_path / "docs" / "requirements-docs.txt",
    ]
    for req in req_candidates:
        if req.exists():
            _run_command(
                ["uv", "pip", "install", "--python", str(python_bin), "-r", str(req)],
                log_path,
            )


def _build_sphinx(docs_path: Path, output_dir: Path, log_path: Path, env_bin: Path) -> None:
    """Render Sphinx documentation into ``output_dir``.

    :param docs_path: Source docs path relative to the repo root.
    :param output_dir: Destination folder for HTML artifacts.
    :param log_path: File accepting build output for debugging.
    :param env_bin: Virtualenv ``bin`` directory locating ``sphinx-build``.
    :raises FileNotFoundError: When the docs path is missing.
    """
    if not docs_path.exists():
        raise FileNotFoundError(f"Docs path {docs_path} missing")

    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    sphinx_exe = env_bin / ("sphinx-build.exe" if os.name == "nt" else "sphinx-build")
    cmd = [
        str(sphinx_exe),
        "-b",
        "html",
        str(docs_path),
        str(output_dir),
    ]
    _run_command(cmd, log_path, timeout=settings.sphinx_timeout)


def _run_command(
    cmd: list[str],
    log_path: Path,
    cwd: Path | None = None,
    timeout: int | None = None,
) -> None:
    """Execute a subprocess and tee stdout/stderr to the build log.

    :param cmd: Command tokens to execute.
    :param log_path: File capturing the combined output.
    :param cwd: Optional working directory for the process.
    :param timeout: Optional timeout passed to :func:`subprocess.run`.
    :raises RuntimeError: If the command exits with a non-zero status.
    """
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n$ {' '.join(cmd)} (cwd={cwd or os.getcwd()})\n")
        log.flush()
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"Command {' '.join(cmd)} failed with exit code {proc.returncode}")


def _detect_optional_extras(pyproject: Path) -> list[str]:
    """Determine which optional dependency extras correspond to docs builds.

    :param pyproject: Path to ``pyproject.toml``.
    :returns: List of extras (``docs``, ``doc``, ``documentation``, ``dev``) found.
    """
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    optional = data.get("project", {}).get("optional-dependencies", {})
    if not isinstance(optional, dict):
        return []
    desired = {"docs", "doc", "documentation", "dev"}
    extras: list[str] = []
    for key in optional:
        if key.lower() in desired:
            extras.append(key)
    return extras


def _inject_navigation_links(
    artifact_dir: Path,
    repo_id: int,
    target: TrackedTarget,
    repo_version: str,
) -> None:
    """Append navigation metadata/script tags to generated HTML pages.

    :param artifact_dir: Directory containing rendered Sphinx HTML files.
    :param repo_id: Repository identifier used to build per-target URLs.
    :param target: Target metadata describing the ref being built.
    :param repo_version: Fallback version string to surface in the UI.
    """
    script_payload = {
        "REPO": repo_id,
        "TARGET": target.slug(),
        "REF_NAME": target.ref_name,
        "REF_TYPE": getattr(target.ref_type, "value", target.ref_type),
        "VERSION": repo_version,
    }
    js_assignments = ";".join(
        [f"window.__SPHINX_SERVER_{key}={json.dumps(value)}" for key, value in script_payload.items()]
    )
    script = (
        f"<script>window.__SPHINX_SERVER_NAV=1;{js_assignments};</script>\n"
        '<script defer src="/assets/sphinx-nav.js"></script>\n'
    )
    for html_file in artifact_dir.rglob("*.html"):
        try:
            contents = html_file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if "__SPHINX_SERVER_NAV" in contents:
            continue
        lower = contents.lower()
        marker = "</body>"
        idx = lower.rfind(marker)
        if idx == -1:
            continue
        contents = contents[:idx] + script + contents[idx:]
        html_file.write_text(contents, encoding="utf-8")


def _extract_project_metadata(repo_path: Path) -> dict[str, str]:
    """Read project metadata from ``pyproject.toml`` when present.

    :param repo_path: Local repository checkout.
    :returns: Mapping with ``name``, ``version``, ``summary``, and ``homepage`` keys.
    """
    pyproject = repo_path / "pyproject.toml"
    if not pyproject.exists():
        return {}
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    project = data.get("project", {}) or {}
    urls = project.get("urls") or {}
    return {
        "name": project.get("name"),
        "version": project.get("version"),
        "summary": project.get("description"),
        "homepage": urls.get("Homepage") or urls.get("homepage"),
    }


def _maybe_update_repo_metadata(
    session: Session,
    repo: Repository,
    target: TrackedTarget,
    metadata: dict[str, str] | None,
) -> None:
    """Persist project metadata if the build belongs to the primary target.

    :param session: Open SQLModel session.
    :param repo: Repository being updated.
    :param target: Tracked target associated with the build.
    :param metadata: Extracted metadata dictionary, if any.
    """
    if not metadata:
        return
    if repo.primary_target_id and repo.primary_target_id != target.id:
        return
    repo.project_name = metadata.get("name") or repo.name
    repo.project_version = metadata.get("version")
    repo.project_summary = metadata.get("summary")
    repo.project_homepage = metadata.get("homepage")
    session.add(repo)


async def enqueue_target_build(
    target_id: int,
    session: Session,
    queue: BuildQueue,
    triggered_by: str = "manual",
) -> Build:
    """Create a :class:`Build` row and enqueue it for processing.

    :param target_id: Identifier of the tracked target to build.
    :param session: Database session used to persist the build.
    :param queue: Build queue instance to receive the job.
    :param triggered_by: Label describing how the build was triggered.
    :returns: The persisted :class:`Build` instance.
    :raises ValueError: If the target does not exist.
    """
    target = session.get(TrackedTarget, target_id)
    if not target:
        raise ValueError("Target not found")
    build = Build(
        repository_id=target.repository_id,
        target_id=target.id,
        ref_name=target.ref_name,
        triggered_by=triggered_by,
    )
    session.add(build)
    session.commit()
    session.refresh(build)
    await queue.enqueue(build.id)
    return build


def pending_builds(session: Session) -> list[Build]:
    """Return builds ordered by creation time newest-first."""
    return list(session.exec(select(Build).order_by(Build.created_at.desc())).all())
