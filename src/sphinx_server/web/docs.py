"""Public-facing documentation explorer routes."""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from sphinx_server.model_converter import convert_build_to_ui_model

from ..auth import get_optional_user, require_user
from ..config import settings
from ..database import get_session
from ..models import Build, Repository, TrackedTarget

router = APIRouter(tags=["docs"], dependencies=[Depends(require_user)])

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
logger = logging.getLogger(__name__)


@router.get("/")
def docs_index(request: Request, session: Session = Depends(get_session)):
    """Render the documentation landing page with latest builds per target."""
    logger.debug("Rendering docs index")
    repo_stmt = select(Repository).options(selectinload(Repository.tracked_targets))
    all_repos = session.exec(repo_stmt).all()
    user = get_optional_user(request)
    if user:
        visible_repos = all_repos
    else:
        visible_repos = [repo for repo in all_repos if repo.public_docs]
        if not visible_repos:
            require_user(request=request, session=session)
    allowed_repo_ids = {repo.id for repo in visible_repos}
    builds: list[Build] = []
    if allowed_repo_ids:
        repo_ids_tuple = tuple(allowed_repo_ids)
        build_stmt = (
            select(Build)
            .where(Build.repository_id.in_(repo_ids_tuple))
            .order_by(Build.created_at.desc())
        )
        builds = session.exec(build_stmt).all()
    latest_artifacts = _latest_artifacts(builds)
    build_map: dict[int, list[Build]] = defaultdict(list)
    for build in builds:
        if build.target_id:
            build_map[build.target_id].append(build)
    return templates.TemplateResponse(
        "docs/index.html",
        {
            "request": request,
            "repos": visible_repos,
            "build_map": build_map,
            "latest_artifacts": latest_artifacts,
            "docs_link_new_tab": settings.docs_link_new_tab,
        },
    )


@router.get("/docs/{repo_id}/refs.json")
def repo_refs(repo_id: int, request: Request, session: Session = Depends(get_session)):
    """Return JSON describing tracked refs and their latest artifacts."""
    repo = session.exec(
        select(Repository).where(Repository.id == repo_id).options(selectinload(Repository.tracked_targets))
    ).one_or_none()
    if not repo:
        logger.error("Repo %s not found when requesting refs", repo_id)
        raise HTTPException(status_code=404)
    _ensure_repo_docs_access(repo, request, session)
    builds = session.exec(
        select(Build)
        .where(Build.repository_id == repo_id)
        .order_by(Build.created_at.desc())
    ).all()
    latest = _latest_artifacts(builds)
    targets = []
    for target in sorted(repo.tracked_targets, key=lambda t: (t.ref_type, t.ref_name)):
        artifact = latest.get(target.id)
        targets.append(
            {
                "id": target.id,
                "ref_type": target.ref_type,
                "ref_name": target.ref_name,
                "slug": target.slug(),
                "url": f"/artifacts/{repo.id}/{target.slug()}/index.html" if artifact else None,
                "has_artifact": bool(artifact),
            }
        )
    return JSONResponse({"repo": {"id": repo.id, "name": repo.name}, "targets": targets})


@router.get("/docs/{repo_id}/{target_id}")
def target_docs(repo_id: int, target_id: int, request: Request, session: Session = Depends(get_session)):
    """Render a detail page showing all builds for a target."""
    repo = session.get(Repository, repo_id)
    target = session.get(TrackedTarget, target_id)
    if not repo or not target or target.repository_id != repo_id:
        logger.error("Repo %s or target %s missing for docs view", repo_id, target_id)
        raise HTTPException(status_code=404)
    _ensure_repo_docs_access(repo, request, session)
    build_stmt = (
        select(Build)
        .where(Build.target_id == target_id)
        .order_by(Build.created_at.desc())
    )
    builds = session.exec(build_stmt).all()

    out_builds = []
    for build in builds:
        out_builds.append(convert_build_to_ui_model(build))

    return templates.TemplateResponse(
        "docs/target.html",
        {"request": request, "repo": repo, "target": target, "builds": out_builds},
    )


@router.get("/artifacts/{repo_id}")
def artifact_index(repo_id: int, request: Request, session: Session = Depends(get_session)):
    """Serve artifact directory index by defaulting to index.html."""
    return artifact_file(repo_id, "", request, session)


@router.get("/artifacts/{repo_id}/{requested_path:path}")
def artifact_file(
    repo_id: int,
    requested_path: str,
    request: Request,
    session: Session = Depends(get_session),
):
    """Serve generated documentation files with access control."""
    repo = session.get(Repository, repo_id)
    if not repo:
        logger.error("Repo %s not found when serving artifact %s", repo_id, requested_path)
        raise HTTPException(status_code=404, detail="Repository not found")
    _ensure_repo_docs_access(repo, request, session)
    base_dir = (settings.build_output_dir / str(repo_id)).resolve()
    if not base_dir.exists():
        raise HTTPException(status_code=404, detail="Artifact directory missing")
    relative = Path(requested_path) if requested_path else Path()
    target_path = (base_dir / relative).resolve()
    try:
        target_path.relative_to(base_dir)
    except ValueError:
        raise HTTPException(status_code=403, detail="Invalid artifact path")
    if not requested_path or requested_path.endswith("/") or target_path.is_dir():
        target_path = (base_dir / relative / "index.html").resolve()
        try:
            target_path.relative_to(base_dir)
        except ValueError:
            raise HTTPException(status_code=403, detail="Invalid artifact path")
    if not target_path.exists():
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(target_path)


def _latest_artifacts(builds: list[Build]) -> dict[int, Build]:
    """Map target ids to the most recent successful build containing artifacts."""
    latest: dict[int, Build] = {}
    for build in builds:
        if not build.target_id or build.target_id in latest:
            continue
        if build.artifact_path:
            latest[build.target_id] = build
    return latest


def _ensure_repo_docs_access(repo: Repository, request: Request, session: Session) -> None:
    """Ensure the requester can view documentation for the repository."""
    if repo.public_docs:
        return
    require_user(request=request, session=session)
