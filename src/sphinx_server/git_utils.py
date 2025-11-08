from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

from .config import settings
from .models import RefType


class GitError(RuntimeError):
    pass


def inject_token(url: str, token: str | None) -> str:
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
    cmd = ["git", *args]
    with log_file.open("a", encoding="utf-8") as log:
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
            env=env,
        )
        if proc.returncode != 0:
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
    checkout_dir.parent.mkdir(parents=True, exist_ok=True)
    if checkout_dir.exists():
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
            break
        except GitError as exc:
            attempt += 1
            if attempt > retries:
                if key_path and key_path.exists():
                    key_path.unlink()
                raise
            with log_file.open("a", encoding="utf-8") as log:
                log.write(f"Retrying clone ({attempt}/{retries}) after error: {exc}\n")
    _cleanup_ssh_key(key_path)
    if token:
        run_git(["remote", "set-url", "origin", repo_url], cwd=checkout_dir, log_file=log_file)


def list_remote_refs(repo_url: str, token: str | None, ref_type: str) -> list[str]:
    flag = "--heads" if ref_type == "branch" else "--tags"
    temp_dir = settings.data_dir / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    log_file = temp_dir / "git-ls.log"
    cmd = ["git", "ls-remote", flag, inject_token(repo_url, token)]
    with log_file.open("a", encoding="utf-8") as log:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
        if proc.returncode != 0:
            log.write(proc.stdout)
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
            env=env,
            timeout=settings.git_default_timeout,
        )
        if proc.returncode != 0:
            raise GitError(proc.stderr.strip() or "git ls-remote failed")
        for line in proc.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) == 2 and parts[1] == refspec:
                return parts[0]
        return None
    finally:
        _cleanup_ssh_key(key_path)


def _prepare_ssh_env(deploy_key: str | None, ssh_workdir: Path | None) -> tuple[dict[str, str] | None, Path | None]:
    if not deploy_key:
        return None, None
    key_dir = Path(ssh_workdir or (settings.data_dir / "ssh_keys"))
    key_dir.mkdir(parents=True, exist_ok=True)
    key_path = key_dir / f"deploy_{uuid.uuid4().hex}"
    key_path.write_text(deploy_key.strip() + ("\n" if not deploy_key.endswith("\n") else ""))
    os.chmod(key_path, 0o600)
    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = f"ssh -i {key_path} -o StrictHostKeyChecking=no"
    return env, key_path


def _cleanup_ssh_key(key_path: Path | None) -> None:
    if key_path and key_path.exists():
        key_path.unlink()
