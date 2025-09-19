from __future__ import annotations
import sys

import httpx
import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from .infra.sql import make_async_engine
from .infra.timings import install_shutdown_flush, timeit


from .model.order import Base, Order
from .model import accounting
from .model.accounting import TicketAmount_first_n, BACKEND as ACCT_BACKEND
from .model.accounting import create_accounts, initial_transfers
from .model.accounting._tigerbeetle import ChainedTransferBatcher
from .model.accounting._postgres import GatedAsyncSession
from .model.paymentsession import (
        PaymentSessionStore, new_store, BACKEND as PAYSESSION_BACKEND
)
from .mockpay import PaymentAdapter, MockPay, MOCK_SECRET

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, ORJSONResponse
from fastapi import Form
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


from sqlalchemy.ext.asyncio import AsyncSession, AsyncConnection
from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_303_SEE_OTHER

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from .helpers import now_ts, to_iso, is_valid_email, ct_equal

import tigerbeetle as tb
import redis.asyncio as redis

templates = Jinja2Templates(directory="tigerfans/templates")

# ----------------------------
# Config & Constants
# ----------------------------
MOCK_WEBHOOK_URL = os.environ.get(
    "MOCK_WEBHOOK_URL",
    "http://localhost:8000/payments/webhook"
)
DATABASE_URL = os.environ.get("DATABASE_URL", None)

if DATABASE_URL is None:
    print("NEED DATABASE_URL! See Makefile")
    sys.exit(1)

TICKET_CLASSES = {"A": {"price": 6500}, "B": {"price": 3500}}  # cents (EUR)
GOODIE_LIMIT_PER_CLASS = TicketAmount_first_n
RESERVATION_TTL_SECONDS = 5 * 60

SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-secret-change-me")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "supasecret")


engine, SessionAsync, _, gated = make_async_engine(DATABASE_URL)


async def get_db() -> AsyncSession:
    async with SessionAsync() as session:
        yield session

adapter: PaymentAdapter = MockPay()

app = FastAPI(
    title="TigerFans",
    default_response_class=ORJSONResponse,
)
app.mount("/static", StaticFiles(directory="tigerfans/static"), name="static")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

# shutdown handler trying to post our detailed timings
install_shutdown_flush(app)


def get_tb_client() -> tb.ClientAsync:
    if ACCT_BACKEND != "tb":
        raise RuntimeError("TigerBeetle backend not enabled")
    client = getattr(app.state, "tb_client", None)
    if client is None:
        raise RuntimeError("TigerBeetle client not initialized")
    return client


def get_tb_batcher() -> ChainedTransferBatcher:
    if ACCT_BACKEND != "tb":
        raise RuntimeError("TigerBeetle backend not enabled")
    batcher = getattr(app.state, "tb_batcher", None)
    if batcher is None:
        raise RuntimeError("TigerBeetle batcher not initialized")
    return batcher


async def paymentsessions() -> PaymentSessionStore:
    if PAYSESSION_BACKEND == 'pg':
        conn: AsyncConnection
        async with SessionAsync() as session:
            yield new_store(db=session, gated=gated)
    else:
        yield new_store(r=app.state.redis)


async def accounting_client() -> tb.ClientAsync | AsyncSession:
    if ACCT_BACKEND == 'pg':
        async with SessionAsync() as session:
            yield GatedAsyncSession(session=session, gated=gated)
    else:
        yield get_tb_client()


async def batched_accounting_client() -> ChainedTransferBatcher | AsyncSession:
    if ACCT_BACKEND == 'pg':
        async with SessionAsync() as session:
            yield GatedAsyncSession(session=session, gated=gated)
    else:
        yield get_tb_batcher()


# ---
# startup / shutdown
# ---
@app.on_event("startup")
async def _say_hello():
    print('\n' * 3)
    print('=' * 50)
    A = 'PostgreSQL' if ACCT_BACKEND == 'pg' else 'Tigerbeetle'
    R = 'PostgreSQL' if PAYSESSION_BACKEND == 'pg' else 'Redis'
    print('TigerFans is starting up...')
    print(f'   - Accounting   Backend: {A}')
    print(f'   - Payment Sessions Backend: {R}')
    print('=' * 50)
    print('\n' * 3)


@app.on_event("startup")
async def _db_init():
    # Create SQL tables for Orders
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if ACCT_BACKEND == "pg":
            await create_accounts(conn)
        if PAYSESSION_BACKEND == "pg":
            from .model.paymentsession._postgres import create_schema
            await create_schema(conn)


@app.on_event("startup")
async def _http_client_start():
    app.state.http = httpx.AsyncClient(
        timeout=5.0,
        limits=httpx.Limits(
            max_connections=512, max_keepalive_connections=512
        ),
    )


@app.on_event("startup")
async def _redis_start():
    if PAYSESSION_BACKEND != 'pg':
        REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379")
        app.state.redis = redis.from_url(
            REDIS_URL,
            decode_responses=True,
            max_connections=int(os.getenv("REDIS_MAX_CONN", "512")),
            socket_timeout=2.0,
            socket_connect_timeout=2.0,
            retry_on_timeout=True,
        )


@app.on_event("startup")
async def _accounting_start():
    # Only spin up TigerBeetle if the accounting backend is TB
    if ACCT_BACKEND == "tb":
        addr = os.getenv("TB_ADDRESS", "3000")
        cluster_id = int(os.getenv("TB_CLUSTER_ID", "0"))
        client = tb.ClientAsync(cluster_id=cluster_id, replica_addresses=addr)
        batcher = ChainedTransferBatcher(client, max_batch_size=8190)
        app.state.tb_client = client
        app.state.tb_batcher = batcher
        if await create_accounts(client):
            await initial_transfers(client)


@app.on_event("shutdown")
async def _http_client_stop():
    http = getattr(app.state, "http", None)
    if http is not None:
        await http.aclose()
        app.state.http = None


@app.on_event("shutdown")
async def _redis_stop():
    r = getattr(app.state, "redis", None)
    if r is not None:
        # optional: close pool explicitly
        await r.close()
        app.state.redis = None


@app.on_event("shutdown")
async def _tb_stop():
    client = getattr(app.state, "tb_client", None)
    if client is not None:
        await client.close()
        app.state.tb_client = None


# ----------------------------
# Helpers
# ----------------------------
def is_admin(request: Request) -> bool:
    return bool(request.session.get("admin_user"))


def require_admin(request: Request) -> None:
    if not is_admin(request):
        # preserve where we wanted to go
        dest = request.url.path
        raise HTTPException(status_code=307, detail="redirect to login",
                            headers={"Location": f"/admin/login?next={dest}"})


# ----------------------------
# Landing page
# ----------------------------
@app.get("/", response_class=HTMLResponse)
async def landing_page(request: Request):
    return templates.TemplateResponse(
        "landing.html",
        {
            "request": request,
            "site_name": "TigerFans",
            "conf_date": "Dec 3–4, 2025",
            "conf_tagline":
                "A conference for people who love fast, correct systems.",
            "PAYSESSION_BACKEND": PAYSESSION_BACKEND,
            "ACCT_BACKEND": ACCT_BACKEND,
        },
    )


@app.get("/tigerbench", response_class=HTMLResponse)
async def tigerbench_page(request: Request):
    return templates.TemplateResponse(
        "tigerbench.html",
        {
            "request": request,
            "site_name": "TigerFans",
            "conf_date": "Dec 3–4, 2025",
            "conf_tagline":
                "A conference for people who love fast, correct systems.",
            "PAYSESSION_BACKEND": PAYSESSION_BACKEND,
            "ACCT_BACKEND": ACCT_BACKEND,
        },
    )

# ----------------------------
# Checkout page (force 1 ticket, require email)
# ----------------------------
@app.get("/demo/checkout", response_class=HTMLResponse)
async def demo_checkout_page(request: Request, status: Optional[str] = None,
                             order_id: Optional[str] = None):
    return templates.TemplateResponse(
        "checkout.html",
        {"request": request, "status": status, "order_id": order_id}
    )


@app.post("/api/checkout")
async def create_checkout(
    payload: dict,
    ac: ChainedTransferBatcher | AsyncSession = Depends(batched_accounting_client),
    rs: PaymentSessionStore = Depends(paymentsessions),
):
    cls = payload.get("cls")
    # Enforce single-ticket policy
    qty = 1
    customer_email = (payload.get("customer_email") or "").strip()

    if not is_valid_email(customer_email):
        raise HTTPException(
            400,
            detail="customer_email is required and must be a valid email "
                   "address"
        )

    if cls not in TICKET_CLASSES:
        raise HTTPException(400, detail="invalid ticket class")

    async with timeit("accounting.hold"):
        tb_transfer_id, goodie_tb_transfer_id, ticket_ok, goodie_ok = (
                await accounting.hold_tickets(ac, cls, qty,
                                              RESERVATION_TTL_SECONDS)
        )
    if not ticket_ok:
        if goodie_ok:
            # cancel the goodie ticket reservation so it doesn't need to
            # time out
            async with timeit("accounting.cancel_goodie"):
                await accounting.cancel_only_goodie(ac, goodie_tb_transfer_id)
        raise RuntimeError("Sold Out")

    amount = TICKET_CLASSES[cls]["price"] * qty
    session = adapter.create_session_id_and_url()
    psid = session["payment_session_id"]
    order_id = uuid.uuid4().hex
    currency = "eur"

    async with timeit("paymentsession.save"):
        await rs.save_payment_session(psid, {
            "order_id": order_id,
            "cls": cls,
            "qty": "1",
            "customer_email": customer_email,
            "tb_transfer_id": str(tb_transfer_id),
            "goodie_tb_transfer_id": str(goodie_tb_transfer_id),
            "try_goodie": "1" if goodie_ok else "0",
            "amount": str(amount),
            "currency": "eur",
            "created_at": str(now_ts()),
        })

    return {
        "order_id": order_id,
        "redirect_url": session["redirect_url"],
        "amount": amount,
        "currency": currency,
    }


#  this endpoint is purely for measuring accounting performance
@app.get("/api/accounting/reserve")
async def perftest_accounting_reserve(
    ac: ChainedTransferBatcher | AsyncSession = Depends(batched_accounting_client),
):
    async with timeit("accounting.hold"):
        tb_transfer_id, goodie_tb_transfer_id, ticket_ok, goodie_ok = (
                await accounting.hold_tickets(
                    ac, 'A', 1, RESERVATION_TTL_SECONDS
                )
        )

    return {
        "tb_transfer_id": str(tb_transfer_id),
        "goodie_tb_transfer_id": str(goodie_tb_transfer_id),
    }


#  this endpoint is purely for measuring accounting performance
@app.get("/api/accounting/commit")
async def perftest_accounting_commit(
    tb_transfer_id: str, goodie_tb_transfer_id: str,
    ac: ChainedTransferBatcher | AsyncSession = Depends(batched_accounting_client),
):

    async with timeit("accounting.commit_order"):
        gets_ticket, gets_goodie = await accounting.commit_order(
            ac,
            tb_transfer_id,
            goodie_tb_transfer_id,
            'A',
            1,
            True,
        )

    return {
        "tb_transfer_id": tb_transfer_id,
        "goodie_tb_transfer_id": goodie_tb_transfer_id,
    }


# ----------------------------
# API: Order status (polled by success page)
# ----------------------------
@app.get("/api/orders/{order_id}")
async def get_order(order_id: str, db: AsyncSession = Depends(get_db)):
    # DB-GATE!!!
    async with timeit("db.get_order"):
        async with gated():
            async with db.begin():
                result = await db.execute(
                    text("""
                        SELECT id, status, cls, qty, amount, currency, paid_at,
                               ticket_code, got_goodie
                        FROM orders WHERE id = :id
                    """),
                    {"id": order_id},
                )
    row = result.mappings().first()
    if not row:
        # not created yet (webhook still processing) -> let client keep polling
        raise HTTPException(404, detail="order not found")
    return {
        "order_id": row["id"],
        "status": row["status"],
        "cls": row["cls"],
        "qty": row["qty"],
        "amount": row["amount"],
        "currency": row["currency"],
        "paid_at": to_iso(row["paid_at"]),
        "ticket_code": row["ticket_code"] or "",
        "got_goodie": row["got_goodie"],
    }


# ----------------------------
# Webhook endpoint (shared for Mock/Stripe)
# ----------------------------
@app.post("/payments/webhook")
async def payments_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    ac: ChainedTransferBatcher | AsyncSession = Depends(batched_accounting_client),
    rs: PaymentSessionStore = Depends(paymentsessions),
):
    payload = await request.body()
    headers = dict(request.headers)

    event = adapter.verify_webhook(payload, headers)
    kind = adapter.event_kind(event)  # succeeded | failed | canceled
    psid, idem = adapter.event_ids(event)
    if not psid:
        raise HTTPException(400, detail="missing payment_session_id")

    async with timeit("paymentsession.get"):
        ps = await rs.get_payment_session(psid)
    if not ps:
        raise HTTPException(404, detail="payment session not found")

    # Combined guard (one durable tx on PG; 1–2 RTT on Redis)
    async with timeit("paymentsession.fulfill"):
        flags = await rs.fulfill_and_mark_event(psid, idem)

    # Short-circuit exactly like before:
    # - already_fulfilled -> skip
    # - event_seen == True -> skip
    if flags["already_fulfilled"] or (flags["event_seen"] is True):
        return {"ok": True, "idempotent": True}

    # Extract inputs from Redis (strings -> ints where needed)
    order_id = ps["order_id"]
    cls = ps["cls"]
    qty = int(ps["qty"])
    amount = int(ps["amount"])
    currency = ps["currency"]
    email = ps["customer_email"]
    tb_transfer_id = ps["tb_transfer_id"]
    goodie_tb_transfer_id = ps["goodie_tb_transfer_id"]
    try_goodie = (ps.get("try_goodie") == "1")

    # --- TigerBeetle first (no DB tx held) ---
    gets_ticket = False
    gets_goodie = False

    if kind == "succeeded":
        async with timeit("accounting.commit_order"):
            gets_ticket, gets_goodie = await accounting.commit_order(
                ac,
                tb_transfer_id,
                goodie_tb_transfer_id,
                cls,
                qty,
                try_goodie,
            )

        # Late-success best effort
        if not gets_ticket:
            async with timeit("accounting.book_immediately"):
                (
                    tb2, goodie2, gets_ticket2, gets_goodie2
                ) = await accounting.book_immediately(ac, cls, qty)
                if gets_ticket2:
                    gets_ticket = True
                    gets_goodie = gets_goodie or gets_goodie2
                    # (Optional) We *could* add a method to PaymentSessionStore
                    # to update these IDs in Redis, but it's not required
                    # anymore since we’re about to persist to Postgres.
    elif kind in ("failed", "canceled"):
        async with timeit("accounting.cancel_order"):
            await accounting.cancel_order(
                ac, tb_transfer_id, goodie_tb_transfer_id, cls, qty
            )
        # No durable write needed for failure/cancel (by design of the hybrid)
        # but we don't forget to flush!
        async with timeit("paymentsession.remove_pending"):
            await rs.remove_pending(psid)

    # --- Durable write to Postgres (success path) ---
    if kind == "succeeded":
        ticket_code = None
        if gets_ticket:
            ticket_code = f"TCK-{uuid.uuid4().hex[:10].upper()}"
        status = "PAID" if gets_ticket else "PAID_UNFULFILLED"

        try:
            # DB GATE
            async with timeit("db.add_order"):
                async with gated():
                    async with db.begin():
                        db.add(Order(
                            id=order_id,
                            tb_transfer_id=str(tb_transfer_id),
                            goodie_tb_transfer_id=str(goodie_tb_transfer_id),
                            try_goodie=try_goodie,
                            cls=cls,
                            qty=qty,
                            amount=amount,
                            currency=currency,
                            customer_email=email,
                            created_at=now_ts(),
                            status=status,
                            paid_at=now_ts(),
                            ticket_code=ticket_code,
                            got_goodie=bool(gets_goodie),
                        ))
        except IntegrityError:
            # If we see this, it’s an idempotent replay racing the first write.
            async with timeit("db.rollback"):
                await db.rollback()
        # don't forget to flush!
        async with timeit("paymentsession.remove_pending"):
            await rs.remove_pending(psid)
        return {"ok": True, "order_status": status}

    # failed/canceled
    return {
        "ok": True,
        "order_status": "FAILED" if kind == "failed" else "CANCELED"
    }


@app.get("/api/inventory")
async def get_inventory(
    client: tb.ClientAsync | AsyncSession = Depends(accounting_client),
):
    return await accounting.compute_inventory(client)


@app.get("/api/pending")
async def api_pending(
    limit: int = 100,
    rs: PaymentSessionStore = Depends(paymentsessions),
):
    total, items = await rs.get_recent_payment_sessions(limit=limit)
    return {"items": items, "enabled": True, 'limit': limit, 'total': total}


# ---- Admin JSON feed: goodies counter ----
@app.get("/api/admin/goodies")
async def api_admin_goodies(
    client: tb.ClientAsync | AsyncSession = Depends(accounting_client),
):
    used = await accounting.count_goodies(client)
    return {
        "used": int(used),
        "limit": int(TicketAmount_first_n),
    }


@app.get("/api/admin/orders")
async def api_admin_orders(limit: int = 200,
                           db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        text("""
            SELECT id, status, cls, qty, amount, currency, paid_at,
                   got_goodie, ticket_code, customer_email, created_at
            FROM orders
            ORDER BY created_at DESC
            LIMIT :limit
        """),
        {"limit": max(1, min(limit, 500))},
    )
    rows = result.mappings().all()
    items = []
    for r in rows:
        paid_iso = '-' if r["paid_at"] is None else datetime.fromtimestamp(
            r["paid_at"], tz=timezone.utc
        ).isoformat()
        items.append({
            "id": r["id"],
            "status": r["status"],
            "cls": r["cls"],
            "qty": r["qty"],
            "amount": r["amount"],
            "currency": r["currency"],
            "paid_at_iso": paid_iso,
            "got_goodie": bool(r["got_goodie"]),
            "ticket_code": r["ticket_code"] or "",
            "email": r["customer_email"] or "",
        })
    return {"items": items, 'limit': limit}


# ----------------------------
# MockPay UI (simple page with 3 buttons)
# ----------------------------
# MockPay
@app.get("/mockpay/{psid}", response_class=HTMLResponse)
async def mockpay_screen(
    request: Request, psid: str,
    db: AsyncSession = Depends(get_db),
    rs: PaymentSessionStore = Depends(paymentsessions),
):
    async with timeit("paymentsession.get"):
        ps = await rs.get_payment_session(psid)
    if not ps:
        raise HTTPException(404, "payment session not found")
    return templates.TemplateResponse("mockpay.html", {
        "request": request,
        "psid": psid,
        "order_id": ps['order_id'],
        "cls": ps["cls"],
        "qty": 1,
        "amount_eur": f"{int(ps['amount'])/100:.2f}",
        "webhook_url": MOCK_WEBHOOK_URL,
    })


@app.post("/mockpay/{psid}/emit")
async def mockpay_emit(
    psid: str, request: Request,
    db: AsyncSession = Depends(get_db),
    rs: PaymentSessionStore = Depends(paymentsessions),
):
    form = await request.form()
    kind = form.get("t")  # succeeded|failed|canceled
    if kind not in {"succeeded", "failed", "canceled"}:
        raise HTTPException(400, detail="invalid kind")

    async with timeit("paymentsession.get"):
        ps = await rs.get_payment_session(psid)
    if not ps:
        raise HTTPException(404, "payment session not found")

    # Build event from Redis hash (strings -> ints as needed)
    order_id = ps['order_id']
    event = {
        "type": f"payment.{kind}",
        "payment_session_id": psid,
        "order_id": order_id,
        "amount": int(ps["amount"]),
        "currency": ps["currency"],
        "created_at": int(time.time()),
        "idempotency_key": f"evt_{uuid.uuid4().hex}",
    }

    payload = json.dumps(event).encode()
    sig = base64.b64encode(
        hmac.new(
            MOCK_SECRET.encode(),
            payload,
            hashlib.sha256
            ).digest()).decode()

    client_http: httpx.AsyncClient = app.state.http
    try:
        await client_http.post(
            MOCK_WEBHOOK_URL,
            content=payload,
            headers={
                "x-mockpay-signature": sig,
                "content-type": "application/json",
            },
        )
    except Exception as e:
        # For the demo, we don't fail the redirect if webhook doesn't reach;
        # user can retry.
        print("Webhook delivery failed:", e)

    # Redirect UX
    if kind == "succeeded":
        return RedirectResponse(
            url=f"/demo/success?order_id={order_id}",
            status_code=303
        )
    elif kind == "failed":
        return RedirectResponse(
            url=f"/demo/checkout?status=failed&order_id={order_id}",
            status_code=303
        )
    else:
        return RedirectResponse(
            url=f"/demo/checkout?status=canceled&order_id={order_id}",
            status_code=303
        )


# ----------------------------
# Success page: polls order status and displays tickets
# ----------------------------
# Success page
@app.get("/demo/success", response_class=HTMLResponse)
async def demo_success_page(request: Request, order_id: str):
    return templates.TemplateResponse(
        "success.html",
        {"request": request, "order_id": order_id}
    )


# ----------------------------
# Admin page: list orders and goodies counters
# ----------------------------

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_get(request: Request, next: str | None = "/admin"):
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "next": next, "error": None}
    )


@app.post("/admin/login", response_class=HTMLResponse)
async def admin_login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/admin"),
):
    ok_user = ct_equal(username.strip(), ADMIN_USERNAME)
    ok_pass = ct_equal(password, ADMIN_PASSWORD)
    if ok_user and ok_pass:
        request.session["admin_user"] = username.strip()
        return RedirectResponse(
            url=(next or "/admin"),
            status_code=HTTP_303_SEE_OTHER
        )
    # auth failed
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "next": next, "error": "Invalid credentials."},
        status_code=401,
    )


@app.get("/admin/logout")
async def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/", status_code=HTTP_303_SEE_OTHER)


# Admin page
@app.get("/admin", response_class=HTMLResponse)
async def admin_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    client: tb.ClientAsync | AsyncSession = Depends(accounting_client),
):
    if not is_admin(request):
        dest = request.url.path
        return RedirectResponse(
            url=f"/admin/login?next={dest}",
            status_code=307
        )

    goodies_count = await accounting.count_goodies(client)
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "goodies": goodies_count,
            "goodie_limit": TicketAmount_first_n,
            "site_name": "TigerFans",
        }
    )
