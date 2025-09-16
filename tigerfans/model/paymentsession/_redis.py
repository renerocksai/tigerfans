# reservations.py
from __future__ import annotations
from typing import Optional, Dict, Any, Tuple, List
import time
import redis.asyncio as redis


# ---- keys
def k_ps(psid: str) -> str: return f"ps:{psid}"
def k_fulfill(psid: str) -> str: return f"fulfill:{psid}"
def k_idemp(evt: str) -> str: return f"idemp:{evt}"


PENDING_INDEX = "pendings"  # optional


class PaymentSessionStore:
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

    async def fulfill_and_mark_event(
        self, psid: str, evt_id: Optional[str]
    ) -> Dict[str, Optional[bool]]:
        """
        Semantics:
          1) Try fulfill gate. If it already exists -> short-circuit, don't
             touch idempotency.
          2) If fulfill gate was set now, and evt_id is provided, mark
             idempotency.

        Returns:
          {
            "already_fulfilled": True  if fulfill gate already existed
            "event_seen":        True  if idempotency key already existed,
                                 False if it already existed
                                 None  if not checked or not provided
          }
        """
        # fulfill_gate() returns True if we just set the gate
        # (i.e., not fulfilled before)
        fulfill_ok = await self.fulfill_gate(psid)
        if not fulfill_ok:
            # gate already existed -> short-circuit; don't check idempotency
            return {"already_fulfilled": True, "event_seen": None}

        # gate set now; optionally check idempotency
        if evt_id:
            # mark_event_seen() returns True if new (not seen),
            # False if already seen
            idemp_ok = await self.mark_event_seen(evt_id)
            return {"already_fulfilled": False, "event_seen": (not idemp_ok)}
        else:
            return {"already_fulfilled": False, "event_seen": None}

    async def mark_event_seen(self, evt_id: Optional[str]) -> bool:
        if not evt_id:
            return True
        ok = await self.r.set(k_idemp(evt_id), "1", nx=True, ex=3600)
        return bool(ok)

    async def _list_recent_psids(
            self, limit: int = 200
    ) -> Tuple[int, List[str]]:
        total = await self.r.zcard(PENDING_INDEX)
        psids = await self.r.zrevrange(PENDING_INDEX, 0, max(0, limit - 1))
        return total, psids

    async def _get_payment_sessions(self, psids: List[str]):
        # Pipeline to fetch all payment-session hashes
        pipe = self.r.pipeline()
        for psid in psids:
            pipe.hgetall(f"ps:{psid}")
        rows = await pipe.execute()
        return rows

    async def get_recent_payment_sessions(
            self, limit: int = 200
    ) -> Tuple[int, Tuple[int, List[Dict[str, str]]]]:
        total, psids = await self._list_recent_psids(limit=limit)
        rows = await self._get_payment_sessions(psids)

        now = time.time()
        items = []
        for psid, h in zip(psids, rows):
            # house-keeping
            if not h:
                await self.remove_pending(psid)
                continue

            try:
                created = float(h.get("created_at", "0"))
            except ValueError:
                created = 0.0
            items.append({
                "psid": psid,
                "created_at": created,
                "age_ms": int(max(0.0, now - created) * 1000),
                "order_id": h.get("order_id", ""),
                "cls": h.get("cls", ""),
                "qty": int(h.get("qty", "1")),
                "email": h.get("customer_email", h.get("email", "")),
                "amount": int(h.get("amount", "0")),
                "currency": h.get("currency", "eur"),
                "try_goodie": (h.get("try_goodie") == "1"),
                "status": "PENDING",
            })
        return total, items
