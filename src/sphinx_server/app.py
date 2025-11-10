"""ASGI application factory and FastAPI wiring for Sphinx Server."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from starlette.middleware.sessions import SessionMiddleware

from .auth import ensure_default_admin
from .auto_builder import AutoBuildMonitor
from .build_service import BuildQueue
from .config import settings
from .database import init_db
from .web import account, admin, docs

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    :returns: Fully configured FastAPI instance with routers/static mounts.
    """
    logger.debug("Initializing database")
    init_db()
    ensure_default_admin()
    app = FastAPI(title="Sphinx Server")
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="sphinx_server_session",
        same_site="lax",
    )

    queue = BuildQueue()
    monitor = AutoBuildMonitor(queue)
    app.state.build_queue = queue
    app.state.auto_monitor = monitor

    @app.on_event("startup")
    async def startup_event() -> None:
        """Start background services (build queue + auto-build monitor)."""
        logger.info("Starting background services")
        await queue.startup()
        await monitor.startup()

    @app.on_event("shutdown")
    async def shutdown_event() -> None:
        """Gracefully stop background services during shutdown."""
        logger.info("Shutting down background services")
        await queue.shutdown()
        await monitor.shutdown()

    app.include_router(admin.router)
    app.include_router(docs.router)
    app.include_router(account.router)

    static_dir = Path(__file__).resolve().parent / "web" / "static"
    app.mount("/assets", StaticFiles(directory=str(static_dir)), name="assets")
    app.mount(
        "/artifacts",
        StaticFiles(directory=str(settings.build_output_dir), html=True),
        name="artifacts",
    )

    return app


def get_app() -> FastAPI:
    """FastAPI factory hook used by uvicorn's ``--factory`` option."""
    return create_app()
