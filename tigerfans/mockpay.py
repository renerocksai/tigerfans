from abc import ABC, abstractmethod
from sqlalchemy.orm import Session
from typing import Optional, Tuple, TypedDict
from fastapi import HTTPException
import os
import uuid
import hmac
import hashlib
import base64
import json
from .model.db import PaymentSession, Order
from .helpers import now_ts

MOCK_SECRET = os.environ.get("MOCK_SECRET", "supersecret")


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
