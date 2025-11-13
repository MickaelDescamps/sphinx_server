"""Utils to manage git actions"""


from __future__ import annotations

import logging
import os
import subprocess
import uuid
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

from .config import settings
from .models import RefType

logger = logging.getLogger(__name__)


class GitError(RuntimeError):
    """Error raised when an underlying git command fails."""


_TRACE_ENV_VARS = {
    "GIT_TRACE": "1",
    "GIT_TRACE_PACKET": "1",
    "GIT_TRACE_PERFORMANCE": "1",
    "GIT_CURL_VERBOSE": "1",
}


def _git_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Return the base git environment with tracing enabled."""
    env = os.environ.copy()
    env.update(_TRACE_ENV_VARS)
    if overrides:
        env.update(overrides)
    return env


def inject_token(url: str, token: str | None) -> str:
    """Inject a personal access token into an HTTPS git URL.

    :param url: Original repository URL.
    :param token: Optional token to insert before the host name.
    :returns: URL with credentials embedded when possible.
    """
    if not token or not url.startswith("http"):
        return url
    parts = urlsplit(url)
    if parts.username:
        return url
    netloc = f"{token}@{parts.hostname}"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def run_git(
    args: Iterable[str],
    cwd: Path | None,
    log_file: Path,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """Run a git command and raise :class:`GitError` on failure.

    :param args: Git arguments (without the ``git`` prefix).
    :param cwd: Working directory where the command executes.
    :param log_file: File for appending stdout/stderr.
    :param timeout: Optional timeout passed to :func:`subprocess.run`.
    :param env: Optional environment overrides, e.g., SSH command.
    """
    cmd = ["git", *args]
    with log_file.open("a", encoding="utf-8") as log:
        logger.debug("Executing git %s (cwd=%s)", " ".join(args), cwd or os.getcwd())
        log.write(f"\n$ {' '.join(cmd)}\n")
        log.flush()
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
            text=True,
            timeout=timeout or settings.git_default_timeout,
            env=_git_env(env),
        )
        if proc.returncode != 0:
            logger.error("git %s failed with %s", " ".join(args), proc.returncode)
            raise GitError(f"git {' '.join(args)} failed with code {proc.returncode}")


def clone_or_fetch(
    repo_url: str,
    token: str | None,
    checkout_dir: Path,
    log_file: Path,
    timeout: int | None = None,
    retries: int = 2,
    deploy_key: str | None = None,
    ssh_workdir: Path | None = None,
) -> None:
    """Clone a repository (with retries) or fetch updates if it already exists.

    :param repo_url: Remote repository URL.
    :param token: HTTP token to inject for private clones.
    :param checkout_dir: Destination directory for the git clone.
    :param log_file: Log capturing command output.
    :param timeout: Optional timeout for git commands.
    :param retries: Number of clone retries before re-raising.
    :param deploy_key: SSH private key contents for private repos.
    :param ssh_workdir: Directory to write temporary SSH keys into.
    """
    checkout_dir.parent.mkdir(parents=True, exist_ok=True)
    if checkout_dir.exists():
        logger.debug("Fetching updates for repo at %s", checkout_dir)
        run_git(["fetch", "--all", "--tags", "--prune"], cwd=checkout_dir, log_file=log_file)
        return

    temp_url = inject_token(repo_url, token)
    env = None
    key_path: Path | None = None
    env, key_path = _prepare_ssh_env(deploy_key, ssh_workdir)

    attempt = 0
    while True:
        try:
            run_git(
                ["clone", "-v", temp_url, str(checkout_dir)],
                cwd=None,
                log_file=log_file,
                timeout=timeout,
                env=env,
            )
            logger.info("Cloned repository %s into %s", repo_url, checkout_dir)
            break
        except GitError as exc:
            attempt += 1
            if attempt > retries:
                if key_path and key_path.exists():
                    key_path.unlink()
                raise
            with log_file.open("a", encoding="utf-8") as log:
                log.write(f"Retrying clone ({attempt}/{retries}) after error: {exc}\n")
            logger.warning("Retrying clone of %s (%s/%s)", repo_url, attempt, retries)
    _cleanup_ssh_key(key_path)
    if token:
        logger.debug("Resetting remote URL to %s", repo_url)
        run_git(["remote", "set-url", "origin", repo_url], cwd=checkout_dir, log_file=log_file)


def list_remote_refs(repo_url: str, token: str | None, ref_type: str) -> list[str]:
    """List branches or tags from a remote repository.

    :param repo_url: Repository URI.
    :param token: Optional HTTP token to inject.
    :param ref_type: ``\"branch\"`` or ``\"tag\"`` to filter refs.
    :returns: Sorted unique list of ref names.
    :raises GitError: On ``git ls-remote`` failure.
    """
    flag = "--heads" if ref_type == "branch" else "--tags"
    temp_dir = settings.data_dir / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    log_file = temp_dir / "git-ls.log"
    cmd = ["git", "ls-remote", flag, inject_token(repo_url, token)]
    with log_file.open("a", encoding="utf-8") as log:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            env=_git_env(),
        )
        if proc.returncode != 0:
            log.write(proc.stdout)
            logger.error("git ls-remote failed for %s", repo_url)
            raise GitError(f"git ls-remote failed: {proc.stdout.strip()}")
    refs = []
    for line in proc.stdout.splitlines():
        if "\t" not in line:
            continue
        _, ref = line.split("\t", 1)
        if ref.startswith("refs/heads/"):
            refs.append(ref.replace("refs/heads/", ""))
        elif ref.startswith("refs/tags/"):
            refs.append(ref.replace("refs/tags/", ""))
        else:
            refs.append(ref)
    return sorted(set(refs))


def get_remote_sha(
    repo_url: str,
    token: str | None,
    ref_type: RefType | str,
    ref_name: str,
    deploy_key: str | None = None,
) -> str | None:
    """Return the SHA of a remote ref without cloning an entire repository.

    :param repo_url: Repository URL.
    :param token: Optional HTTP token.
    :param ref_type: :class:`RefType` enum or raw string.
    :param ref_name: Branch or tag name.
    :param deploy_key: Optional SSH deploy key contents.
    :returns: SHA string or ``None`` if the ref does not exist.
    :raises GitError: When ``git ls-remote`` exits with an error.
    """
    ref_type_str = ref_type.value if hasattr(ref_type, "value") else ref_type
    if ref_type_str == "branch":
        refspec = f"refs/heads/{ref_name}"
    else:
        refspec = f"refs/tags/{ref_name}"
    env, key_path = _prepare_ssh_env(deploy_key, None)
    try:
        cmd = ["git", "ls-remote", inject_token(repo_url, token), refspec]
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            env=_git_env(env),
            timeout=settings.git_default_timeout,
        )
        if proc.returncode != 0:
            logger.error("git ls-remote failed for %s %s", repo_url, refspec)
            raise GitError(proc.stderr.strip() or "git ls-remote failed")
        for line in proc.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) == 2 and parts[1] == refspec:
                return parts[0]
        return None
    finally:
        _cleanup_ssh_key(key_path)


def _prepare_ssh_env(deploy_key: str | None, ssh_workdir: Path | None) -> tuple[dict[str, str] | None, Path | None]:
    """Generate a temporary SSH key file and return env overrides.

    :param deploy_key: Private key contents to persist temporarily.
    :param ssh_workdir: Directory for storing the ephemeral key.
    :returns: Tuple of ``(env, key_path)`` used by git commands.
    """
    if not deploy_key:
        return None, None
    key_dir = Path(ssh_workdir or (settings.data_dir / "ssh_keys"))
    key_dir.mkdir(parents=True, exist_ok=True)
    key_path = key_dir / f"deploy_{uuid.uuid4().hex}"
    key_path.write_text(deploy_key.strip() + ("\n" if not deploy_key.endswith("\n") else ""))
    os.chmod(key_path, 0o600)
    logger.debug("Created temporary deploy key at %s", key_path)
    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = f"ssh -i {key_path} -o StrictHostKeyChecking=no"
    return env, key_path


def _cleanup_ssh_key(key_path: Path | None) -> None:
    """Remove an on-disk SSH key once it is no longer needed.

    :param key_path: Path to delete.
    """
    if key_path and key_path.exists():
        logger.debug("Deleting temporary deploy key %s", key_path)
        key_path.unlink()
