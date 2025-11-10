"""Facilities for queuing, executing, and recording documentation builds."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore

from sqlmodel import Session, select

from .config import get_settings, settings
from .database import engine
from .git_utils import clone_or_fetch, run_git
from .models import Build, BuildStatus, RefType, Repository, TrackedTarget
from .time_utils import format_local_datetime

logger = logging.getLogger(__name__)
DOC_DEPENDENCY_KEYS = {"docs", "doc", "documentation", "dev"}


class BuildExecutor:
    """Runs build jobs inside a process pool with isolated workspaces."""

    def __init__(self) -> None:
        """Instantiate the underlying :class:`~concurrent.futures.ProcessPoolExecutor`."""
        logger.debug("Initializing BuildExecutor with %s workers", settings.build_processes)
        self.pool = ProcessPoolExecutor(max_workers=settings.build_processes)

    async def run_build(self, build_id: int) -> None:
        """Delegate a build job to the process pool.

        :param build_id: Primary key of the :class:`sphinx_server.models.Build`.
        """
        loop = asyncio.get_running_loop()
        logger.info("Dispatching build %s to executor", build_id)
        await loop.run_in_executor(self.pool, _process_build, build_id)

    async def shutdown(self) -> None:
        """Tear down the executor without waiting for running jobs."""
        logger.debug("Shutting down BuildExecutor")
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
        logger.debug("Starting build queue worker")
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
        logger.debug("Enqueuing build %s", build_id)
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
            logger.error("Build %s no longer exists", build_id)
            return
        repo = session.get(Repository, build.repository_id)
        target = session.get(TrackedTarget, build.target_id)
        if not repo or not target:
            logger.error("Missing repo/target for build %s", build_id)
            return

        log_path = local_settings.log_dir / f"build_{build.id}.log"
        build.log_path = str(log_path)
        build.status = BuildStatus.running
        build.started_at = datetime.utcnow()
        session.add(build)
        session.commit()

        try:
            logger.info("Starting build %s for repo %s target %s", build.id, repo.id, target.id)
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
            env_manager = target.environment_manager or settings.environment_manager
            env_bin = _prepare_repo_environment(env_dir, workspace_repo, log_path, env_manager)
            _build_sphinx(workspace_repo / repo.docs_path, artifact_dir, log_path, env_bin)

            metadata = _extract_project_metadata(workspace_repo)
            _maybe_update_repo_metadata(session, repo, target, metadata)

            version_hint = (
                (metadata.get("version") if metadata else None)
                or repo.project_version
                or "unknown"
            )
            completion_time = datetime.utcnow()
            _inject_navigation_links(
                artifact_dir,
                repo.id,
                target,
                version_hint,
                completion_time,
            )

            build.status = BuildStatus.success
            build.finished_at = completion_time
            if build.started_at and build.finished_at:
                build.duration_seconds = (build.finished_at - build.started_at).total_seconds()
            build.artifact_path = str(artifact_dir)
            target.last_sha = build.sha
            session.add(target)
        except Exception as exc:  # pragma: no cover - best effort logging
            logger.exception("Build %s failed: %s", build_id, exc)
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"\nBuild failed: {exc}\n")
            build.status = BuildStatus.failed
            build.finished_at = datetime.utcnow()
            if build.started_at and build.finished_at:
                build.duration_seconds = (build.finished_at - build.started_at).total_seconds()
        finally:
            logger.info("Completed build %s (status=%s)", build_id, build.status)
            session.add(build)
            session.commit()
            shutil.rmtree(workspace, ignore_errors=True)


def _prepare_workspace(workspace: Path) -> None:
    """Create an empty workspace directory for the build.

    :param workspace: Directory that will hold repo checkout and venv.
    """
    if workspace.exists():
        logger.debug("Cleaning existing workspace %s", workspace)
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    logger.debug("Created workspace %s", workspace)


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


def _prepare_repo_environment(
    env_dir: Path,
    repo_path: Path,
    log_path: Path,
    manager: Literal["uv", "pyenv"],
) -> Path:
    """Provision a virtual environment with project dependencies.

    :param env_dir: Directory where the venv will live.
    :param repo_path: Local repository clone used to read dependency files.
    :param log_path: Log file to append command output.
    :param manager: Environment backend to use for provisioning.
    :returns: Path to the environment ``bin`` directory.
    """
    if env_dir.exists():
        shutil.rmtree(env_dir)
    env_dir.parent.mkdir(parents=True, exist_ok=True)

    if manager == "uv":
        return _prepare_uv_environment(env_dir, repo_path, log_path)
    if manager == "pyenv":
        return _prepare_pyenv_environment(env_dir, repo_path, log_path)
    raise RuntimeError(f"Unsupported environment manager: {manager}")


def _prepare_uv_environment(env_dir: Path, repo_path: Path, log_path: Path) -> Path:
    """Create and populate an environment using uv for venv + installers."""

    logger.debug("Creating uv virtualenv at %s", env_dir)
    _run_command(["uv", "venv", str(env_dir)], log_path)
    bin_dir = env_dir / ("Scripts" if os.name == "nt" else "bin")
    python_bin = bin_dir / ("python.exe" if os.name == "nt" else "python")

    _pip_install(log_path, python_bin, ["sphinx"], installer="uv")
    _install_repo_dependencies(repo_path, python_bin, log_path, installer="uv")
    return bin_dir


def _prepare_pyenv_environment(env_dir: Path, repo_path: Path, log_path: Path) -> Path:
    """Create an environment using pyenv for Python selection and pip installs."""

    python_version = _resolve_pyenv_python_version(repo_path)
    logger.debug("Ensuring pyenv Python %s is available", python_version)
    _run_command(["pyenv", "install", "-s", python_version], log_path)

    env = os.environ.copy()
    env["PYENV_VERSION"] = python_version
    logger.debug("Creating pyenv virtualenv at %s", env_dir)
    _run_command(["pyenv", "exec", "python", "-m", "venv", str(env_dir)], log_path, env=env)

    bin_dir = env_dir / ("Scripts" if os.name == "nt" else "bin")
    python_bin = bin_dir / ("python.exe" if os.name == "nt" else "python")

    _pip_install(log_path, python_bin, ["sphinx"], installer="pip")
    _install_repo_dependencies(repo_path, python_bin, log_path, installer="pip")
    return bin_dir


def _install_repo_dependencies(
    repo_path: Path,
    python_bin: Path,
    log_path: Path,
    *,
    installer: Literal["uv", "pip"],
) -> None:
    """Install optional extras plus common requirements files for the repo.

    :param repo_path: Repository checkout location.
    :param python_bin: Python interpreter inside the temporary environment.
    :param log_path: Build log path for recording installer output.
    """
    pyproject = repo_path / "pyproject.toml"
    if pyproject.exists():
        pyproject_data = _load_pyproject(pyproject)
        extras = _detect_optional_extras(pyproject_data)
        spec = "."
        if extras:
            spec = f".[{','.join(extras)}]"
        logger.debug("Installing repo dependencies %s with extras %s", repo_path, extras)
        _pip_install(log_path, python_bin, [spec], installer=installer, cwd=repo_path)
        group_requirements = _poetry_group_requirements(pyproject_data, repo_path)
        if group_requirements:
            logger.debug("Installing Poetry group dependencies: %s", group_requirements)
            _pip_install(log_path, python_bin, group_requirements, installer=installer, cwd=repo_path)

    req_candidates = [
        repo_path / "requirements.txt",
        repo_path / "docs" / "requirements.txt",
        repo_path / "docs" / "requirements-docs.txt",
    ]
    for req in req_candidates:
        if req.exists():
            logger.debug("Installing requirements from %s", req)
            _pip_install(log_path, python_bin, ["-r", str(req)], installer=installer)


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
    logger.info("Running sphinx build for %s -> %s", docs_path, output_dir)
    cmd = [
        str(sphinx_exe),
        "-b",
        "html",
        str(docs_path),
        str(output_dir),
    ]
    _run_command(cmd, log_path, timeout=settings.sphinx_timeout)


def _pip_install(
    log_path: Path,
    python_bin: Path,
    args: list[str],
    *,
    installer: Literal["uv", "pip"],
    cwd: Path | None = None,
) -> None:
    """Run either uv or pip installs using the requested interpreter."""

    if installer == "uv":
        base_cmd = ["uv", "pip", "install", "--python", str(python_bin)]
    else:
        base_cmd = [str(python_bin), "-m", "pip", "install"]
    _run_command(base_cmd + args, log_path, cwd=cwd)


def _run_command(
    cmd: list[str],
    log_path: Path,
    cwd: Path | None = None,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """Execute a subprocess and tee stdout/stderr to the build log.

    :param cmd: Command tokens to execute.
    :param log_path: File capturing the combined output.
    :param cwd: Optional working directory for the process.
    :param timeout: Optional timeout passed to :func:`subprocess.run`.
    :raises RuntimeError: If the command exits with a non-zero status.
    """
    with log_path.open("a", encoding="utf-8") as log:
        logger.debug("Running command: %s", " ".join(cmd))
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
            env=env,
        )
        if proc.returncode != 0:
            logger.error("Command failed: %s (code %s)", " ".join(cmd), proc.returncode)
            raise RuntimeError(f"Command {' '.join(cmd)} failed with exit code {proc.returncode}")


def _resolve_pyenv_python_version(repo_path: Path) -> str:
    """Return the Python version to request from pyenv for this repo."""

    version_file = repo_path / ".python-version"
    if version_file.exists():
        version = version_file.read_text(encoding="utf-8").strip()
        if version:
            return version.splitlines()[0].strip()

    default = settings.pyenv_default_python_version
    if default:
        return default
    raise RuntimeError(
        "pyenv environment manager selected but no .python-version or "
        "SPHINX_SERVER_PYENV_DEFAULT_PYTHON_VERSION provided",
    )


def _load_pyproject(pyproject: Path) -> dict[str, Any]:
    """Parse the pyproject.toml file into a dictionary."""
    try:
        return tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _detect_optional_extras(pyproject_data: dict[str, Any]) -> list[str]:
    """Determine which optional dependency extras correspond to docs builds."""
    desired = DOC_DEPENDENCY_KEYS
    extras: set[str] = set()

    optional = pyproject_data.get("project", {}).get("optional-dependencies", {})
    if isinstance(optional, dict):
        for key in optional:
            if key.lower() in desired:
                extras.add(key)

    poetry_extras = pyproject_data.get("tool", {}).get("poetry", {}).get("extras", {})
    if isinstance(poetry_extras, dict):
        for key in poetry_extras:
            if key.lower() in desired:
                extras.add(key)

    return sorted(extras)


def _poetry_group_requirements(pyproject_data: dict[str, Any], repo_path: Path) -> list[str]:
    """Collect dependency specs declared under Poetry dependency groups."""
    groups = (
        pyproject_data.get("tool", {})
        .get("poetry", {})
        .get("group", {})
    )
    if not isinstance(groups, dict):
        return []
    requirements: list[str] = []
    for name, group_info in groups.items():
        if not isinstance(group_info, dict) or name.lower() not in DOC_DEPENDENCY_KEYS:
            continue
        deps = group_info.get("dependencies")
        if not isinstance(deps, dict):
            continue
        for dep_name, dep_spec in deps.items():
            requirement = _convert_poetry_dependency(dep_name, dep_spec, repo_path)
            if requirement:
                requirements.append(requirement)
    return requirements


def _convert_poetry_dependency(name: str, spec: Any, repo_path: Path) -> str | None:
    """Translate a Poetry dependency declaration into a pip-compatible spec."""
    extras_suffix = ""
    version_spec = ""

    def _format_name(base: str) -> str:
        return f"{base}{extras_suffix}{version_spec}"

    if isinstance(spec, str):
        version_spec = _translate_poetry_version(spec)
        return _format_name(name)

    if isinstance(spec, dict):
        extras = spec.get("extras")
        if isinstance(extras, list) and extras:
            extras_suffix = "[" + ",".join(extras) + "]"

        if "git" in spec:
            return _format_git_dependency(f"{name}{extras_suffix}", spec)

        if "path" in spec:
            path_value = Path(spec["path"])
            if not path_value.is_absolute():
                path_value = (repo_path / path_value).resolve()
            return str(path_value)

        version_value = spec.get("version")
        if isinstance(version_value, str):
            version_spec = _translate_poetry_version(version_value)
            return _format_name(name)

        if version_value in (None, "*"):
            return _format_name(name)

    logger.debug("Skipping unsupported Poetry dependency for %s: %s", name, spec)
    return None


def _format_git_dependency(name_with_extras: str, spec: dict[str, Any]) -> str | None:
    """Convert a Poetry git dependency to a pip requirement string."""
    git_url = spec.get("git")
    if not isinstance(git_url, str):
        return None
    ref = spec.get("rev") or spec.get("tag") or spec.get("branch")
    requirement = f"{name_with_extras} @ git+{git_url}"
    if ref:
        requirement += f"@{ref}"
    return requirement


def _translate_poetry_version(constraint: str) -> str:
    """Translate Poetry's version constraint syntax to pip-compatible specifiers."""
    spec = constraint.strip()
    if not spec or spec == "*":
        return ""
    if spec.startswith("^"):
        base = spec[1:].strip()
        if not base:
            return ""
        upper = _increment_caret_upper_bound(base)
        return f">={base},<{upper}"
    if spec.startswith("~"):
        base = spec[1:].strip()
        if not base:
            return ""
        return f"~={base}"
    if any(spec.startswith(op) for op in (">", "<", "=", "!", "~")) or "," in spec:
        return spec
    if spec[0].isdigit():
        return f"=={spec}"
    return spec


def _increment_caret_upper_bound(version: str) -> str:
    """Calculate the exclusive upper bound for a caret constraint."""
    def _parse_int(part: str) -> int:
        digits = ""
        for char in part:
            if char.isdigit():
                digits += char
            else:
                break
        return int(digits) if digits else 0

    parts = [_parse_int(p) for p in version.split(".")]
    while len(parts) < 3:
        parts.append(0)
    major, minor, patch = parts[:3]
    if major:
        major += 1
        minor = 0
        patch = 0
    elif minor:
        minor += 1
        patch = 0
    else:
        patch += 1
    return ".".join(str(x) for x in (major, minor, patch))


def _inject_navigation_links(
    artifact_dir: Path,
    repo_id: int,
    target: TrackedTarget,
    repo_version: str,
    built_at: datetime,
) -> None:
    """Append navigation metadata/script tags to generated HTML pages.

    :param artifact_dir: Directory containing rendered Sphinx HTML files.
    :param repo_id: Repository identifier used to build per-target URLs.
    :param target: Target metadata describing the ref being built.
    :param repo_version: Fallback version string to surface in the UI.
    :param built_at: Completion timestamp for the rendered artifact.
    """
    build_date = format_local_datetime(built_at)
    script_payload = {
        "REPO": repo_id,
        "TARGET": target.slug(),
        "REF_NAME": target.ref_name,
        "REF_TYPE": getattr(target.ref_type, "value", target.ref_type),
        "VERSION": repo_version,
        "BUILD_DATE": build_date,
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
    logger.info("Queued build %s for target %s (triggered_by=%s)", build.id, target_id, triggered_by)
    await queue.enqueue(build.id)
    return build


def pending_builds(session: Session) -> list[Build]:
    """Return builds ordered by creation time newest-first."""
    return list(session.exec(select(Build).order_by(Build.created_at.desc())).all())
