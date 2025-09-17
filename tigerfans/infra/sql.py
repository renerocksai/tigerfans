import os
import asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    create_async_engine, async_sessionmaker, AsyncSession
)
from contextlib import asynccontextmanager


def _normalize_async_url(url: str) -> str:
    if url.startswith("sqlite://"):
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


# DB-GATE!!!!!!!!!!!
@asynccontextmanager
async def _gated(sem: asyncio.Semaphore):
    await sem.acquire()
    try:
        yield
    finally:
        sem.release()


def make_async_engine(database_url: str):
    db_url = _normalize_async_url(database_url)
    kw = dict(future=True, pool_pre_ping=True)

    pool_size = None
    if db_url.startswith("postgresql+asyncpg://"):
        pool_size = int(os.getenv("DB_POOL_SIZE", "10"))
        kw.update(
            pool_size=pool_size,
            max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "10")),
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

    # DB-GATE!!!!!!!!!!!
    # Create a per-engine gate. Default to pool_size
    if pool_size is None:
        # sqlite
        gate_limit = int(os.getenv("DB_GATE_LIMIT", "10"))
    else:
        # postgres
        gate_limit = int(
            os.getenv("DB_GATE_LIMIT", pool_size)
        )

    db_gate = asyncio.Semaphore(max(1, gate_limit))

    # expose a tiny helper for `async with gated(db_gate): ...`
    def gated():
        return _gated(db_gate)

    # return the gate too so callers can pass it to stores
    return engine, SessionAsync, db_gate, gated
