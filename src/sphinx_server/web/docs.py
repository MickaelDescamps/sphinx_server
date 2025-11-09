"""Public-facing documentation explorer routes."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from ..database import get_session
from ..models import Build, Repository, TrackedTarget

router = APIRouter(tags=["docs"])

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


@router.get("/")
def docs_index(request: Request, session: Session = Depends(get_session)):
    """Render the documentation landing page with latest builds per target."""
    repo_stmt = select(Repository).options(selectinload(Repository.tracked_targets))
    repos = session.exec(repo_stmt).all()
    builds = session.exec(select(Build).order_by(Build.created_at.desc())).all()
    latest_artifacts = _latest_artifacts(builds)
    build_map: dict[int, list[Build]] = defaultdict(list)
    for build in builds:
        if build.target_id:
            build_map[build.target_id].append(build)
    return templates.TemplateResponse(
        "docs/index.html",
        {
            "request": request,
            "repos": repos,
            "build_map": build_map,
            "latest_artifacts": latest_artifacts,
        },
    )


@router.get("/docs/{repo_id}/refs.json")
def repo_refs(repo_id: int, session: Session = Depends(get_session)):
    """Return JSON describing tracked refs and their latest artifacts."""
    repo = session.exec(
        select(Repository).where(Repository.id == repo_id).options(selectinload(Repository.tracked_targets))
    ).one_or_none()
    if not repo:
        raise HTTPException(status_code=404)
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
        raise HTTPException(status_code=404)
    build_stmt = (
        select(Build)
        .where(Build.target_id == target_id)
        .order_by(Build.created_at.desc())
    )
    builds = session.exec(build_stmt).all()
    return templates.TemplateResponse(
        "docs/target.html",
        {"request": request, "repo": repo, "target": target, "builds": builds},
    )


def _latest_artifacts(builds: list[Build]) -> dict[int, Build]:
    """Map target ids to the most recent successful build containing artifacts."""
    latest: dict[int, Build] = {}
    for build in builds:
        if not build.target_id or build.target_id in latest:
            continue
        if build.artifact_path:
            latest[build.target_id] = build
    return latest
