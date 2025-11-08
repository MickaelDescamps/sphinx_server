from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from .config import settings

engine = create_engine(settings.db_url, connect_args={"check_same_thread": False})


def init_db() -> None:
    _ensure_sqlite_columns()
    SQLModel.metadata.create_all(engine)


@contextmanager
def session_scope():
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()


def get_session():
    with Session(engine) as session:
        yield session


def _ensure_sqlite_columns() -> None:
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
    with engine.connect() as conn:
        repo_existing = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(repository)")
        }
        for col, ddl in repo_columns.items():
            if col not in repo_existing:
                conn.exec_driver_sql(f"ALTER TABLE repository ADD COLUMN {col} {ddl}")

        build_existing = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(build)")
        }
        for col, ddl in build_columns.items():
            if col not in build_existing:
                conn.exec_driver_sql(f"ALTER TABLE build ADD COLUMN {col} {ddl}")
