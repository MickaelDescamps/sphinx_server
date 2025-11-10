"""Authentication helpers, password utilities, and role guards."""

from __future__ import annotations

import binascii
import hashlib
import hmac
import logging
import secrets
from typing import Callable
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request, status
from sqlmodel import Session, select

from .config import settings
from .database import engine, get_session, session_scope
from .models import User, UserRole

logger = logging.getLogger(__name__)

PBKDF2_ITERATIONS = 390_000
ROLE_ORDER = {
    UserRole.viewer: 0,
    UserRole.contributor: 1,
    UserRole.administrator: 2,
}


def hash_password(password: str) -> str:
    """Return a salted PBKDF2 hash for the provided password."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Compare the submitted password against the stored PBKDF2 hash."""
    try:
        algo, iterations, salt_hex, digest_hex = stored_hash.split("$", 3)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    try:
        iterations_int = int(iterations)
        salt = bytes.fromhex(salt_hex)
        digest = bytes.fromhex(digest_hex)
    except (ValueError, binascii.Error):
        return False
    comparison = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations_int)
    return hmac.compare_digest(comparison, digest)


def _login_redirect(request: Request) -> HTTPException:
    target = str(request.url.path)
    if request.url.query:
        target = f"{target}?{request.url.query}"
    encoded = quote(target, safe="")
    return HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": f"/login?next={encoded}"},
    )


def require_user(
    request: Request,
    session: Session = Depends(get_session),
) -> User:
    """Ensure there is an authenticated user attached to the request."""
    user_id = request.session.get("user_id") if "session" in request.scope else None
    if not user_id:
        raise _login_redirect(request)
    user = session.get(User, user_id)
    if not user or not user.is_active:
        request.session.pop("user_id", None)
        raise _login_redirect(request)
    request.state.user = user
    if user.must_change_password and not _path_allows_account_only(request.url.path):
        raise _password_change_redirect()
    return user


def require_role(min_role: UserRole) -> Callable[[User], User]:
    """Factory returning a dependency that enforces a minimum role."""

    def dependency(user: User = Depends(require_user)) -> User:
        user_rank = ROLE_ORDER.get(user.role, 0)
        min_rank = ROLE_ORDER[min_role]
        if user_rank < min_rank:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        return user

    return dependency


require_viewer = require_role(UserRole.viewer)
require_contributor = require_role(UserRole.contributor)
require_admin = require_role(UserRole.administrator)


def get_optional_user(request: Request) -> User | None:
    """Return the authenticated user if a valid session cookie exists."""
    user_id = request.session.get("user_id") if "session" in request.scope else None
    if not user_id:
        return None
    with session_scope() as session:
        user = session.get(User, user_id)
        if user and user.is_active:
            request.state.user = user
            return user
    request.session.pop("user_id", None)
    return None


def _path_allows_account_only(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in PASSWORD_ENFORCED_PATHS)


def _password_change_redirect() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": f"/account?{PASSWORD_FORCE_QUERY}"},
    )


def ensure_default_admin() -> None:
    """Seed the database with a default admin user when none exist."""
    with Session(engine) as session:
        existing = session.exec(select(User).limit(1)).first()
        if existing:
            return
        username = "admin"
        password = "password"
        admin = User(
            username=username,
            full_name="Administrator",
            role=UserRole.administrator,
            password_hash=hash_password(password),
            must_change_password=True,
        )
        session.add(admin)
        session.commit()
        logger.warning(
            "No users found, created default admin account '%s' with a temporary password. Password change is required on first login.",
            username,
        )
PASSWORD_ENFORCED_PATHS = ("/account", "/logout")
PASSWORD_FORCE_QUERY = "force=password"
