# model/accounting/postgres.py
"""
PostgreSQL accounting backend that mirrors the TigerBeetle semantics used in
the app:
- time-limited "holds" on capacity (tickets, goodies)
- commit (post) of holds on payment success
- void/cancel of holds on failure/timeout
- inventory computation (sold, held, available)
- goodies counter
"""

from __future__ import annotations
import time
from typing import Tuple, Dict, Any, Optional
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, AsyncConnection
from sqlalchemy import text

from typing import Callable, AsyncContextManager

Gated = Callable[[], AsyncContextManager[None]]


@dataclass
class GatedAsyncSession:
    session: AsyncSession
    gated: Gated


# ------------------------------------------------------------------------------
# CONFIG: keep in sync with TB demo numbers if you want apples-to-apples
# ------------------------------------------------------------------------------
TicketAmount_Class_A = 5_000_000
TicketAmount_Class_B = 5_000_000
TicketAmount_first_n = 100_000

# Resource names (rows in resources table)
RES_CLASS_A = "class_a"
RES_CLASS_B = "class_b"
RES_GOODIE = "goodie"

# Hold statuses
H_PENDING = "pending"
H_POSTED = "posted"
H_VOIDED = "voided"


# ------------------------------------------------------------------------------
# DDL (idempotent) + fixtures
# ------------------------------------------------------------------------------

SQL_CREATE_RESOURCES = r"""
-- Resources catalog: each row is a resource with a total capacity.
CREATE TABLE IF NOT EXISTS resources (
    name        TEXT PRIMARY KEY,
    capacity    BIGINT NOT NULL CHECK (capacity >= 0)
);
"""

SQL_CREATE_HOLDS = r"""
-- Holds: pending/posted/voided reservations against a resource.
--   qty            : number of units reserved/posted
--   status         : 'pending', 'posted', or 'voided'
--   expires_at     : epoch seconds when the hold times out (NULL = no timeout)
--   created_at     : epoch seconds
-- Notes:
--   We keep holds immutable apart from the status (pending -> posted/voided).
CREATE TABLE IF NOT EXISTS holds (
    id          BIGSERIAL PRIMARY KEY,
    resource    TEXT NOT NULL REFERENCES resources(name) ON DELETE RESTRICT,
    qty         INTEGER NOT NULL CHECK (qty > 0),
    status      TEXT NOT NULL CHECK (status IN ('pending','posted','voided')),
    expires_at  DOUBLE PRECISION,
    created_at  DOUBLE PRECISION NOT NULL
);
"""

SQL_CREATE_RESOURCE_STATUS_IDX = r"""
CREATE INDEX IF NOT EXISTS holds_resource_status_idx
    ON holds(resource, status);
"""

SQL_CREATE_HOLDS_PENDING_NOTEXPIRED_IDX = r"""
CREATE INDEX IF NOT EXISTS holds_pending_notexpired_idx
    ON holds(resource, status, expires_at)
    WHERE status = 'pending';

-- Fast aggregation helpers (optional but maybe useful):
-- We could create partial indexes to speed up posted sums, etc.
"""


async def ensure_schema_and_fixtures(
    db_or_conn: AsyncSession | AsyncConnection
) -> None:
    """
    Create tables if missing and seed capacity rows if they don't exist.
    """
    # get an execute handle that works for both session and connection
    exec_ = db_or_conn.execute

    await exec_(text(SQL_CREATE_RESOURCES))
    await exec_(text(SQL_CREATE_HOLDS))
    await exec_(text(SQL_CREATE_RESOURCE_STATUS_IDX))
    await exec_(text(SQL_CREATE_HOLDS_PENDING_NOTEXPIRED_IDX))

    # Seed resources if not present
    await exec_(text("""
        INSERT INTO resources (name, capacity) VALUES
            (:ra, :ca),
            (:rb, :cb),
            (:rg, :cg)
        ON CONFLICT (name) DO NOTHING
    """), {
        "ra": RES_CLASS_A, "ca": TicketAmount_Class_A,
        "rb": RES_CLASS_B, "cb": TicketAmount_Class_B,
        "rg": RES_GOODIE,  "cg": TicketAmount_first_n,
    })


# ------------------------------------------------------------------------------
# Compatibility helpers (to mirror TB-like API)
# ------------------------------------------------------------------------------

async def create_accounts(db: AsyncConnection) -> bool:
    """
    For parity with TB: ensures schema + fixtures. Returns True on success.
    """
    await ensure_schema_and_fixtures(db)
    print('✅ accounts existing / created')
    print('✅ initial transfers present / executed')
    return True


async def initial_transfers(db: AsyncConnection) -> None:
    """
    TB seeds balances by transfers; here capacity is already seeded in
    resources.
    No-op maintained for API parity.
    """
    return None


# ------------------------------------------------------------------------------
# Core logic
# ------------------------------------------------------------------------------

# UN-GATED internal function
async def _available_units(
    db: AsyncSession, resource: str, now_ts: float
) -> Dict[str, int]:
    """
    Compute (sold, held, available) for a given resource.
    sold = sum(qty WHERE status='posted')
    held = sum(qty WHERE status='pending'
                     AND (expires_at IS NULL OR expires_at > now))
    available = capacity - sold - held
    """
    res = (
        await db.execute(
            text("SELECT capacity FROM resources WHERE name=:r"),
            {"r": resource},
        )
    ).first()

    if not res:
        raise RuntimeError(f"Unknown resource: {resource}")
    capacity = int(res[0])

    sold = (await db.execute(text("""
        SELECT COALESCE(SUM(qty),0) FROM holds
        WHERE resource=:r AND status='posted'
    """), {"r": resource})).scalar_one()

    held = (await db.execute(text("""
        SELECT COALESCE(SUM(qty),0) FROM holds
        WHERE resource=:r AND status='pending'
          AND (expires_at IS NULL OR expires_at > :now)
    """), {"r": resource, "now": now_ts})).scalar_one()

    return {
        "sold": int(sold),
        "held": int(held),
        "available": capacity - int(sold) - int(held),
        "capacity": capacity,
    }


# UN-GATED internal function
async def _insert_hold_if_capacity(
    db: AsyncSession, resource: str, qty: int, timeout_seconds: Optional[int]
) -> Optional[int]:
    """
    Atomically (within a DB txn) insert a PENDING hold for `resource` if
    capacity remains.
    Returns hold_id or None if not enough capacity at this moment.
    """
    now_ts = time.time()
    avail = await _available_units(db, resource, now_ts)
    if avail["available"] < qty:
        return None

    expires_at = (
        now_ts + timeout_seconds
        if timeout_seconds and timeout_seconds > 0
        else None
    )
    # Insert the pending hold
    row = (await db.execute(text("""
        INSERT INTO holds(resource, qty, status, expires_at, created_at)
        VALUES(:r, :q, 'pending', :e, :c)
        RETURNING id
    """), {"r": resource, "q": qty, "e": expires_at, "c": now_ts})).first()
    return int(row[0])


# Public API (parallels TB impl)

async def hold_tickets(
    db: GatedAsyncSession,
    ticket_class: str,
    qty: int,
    timeout_seconds: int,
) -> Tuple[str, str, bool, bool]:
    """
    Create PENDING holds for (ticket_class) and (goodie: qty=1).
    Return (ticket_hold_id, goodie_hold_id, has_ticket, has_goodie).
    """
    if ticket_class not in ("A", "B"):
        raise ValueError("ticket_class must be 'A' or 'B'")

    res_name = RES_CLASS_A if ticket_class == "A" else RES_CLASS_B

    async with db.gated():
        async with db.session.begin():
            ticket_hold_id = await _insert_hold_if_capacity(
                db.session, res_name, qty, timeout_seconds
            )
            goodie_hold_id = await _insert_hold_if_capacity(
                db.session, RES_GOODIE, 1, timeout_seconds
            )

    has_ticket = ticket_hold_id is not None
    has_goodie = goodie_hold_id is not None
    # If no ticket capacity, we still return ids (or None) – the caller
    # handles canceling goodie if needed.
    return (
        str(ticket_hold_id) if ticket_hold_id is not None else "0",
        str(goodie_hold_id) if goodie_hold_id is not None else "0",
        has_ticket,
        has_goodie,
    )


async def book_immediately(
    db: GatedAsyncSession,
    ticket_class: str,
    qty: int,
) -> Tuple[str, str, bool, bool]:
    """
    Direct "posted" booking without a pending step (parallels TB fast-path).
    Returns (ticket_hold_id, goodie_hold_id, gets_ticket, gets_goodie).
    """
    if ticket_class not in ("A", "B"):
        raise ValueError("ticket_class must be 'A' or 'B'")

    res_name = RES_CLASS_A if ticket_class == "A" else RES_CLASS_B

    async with db.gated():
        async with db.session.begin():
            # Try to insert ticket as posted if capacity remains (simulate
            # "immediate" confirmation).
            # We'll use the same capacity check function and then insert with
            # status='posted'
            now_ts = time.time()
            avail_ticket = await _available_units(db.session, res_name, now_ts)
            ticket_id = None
            if avail_ticket["available"] >= qty:
                row = (await db.session.execute(text("""
                    INSERT INTO holds(
                        resource, qty, status, expires_at, created_at)
                    VALUES(:r, :q, 'posted', NULL, :c)
                    RETURNING id
                """), {"r": res_name, "q": qty, "c": now_ts})).first()
                ticket_id = int(row[0])

            # Goodie: qty=1
            goodie_id = None
            avail_goodie = await _available_units(db.session, RES_GOODIE,
                                                  now_ts)
            if avail_goodie["available"] >= 1:
                row = (await db.session.execute(text("""
                    INSERT INTO holds(resource, qty, status, expires_at,
                        created_at)
                    VALUES(:r, 1, 'posted', NULL, :c)
                    RETURNING id
                """), {"r": RES_GOODIE, "c": now_ts})).first()
                goodie_id = int(row[0])

    return (
        str(ticket_id) if ticket_id is not None else "0",
        str(goodie_id) if goodie_id is not None else "0",
        ticket_id is not None,
        goodie_id is not None,
    )


async def commit_order(
    db: GatedAsyncSession,
    ticket_hold_id: str | int,
    goodie_hold_id: str | int,
    ticket_class: str,
    qty: int,
    try_goodie: bool,
) -> Tuple[bool, bool]:
    """
    POST (commit) pending holds to posted.
    If a hold is already posted, or expired/voided, we treat accordingly.
    Returns (gets_ticket, gets_goodie).
    """
    # normalize ids
    def _as_int(x): return int(x) if isinstance(x, (str, bytes)) else int(x)
    th = _as_int(ticket_hold_id) if ticket_hold_id not in (None, "0") else None
    gh = (
      _as_int(goodie_hold_id)
      if try_goodie and goodie_hold_id not in (None, "0") else None
    )

    now_ts = time.time()
    gets_ticket = False
    gets_goodie = False

    async with db.gated():
        async with db.session.begin():
            if th is not None:
                # Only post if still pending and not expired
                row = (await db.session.execute(text("""
                    UPDATE holds
                    SET status='posted', expires_at=NULL
                    WHERE id=:id
                      AND status='pending'
                      AND (expires_at IS NULL OR expires_at > :now)
                    RETURNING id
                """), {"id": th, "now": now_ts})).first()
                gets_ticket = row is not None

            if gh is not None:
                row = (await db.session.execute(text("""
                    UPDATE holds
                    SET status='posted', expires_at=NULL
                    WHERE id=:id
                      AND status='pending'
                      AND (expires_at IS NULL OR expires_at > :now)
                    RETURNING id
                """), {"id": gh, "now": now_ts})).first()
                gets_goodie = row is not None

    return gets_ticket, gets_goodie


async def cancel_order(
    db: GatedAsyncSession,
    ticket_hold_id: str | int,
    goodie_hold_id: str | int,
    ticket_class: str,
    qty: int,
) -> None:
    """
    VOID pending holds (best-effort). If already posted/voided/expired, no
    change.
    """
    def _as_int(x): return int(x) if isinstance(x, (str, bytes)) else int(x)
    now_ts = time.time()
    ids = []
    if ticket_hold_id not in (None, "0"):
        ids.append(_as_int(ticket_hold_id))
    if goodie_hold_id not in (None, "0"):
        ids.append(_as_int(goodie_hold_id))
    if not ids:
        return

    async with db.gated():
        async with db.session.begin():
            await db.session.execute(text("""
                UPDATE holds
                SET status='voided'
                WHERE id = ANY(:ids)
                  AND status='pending'
                  AND (expires_at IS NULL OR expires_at > :now)
            """), {"ids": ids, "now": now_ts})


async def cancel_only_goodie(
    db: GatedAsyncSession,
    goodie_hold_id: str | int,
    ticket_class: str,
) -> None:
    """
    VOID pending holds (best-effort). If already posted/voided/expired, no
    change.
    """
    def _as_int(x): return int(x) if isinstance(x, (str, bytes)) else int(x)
    now_ts = time.time()
    ids = []
    if goodie_hold_id not in (None, "0"):
        ids.append(_as_int(goodie_hold_id))
    if not ids:
        return

    async with db.gated():
        async with db.session.begin():
            await db.session.execute(text("""
                UPDATE holds
                SET status='voided'
                WHERE id = ANY(:ids)
                  AND status='pending'
                  AND (expires_at IS NULL OR expires_at > :now)
            """), {"ids": ids, "now": now_ts})


# ------------------------------------------------------------------------------
# Read APIs
# ------------------------------------------------------------------------------

async def compute_inventory(db: GatedAsyncSession) -> Dict[str, Any]:
    """
    Returns:
      {
        "A": { "capacity": ..., "sold": ..., "active_holds": ...,
               "available": ..., "sold_out": ..., "timestamp": ... },
        "B": { ... }
      }
    """
    now_ts = time.time()
    out: Dict[str, Any] = {}

    async with db.gated():
        # make explicit transaction for the 2 lookups
        async with db.session.begin():
            for label, res_name, cap in (
                ("A", RES_CLASS_A, TicketAmount_Class_A),
                ("B", RES_CLASS_B, TicketAmount_Class_B),
            ):
                stats = await _available_units(db.session, res_name, now_ts)
                out[label] = {
                    "capacity": cap,
                    "sold": stats["sold"],
                    "active_holds": stats["held"],
                    "available": stats["available"],
                    "sold_out": stats["available"] <= 0,
                    "timestamp": _to_iso(now_ts),
                }
    return out


async def count_goodies(db: GatedAsyncSession) -> int:
    """
    Number of goodies posted so far.
    """
    async with db.gated():
        # make explicit transaction
        async with db.session.begin():
            posted = (await db.session.execute(text("""
                SELECT COALESCE(SUM(qty),0) FROM holds
                WHERE resource=:r AND status='posted'
            """), {"r": RES_GOODIE})).scalar_one()
    return int(posted)


# ------------------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------------------

def _to_iso(ts: float) -> str:
    # Minimal local ISO helper to avoid shared helpers here.
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
