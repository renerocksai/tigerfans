"""
Ticketing Demo Skeleton (FastAPI + MockPay adapter + TigerBeetle placeholders + SQLite)
-----------------------------------------------------------------------------
Goals:
- Two ticket classes: A (premium) and B (standard)
- First 100 successful buyers per class receive a goodie
  (via TigerBeetle transfer from a pool)
- Mock payment provider with redirect + webhook flow
- Clean adapter boundary so you can later swap in Stripe
- **SQLite instead of in-memory** so the demo persists and survives restarts

Run locally:
  uvicorn ticketing_demo_fastapi_mockpay_tigerbeetle_skeleton:app --reload

Env (optional):
  MOCK_WEBHOOK_URL="http://localhost:8000/payments/webhook"
  MOCK_SECRET="supersecret"
  DATABASE_URL="sqlite:///./demo.db"  (default)

Notes:
- All TigerBeetle interactions are stubbed via tb_* functions.
- SQLite is used for persistence. For a demo and single-node setup it works
  well; see README notes at bottom of file.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, TypedDict

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi import Form

import re


from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Boolean,
    create_engine,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session

from starlette.middleware.sessions import SessionMiddleware
from starlette.status import HTTP_303_SEE_OTHER
from fastapi.responses import RedirectResponse

templates = Jinja2Templates(directory="tigerfans/templates")


# ----------------------------
# Config & Constants
# ----------------------------
MOCK_WEBHOOK_URL = os.environ.get(
    "MOCK_WEBHOOK_URL",
    "http://localhost:8000/payments/webhook"
)
MOCK_SECRET = os.environ.get("MOCK_SECRET", "supersecret")
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./demo.db")

TICKET_CLASSES = {"A": {"price": 6500}, "B": {"price": 3500}}  # cents (EUR)
GOODIE_LIMIT_PER_CLASS = 100
RESERVATION_TTL_SECONDS = 15 * 60

SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-secret-change-me")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "supersecret")


# ----------------------------
# Database setup (SQLite)
# ----------------------------
engine = create_engine(
    DATABASE_URL,
    future=True,
    connect_args={
        "check_same_thread": False
    } if DATABASE_URL.startswith("sqlite") else {},
    pool_pre_ping=True,
)

# Enable WAL + busy timeout for SQLite
if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def sqlite_pragmas(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA busy_timeout=5000;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.close()

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False,
                            future=True)
Base = declarative_base()


# ----------------------------
# ORM models
# ----------------------------
class Reservation(Base):
    __tablename__ = "reservations"
    id = Column(String, primary_key=True)
    cls = Column(String, nullable=False)
    qty = Column(Integer, nullable=False)
    created_at = Column(Float, nullable=False)
    expires_at = Column(Float, nullable=False)
    # ACTIVE | EXPIRED | CANCELED | CONVERTED
    status = Column(String, nullable=False)


class Order(Base):
    __tablename__ = "orders"
    id = Column(String, primary_key=True)
    reservation_id = Column(String, nullable=True)
    cls = Column(String, nullable=False)
    qty = Column(Integer, nullable=False)
    amount = Column(Integer, nullable=False)  # cents
    currency = Column(String, nullable=False, default="eur")
    customer_email = Column(String, nullable=True)

    # PENDING | PAID | FAILED | CANCELED | REFUNDED
    status = Column(String, nullable=False, default="PENDING")
    created_at = Column(Float, nullable=False)
    paid_at = Column(Float, nullable=True)
    payment_session_id = Column(String, nullable=True)

    # for Stripe; unused in Mock
    payment_intent_id = Column(String, nullable=True)

    # for Stripe; unused in Mock
    charge_id = Column(String, nullable=True)

    tickets_csv = Column(String, nullable=True)  # comma-separated ticket codes
    got_goodie = Column(Boolean, nullable=False, default=False)


class PaymentSession(Base):
    __tablename__ = "payment_sessions"
    id = Column(String, primary_key=True)
    order_id = Column(String, nullable=False)
    amount = Column(Integer, nullable=False)
    currency = Column(String, nullable=False)
    created_at = Column(Float, nullable=False)


class WebhookEventSeen(Base):
    __tablename__ = "webhook_events_seen"
    idempotency_key = Column(String, primary_key=True)


class FulfillmentKey(Base):
    __tablename__ = "fulfillment_keys"
    key = Column(String, primary_key=True)  # order_id:session_id


class GoodiesCounter(Base):
    __tablename__ = "goodies_counter"
    cls = Column(String, primary_key=True)
    granted = Column(Integer, nullable=False, default=0)


Base.metadata.create_all(bind=engine)

# seed goodies counter rows
with SessionLocal() as db:
    for c in ("A", "B"):
        if not db.get(GoodiesCounter, c):
            db.add(GoodiesCounter(cls=c, granted=0))
    db.commit()


# ----------------------------
# Helpers
# ----------------------------
def now_ts() -> float:
    return time.time()


def to_iso(ts: float | None) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

def is_valid_email(email: Optional[str]) -> bool:
    if not email:
        return False
    email = email.strip()
    # simple but effective email check
    return re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email) is not None


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def is_admin(request: Request) -> bool:
    return bool(request.session.get("admin_user"))


def require_admin(request: Request) -> None:
    if not is_admin(request):
        # preserve where we wanted to go
        dest = request.url.path
        raise HTTPException(status_code=307, detail="redirect to login",
                            headers={"Location": f"/admin/login?next={dest}"})


def ct_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# ----------------------------
# Payment Adapter Interface
# ----------------------------
class CreateSessionResult(TypedDict):
    payment_session_id: str
    redirect_url: str

class PaymentAdapter(ABC):
    @abstractmethod
    def create_session(self, db: Session, order: Order, meta: dict) -> CreateSessionResult: ...

    @abstractmethod
    def verify_webhook(self, payload: bytes, headers: dict) -> dict: ...

    @abstractmethod
    def event_kind(self, event: dict) -> str:  # "succeeded" | "failed" | "canceled"
        ...

    @abstractmethod
    def event_ids(self, event: dict) -> Tuple[str, Optional[str]]:  # (payment_session_id, idempotency_key)
        ...

# ----------------------------
# MockPay implementation
# ----------------------------
class MockPay(PaymentAdapter):
    def create_session(self, db: Session, order: Order, meta: dict) -> CreateSessionResult:
        psid = f"mock_{uuid.uuid4().hex}"
        db.add(PaymentSession(
            id=psid,
            order_id=order.id,
            amount=order.amount,
            currency=order.currency,
            created_at=now_ts(),
        ))
        db.commit()
        redirect_url = f"/mockpay/{psid}"
        return {"payment_session_id": psid, "redirect_url": redirect_url}

    def verify_webhook(self, payload: bytes, headers: dict) -> dict:
        sig = headers.get("x-mockpay-signature")
        mac = hmac.new(MOCK_SECRET.encode(), payload, hashlib.sha256).digest()
        expected = base64.b64encode(mac).decode()
        if not sig or not hmac.compare_digest(expected, sig):
            raise HTTPException(status_code=400, detail="Invalid signature")
        try:
            return json.loads(payload.decode())
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON")

    def event_kind(self, event: dict) -> str:
        return event.get("type", "").split(".")[-1]

    def event_ids(self, event: dict) -> Tuple[str, Optional[str]]:
        return event.get("payment_session_id", ""), event.get("idempotency_key")

adapter: PaymentAdapter = MockPay()

# ----------------------------
# TigerBeetle placeholders (replace with real TB RPCs)
# ----------------------------
# These are stubs; wire to your TB client.

def tb_hold_tickets(ticket_class: str, qty: int, reservation_id: str) -> None:
    # TODO: call TB; raise on insufficient inventory
    return None


def tb_commit_order(order: Order) -> None:
    # TODO: TB: commit hold, settle money flows using order.id as transfer id
    return None


def tb_rollback_reservation(reservation_id: str) -> None:
    # TODO: TB: move hold back to inventory idempotently
    return None


def tb_try_grant_goodie(db: Session, ticket_class: str, user_id: str, order_id: str) -> bool:
    # In real life: TB atomic transfer goodie_pool:class -> goodie_user:user_id (idempotent via order_id)
    row = db.get(GoodiesCounter, ticket_class)
    if row.granted >= GOODIE_LIMIT_PER_CLASS:
        return False
    row.granted += 1
    db.commit()
    return True


# ----------------------------
# FastAPI app
# ----------------------------
app = FastAPI(title="Ticketing Demo with MockPay & TigerBeetle stubs (SQLite)")
app.mount("/static", StaticFiles(directory="tigerfans/static"), name="static")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

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
        },
    )


# ----------------------------
# API: Create Reservation
# ----------------------------
@app.post("/api/reservations")
async def create_reservation(payload: dict, db: Session = Depends(get_db)):
    cls = payload.get("cls")
    qty = 1  # single-ticket policy
    if cls not in TICKET_CLASSES:
        raise HTTPException(400, detail="invalid class")

    rid = uuid.uuid4().hex
    expires_at = now_ts() + RESERVATION_TTL_SECONDS
    tb_hold_tickets(cls, qty, rid)

    res = Reservation(
        id=rid, cls=cls, qty=qty,
        created_at=now_ts(),
        expires_at=expires_at,
        status="ACTIVE",
    )
    db.add(res)
    db.commit()
    return {"reservation_id": rid, "cls": cls, "qty": qty, "expires_at": to_iso(expires_at)}


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
async def create_checkout(payload: dict, db: Session = Depends(get_db)):
    reservation_id = payload.get("reservation_id")
    cls = payload.get("cls")
    # Enforce single-ticket policy
    qty = 1
    customer_email = (payload.get("customer_email") or "").strip()

    if not is_valid_email(customer_email):
        raise HTTPException(400, detail="customer_email is required and must be a valid email address")

    if reservation_id:
        res = db.get(Reservation, reservation_id)
        if not res:
            raise HTTPException(404, detail="reservation not found")
        if res.status != "ACTIVE" or res.expires_at <= now_ts():
            res.status = "EXPIRED"
            db.commit()
            tb_rollback_reservation(reservation_id)
            raise HTTPException(409, detail="reservation expired")
        cls = res.cls
        # Even if an older reservation had qty>1, enforce single-ticket policy
        qty = 1
    else:
        if cls not in TICKET_CLASSES:
            raise HTTPException(400, detail="invalid class")
        # Implicit hold for exactly 1 ticket when no reservation provided
        reservation_id = uuid.uuid4().hex
        tb_hold_tickets(cls, qty, reservation_id)
        res = Reservation(
            id=reservation_id,
            cls=cls,
            qty=qty,
            created_at=now_ts(),
            expires_at=now_ts() + RESERVATION_TTL_SECONDS,
            status="ACTIVE",
        )
        db.add(res)
        db.commit()

    amount = TICKET_CLASSES[cls]["price"] * qty
    order_id = uuid.uuid4().hex
    order = Order(
        id=order_id,
        reservation_id=reservation_id,
        cls=cls,
        qty=qty,
        amount=amount,
        currency="eur",
        customer_email=customer_email,
        created_at=now_ts(),
        status="PENDING",
    )
    db.add(order)
    db.commit()

    session = adapter.create_session(db, order, meta={"order_id": order_id})
    order.payment_session_id = session["payment_session_id"]
    db.commit()

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
async def get_order(order_id: str, db: Session = Depends(get_db)):
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(404, detail="order not found")
    tickets = (order.tickets_csv or "").split(",") if order.tickets_csv else []
    goodies = db.get(GoodiesCounter, order.cls)
    return {
        "order_id": order.id,
        "status": order.status,
        "cls": order.cls,
        "qty": order.qty,
        "amount": order.amount,
        "currency": order.currency,
        "paid_at": to_iso(order.paid_at),
        "tickets": tickets,
        "got_goodie": order.got_goodie,
        "goodies_granted_in_class": goodies.granted if goodies else 0,
    }

# ----------------------------
# Webhook endpoint (shared for Mock/Stripe)
# ----------------------------
@app.post("/payments/webhook")
async def payments_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    headers = dict(request.headers)

    event = adapter.verify_webhook(payload, headers)
    kind = adapter.event_kind(event)  # succeeded | failed | canceled
    psid, idem = adapter.event_ids(event)
    if not psid:
        raise HTTPException(400, detail="missing payment_session_id")

    # Event-level idempotency
    if idem:
        if db.get(WebhookEventSeen, idem):
            return {"ok": True, "idempotent": True}
        db.add(WebhookEventSeen(idempotency_key=idem))
        db.commit()

    session = db.get(PaymentSession, psid)
    if not session:
        raise HTTPException(404, detail="payment session not found")
    order = db.get(Order, session.order_id)
    if not order:
        raise HTTPException(404, detail="order not found")

    # Fulfillment idempotency
    fulfill_key = f"{order.id}:{psid}"
    if db.get(FulfillmentKey, fulfill_key):
        return {"ok": True, "idempotent": True}

    if kind == "succeeded":
        # 1) Commit reservation via TigerBeetle
        tb_commit_order(order)

        # 2) Issue tickets (one code per unit)
        tickets = [f"TCK-{uuid.uuid4().hex[:10].upper()}" for _ in range(order.qty)]
        order.tickets_csv = ",".join(tickets)

        # 3) Goodie attempt (first 100 per class) — should be atomic in TB in real code
        user_id = order.customer_email or f"anonymous:{order.id}"
        order.got_goodie = tb_try_grant_goodie(db, order.cls, user_id, order.id)

        # 4) Update order
        order.status = "PAID"
        order.paid_at = now_ts()
        db.add(FulfillmentKey(key=fulfill_key))
        db.commit()

    elif kind in ("failed", "canceled"):
        # Rollback reservation & release hold
        if order.reservation_id:
            tb_rollback_reservation(order.reservation_id)
        order.status = "FAILED" if kind == "failed" else "CANCELED"
        db.add(FulfillmentKey(key=fulfill_key))
        db.commit()

    return {"ok": True, "order_status": order.status}

# ----------------------------
# MockPay UI (simple page with 3 buttons)
# ----------------------------
# MockPay
@app.get("/mockpay/{psid}", response_class=HTMLResponse)
async def mockpay_screen(request: Request, psid: str, db: Session = Depends(get_db)):
    session = db.get(PaymentSession, psid)
    if not session:
        raise HTTPException(404, detail="payment session not found")
    order = db.get(Order, session.order_id)
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
async def mockpay_emit(psid: str, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    kind = form.get("t")  # succeeded|failed|canceled
    if kind not in {"succeeded", "failed", "canceled"}:
        raise HTTPException(400, detail="invalid kind")

    session = db.get(PaymentSession, psid)
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

    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            await client.post(
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
async def demo_success_page(request: Request, order_id: str, db: Session = Depends(get_db)):
    order = db.get(Order, order_id)
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
async def admin_page(request: Request, db: Session = Depends(get_db)):
    if not is_admin(request):
        dest = request.url.path
        return RedirectResponse(url=f"/admin/login?next={dest}", status_code=307)

    rows = db.execute(
        text("SELECT id, status, cls, qty, amount, currency, paid_at, got_goodie FROM orders ORDER BY created_at DESC LIMIT 200")
    ).all()
    orders = []
    for (oid, status, cls, qty, amount, currency, paid_at, got_goodie) in rows:
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
        })
    a = db.get(GoodiesCounter, 'A')
    b = db.get(GoodiesCounter, 'B')
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "orders": orders,
            "goodies_a": (a.granted if a else 0),
            "goodies_b": (b.granted if b else 0),
            "goodie_limit": GOODIE_LIMIT_PER_CLASS,
            "site_name": "TigerFans",
        }
    )

# ----------------------------
# README notes (SQLite performance quick tips)
# ----------------------------
# - SQLite is great for a single-node demo and small/medium traffic. With WAL mode it's fine for many readers and a single writer.
# - Avoid running multiple gunicorn/uvicorn workers writing to the same SQLite file. Prefer a single process with async I/O.
# - If you need higher write concurrency, move to Postgres. Keep the adapter boundary so the migration is easy.
# - Webhooks: keep fulfillment idempotent; DB has FulfillmentKey + WebhookEventSeen as guards.

