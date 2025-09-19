from __future__ import annotations
from typing import Optional, Dict, Any, List, Tuple
from sqlalchemy import text, bindparam
from sqlalchemy.ext.asyncio import AsyncSession, AsyncConnection
import time
from typing import Callable, AsyncContextManager


# ------------------------------------------------------------------------------
# DDL (idempotent) + fixtures
# ------------------------------------------------------------------------------
SQL_CREATE_PAYMENT_SESSIONS_HOT = r"""
-- payment sessions (the “hot” reservation metadata we currently put in Redis)
CREATE TABLE IF NOT EXISTS payment_sessions_hot (
  psid         TEXT PRIMARY KEY,
  order_id     TEXT NOT NULL,
  cls          TEXT NOT NULL,
  qty          INTEGER NOT NULL,
  amount       INTEGER NOT NULL,
  currency     TEXT NOT NULL,
  customer_email TEXT NOT NULL,
  try_goodie   BOOLEAN NOT NULL,
  tb_transfer_id TEXT,              -- present when ACCT=TB
  goodie_tb_transfer_id TEXT,       -- present when ACCT=TB
  pg_reservation_id   TEXT,         -- present when ACCT=PG
  created_at   DOUBLE PRECISION NOT NULL,
  expires_at   DOUBLE PRECISION NOT NULL
);
"""

SQL_CREATE_PAYMENT_SESSIONS_PENDING = r"""
-- live “pending” index for admin view (optional but matches Redis UX)
CREATE TABLE IF NOT EXISTS payment_sessions_pending (
  psid     TEXT PRIMARY KEY,
  created_at DOUBLE PRECISION NOT NULL
);
"""

SQL_CREATE_IDEMPOTENCY_KEYS = r"""
-- idempotency keys
CREATE TABLE IF NOT EXISTS idempotency_keys (
  key TEXT PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

SQL_CREATE_FULFILLMENT_GATES = r"""
-- fulfillment gate: one row per psid that has been fulfilled
CREATE TABLE IF NOT EXISTS fulfillment_gates (
  psid TEXT PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

SQL_CREATE_IDX_PS_HOT_CREATED_AT = r"""
CREATE INDEX IF NOT EXISTS idx_ps_hot_created_at
  ON payment_sessions_hot (created_at DESC);
"""


async def create_schema(db_or_conn: AsyncSession | AsyncConnection):
    # get an execute handle that works for both session and connection
    exec_ = db_or_conn.execute
    await exec_(text(SQL_CREATE_PAYMENT_SESSIONS_HOT))
    await exec_(text(SQL_CREATE_PAYMENT_SESSIONS_PENDING))
    await exec_(text(SQL_CREATE_IDEMPOTENCY_KEYS))
    await exec_(text(SQL_CREATE_FULFILLMENT_GATES))
    await exec_(text(SQL_CREATE_IDX_PS_HOT_CREATED_AT))


class PaymentSessionStore:
    def __init__(
        self, *, db: AsyncConnection, ttl_seconds: int,
        gated: Callable[[], AsyncContextManager[None]]
    ) -> None:
        self.db = db
        self.ttl = ttl_seconds
        self.gated = gated

    async def save_payment_session(
            self, psid: str, mapping: Dict[str, Any]
    ) -> None:
        m = mapping.copy()
        m.setdefault("created_at", 0.0)
        m["expires_at"] = float(m["created_at"]) + self.ttl + 60
        async with self.gated():
            async with self.db.begin():
                await self.db.execute(text("""
                  INSERT INTO payment_sessions_hot(
                    psid, order_id, cls, qty, amount, currency, customer_email,
                    try_goodie, tb_transfer_id, goodie_tb_transfer_id,
                    pg_reservation_id, created_at, expires_at
                  ) VALUES (
                    :psid, :order_id, :cls, :qty, :amount, :currency,
                    :customer_email, :try_goodie, :tb_transfer_id,
                    :goodie_tb_transfer_id, :pg_reservation_id, :created_at,
                    :expires_at
                  )
                  ON CONFLICT (psid) DO UPDATE SET
                    order_id=EXCLUDED.order_id, cls=EXCLUDED.cls,
                    qty=EXCLUDED.qty, amount=EXCLUDED.amount,
                    currency=EXCLUDED.currency,
                    customer_email=EXCLUDED.customer_email,
                    try_goodie=EXCLUDED.try_goodie,
                    tb_transfer_id=EXCLUDED.tb_transfer_id,
                    goodie_tb_transfer_id=EXCLUDED.goodie_tb_transfer_id,
                    pg_reservation_id=EXCLUDED.pg_reservation_id,
                    created_at=EXCLUDED.created_at,
                    expires_at=EXCLUDED.expires_at
                """), {
                    "psid": psid,
                    "order_id": m["order_id"],
                    "cls": m["cls"],
                    "qty": int(m["qty"]),
                    "amount": int(m["amount"]),
                    "currency": m["currency"],
                    "customer_email": (
                        m.get("customer_email") or m.get("email") or ""
                    ),
                    "try_goodie": (
                        m.get("try_goodie") in ("1", 1, True, "true")
                    ),
                    "tb_transfer_id": m.get("tb_transfer_id"),
                    "goodie_tb_transfer_id": m.get("goodie_tb_transfer_id"),
                    "pg_reservation_id": m.get("pg_reservation_id"),
                    "created_at": float(m["created_at"]),
                    "expires_at": float(m["created_at"]) + self.ttl + 60,
                })
                await self.db.execute(text("""
                  INSERT INTO payment_sessions_pending(psid, created_at)
                  VALUES(:psid, :created_at)
                  ON CONFLICT (psid) DO UPDATE
                  SET created_at=EXCLUDED.created_at
                """), {"psid": psid, "created_at": float(m["created_at"])})

    async def get_payment_session(self, psid: str) -> Optional[Dict[str, Any]]:
        async with self.gated():
            async with self.db.begin():
                row = (await self.db.execute(text("""
                  SELECT * FROM payment_sessions_hot WHERE psid=:psid
                """), {"psid": psid})).mappings().first()
                return dict(row) if row else None

    async def remove_pending(self, psid: str) -> None:
        async with self.gated():
            async with self.db.begin():
                await self.db.execute(
                    text(
                        "DELETE FROM payment_sessions_pending WHERE psid=:psid"
                    ),
                    {"psid": psid}
                )

    async def fulfill_gate(self, psid: str) -> bool:
        async with self.gated():
            async with self.db.begin():
                row = (await self.db.execute(text("""
                  INSERT INTO fulfillment_gates(psid) VALUES(:psid)
                  ON CONFLICT (psid) DO NOTHING
                  RETURNING psid
                """), {"psid": psid})).first()
                return row is not None

    async def mark_event_seen(self, evt_id: Optional[str]) -> bool:
        if not evt_id:
            return True
        async with self.gated():
            async with self.db.begin():
                row = (await self.db.execute(text("""
                  INSERT INTO idempotency_keys(key) VALUES(:k)
                  ON CONFLICT (key) DO NOTHING
                  RETURNING key
                """), {"k": evt_id})).first()
        return row is not None

    async def list_recent_psids(self, limit: int = 200) -> List[str]:
        async with self.gated():
            async with self.db.begin():
                rows = (await self.db.execute(text("""
                  SELECT psid FROM payment_sessions_pending
                  ORDER BY created_at DESC LIMIT :lim
                """), {"lim": limit})).all()
        return [r[0] for r in rows]

    async def get_recent_payment_sessions(
        self, limit: int = 200
    ) -> Tuple[int, List[Dict[str, Any]]]:
        async with self.gated():
            async with self.db.begin():
                # total pending
                total = (await self.db.execute(
                    text("SELECT COUNT(*) FROM payment_sessions_pending")
                )).scalar_one()

                # fetch latest pending PSIDs and their hot metadata
                # (if present)
                rows = (await self.db.execute(text("""
                    SELECT
                        p.psid,
                        h.created_at,
                        h.order_id,
                        h.cls,
                        h.qty,
                        h.amount,
                        h.currency,
                        h.customer_email,
                        h.try_goodie
                    FROM payment_sessions_pending AS p
                    LEFT JOIN payment_sessions_hot AS h ON h.psid = p.psid
                    ORDER BY p.created_at DESC
                    LIMIT :lim
                """), {"lim": int(limit)})).mappings().all()

                now = time.time()
                items: List[Dict[str, Any]] = []
                missing: List[str] = []

                for r in rows:
                    psid = r["psid"]
                    created = r["created_at"]

                    # Housekeeping: pending entry without a hot row
                    # -> remove it
                    if created is None:
                        missing.append(psid)
                        continue

                    created = float(created)
                    items.append({
                        "psid": psid,
                        "created_at": created,
                        "age_ms": int(max(0.0, now - created) * 1000),
                        "order_id": r.get("order_id", "") or "",
                        "cls": r.get("cls", "") or "",
                        "qty": int(r.get("qty") or 1),
                        "email": r.get("customer_email", "") or "",
                        "amount": int(r.get("amount") or 0),
                        "currency": r.get("currency", "eur") or "eur",
                        "try_goodie": bool(r.get("try_goodie")),
                        "status": "PENDING",
                    })

                # Delete any “dangling” pendings in a single transaction
                if missing:
                    stmt = text(
                        "DELETE FROM payment_sessions_pending "
                        "WHERE psid IN :psids"
                        ).bindparams(bindparam("psids", expanding=True))
                    await self.db.execute(stmt, {"psids": tuple(missing)})

        return int(total), items

    # OPTIMIZED version: do both idempotency checks at once
    async def fulfill_and_mark_event(
            self, psid: str, idem: str | None
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
        out = {"already_fulfilled": False, "event_seen": False}
        async with self.gated():
            async with self.db.begin():  # use the existing connection/session
                gate = (await self.db.execute(
                    text("""
                      INSERT INTO fulfillment_gates(psid) VALUES(:psid)
                      ON CONFLICT (psid) DO NOTHING
                      RETURNING psid
                    """),
                    {"psid": psid},
                )).first()

                if gate is None:
                    out["already_fulfilled"] = True
                    out["event_seen"] = None  # not checked
                    return out

                out["already_fulfilled"] = False

                if idem:
                    idem_row = (await self.db.execute(
                        text("""
                          INSERT INTO idempotency_keys(key) VALUES(:k)
                          ON CONFLICT (key) DO NOTHING
                          RETURNING key
                        """),
                        {"k": idem},
                    )).first()
                    out["event_seen"] = idem_row is None
                else:
                    out["event_seen"] = None  # not provided
        return out
