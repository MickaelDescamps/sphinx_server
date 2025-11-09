"""Administrative FastAPI routes for managing repositories and builds."""

from __future__ import annotations

from typing import Annotated

import shutil
import subprocess
import tempfile
from pathlib import Path

import hashlib
import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from ..build_service import BuildQueue, enqueue_target_build
from ..config import settings
from ..database import get_session
from ..git_utils import GitError, list_remote_refs
from ..models import Build, ProviderType, RefType, Repository, TrackedTarget

router = APIRouter(prefix="/admin", tags=["admin"])

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def get_queue(request: Request) -> BuildQueue:
    """Extract the shared :class:`BuildQueue` from the FastAPI app state."""
    return request.app.state.build_queue


def _safe_unlink(path: str | None) -> None:
    """Delete a file while ignoring errors such as missing paths."""
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _safe_rmtree(path: str | Path | None) -> None:
    """Remove a directory tree while swallowing exceptions."""
    if not path:
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def _cleanup_build_artifacts(build: Build) -> None:
    """Delete stored log and artifact files for a build."""
    _safe_unlink(build.log_path)
    _safe_rmtree(build.artifact_path)


def _delete_build(session: Session, build: Build) -> None:
    """Remove a build row and any associated artifacts from disk."""
    _cleanup_build_artifacts(build)
    session.delete(build)


@router.get("/")
def admin_index(
    request: Request,
    session: Session = Depends(get_session),
):
    """Render the admin dashboard listing repositories and recent builds."""
    repo_stmt = (
        select(Repository)
        .options(selectinload(Repository.tracked_targets))
        .order_by(Repository.name)
    )
    repos = session.exec(repo_stmt).all()
    build_stmt = (
        select(Build)
        .options(selectinload(Build.repository), selectinload(Build.target))
        .order_by(Build.created_at.desc())
        .limit(10)
    )
    builds = session.exec(build_stmt).all()
    return templates.TemplateResponse(
        "admin/index.html",
        {"request": request, "repos": repos, "builds": builds},
    )


@router.get("/repos/new")
def new_repo(request: Request):
    """Render a blank form for onboarding a repository."""
    return templates.TemplateResponse(
        "admin/repo_form.html",
        {
            "request": request,
            "title": "New repository",
            "action_url": "/admin/repos/new",
            "repo": None,
        },
    )


@router.post("/repos/new")
async def create_repo(
    request: Request,
    name: Annotated[str, Form(...)],
    provider: Annotated[ProviderType, Form(...)],
    url: Annotated[str, Form(...)],
    description: Annotated[str | None, Form()] = None,
    default_branch: Annotated[str | None, Form()] = None,
    docs_path: Annotated[str, Form()] = "docs",
    auth_token: Annotated[str | None, Form()] = None,
    deploy_key: Annotated[str | None, Form()] = None,
    session: Session = Depends(get_session),
):
    """Persist a new repository based on submitted form data."""
    cleaned_key = deploy_key.strip() if deploy_key else None
    repo = Repository(
        name=name,
        provider=provider,
        url=url.strip(),
        description=description,
        default_branch=default_branch,
        docs_path=docs_path or "docs",
        auth_token=auth_token,
        deploy_key=cleaned_key,
    )
    session.add(repo)
    session.commit()
    session.refresh(repo)
    return RedirectResponse(url=f"/admin/repos/{repo.id}", status_code=303)


@router.get("/repos/{repo_id}/edit")
def edit_repo_form(
    repo_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    """Render the edit form populated with an existing repository."""
    repo = session.get(Repository, repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")
    return templates.TemplateResponse(
        "admin/repo_form.html",
        {
            "request": request,
            "title": f"Edit {repo.name}",
            "action_url": f"/admin/repos/{repo_id}/edit",
            "repo": repo,
        },
    )


@router.post("/repos/{repo_id}/edit")
async def update_repo(
    repo_id: int,
    name: Annotated[str, Form(...)],
    provider: Annotated[ProviderType, Form(...)],
    url: Annotated[str, Form(...)],
    description: Annotated[str | None, Form()] = None,
    default_branch: Annotated[str | None, Form()] = None,
    docs_path: Annotated[str, Form()] = "docs",
    auth_token: Annotated[str | None, Form()] = None,
    deploy_key: Annotated[str | None, Form()] = None,
    session: Session = Depends(get_session),
):
    """Update repository metadata based on admin input."""
    repo = session.get(Repository, repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")
    repo.name = name
    repo.provider = provider
    repo.url = url.strip()
    repo.description = description
    repo.default_branch = default_branch
    repo.docs_path = docs_path or "docs"
    repo.auth_token = auth_token
    if deploy_key is not None and deploy_key.strip() != "":
        repo.deploy_key = deploy_key.strip()
    session.add(repo)
    session.commit()
    return RedirectResponse(url=f"/admin/repos/{repo_id}", status_code=303)


@router.get("/repos/{repo_id}")
def repo_detail(
    repo_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    """Show repository details, tracked targets, and build history."""
    repo_stmt = (
        select(Repository)
        .where(Repository.id == repo_id)
        .options(selectinload(Repository.tracked_targets))
    )
    repo = session.exec(repo_stmt).one_or_none()
    if not repo:
        return RedirectResponse(url="/admin", status_code=302)
    build_stmt = (
        select(Build)
        .where(Build.repository_id == repo_id)
        .options(selectinload(Build.target))
        .order_by(Build.created_at.desc())
    )
    builds = session.exec(build_stmt).all()
    return templates.TemplateResponse(
        "admin/repo_detail.html",
        {"request": request, "repo": repo, "builds": builds},
    )


@router.get("/repos/{repo_id}/builds.json")
def repo_builds_json(
    repo_id: int,
    token: str | None = None,
    session: Session = Depends(get_session),
):
    """Return JSON-encoded build metadata for polling in the UI."""
    build_stmt = (
        select(Build)
        .where(Build.repository_id == repo_id)
        .options(selectinload(Build.target))
        .order_by(Build.created_at.desc())
    )
    builds = session.exec(build_stmt).all()
    payload = []
    for build in builds:
        target = build.target
        slug = target.slug() if target else None
        artifact_url = None
        if build.artifact_path and slug:
            artifact_url = f"/artifacts/{build.repository_id}/{slug}/index.html"
        status_value = str(build.status)
        duration = build.duration_seconds
        duration_label = f"{duration:.1f}s" if duration else "-"
        ref_type = target.ref_type.value if target else None
        payload.append(
            {
                "id": build.id,
                "status": status_value,
                "status_label": status_value.replace("BuildStatus.", "").replace("_", " "),
                "artifact_url": artifact_url,
                "has_artifact": bool(build.artifact_path),
                "log_path": build.log_path,
                "log_url": f"/admin/builds/{build.id}/log" if build.log_path else None,
                "duration_label": duration_label,
                "triggered_by": build.triggered_by or "manual",
                "ref_name": build.ref_name,
                "ref_type": ref_type,
            }
        )
    signature = hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    if token and token == signature:
        return Response(status_code=204, headers={"X-Build-Token": signature})
    return JSONResponse({"builds": payload, "token": signature})


@router.post("/repos/{repo_id}/delete")
async def delete_repo(
    repo_id: int,
    session: Session = Depends(get_session),
):
    """Delete a repository along with its tracked targets and builds."""
    repo = session.get(Repository, repo_id)
    if repo:
        builds = session.exec(select(Build).where(Build.repository_id == repo_id)).all()
        for build in builds:
            _delete_build(session, build)
        targets = session.exec(select(TrackedTarget).where(TrackedTarget.repository_id == repo_id)).all()
        for target in targets:
            session.delete(target)
        repo_cache = settings.repo_cache_dir / f"repo_{repo.id}"
        artifacts_root = settings.build_output_dir / str(repo.id)
        _safe_rmtree(repo_cache)
        _safe_rmtree(artifacts_root)
        session.delete(repo)
        session.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/repos/{repo_id}/targets")
async def add_target(
    repo_id: int,
    ref_type: Annotated[RefType, Form(...)],
    ref_name: Annotated[str, Form(...)],
    auto_build: Annotated[bool | None, Form()] = False,
    session: Session = Depends(get_session),
):
    """Create a tracked branch or tag for the given repository."""
    repo = session.get(Repository, repo_id)
    if not repo:
        return RedirectResponse("/admin", status_code=302)
    target = TrackedTarget(
        repository_id=repo_id,
        ref_type=ref_type,
        ref_name=ref_name.strip(),
        auto_build=bool(auto_build),
    )
    session.add(target)
    session.commit()
    return RedirectResponse(url=f"/admin/repos/{repo_id}", status_code=303)


@router.post("/repos/{repo_id}/primary")
async def set_primary_target(
    repo_id: int,
    target_id: Annotated[int, Form(...)],
    session: Session = Depends(get_session),
):
    """Mark a tracked target as the repository's primary source of metadata."""
    repo = session.get(Repository, repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")
    target = session.get(TrackedTarget, target_id)
    if not target or target.repository_id != repo_id:
        raise HTTPException(status_code=404, detail="Target not found")
    repo.primary_target_id = target.id
    session.add(repo)
    session.commit()
    return RedirectResponse(url=f"/admin/repos/{repo_id}", status_code=303)


@router.post("/targets/{target_id}/build")
async def build_target(
    target_id: int,
    request: Request,
    session: Session = Depends(get_session),
    queue: BuildQueue = Depends(get_queue),
):
    """Enqueue a manual build for the specified target."""
    await enqueue_target_build(target_id, session, queue, triggered_by="manual")
    referer = request.headers.get("referer") or "/admin"
    return RedirectResponse(url=referer, status_code=303)


@router.get("/repos/{repo_id}/refs")
def available_refs(
    repo_id: int,
    ref_type: RefType,
    session: Session = Depends(get_session),
):
    """Fetch remote branches or tags to populate autocomplete UI."""
    repo = session.get(Repository, repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")
    try:
        refs = list_remote_refs(repo.url, repo.auth_token, ref_type.value)
    except GitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"refs": refs})


@router.get("/builds/{build_id}/log")
def view_build_log(
    build_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    """Render the stored build log inside the admin UI."""
    build_stmt = (
        select(Build)
        .where(Build.id == build_id)
        .options(selectinload(Build.repository), selectinload(Build.target))
    )
    build = session.exec(build_stmt).one_or_none()
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")
    log_content = "Log file not found."
    if build.log_path and Path(build.log_path).exists():
        log_content = Path(build.log_path).read_text(encoding="utf-8", errors="replace")
    return templates.TemplateResponse(
        "admin/build_log.html",
        {"request": request, "build": build, "log_content": log_content},
    )


@router.get("/builds/{build_id}/log.txt")
def view_build_log_raw(build_id: int, session: Session = Depends(get_session)):
    """Return the plain-text build log for downloading or streaming."""
    build = session.get(Build, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")
    if build.log_path and Path(build.log_path).exists():
        return PlainTextResponse(Path(build.log_path).read_text(encoding="utf-8", errors="replace"))
    return PlainTextResponse("Log not available yet.")


@router.post("/builds/{build_id}/delete")
async def delete_build(
    build_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    """Remove a single build record and associated artifacts."""
    build = session.get(Build, build_id)
    if build:
        repo_id = build.repository_id
        _delete_build(session, build)
        session.commit()
        referer = request.headers.get("referer") or f"/admin/repos/{repo_id}"
        return RedirectResponse(url=referer, status_code=303)
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/repos/{repo_id}/builds/clear")
async def clear_repo_builds(
    repo_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    """Delete every build belonging to the provided repository."""
    builds = session.exec(select(Build).where(Build.repository_id == repo_id)).all()
    for build in builds:
        _delete_build(session, build)
    session.commit()
    referer = request.headers.get("referer") or f"/admin/repos/{repo_id}"
    return RedirectResponse(url=referer, status_code=303)


@router.get("/targets/{target_id}/edit")
def edit_target_form(
    target_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    """Display the edit form for a tracked target."""
    target = session.get(TrackedTarget, target_id)
    if not target:
        raise HTTPException(status_code=404, detail="Target not found")
    repo = session.get(Repository, target.repository_id)
    return templates.TemplateResponse(
        "admin/target_form.html",
        {
            "request": request,
            "target": target,
            "repo": repo,
        },
    )


@router.post("/targets/{target_id}/edit")
async def update_target(
    target_id: int,
    ref_type: Annotated[RefType, Form(...)],
    ref_name: Annotated[str, Form(...)],
    auto_build: Annotated[bool | None, Form()] = False,
    session: Session = Depends(get_session),
):
    """Persist changes to a tracked target."""
    target = session.get(TrackedTarget, target_id)
    if not target:
        raise HTTPException(status_code=404, detail="Target not found")
    target.ref_type = ref_type
    target.ref_name = ref_name.strip()
    target.auto_build = bool(auto_build)
    session.add(target)
    session.commit()
    return RedirectResponse(url=f"/admin/repos/{target.repository_id}", status_code=303)


@router.post("/targets/{target_id}/delete")
async def delete_target(
    target_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    """Remove a tracked target and delete its builds."""
    target = session.get(TrackedTarget, target_id)
    if target:
        builds = session.exec(select(Build).where(Build.target_id == target_id)).all()
        for build in builds:
            _delete_build(session, build)
        repo_id = target.repository_id
        repo = session.get(Repository, repo_id)
        if repo and repo.primary_target_id == target_id:
            repo.primary_target_id = None
            session.add(repo)
        session.delete(target)
        session.commit()
        referer = request.headers.get("referer") or f"/admin/repos/{repo_id}"
        return RedirectResponse(url=referer, status_code=303)
    return RedirectResponse(url="/admin", status_code=303)
@router.post("/targets/bulk")
async def bulk_target_action(
    repo_id: Annotated[int, Form(...)],
    action: Annotated[str, Form(...)],
    target_ids: Annotated[list[int], Form(...)],
    request: Request,
    session: Session = Depends(get_session),
    queue: BuildQueue = Depends(get_queue),
):
    """Execute bulk build or delete actions on selected targets."""
    if action not in {"build", "delete"}:
        raise HTTPException(status_code=400, detail="Unsupported action")
    for target_id in target_ids:
        target = session.get(TrackedTarget, target_id)
        if not target or target.repository_id != repo_id:
            continue
        if action == "build":
            await enqueue_target_build(target_id, session, queue, triggered_by="manual")
        elif action == "delete":
            builds = session.exec(select(Build).where(Build.target_id == target_id)).all()
            for build in builds:
                _delete_build(session, build)
            repo = session.get(Repository, repo_id)
            if repo and repo.primary_target_id == target_id:
                repo.primary_target_id = None
                session.add(repo)
            session.delete(target)
            session.commit()
    referer = request.headers.get("referer") or f"/admin/repos/{repo_id}"
    return RedirectResponse(url=referer, status_code=303)
@router.post("/ssh-key")
def generate_ssh_key(
    algorithm: Annotated[str, Form()] = "ssh-ed25519",
):
    """Generate an SSH deploy key pair using ``ssh-keygen``."""
    allowed = {"ssh-ed25519", "ssh-rsa", "ssh-mlkem768x25519-sha256"}
    if algorithm not in allowed:
        raise HTTPException(status_code=400, detail="Unsupported algorithm")
    with tempfile.TemporaryDirectory() as tmpdir:
        key_path = Path(tmpdir) / "deploy_key"
        cmd = [
            "ssh-keygen",
            "-t",
            algorithm,
            "-N",
            "",
            "-C",
            f"sphinx-server-{algorithm}",
            "-f",
            str(key_path),
        ]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as exc:
            raise HTTPException(status_code=500, detail=f"ssh-keygen failed: {exc.stderr.decode()}" if exc.stderr else "ssh-keygen failed") from exc
        private_key = key_path.read_text()
        public_key = (key_path.with_suffix(".pub")).read_text()
        return JSONResponse({"private_key": private_key, "public_key": public_key})
