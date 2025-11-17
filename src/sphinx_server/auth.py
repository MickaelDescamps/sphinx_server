"""Authentication helpers, password utilities, and role guards."""

from __future__ import annotations

import binascii
import hashlib
import hmac
import logging
import secrets
import ssl
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request, status
from sqlmodel import Session, select

logger = logging.getLogger(__name__)
LDAP_SESSION_KEY = "ldap_user"

try:  # pragma: no cover - ldap3 is optional until configured
    from ldap3 import ALL, BASE, SUBTREE, Connection, Server, Tls
    from ldap3.core.exceptions import LDAPBindError, LDAPException
    from ldap3.utils.conv import escape_filter_chars
    from ldap3.utils.dn import escape_rdn

    LDAP_LIB_AVAILABLE = True
except ImportError as ex:  # pragma: no cover
    logger.error("Fail to load ldap3", exc_info=True)
    Connection = Server = Tls = None  # type: ignore[assignment]
    ALL = BASE = SUBTREE = None  # type: ignore[assignment]
    LDAPBindError = LDAPException = Exception  # type: ignore[assignment]
    escape_filter_chars = escape_rdn = lambda value: value  # type: ignore[assignment]
    LDAP_LIB_AVAILABLE = False

from .config import settings
from .database import engine, get_session, session_scope
from .models import User, UserRole


@dataclass
class LdapDirectoryUser:
    """Representation of a directory user resolved from LDAP groups."""

    identifier: str
    role: UserRole
    group_dn: str
    raw_value: str
    dn: str | None = None

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
    if ldap_backend_enabled():
        user = _load_ldap_session_user(request)
        if not user:
            raise _login_redirect(request)
        request.state.user = user
        return user
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
    if ldap_backend_enabled():
        user = _load_ldap_session_user(request)
        if user:
            request.state.user = user
            return user
        return None
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


def ldap_backend_enabled() -> bool:
    """Return ``True`` when the LDAP backend is active."""
    return settings.ldap_enabled


def store_ldap_session_user(request: Request, user: User) -> None:
    """Persist the LDAP-authenticated user details in the session."""
    if "session" not in request.scope:
        return
    request.session.pop("user_id", None)
    request.session[LDAP_SESSION_KEY] = {
        "username": user.username,
        "full_name": user.full_name,
        "email": user.email,
        "role": user.role.value,
    }
    request.session["role"] = user.role.value


def _load_ldap_session_user(request: Request) -> User | None:
    if "session" not in request.scope:
        return None
    payload = request.session.get(LDAP_SESSION_KEY)
    if not payload:
        return None
    try:
        role = UserRole(payload.get("role", UserRole.viewer.value))
    except ValueError:
        logger.warning("Invalid role '%s' in LDAP session, defaulting to viewer.", payload.get("role"))
        role = UserRole.viewer
    return User(
        username=payload.get("username", ""),
        full_name=payload.get("full_name"),
        email=payload.get("email"),
        role=role,
        password_hash="ldap",
        is_active=True,
        must_change_password=False,
    )


def authenticate_ldap_user(username: str, password: str) -> User | None:
    """Authenticate the provided credentials against the configured LDAP server."""

    if not ldap_backend_enabled():
        return None
    if not LDAP_LIB_AVAILABLE:
        logger.error("ldap3 dependency is missing but LDAP authentication was requested.")
        return None
    if not settings.ldap_server_uri:
        logger.error("LDAP backend is enabled but SPHINX_SERVER_LDAP_SERVER_URI is not configured.")
        return None
    if not password:
        return None
    profile = _ldap_bind_and_fetch_profile(username, password)
    if not profile:
        return None
    group_role = _ldap_role_from_groups(profile.get("dn"), username)
    resolved_role = group_role or _ldap_default_role()
    return User(
        username=username,
        full_name=profile.get("full_name"),
        email=profile.get("email"),
        role=resolved_role,
        password_hash="ldap",
        is_active=True,
        must_change_password=False,
    )


def _ldap_default_role() -> UserRole:
    try:
        return UserRole(settings.ldap_default_role)
    except ValueError:
        logger.warning(
            "Invalid LDAP default role '%s', falling back to viewer.",
            settings.ldap_default_role,
        )
        return UserRole.viewer


def _ldap_role_from_groups(user_dn: str | None, username: str) -> UserRole | None:
    if not user_dn:
        return None
    group_checks: list[tuple[str | None, UserRole]] = [
        (settings.ldap_admin_group_dn, UserRole.administrator),
        (settings.ldap_contributor_group_dn, UserRole.contributor),
        (settings.ldap_viewer_group_dn, UserRole.viewer),
    ]
    for group_dn, role in group_checks:
        if group_dn and _ldap_user_in_group(user_dn, username, group_dn):
            return role
    return None


def _ldap_bind_and_fetch_profile(username: str, password: str) -> dict[str, str | None] | None:
    resolution = _ldap_resolve_user_dn(username)
    if not resolution:
        return None
    user_dn, attributes = resolution
    success, resolved_attributes = _ldap_bind_user(user_dn, password, attributes)
    if not success or not user_dn:
        return None
    attributes_dict = resolved_attributes or {}
    return {
        "dn": user_dn,
        "full_name": _ldap_attr_value(attributes_dict, settings.ldap_full_name_attribute),
        "email": _ldap_attr_value(attributes_dict, settings.ldap_email_attribute),
    }


def _ldap_resolve_user_dn(username: str) -> tuple[str, dict[str, list[str]] | None] | None:
    server = _ldap_server()
    if not server:
        return None
    template = settings.ldap_user_dn_template
    if template:
        safe_username = escape_rdn(username)
        return template.format(username=safe_username), None
    if not settings.ldap_user_base_dn:
        logger.error("SPHINX_SERVER_LDAP_USER_BASE_DN is required when no DN template is provided.")
        return None
    if not settings.ldap_bind_dn or not settings.ldap_bind_password:
        logger.error("LDAP bind DN and password are required to search for users.")
        return None
    search_filter = _ldap_filter(username)
    attributes = _ldap_attribute_list()
    try:
        with Connection(
            server,
            user=settings.ldap_bind_dn,
            password=settings.ldap_bind_password,
            auto_bind=True,
            read_only=True,
            receive_timeout=settings.ldap_timeout,
        ) as conn:
            conn.search(
                search_base=settings.ldap_user_base_dn,
                search_filter=search_filter,
                search_scope=SUBTREE,
                attributes=attributes or None,
                size_limit=1,
            )
            if not conn.entries:
                logger.info("LDAP lookup returned no entries for filter %s", search_filter)
                return None
            entry = conn.entries[0]
            return str(entry.entry_dn), entry.entry_attributes_as_dict
    except LDAPException as exc:
        logger.error("LDAP user lookup failed: %s", exc)
    return None


def _ldap_bind_user(
    user_dn: str,
    password: str,
    attributes: dict[str, list[str]] | None,
) -> tuple[bool, dict[str, list[str]] | None]:
    server = _ldap_server()
    if not server:
        return False, None
    try:
        with Connection(
            server,
            user=user_dn,
            password=password,
            auto_bind=True,
            read_only=True,
            receive_timeout=settings.ldap_timeout,
        ) as conn:
            if attributes is None:
                attributes = _ldap_fetch_attributes(conn, user_dn)
            return True, attributes
    except LDAPBindError:
        logger.info("LDAP bind failed for user %s", user_dn)
    except LDAPException as exc:
        logger.error("LDAP bind error for user %s: %s", user_dn, exc)
    return False, None


def _ldap_fetch_attributes(conn: Connection, user_dn: str) -> dict[str, list[str]] | None:
    attr_list = _ldap_attribute_list()
    if not attr_list:
        return {}
    conn.search(
        search_base=user_dn,
        search_filter="(objectClass=*)",
        search_scope=BASE,
        attributes=attr_list,
        size_limit=1,
    )
    if not conn.entries:
        return {}
    return conn.entries[0].entry_attributes_as_dict


def _ldap_attr_value(attributes: dict[str, list[str]] | None, key: str | None) -> str | None:
    if not attributes or not key:
        return None
    value = attributes.get(key)
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _ldap_filter(username: str) -> str:
    template = settings.ldap_user_filter or "(uid={username})"
    safe_username = escape_filter_chars(username)
    return template.format(username=safe_username)


def _ldap_attribute_list() -> list[str]:
    attrs: list[str] = []
    if settings.ldap_full_name_attribute:
        attrs.append(settings.ldap_full_name_attribute)
    if settings.ldap_email_attribute and settings.ldap_email_attribute not in attrs:
        attrs.append(settings.ldap_email_attribute)
    return attrs


def _ldap_user_in_group(user_dn: str, username: str, group_dn: str) -> bool:
    server = _ldap_server()
    if not server:
        return False
    if not settings.ldap_bind_dn or not settings.ldap_bind_password:
        logger.debug("LDAP group checks require SPHINX_SERVER_LDAP_BIND_DN and password.")
        return False

    member_attr = settings.ldap_group_member_attribute or "member"
    target = _normalize_group_value(_ldap_group_member_value(user_dn, username))

    try:
        with Connection(
            server,
            user=settings.ldap_bind_dn,
            password=settings.ldap_bind_password,
            auto_bind=True,
            read_only=True,
            receive_timeout=settings.ldap_timeout,
        ) as conn:
            values = _ldap_fetch_group_member_values(conn, group_dn, member_attr)
            for value in values:
                if _normalize_group_value(value) == target:
                    return True
            return False

    except LDAPException as exc:
        logger.error(
            "LDAP group membership check failed for user %s in %s: %s",
            user_dn,
            group_dn,
            exc,
        )
    return False


def _ldap_fetch_group_member_values(conn: Connection, group_dn: str, member_attr: str) -> list[str]:
    res = conn.search(
        search_base=group_dn,
        search_filter="(objectClass=Group)",
        search_scope=SUBTREE,
        attributes=["member"],
    )
    logger.debug("LDAP group search result for %s: %s", group_dn, res)
    if not conn.entries:
        logger.info("LDAP group DN %s returned no entries.", group_dn)
        return []
    entry = conn.entries[0]
    attr_map = {k.lower(): v for k, v in entry.entry_attributes_as_dict.items()}
    values = attr_map.get(member_attr.lower())
    if not values:
        logger.debug("LDAP group %s does not expose attribute %s.", group_dn, member_attr)
        return []
    decoded_values: list[str] = []
    for value in values:
        decoded = _decode_ldap_value(value)
        if decoded:
            decoded_values.append(decoded)
    logger.debug("LDAP group %s %s values: %s", group_dn, member_attr, decoded_values)
    return decoded_values


def list_ldap_authorized_users() -> list[LdapDirectoryUser]:
    """Return LDAP users who are authorized via configured role groups."""

    if not ldap_backend_enabled():
        return []

    server = _ldap_server()
    if not server:
        return []

    if not settings.ldap_bind_dn or not settings.ldap_bind_password:
        logger.debug("LDAP group listing requires SPHINX_SERVER_LDAP_BIND_DN and password.")
        return []

    member_attr = settings.ldap_group_member_attribute or "member"
    group_checks: list[tuple[str | None, UserRole]] = [
        (settings.ldap_admin_group_dn, UserRole.administrator),
        (settings.ldap_contributor_group_dn, UserRole.contributor),
        (settings.ldap_viewer_group_dn, UserRole.viewer),
    ]
    resolved: dict[str, LdapDirectoryUser] = {}

    try:
        with Connection(
            server,
            user=settings.ldap_bind_dn,
            password=settings.ldap_bind_password,
            auto_bind=True,
            read_only=True,
            receive_timeout=settings.ldap_timeout,
        ) as conn:
            for group_dn, role in group_checks:
                if not group_dn:
                    continue
                values = _ldap_fetch_group_member_values(conn, group_dn, member_attr)
                for value in values:
                    normalized = _normalize_group_value(value)
                    if not normalized or normalized in resolved:
                        continue
                    identifier, dn = _ldap_group_value_identifier(value)
                    resolved[normalized] = LdapDirectoryUser(
                        identifier=identifier,
                        role=role,
                        group_dn=group_dn,
                        raw_value=value,
                        dn=dn,
                    )
    except LDAPException as exc:
        logger.error("LDAP group listing failed: %s", exc)
        return []

    return sorted(resolved.values(), key=lambda entry: entry.identifier.casefold())


def _ldap_group_member_value(user_dn: str, username: str) -> str:
    template = settings.ldap_group_member_value_template or "{user_dn}"
    try:
        return template.format(user_dn=user_dn, username=username)
    except KeyError:
        logger.error(
            "Invalid LDAP group member template '%s'. Use placeholders {user_dn} or {username}.",
            template,
        )
    return user_dn


def _ldap_group_value_identifier(value: str) -> tuple[str, str | None]:
    """Return a friendly identifier and optional DN from a raw group value."""

    stripped = value.strip()
    if "=" in stripped:
        first = stripped.split(",", 1)[0]
        if "=" in first:
            _, attr_value = first.split("=", 1)
            cleaned = attr_value.strip()
            if cleaned:
                return cleaned, stripped
        return stripped, stripped
    return stripped, None


def _normalize_group_value(value: str) -> str:
    return value.strip().casefold()


def _decode_ldap_value(value: str | bytes | object) -> str | None:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return str(value)


def _ldap_server() -> Server | None:
    if not settings.ldap_server_uri:
        return None
    tls = None
    if settings.ldap_use_ssl:
        validate = ssl.CERT_REQUIRED if settings.ldap_verify_ssl else ssl.CERT_NONE
        tls_kwargs = {"validate": validate}
        if settings.ldap_ca_cert_path:
            tls_kwargs["ca_certs_file"] = str(settings.ldap_ca_cert_path)
        tls = Tls(**tls_kwargs) if LDAP_LIB_AVAILABLE else None
    return Server(
        settings.ldap_server_uri,
        use_ssl=settings.ldap_use_ssl,
        tls=tls,
        connect_timeout=settings.ldap_timeout,
        get_info=ALL,
    )
PASSWORD_ENFORCED_PATHS = ("/account", "/logout")
PASSWORD_FORCE_QUERY = "force=password"
