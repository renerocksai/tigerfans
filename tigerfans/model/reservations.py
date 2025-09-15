# reservations.py
from __future__ import annotations
from typing import Optional, Dict, Any
import time
import redis.asyncio as redis


# ---- keys
def k_ps(psid: str) -> str: return f"ps:{psid}"
def k_fulfill(psid: str) -> str: return f"fulfill:{psid}"
def k_idemp(evt: str) -> str: return f"idemp:{evt}"


PENDING_INDEX = "pendings"  # optional


class ReservationStore:
    def __init__(self, r: redis.Redis, ttl_seconds: int) -> None:
        self.r = r
        self.ttl = ttl_seconds
        self.PENDING_INDEX = PENDING_INDEX

    async def save_payment_session(
            self, psid: str, mapping: Dict[str, Any]) -> None:
        # mapping values should be strings for decode_responses=True
        pipe = self.r.pipeline(transaction=True)
        pipe.hset(k_ps(psid), mapping=mapping)
        pipe.expire(k_ps(psid), self.ttl + 60)
        pipe.zadd(
            PENDING_INDEX,
            {psid: float(mapping.get("created_at", time.time()))}
        )
        await pipe.execute()

    async def get_payment_session(self, psid: str) -> Optional[Dict[str, str]]:
        h = await self.r.hgetall(k_ps(psid))
        return h or None

    async def remove_pending(self, psid: str) -> None:
        # Drop from the live index and delete the session hash.
        pipe = self.r.pipeline(transaction=True)
        pipe.zrem(PENDING_INDEX, psid)
        pipe.delete(k_ps(psid))
        await pipe.execute()

    async def fulfill_gate(self, psid: str) -> bool:
        # NX gate for fulfillment, 24h TTL
        ok = await self.r.set(k_fulfill(psid), "1", nx=True, ex=24*3600)
        return bool(ok)

    async def mark_event_seen(self, evt_id: Optional[str]) -> bool:
        if not evt_id:
            return True
        ok = await self.r.set(k_idemp(evt_id), "1", nx=True, ex=3600)
        return bool(ok)

    async def list_recent_psids(self, limit: int = 200) -> list[str]:
        return await self.r.zrevrange(PENDING_INDEX, 0, max(0, limit - 1))
