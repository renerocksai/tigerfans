"""
Ticketing Demo  (FastAPI + MockPay adapter + TigerBeetle + SQLite)
------------------------------------------------------------------
Goals:
- Two ticket classes: A (premium) and B (standard)
- First 100 successful buyers receive a goodie
  (via TigerBeetle transfer from a pool)
- Mock payment provider with redirect + webhook flow
- Clean adapter boundary so we can later swap in Stripe
- **SQLite instead of in-memory** so the demo persists and survives restarts

Run locally:
  uvicorn tigerfans/server:app --reload

Env (optional):
  MOCK_WEBHOOK_URL="http://localhost:8000/payments/webhook"
  MOCK_SECRET="supersecret"
  DATABASE_URL="sqlite:///./demo.db"  (default)

Notes:
- SQLite is used for persistence. For a demo and single-node setup it works well.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, ORJSONResponse
from fastapi import Form
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


from .model import tigerbeetledb

from sqlalchemy.ext.asyncio import AsyncSession

from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_303_SEE_OTHER

from sqlalchemy import (
    text,
    select,
    join,
)
from sqlalchemy.exc import IntegrityError
from .model.db import (
    Base, Order, PaymentSession, WebhookEventSeen, FulfillmentKey, make_async_engine
)
from .helpers import now_ts, to_iso, is_valid_email, ct_equal
from .mockpay import PaymentAdapter, MockPay, MOCK_SECRET

import tigerbeetle as tb

templates = Jinja2Templates(directory="tigerfans/templates")

# ----------------------------
# Config & Constants
# ----------------------------
MOCK_WEBHOOK_URL = os.environ.get(
    "MOCK_WEBHOOK_URL",
    "http://localhost:8000/payments/webhook"
)
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./demo.db")
DB_KIND = "postgres" if DATABASE_URL.startswith("postgres") else "sqlite"

TICKET_CLASSES = {"A": {"price": 6500}, "B": {"price": 3500}}  # cents (EUR)
GOODIE_LIMIT_PER_CLASS = tigerbeetledb.TicketAmount_first_n
RESERVATION_TTL_SECONDS = 5 * 60

SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-secret-change-me")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "supasecret")


# ----------------------------
# Database setup (SQLite)
# ----------------------------
engine, SessionAsync = make_async_engine(DATABASE_URL)


async def get_db() -> AsyncSession:
    async with SessionAsync() as session:
        yield session




adapter: PaymentAdapter = MockPay()


# ----------------------------
# FastAPI app
# ----------------------------

app = FastAPI(
    title="Ticketing Demo with MockPay & TigerBeetle (SQLite/PostgreSQL)",
    default_response_class=ORJSONResponse,
)
app.mount("/static", StaticFiles(directory="tigerfans/static"), name="static")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)


# ---
# startup / shutdown
# ---
@app.on_event("startup")
async def _tb_start():
    # env or defaults; adjust cluster_id/address as needed
    addr = os.getenv("TB_ADDRESS", "3000")
    cluster_id = int(os.getenv("TB_CLUSTER_ID", "0"))
    # create once per process; keep it on app.state
    client = tb.ClientAsync(cluster_id=cluster_id, replica_addresses=addr)
    app.state.tb_client = client
    if await tigerbeetledb.create_accounts(client):
        await tigerbeetledb.initial_transfers(client)


@app.on_event("startup")
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@app.on_event("startup")
async def _http_client_start():
    app.state.http = httpx.AsyncClient(
        timeout=5.0,
        limits=httpx.Limits(max_connections=128, max_keepalive_connections=128)
    )


@app.on_event("shutdown")
async def _http_client_stop():
    http = getattr(app.state, "http", None)
    if http is not None:
        await http.aclose()
        app.state.http = None


@app.on_event("shutdown")
async def _tb_stop():
    client = getattr(app.state, "tb_client", None)
    if client is not None:
        await client.close()
        app.state.tb_client = None


# ----------------------------
# Helpers
# ----------------------------
def get_tb_client() -> "tb.ClientSync":
    client = getattr(app.state, "tb_client", None)
    if client is None:
        # Shouldn't happen after startup, but be explicit
        raise RuntimeError("TigerBeetle client not initialized")
    return client


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
            "conf_tagline": "A conference for people who love fast, correct systems.",
            "db": DB_KIND,
        },
    )


# ----------------------------
# Checkout page (force 1 ticket, require email)
# ----------------------------
@app.get("/demo/checkout", response_class=HTMLResponse)
async def demo_checkout_page(request: Request, status: Optional[str] = None, order_id: Optional[str] = None):
    return templates.TemplateResponse(
        "checkout.html",
        {"request": request, "status": status, "order_id": order_id}
    )


@app.post("/api/checkout")
async def create_checkout(payload: dict, db: SessionAsync = Depends(get_db), client: "tb.ClientSync" = Depends(get_tb_client)):
    cls = payload.get("cls")
    # Enforce single-ticket policy
    qty = 1
    customer_email = (payload.get("customer_email") or "").strip()

    if not is_valid_email(customer_email):
        raise HTTPException(400, detail="customer_email is required and must be a valid email address")

    if cls not in TICKET_CLASSES:
        raise HTTPException(400, detail="invalid ticket class")

    tb_transfer_id, goodie_tb_transfer_id, ticket_ok, goodie_ok = await tigerbeetledb.hold_tickets(client, cls, qty, RESERVATION_TTL_SECONDS)
    if not ticket_ok:
        raise RuntimeError("Sold Out")

    amount = TICKET_CLASSES[cls]["price"] * qty
    order_id = uuid.uuid4().hex
    session = adapter.create_session_id_and_url()
    currency = "eur"

    async with db.begin():
        order = Order(
            id=order_id,
            tb_transfer_id=str(tb_transfer_id),
            goodie_tb_transfer_id=str(goodie_tb_transfer_id),
            try_goodie=goodie_ok,
            cls=cls,
            qty=qty,
            amount=amount,
            currency=currency,
            customer_email=customer_email,
            created_at=now_ts(),
            status="PENDING",
        )
        db.add(order)

        payment_session = PaymentSession(
            id=session["payment_session_id"],
            order_id=order_id,
            amount=amount,
            currency=currency,
            created_at=now_ts(),
        )
        order.payment_session_id = session["payment_session_id"]
        db.add(payment_session)

    return {
        "order_id": order_id,
        "redirect_url": session["redirect_url"],
        "amount": amount,
        "currency": order.currency,
    }


# ----------------------------
# API: Order status (polled by success page)
# ----------------------------
@app.get("/api/orders/{order_id}")
async def get_order(order_id: str, db: SessionAsync = Depends(get_db)):
    # raw sql -> sqlalchemy won't start an implicit transaction
    result = await db.execute(
        text("""
            SELECT id, status, cls, qty, amount, currency, paid_at, ticket_code, got_goodie
            FROM orders WHERE id = :id
        """),
        {"id": order_id},
    )
    row = result.mappings().first()
    if not row:
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
    db: SessionAsync = Depends(get_db),
    client: "tb.ClientSync" = Depends(get_tb_client),
):
    payload = await request.body()
    headers = dict(request.headers)

    event = adapter.verify_webhook(payload, headers)
    kind = adapter.event_kind(event)  # succeeded | failed | canceled
    psid, idem = adapter.event_ids(event)
    if not psid:
        raise HTTPException(400, detail="missing payment_session_id")
    ######
    # Variant 1
    ######
    # # look up session + order (reads)
    # session = await db.get(PaymentSession, psid)
    # if not session:
    #     raise HTTPException(404, detail="payment session not found")
    # order = await db.get(Order, session.order_id)
    # if not order:
    #     raise HTTPException(404, detail="order not found")
    #
    # # Try to short-circuit idempotency **without a transaction** (optional read).
    # # We **could** remove this read: inserts below are guarded anyway.
    # fulfill_key = f"{order.id}:{psid}"
    # if await db.get(FulfillmentKey, fulfill_key):
    #     return {"ok": True, "idempotent": True}
    ######

    ######
    # Variant 2
    ######
    # one round-trip: PaymentSession -> Order
    result = await db.execute(
        select(Order).select_from(
            join(PaymentSession, Order, PaymentSession.order_id == Order.id)
        ).where(PaymentSession.id == psid)
    )
    order = result.scalars().first()
    if not order:
        # either the payment session doesn't exist or it doesn't link to an order
        raise HTTPException(404, detail="order not found for payment_session_id")

    # Optional early idempotency short-circuit (kept same behavior)
    fulfill_key = f"{order.id}:{psid}"
    if await db.get(FulfillmentKey, fulfill_key):
        return {"ok": True, "idempotent": True}
    #
    ######

    # --- Do provider-side work first (TB), without holding a DB tx ---
    if kind == "succeeded":
        gets_ticket, gets_goodie = await tigerbeetledb.commit_order(
            client,
            order.tb_transfer_id,
            order.goodie_tb_transfer_id,
            order.cls,
            order.qty,
            order.try_goodie,
        )

        # Late-success handling
        if not gets_ticket:
            order.status = "PAID_UNFULFILLED"
            tb_transfer_id, goodie_tb_transfer_id, gets_ticket2, gets_goodie2 = await tigerbeetledb.book_immediately(
                client, order.cls, order.qty
            )
            if gets_ticket2:
                gets_ticket = True
                gets_goodie = gets_goodie or gets_goodie2
                order.tb_transfer_id = str(tb_transfer_id)
                order.goodie_tb_transfer_id = str(goodie_tb_transfer_id)
    elif kind in ("failed", "canceled"):
        # Void/rollback TB holds (no DB tx held)
        await tigerbeetledb.cancel_order(
            client, order.tb_transfer_id, order.goodie_tb_transfer_id, order.cls, order.qty
        )

    # --- Single DB transaction for ALL writes (no early commits) ---
    try:
        # Event-level idempotency marker (PK on idempotency_key). If dup, we ignore.
        if idem:
            db.add(WebhookEventSeen(idempotency_key=idem))

        # Fulfillment idempotency (PK on key). If dup, treat as idempotent.
        db.add(FulfillmentKey(key=fulfill_key))

        if kind == "succeeded":
            if gets_ticket:
                if not order.ticket_code:
                    order.ticket_code = f"TCK-{uuid.uuid4().hex[:10].upper()}"
                order.got_goodie = bool(gets_goodie)
                order.status = "PAID"
            else:
                # paid but still unfulfilled after immediate attempt
                order.status = "PAID_UNFULFILLED"
            order.paid_at = now_ts()
        else:
            order.status = "FAILED" if kind == "failed" else "CANCELED"
            # (paid_at remains None)

        await db.commit()                 # SINGLE COMMIT

        # If we got here, commit succeeded
        return {"ok": True, "order_status": order.status}

    except IntegrityError:
        # Either idempotency_key or fulfillment key already seen: idempotent replay.
        # We deliberately do NOT re-raise; just say it's fine.
        await db.rollback()
        return {"ok": True, "idempotent": True, "order_status": order.status}


@app.get("/api/inventory")
async def get_inventory(client: "tb.ClientSync" = Depends(get_tb_client)):
    return await tigerbeetledb.compute_inventory(client)


# ----------------------------
# MockPay UI (simple page with 3 buttons)
# ----------------------------
# MockPay
@app.get("/mockpay/{psid}", response_class=HTMLResponse)
async def mockpay_screen(request: Request, psid: str, db: SessionAsync = Depends(get_db)):
    session = await db.get(PaymentSession, psid)
    if not session:
        raise HTTPException(404, detail="payment session not found")
    order = await db.get(Order, session.order_id)
    return templates.TemplateResponse(
        "mockpay.html",
        {
            "request": request,
            "psid": psid,
            "order_id": order.id,
            "cls": order.cls,
            "qty": order.qty,
            "amount_eur": f"{session.amount/100.0:.2f}",
            "webhook_url": MOCK_WEBHOOK_URL,
        }
    )


@app.post("/mockpay/{psid}/emit")
async def mockpay_emit(psid: str, request: Request, db: SessionAsync = Depends(get_db)):
    form = await request.form()
    kind = form.get("t")  # succeeded|failed|canceled
    if kind not in {"succeeded", "failed", "canceled"}:
        raise HTTPException(400, detail="invalid kind")

    session = await db.get(PaymentSession, psid)
    if not session:
        raise HTTPException(404, detail="payment session not found")

    event = {
        "type": f"payment.{kind}",
        "payment_session_id": psid,
        "order_id": session.order_id,
        "amount": session.amount,
        "currency": session.currency,
        "created_at": int(time.time()),
        "idempotency_key": f"evt_{uuid.uuid4().hex}",
    }
    payload = json.dumps(event).encode()
    sig = base64.b64encode(hmac.new(MOCK_SECRET.encode(), payload, hashlib.sha256).digest()).decode()

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
        # For the demo, we don't fail the redirect if webhook doesn't reach; user can retry.
        print("Webhook delivery failed:", e)

    # Redirect UX
    if kind == "succeeded":
        return RedirectResponse(url=f"/demo/success?order_id={session.order_id}", status_code=303)
    elif kind == "failed":
        return RedirectResponse(url=f"/demo/checkout?status=failed&order_id={session.order_id}", status_code=303)
    else:
        return RedirectResponse(url=f"/demo/checkout?status=canceled&order_id={session.order_id}", status_code=303)


# ----------------------------
# Success page: polls order status and displays tickets
# ----------------------------
# Success page
@app.get("/demo/success", response_class=HTMLResponse)
async def demo_success_page(request: Request, order_id: str, db: SessionAsync = Depends(get_db)):
    order = await db.get(Order, order_id)
    if not order:
        raise HTTPException(404, detail="order not found")
    return templates.TemplateResponse("success.html", {"request": request, "order_id": order_id})


# ----------------------------
# Admin page: list orders and goodies counters
# ----------------------------

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_get(request: Request, next: str | None = "/admin"):
    return templates.TemplateResponse("login.html", {"request": request, "next": next, "error": None})


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
        return RedirectResponse(url=(next or "/admin"), status_code=HTTP_303_SEE_OTHER)
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
async def admin_page(request: Request, db: SessionAsync = Depends(get_db), client: "tb.ClientSync" = Depends(get_tb_client)):
    if not is_admin(request):
        dest = request.url.path
        return RedirectResponse(url=f"/admin/login?next={dest}", status_code=307)

    result = await db.execute(
        text("SELECT id, status, cls, qty, amount, currency, paid_at, got_goodie, ticket_code, customer_email FROM orders ORDER BY created_at DESC LIMIT 200")
    )
    rows = result.all()
    orders = []
    for (oid, status, cls, qty, amount, currency, paid_at, got_goodie, ticket_code, email) in rows:
        paid_iso = '-' if paid_at is None else datetime.fromtimestamp(paid_at, tz=timezone.utc).isoformat()
        orders.append({
            "id": oid,
            "status": status,
            "cls": cls,
            "qty": qty,
            "amount": amount,
            "currency": currency,
            "paid_at_iso": paid_iso,
            "got_goodie": got_goodie,
            "ticket_code": ticket_code,
            "email": email,
        })
    goodies_count = await tigerbeetledb.count_goodies(client)
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "orders": orders,
            "goodies": goodies_count,
            "goodie_limit": tigerbeetledb.TicketAmount_first_n,
            "site_name": "TigerFans",
        }
    )
