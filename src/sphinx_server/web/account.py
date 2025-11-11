"""Authentication, account management, and user administration routes."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from ..auth import (
    get_optional_user,
    hash_password,
    require_admin,
    require_user,
    verify_password,
)
from ..database import get_session
from ..models import User, UserRole

router = APIRouter(tags=["auth"])

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def _safe_next_url(target: str | None) -> str:
    if not target:
        return "/"
    if target.startswith("http://") or target.startswith("https://"):
        return "/"
    if target.startswith("//"):
        return "/"
    return target or "/"


def _render_account(
    request: Request,
    user: User,
    status: str | None = None,
    error: str | None = None,
    force: str | None = None,
):
    return templates.TemplateResponse(
        "account.html",
        {
            "request": request,
            "status": status,
            "error": error,
            "user_obj": user,
            "force": force or ("password" if user.must_change_password else None),
        },
    )


def _render_user_admin(
    request: Request,
    session: Session,
    status: str | None = None,
    error: str | None = None,
):
    users = session.exec(select(User).order_by(User.username)).all()
    return templates.TemplateResponse(
        "admin/users.html",
        {
            "request": request,
            "users": users,
            "roles": list(UserRole),
            "status": status,
            "error": error,
        },
    )


@router.get("/login")
def login_form(request: Request, next: str | None = None):
    """Render the login form."""
    user = get_optional_user(request)
    if user:
        target = "/account?force=password" if user.must_change_password else _safe_next_url(next)
        return RedirectResponse(url=target, status_code=303)
    return templates.TemplateResponse("auth/login.html", {"request": request, "next": _safe_next_url(next)})


@router.post("/login")
def login_action(
    request: Request,
    username: Annotated[str, Form(...)],
    password: Annotated[str, Form(...)],
    next: Annotated[str | None, Form()] = None,
    session: Session = Depends(get_session),
):
    """Handle login form submission and persist session state."""
    username_clean = username.strip()
    user = session.exec(select(User).where(User.username == username_clean)).one_or_none()
    safe_next = _safe_next_url(next)
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            "auth/login.html",
            {"request": request, "next": safe_next, "error": "Invalid username or password."},
            status_code=400,
        )
    if not user.is_active:
        return templates.TemplateResponse(
            "auth/login.html",
            {"request": request, "next": safe_next, "error": "Account is disabled."},
            status_code=403,
        )
    request.session["user_id"] = user.id
    request.session["role"] = user.role.value
    user.last_login_at = datetime.utcnow()
    user.updated_at = datetime.utcnow()
    session.add(user)
    session.commit()
    redirect_url = "/account?force=password" if user.must_change_password else safe_next
    return RedirectResponse(url=redirect_url, status_code=303)




@router.post("/logout")
def logout_action(request: Request):
    """Destroy the current session and redirect to login."""
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@router.get("/account")
def account_page(
    request: Request,
    status: str | None = None,
    error: str | None = None,
    force: str | None = None,
    user: User = Depends(require_user),
):
    """Display the account management dashboard."""
    return _render_account(request, user, status=status, error=error, force=force)


@router.post("/account/profile")
def update_profile(
    request: Request,
    full_name: Annotated[str | None, Form()] = None,
    email: Annotated[str | None, Form()] = None,
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    """Update the user's profile metadata."""
    user.full_name = (full_name or "").strip() or None
    user.email = (email or "").strip() or None
    user.updated_at = datetime.utcnow()
    session.add(user)
    session.commit()
    return RedirectResponse(url="/account?status=profile-updated", status_code=303)


@router.post("/account/password")
def change_password(
    request: Request,
    current_password: Annotated[str, Form(...)],
    new_password: Annotated[str, Form(...)],
    confirm_password: Annotated[str, Form(...)],
    session: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    """Allow the logged-in user to update their password."""
    if new_password != confirm_password:
        return _render_account(request, user, error="New passwords do not match.")
    if not verify_password(current_password, user.password_hash):
        return _render_account(request, user, error="Current password is incorrect.")
    if len(new_password) < 8:
        return _render_account(request, user, error="Password must be at least 8 characters long.")
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    user.updated_at = datetime.utcnow()
    session.add(user)
    session.commit()
    return RedirectResponse(url="/account?status=password-updated", status_code=303)


@router.get("/admin/users")
def admin_users_page(
    request: Request,
    status: str | None = None,
    error: str | None = None,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    """Render the user management dashboard."""
    return _render_user_admin(request, session, status=status, error=error)


@router.post("/admin/users")
def create_user(
    request: Request,
    username: Annotated[str, Form(...)],
    password: Annotated[str, Form(...)],
    role: Annotated[UserRole, Form(...)],
    full_name: Annotated[str | None, Form()] = None,
    email: Annotated[str | None, Form()] = None,
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    """Create a new user account."""
    username_clean = username.strip()
    if session.exec(select(User).where(User.username == username_clean)).one_or_none():
        return _render_user_admin(request, session, error="Username already exists.")
    if len(password) < 8:
        return _render_user_admin(request, session, error="Password must be at least 8 characters long.")
    user = User(
        username=username_clean,
        full_name=(full_name or "").strip() or None,
        email=(email or "").strip() or None,
        role=role,
        password_hash=hash_password(password),
        must_change_password=True,
    )
    session.add(user)
    session.commit()
    return RedirectResponse(url="/admin/users?status=created", status_code=303)


@router.post("/admin/users/{user_id}/role")
def update_user_role(
    request: Request,
    user_id: int,
    role: Annotated[UserRole, Form(...)],
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    """Update the role assigned to a user."""
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role == UserRole.administrator and role != UserRole.administrator:
        remaining_admins = session.exec(
            select(User).where(User.role == UserRole.administrator, User.id != user.id)
        ).all()
        if not remaining_admins:
            return _render_user_admin(request, session, error="At least one administrator is required.")
    user.role = role
    user.updated_at = datetime.utcnow()
    session.add(user)
    session.commit()
    return RedirectResponse(url="/admin/users?status=role-updated", status_code=303)


@router.post("/admin/users/{user_id}/reset-password")
def admin_reset_password(
    request: Request,
    user_id: int,
    password: Annotated[str, Form(...)],
    session: Session = Depends(get_session),
    _: User = Depends(require_admin),
):
    """Allow administrators to reset another user's password."""
    if len(password) < 8:
        return _render_user_admin(request, session, error="Password must be at least 8 characters long.")
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.password_hash = hash_password(password)
    user.must_change_password = True
    user.updated_at = datetime.utcnow()
    session.add(user)
    session.commit()
    return RedirectResponse(url="/admin/users?status=password-reset", status_code=303)
