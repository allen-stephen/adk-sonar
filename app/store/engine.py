"""Database engine and session lifecycle management for ADK Sonar."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.store.models import Base

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_default_state_dir() -> Path:
    """Returns the persistent state directory for local dev SQLite and artifacts."""
    custom = os.getenv("ORCHESTRATOR_STATE_DIR")
    if custom:
        p = Path(custom).expanduser().resolve()
    else:
        p = Path.home() / ".adk-sonar"
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_database_url() -> str:
    """Resolves database URL from environment with sensible defaults."""
    for key in ("TASK_DB_URL", "DATABASE_URL", "SESSION_DB_URL"):
        val = os.getenv(key)
        if val and val.strip():
            url = val.strip()
            # If standard postgresql:// provided, ensure asyncpg driver
            if url.startswith("postgresql://"):
                url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
            elif url.startswith("sqlite://") and not url.startswith("sqlite+aiosqlite://"):
                url = url.replace("sqlite://", "sqlite+aiosqlite://", 1)
            return url

    # Default to persistent SQLite in configured state dir
    db_path = get_default_state_dir() / "sonar_tasks.db"
    return f"sqlite+aiosqlite:///{db_path}"


def get_async_engine() -> AsyncEngine:
    """Returns the process-wide singleton AsyncEngine."""
    global _engine
    if _engine is None:
        url = resolve_database_url()
        is_postgres = "postgresql" in url

        engine_kwargs: dict[str, Any] = {
            "echo": os.getenv("SQL_DEBUG", "").lower() in ("1", "true", "yes"),
        }

        if is_postgres:
            # Conservative connection pool budget for Cloud SQL / Cloud Run
            engine_kwargs.update(
                {
                    "pool_size": int(os.getenv("DB_POOL_SIZE", "2")),
                    "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "3")),
                    "pool_pre_ping": True,
                    "pool_recycle": 300,
                }
            )

        _engine = create_async_engine(url, **engine_kwargs)
        logger.info("Initialized database engine with URL schema: %s", url.split("://")[0])

    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Returns the async_sessionmaker factory."""
    global _sessionmaker
    if _sessionmaker is None:
        engine = get_async_engine()
        _sessionmaker = async_sessionmaker(
            bind=engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _sessionmaker


async def init_db() -> None:
    """Creates database schema if not already present."""
    engine = get_async_engine()
    url = resolve_database_url()

    # If SQLite, ensure WAL mode for concurrent read/write
    if "sqlite" in url:
        from sqlalchemy import text

        async with engine.connect() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL;"))
            await conn.execute(text("PRAGMA synchronous=NORMAL;"))

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database schema initialized.")


async def close_db() -> None:
    """Disposes database engine connections gracefully."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None
