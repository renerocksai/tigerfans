from abc import ABC, abstractmethod
from sqlalchemy.ext.asyncio import AsyncSession
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
    def create_session(
            self, db: AsyncSession, order: Order, meta: dict
    ) -> CreateSessionResult: ...

    # this is to take a shortcut and commit in the endpoint
    @abstractmethod
    def create_session_id_and_url(self) -> CreateSessionResult: ...

    @abstractmethod
    def verify_webhook(self, payload: bytes, headers: dict) -> dict: ...

    # "succeeded" | "failed" | "canceled"
    @abstractmethod
    def event_kind(self, event: dict) -> str:
        ...

    # (payment_session_id, idempotency_key)
    @abstractmethod
    def event_ids(self, event: dict) -> Tuple[str, Optional[str]]:
        ...


# ----------------------------
# MockPay implementation
# ----------------------------
class MockPay(PaymentAdapter):

    # we don't use that anymore. to save one DB commit, create the session in
    # the endpoint
    async def create_session(
            self, db: AsyncSession, order: Order, meta: dict
    ) -> CreateSessionResult:
        psid = f"mock_{uuid.uuid4().hex}"
        db.add(PaymentSession(
            id=psid,
            order_id=order.id,
            amount=order.amount,
            currency=order.currency,
            created_at=now_ts(),
        ))
        await db.commit()
        redirect_url = f"/mockpay/{psid}"
        return {"payment_session_id": psid, "redirect_url": redirect_url}

    # we use that instead
    def create_session_id_and_url(self) -> CreateSessionResult:
        psid = f"mock_{uuid.uuid4().hex}"
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
        return (
                event.get("payment_session_id", ""),
                event.get("idempotency_key")
        )
