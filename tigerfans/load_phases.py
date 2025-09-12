#!/usr/bin/env python3
import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import random
import re
import time
from dataclasses import dataclass, asdict
from typing import List, Optional

import httpx

PSID_RE = re.compile(r"/mockpay/([^/?#]+)")


@dataclass
class CheckoutResult:
    order_id: str
    psid: str
    cls: str
    amount: int
    currency: str


def rand_email(i: int) -> str:
    return f"user{i}-{random.randrange(1_000_000)}@example.com"


def pick_class(i: int) -> str:
    return "A" if (i % 2 == 0) else "B"


async def phase_checkout(
        base: str, total: int, conc: int
) -> List[CheckoutResult]:
    results: List[CheckoutResult] = []
    errors = 0
    limits = httpx.Limits(max_connections=conc, max_keepalive_connections=conc)
    async with httpx.AsyncClient(
        base_url=base, timeout=10.0, limits=limits
    ) as client:
        sem = asyncio.Semaphore(conc)

        async def one(i: int):
            nonlocal errors
            async with sem:
                payload = {
                    "cls": pick_class(i),
                    "customer_email": rand_email(i)
                }
                try:
                    r = await client.post("/api/checkout", json=payload)
                    r.raise_for_status()
                    j = r.json()
                    order_id = j["order_id"]
                    redirect_url = j["redirect_url"]
                    m = PSID_RE.search(redirect_url or "")
                    if not m:
                        errors += 1
                        return
                    psid = m.group(1)
                    results.append(
                        CheckoutResult(
                            order_id=order_id,
                            psid=psid,
                            cls=payload["cls"],
                            amount=j["amount"],
                            currency=j["currency"],
                        )
                    )
                except Exception:
                    errors += 1

        t0 = time.perf_counter()
        await asyncio.gather(*(one(i) for i in range(total)))
        dt = time.perf_counter() - t0

    ok = len(results)
    print("\n=== Phase 1: Checkout / Reservations ===")
    print(f"Total: {total}   OK: {ok}   ERROR: {errors}")
    print(f"Wall time: {dt:.3f}s   Throughput: {ok/dt:.1f} ops/s")
    return results


def sign_mockpay(secret: str, payload_bytes: bytes) -> str:
    mac = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).digest()
    return base64.b64encode(mac).decode()


async def phase_reservation(
        base: str, total: int, concurrency: int
) -> List[CheckoutResult]:
    """
    Phase 1: create holds/orders via POST /api/checkout.
    Returns list of (order_id, psid, cls, amount, currency).
    """
    results: List[CheckoutResult] = []
    errors = 0

    async with httpx.AsyncClient(
        base_url=base, timeout=10.0,
        limits=httpx.Limits(
            max_connections=concurrency,
            max_keepalive_connections=concurrency
        )
    ) as client:
        async def one(i: int):
            nonlocal errors
            payload = {"cls": pick_class(i), "customer_email": rand_email(i)}
            try:
                r = await client.post("/api/checkout", json=payload)
                r.raise_for_status()
                j = r.json()
                order_id = j["order_id"]
                redirect_url = j["redirect_url"]
                m = PSID_RE.search(redirect_url or "")
                if not m:
                    errors += 1
                    return
                psid = m.group(1)
                results.append(
                    CheckoutResult(
                        order_id=order_id,
                        psid=psid,
                        cls=payload["cls"],
                        amount=j["amount"],
                        currency=j["currency"],
                    )
                )
            except Exception:
                errors += 1

        t0 = time.perf_counter()
        # batch in chunks of `concurrency`
        for start in range(0, total, concurrency):
            batch = [
                one(i) for i in range(
                    start, min(start + concurrency, total)
                )
            ]
            await asyncio.gather(*batch)
        dt = time.perf_counter() - t0

    ok = len(results)
    print("\n=== Phase 1: Reservations (/api/checkout) ===")
    print(f"Total: {total}   OK: {ok}   ERROR: {errors}")
    print(f"Wall time: {dt:.3f}s   Throughput: {ok/dt:.1f} ops/s")
    return results


async def phase_webhook(
    base: str,
    sessions: List[CheckoutResult],
    concurrency: int,
    mode: str,                # "emit" or "direct"
    succeed_rate: float,      # 0.0..1.0
    mock_webhook_url: Optional[str],
    mock_secret: Optional[str],
):
    """
    Phase 2: confirm payments.
      - mode="emit": POST /mockpay/{psid}/emit (realistic, triggers webhook)
      - mode="direct": POST /payments/webhook with signed JSON (single hop)
    """
    assert mode in ("emit", "direct")
    if mode == "direct" and (not mock_webhook_url or not mock_secret):
        raise SystemExit(
            "--webhook-url and --secret are required for --webhook-mode direct"
        )

    ok = 0
    errors = 0

    async with httpx.AsyncClient(
        base_url=base, timeout=10.0, follow_redirects=False,
        limits=httpx.Limits(
            max_connections=concurrency,
            max_keepalive_connections=concurrency
        )
    ) as client:

        async def one(res: CheckoutResult):
            nonlocal ok, errors
            kind = "succeeded" if (random.random() < succeed_rate) else "failed"
            try:
                if mode == "emit":
                    r = await client.post(
                        f"/mockpay/{res.psid}/emit",
                        data={"t": kind}
                    )
                    if r.status_code in (200, 303):
                        ok += 1
                    else:
                        errors += 1
                else:
                    event = {
                        "type": f"payment.{kind}",
                        "payment_session_id": res.psid,
                        "order_id": res.order_id,
                        "amount": res.amount,
                        "currency": res.currency,
                        "created_at": int(time.time()),
                        "idempotency_key":
                            f"evt_{res.psid}_{random.randrange(1_000_000)}",
                    }
                    payload = json.dumps(event).encode()
                    sig = sign_mockpay(mock_secret, payload)
                    r = await client.post(
                        mock_webhook_url,
                        content=payload,
                        headers={
                            "x-mockpay-signature": sig,
                            "content-type": "application/json"
                        },
                    )
                    if r.status_code == 200:
                        ok += 1
                    else:
                        errors += 1
            except Exception:
                errors += 1

        t0 = time.perf_counter()
        # batch in chunks of `concurrency`
        total = len(sessions)
        for start in range(0, total, concurrency):
            batch = [one(s) for s in sessions[start:start + concurrency]]
            await asyncio.gather(*batch)
        dt = time.perf_counter() - t0

    print("\n=== Phase 2: Payment Confirmations / Webhooks ===")
    print(f"Mode: {mode}")
    print(f"Total: {len(sessions)}   OK: {ok}   ERROR: {errors}")
    print(f"Wall time: {dt:.3f}s   Throughput: {ok/dt:.1f} ops/s")


def save_sessions(path: str, sessions: List[CheckoutResult]) -> None:
    with open(path, "w") as f:
        json.dump([asdict(s) for s in sessions], f)


def load_sessions(path: str) -> List[CheckoutResult]:
    with open(path, "r") as f:
        data = json.load(f)
    return [CheckoutResult(**d) for d in data]


async def main():
    ap = argparse.ArgumentParser(
        description="TigerFans two-phase load tester (reservations + webhooks)"
    )
    ap.add_argument("--base", default="http://localhost:8000",
                    help="Base URL of the app")
    ap.add_argument("--total", type=int, default=1000,
                    help="Total operations per phase")
    ap.add_argument("--concurrency", type=int, default=30,
                    help="Concurrent clients")
    ap.add_argument("--phase", choices=["both", "checkout", "webhook"],
                    default="both", help='Which phase to run (default: both)')
    ap.add_argument("--succeed-rate", type=float, default=1.0,
                    help="Payment success probability for phase 2")
    ap.add_argument("--webhook-mode", choices=["emit", "direct"],
                    default="direct",
                    help="Which webhook mode (default: direct). "
                    "Emit calls the server's emit webhook endpoint, "
                    "direct calls the webhook endpoint directly")
    ap.add_argument("--webhook-url",
                    default="http://localhost:8000/payments/webhook",
                    help="Direct webhook URL (for --webhook-mode direct)")
    ap.add_argument("--secret", default="supersecret",
                    help="MockPay signing secret (for --webhook-mode direct)")
    ap.add_argument("--save",
                    help="Save sessions to JSON after checkout phase")
    ap.add_argument("--load", help="Load sessions JSON (skip checkout)")
    args = ap.parse_args()

    sessions: List[CheckoutResult] = []

    if args.phase in ("both", "checkout") and not args.load:
        sessions = await phase_checkout(
            args.base, args.total, args.concurrency
        )
        if args.save:
            save_sessions(args.save, sessions)

    if args.phase in ("both", "webhook"):
        if args.load and not sessions:
            sessions = load_sessions(args.load)
        if not sessions:
            print("No sessions available. Run checkout phase or "
                  "provide --load file.")
            return
        await phase_webhook(
            base=args.base,
            sessions=sessions,
            concurrency=args.concurrency,
            mode=args.webhook_mode,
            succeed_rate=args.succeed_rate,
            mock_webhook_url=args.webhook_url,
            mock_secret=args.secret,
        )

if __name__ == "__main__":
    asyncio.run(main())
