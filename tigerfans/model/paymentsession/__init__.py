import os
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncConnection
import redis.asyncio as redis

BACKEND = os.getenv("PAYSESSION_BACKEND", "redis").lower()  # 'redis' | 'pg'

if BACKEND == "pg":
    from ._postgres import PaymentSessionStore as _PaymentSessionStore
else:
    from ._redis import PaymentSessionStore as _PaymentSessionStore


# Factory keeps server.py simple and constructor-agnostic:
def new_store(*, db: Optional[AsyncConnection] = None,
              r: Optional[redis.Redis] = None,
              ttl_seconds: int = 300):
    if BACKEND == "pg":
        if db is None:
            raise RuntimeError(
                "PaymentSessionStore(pg) requires db=AsyncConnection"
            )
        return _PaymentSessionStore(db=db, ttl_seconds=ttl_seconds)
    else:
        if r is None:
            raise RuntimeError(
                "ReservationStore(redis) requires r=redis.Redis"
            )
        return _PaymentSessionStore(r=r, ttl_seconds=ttl_seconds)


# Optional: also export the selected class name for typing/imports
PaymentSessionStore = _PaymentSessionStore
__all__ = ["PaymentSessionStore", "new_store", "BACKEND"]
