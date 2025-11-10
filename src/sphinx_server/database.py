"""Database engine creation and helper utilities."""

from __future__ import annotations

import logging
from contextlib import contextmanager

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from .config import settings

logger = logging.getLogger(__name__)

engine = create_engine(settings.db_url, connect_args={"check_same_thread": False})


def init_db() -> None:
    """Create database tables and ensure SQLite-compatible schema evolution."""
    logger.debug("Creating database schema")
    SQLModel.metadata.create_all(engine)
    _ensure_sqlite_columns()


@contextmanager
def session_scope():
    """Context manager yielding a short-lived session."""
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()


def get_session():
    """FastAPI dependency hook yielding a new session."""
    with Session(engine) as session:
        yield session


def _ensure_sqlite_columns() -> None:
    """Add missing optional columns when using SQLite."""
    if not settings.db_url.startswith("sqlite"):
        return
    repo_columns = {
        "project_name": "TEXT",
        "project_version": "TEXT",
        "project_summary": "TEXT",
        "project_homepage": "TEXT",
        "primary_target_id": "INTEGER",
        "deploy_key": "TEXT",
    }
    build_columns = {
        "duration_seconds": "REAL",
        "triggered_by": "TEXT DEFAULT 'manual'",
    }
    tracked_target_columns = {
        "environment_manager": "TEXT",
    }
    user_columns = {
        "must_change_password": "BOOLEAN DEFAULT 0",
    }
    with engine.connect() as conn:
        repo_existing = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(repository)")
        }
        for col, ddl in repo_columns.items():
            if col not in repo_existing:
                logger.debug("Adding repo column %s", col)
                conn.exec_driver_sql(f"ALTER TABLE repository ADD COLUMN {col} {ddl}")

        build_existing = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(build)")
        }
        for col, ddl in build_columns.items():
            if col not in build_existing:
                logger.debug("Adding build column %s", col)
                conn.exec_driver_sql(f"ALTER TABLE build ADD COLUMN {col} {ddl}")

        target_existing = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(trackedtarget)")
        }
        for col, ddl in tracked_target_columns.items():
            if col not in target_existing:
                logger.debug("Adding trackedtarget column %s", col)
                conn.exec_driver_sql(f"ALTER TABLE trackedtarget ADD COLUMN {col} {ddl}")

        user_existing = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(user)")
        }
        for col, ddl in user_columns.items():
            if col not in user_existing:
                logger.debug("Adding user column %s", col)
                conn.exec_driver_sql(f"ALTER TABLE user ADD COLUMN {col} {ddl}")
