from typing import Optional, Dict, Any, List
import redis.asyncio as redis

STATUS_PENDING = "PENDING"


def k_order(oid: str) -> str:
    return f"order:{oid}"


def k_ps(psid: str) -> str:
    return f"ps:{psid}"


def k_idx_created() -> str:
    return "idx:orders:created"


def k_idx_status(s: str) -> str:
    return f"idx:orders:status:{s}"


def k_fulfill(oid: str, psid: str) -> str:
    return f"fulfill:{oid}:{psid}"


def k_idemp(key: str) -> str:
    return f"idemp:{key}"


async def create_order_and_session(
    r: redis.Redis,
    *,
    order_id: str,
    cls: str,
    qty: int,
    amount: int,
    currency: str,
    email: str,
    created_at: float,
    tb_transfer_id: str,
    goodie_tb_transfer_id: str,
    try_goodie: bool,
    psid: str,
) -> None:
    pipe = r.pipeline(transaction=True)
    pipe.hset(k_order(order_id), mapping={
        "id": order_id,
        "status": STATUS_PENDING,
        "cls": cls,
        "qty": qty,
        "amount": amount,
        "currency": currency,
        "customer_email": email,
        "created_at": created_at,
        "tb_transfer_id": tb_transfer_id,
        "goodie_tb_transfer_id": goodie_tb_transfer_id,
        "try_goodie": "1" if try_goodie else "0",
        "payment_session_id": psid,
        "got_goodie": "0",
    })
    pipe.hset(k_ps(psid), mapping={
        "order_id": order_id,
        "amount": amount,
        "currency": currency,
        "created_at": created_at,
    })
    pipe.zadd(k_idx_created(), {order_id: created_at})
    pipe.sadd(k_idx_status(STATUS_PENDING), order_id)
    await pipe.execute()


async def get_payment_session(
        r: redis.Redis, psid: str) -> Optional[Dict[str, Any]]:
    h = await r.hgetall(k_ps(psid))
    return h or None


async def get_order(r: redis.Redis, order_id: str) -> Optional[Dict[str, Any]]:
    h = await r.hgetall(k_order(order_id))
    return h or None


async def list_recent_orders(r: redis.Redis, limit: int = 200) -> List[str]:
    return await r.zrevrange(k_idx_created(), 0, max(0, limit-1))


async def mark_failed_or_canceled(
        r: redis.Redis, order_id: str, new_status: str) -> None:
    pipe = r.pipeline(transaction=True)
    pipe.hset(k_order(order_id), mapping={"status": new_status})
    pipe.srem(k_idx_status(STATUS_PENDING), order_id)
    pipe.sadd(k_idx_status(new_status), order_id)
    await pipe.execute()


async def fulfill_success(
    r: redis.Redis,
    *,
    order_id: str,
    psid: str,
    idem_key: Optional[str],
    ticket_code: str,
    got_goodie: bool,
    now_ts: float,
) -> str:
    # 1) fulfillment idempotency: single atomic gate
    ok = await r.set(k_fulfill(order_id, psid), "1", nx=True, ex=24*3600)
    if not ok:
        return "idempotent"

    # 2) optional event idempotency
    if idem_key:
        ok2 = await r.set(k_idemp(idem_key), "1", nx=True, ex=3600)
        if not ok2:
            return "idempotent"

    # 3) atomic status change + indexes (MULTI/EXEC)
    pipe = r.pipeline(transaction=True)
    pipe.hset(k_order(order_id), mapping={
        "status": "PAID",
        "ticket_code": ticket_code,
        "paid_at": now_ts,
        "got_goodie": "1" if got_goodie else "0",
    })
    pipe.srem(k_idx_status(STATUS_PENDING), order_id)
    pipe.sadd(k_idx_status("PAID"), order_id)
    await pipe.execute()
    return "ok"
