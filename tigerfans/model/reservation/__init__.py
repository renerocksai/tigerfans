# model/reservation/__init__.py
import os
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as redis

BACKEND = os.getenv("RESV_BACKEND", "redis").lower()  # 'redis' | 'pg'

if BACKEND == "pg":
    from ._postgres import ReservationStore as _ReservationStore
else:
    from ._redis import ReservationStore as _ReservationStore


# Factory keeps server.py simple and constructor-agnostic:
def new_store(*, db: Optional[AsyncSession] = None,
              r: Optional[redis.Redis] = None,
              ttl_seconds: int = 300):
    if BACKEND == "pg":
        if db is None:
            raise RuntimeError("ReservationStore(pg) requires db=AsyncSession")
        return _ReservationStore(db=db, ttl_seconds=ttl_seconds)
    else:
        if r is None:
            raise RuntimeError(
                "ReservationStore(redis) requires r=redis.Redis"
            )
        return _ReservationStore(r=r, ttl_seconds=ttl_seconds)


# Optional: also export the selected class name for typing/imports
ReservationStore = _ReservationStore
__all__ = ["ReservationStore", "new_store", "BACKEND"]
