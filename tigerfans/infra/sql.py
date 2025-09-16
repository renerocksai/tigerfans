import os
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    create_async_engine, async_sessionmaker, AsyncSession
)


def _normalize_async_url(url: str) -> str:
    if url.startswith("sqlite://"):
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


def make_async_engine(database_url: str):
    db_url = _normalize_async_url(database_url)
    kw = dict(future=True, pool_pre_ping=True)

    if db_url.startswith("postgresql+asyncpg://"):
        kw.update(
            pool_size=int(os.getenv("DB_POOL_SIZE", "5")),
            max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "5")),
            pool_timeout=int(os.getenv("DB_POOL_TIMEOUT", "30")),
        )

    engine = create_async_engine(db_url, **kw)

    if db_url.startswith("sqlite+aiosqlite://"):
        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _):
            cur = dbapi_connection.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA busy_timeout=5000;")
            cur.execute("PRAGMA synchronous=NORMAL;")
            cur.close()

    SessionAsync = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    return engine, SessionAsync
