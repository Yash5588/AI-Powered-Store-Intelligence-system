"""Database engine, session management, and connectivity helpers.

We use SQLAlchemy 2.0 over SQLite. SQLite is sufficient for a single-node
analytics service handling event batches, and it keeps `docker compose up`
zero-dependency (no external DB container required). The data access layer is
written against the SQLAlchemy ORM so swapping to PostgreSQL later is a
connection-string change, not a rewrite (documented in CHOICES.md).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# DATABASE_URL lets docker-compose / tests point at a different store without
# code changes. Default is a file-backed SQLite DB in the project root.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./store_intelligence.db")

# `check_same_thread=False` is required because FastAPI serves requests across
# multiple threads while sharing one SQLite connection pool.
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(
    DATABASE_URL,
    connect_args=_connect_args,
    # Keeps a session's objects usable after commit (we return ids/timestamps).
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def init_db() -> None:
    """Create all tables. Idempotent — safe to call on every startup."""
    # Import here to avoid circular imports; models register on Base.metadata.
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)


def get_db() -> Iterator[Session]:
    """FastAPI dependency that yields a scoped session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context-managed session for non-request code (simulator, scripts)."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def check_connection() -> bool:
    """Lightweight DB ping used by /health for graceful-degradation checks."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
