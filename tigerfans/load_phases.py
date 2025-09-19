#!/usr/bin/env python3
import os
import sys
import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import random
import re
import time
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from typing import List, Optional, Tuple
import traceback

import httpx


def now_ts() -> float:
    return time.time()


def to_iso(ts: float | None) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


PSID_RE = re.compile(r"/mockpay/([^/?#]+)")


@dataclass
class CheckoutResult:
    order_id: str
    psid: str
    cls: str
    amount: int
    currency: str


@dataclass
class AccountingResult:
    tb_transfer_id: str
    goodie_tb_transfer_id: str


@dataclass
class Summary:
    total: int = 0
    ok: int = 0
    errors: int = 0
    wall_time: float = 0.0
    throughput: float = 0.0


def write_csv(
    csv: str, tag: str, concurrency: int, webhook_mode: str, succeed_rate: float,
    checkout_summary: Summary, webhook_summary: Summary,
    reserve_summary, commit_summary,
    accounting_backend: str, paymentsessions_backend: str,
    db_pool_size: int, redis_max_conn: int,
):
    write_header = not os.path.exists(csv)
    now = now_ts()
    timestamp = to_iso(now)

    with open(csv, 'at') as f:
        if write_header:
            f.write(
                'timestamp,tag,phase,accounting,payments,concurrency,'
                'db_pool_size,redis_max_conn,'
                'webhook_mode,'
                'arg_succeed_rate,total,ok,errors,walltime,throughput'
                '\n'
            )
        if checkout_summary:
            f.write(
                f'{timestamp},{tag},checkout,{accounting_backend},'
                f'{paymentsessions_backend},{concurrency},'
                f'{db_pool_size},{redis_max_conn},'
                f'{webhook_mode},'
                f'{succeed_rate},{checkout_summary.total},'
                f'{checkout_summary.ok},{checkout_summary.errors},'
                f'{checkout_summary.wall_time},{checkout_summary.throughput}'
                '\n'
            )
        if webhook_summary:
            f.write(
                f'{timestamp},{tag},webhook,{accounting_backend},'
                f'{paymentsessions_backend},{concurrency},'
                f'{db_pool_size},{redis_max_conn},'
                f'{webhook_mode},'
                f'{succeed_rate},{webhook_summary.total},'
                f'{webhook_summary.ok},{webhook_summary.errors},'
                f'{webhook_summary.wall_time},{webhook_summary.throughput}'
                '\n'
            )
        if reserve_summary:
            f.write(
                f'{timestamp},{tag},reserve,{accounting_backend},'
                f'{paymentsessions_backend},{concurrency},'
                f'{db_pool_size},{redis_max_conn},'
                f'{webhook_mode},'
                f'{succeed_rate},{reserve_summary.total},'
                f'{reserve_summary.ok},{reserve_summary.errors},'
                f'{reserve_summary.wall_time},{reserve_summary.throughput}'
                '\n'
            )
        if commit_summary:
            f.write(
                f'{timestamp},{tag},commit,{accounting_backend},'
                f'{paymentsessions_backend},{concurrency},'
                f'{db_pool_size},{redis_max_conn},'
                f'{webhook_mode},'
                f'{succeed_rate},{commit_summary.total},'
                f'{commit_summary.ok},{commit_summary.errors},'
                f'{commit_summary.wall_time},{commit_summary.throughput}'
                '\n'
            )


def rand_email(i: int) -> str:
    return f"user{i}-{random.randrange(1_000_000)}@example.com"


def pick_class(i: int) -> str:
    return "A" if (i % 2 == 0) else "B"


async def phase_checkout(
        base: str, total: int, conc: int
) -> Tuple[List[CheckoutResult], Summary]:
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

    summary = Summary(
        total=total,
        ok=ok,
        errors=errors,
        wall_time=dt,
        throughput=ok/dt,
    )
    return results, summary


def sign_mockpay(secret: str, payload_bytes: bytes) -> str:
    mac = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).digest()
    return base64.b64encode(mac).decode()


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
            kind = (
                    "succeeded" if (random.random() < succeed_rate)
                    else "failed"
            )
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
    summary = Summary(
        total=len(sessions),
        ok=ok,
        errors=errors,
        wall_time=dt,
        throughput=ok/dt,
    )
    return sessions, summary


async def phase_accounting_reserve(
        base: str, total: int, conc: int
) -> Tuple[List[CheckoutResult], Summary]:
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
                try:
                    r = await client.get("/api/accounting/reserve")
                    r.raise_for_status()
                    j = r.json()
                    tb_transfer_id = j["tb_transfer_id"]
                    goodie_tb_transfer_id = j["goodie_tb_transfer_id"]
                    results.append(
                        AccountingResult(
                            tb_transfer_id=tb_transfer_id,
                            goodie_tb_transfer_id=goodie_tb_transfer_id
                        )
                    )
                except Exception:
                    errors += 1

        t0 = time.perf_counter()
        await asyncio.gather(*(one(i) for i in range(total)))
        dt = time.perf_counter() - t0

    ok = len(results)
    print("\n=== Phase 3: Accounting / Reservations ===")
    print(f"Total: {total}   OK: {ok}   ERROR: {errors}")
    print(f"Wall time: {dt:.3f}s   Throughput: {ok/dt:.1f} ops/s")

    summary = Summary(
        total=total,
        ok=ok,
        errors=errors,
        wall_time=dt,
        throughput=ok/dt,
    )
    return results, summary


async def phase_accounting_commit(
        base: str, sessions: List[AccountingResult], conc: int,
) -> Tuple[List[AccountingResult], Summary]:
    results: List[CheckoutResult] = []
    errors = 0
    limits = httpx.Limits(max_connections=conc, max_keepalive_connections=conc)
    async with httpx.AsyncClient(
        base_url=base, timeout=10.0, limits=limits
    ) as client:
        sem = asyncio.Semaphore(conc)

        async def one(sess: AccountingResult):
            nonlocal errors
            async with sem:
                try:
                    r = await client.get(
                        "/api/accounting/commit",
                        params={
                            'tb_transfer_id': sess.tb_transfer_id,
                            'goodie_tb_transfer_id': sess.goodie_tb_transfer_id,
                        }
                    )
                    r.raise_for_status()
                    j = r.json()
                    tb_transfer_id = j["tb_transfer_id"]
                    goodie_tb_transfer_id = j["goodie_tb_transfer_id"]
                    results.append(
                        AccountingResult(
                            tb_transfer_id=tb_transfer_id,
                            goodie_tb_transfer_id=goodie_tb_transfer_id
                        )
                    )
                except Exception:
                    errors += 1
                    # traceback.print_exc(file=sys.stderr)   # full stacktrace

        total = len(sessions)
        t0 = time.perf_counter()
        for start in range(0, total, conc):
            batch = [one(s) for s in sessions[start:start + conc]]
            await asyncio.gather(*batch)
        dt = time.perf_counter() - t0

    ok = len(results)
    print("\n=== Phase 4: Accounting / Commits ===")
    print(f"Total: {total}   OK: {ok}   ERROR: {errors}")
    print(f"Wall time: {dt:.3f}s   Throughput: {ok/dt:.1f} ops/s")

    summary = Summary(
        total=total,
        ok=ok,
        errors=errors,
        wall_time=dt,
        throughput=ok/dt,
    )
    return results, summary


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
    ap.add_argument("--phase",
                    choices=[
                        "all", "checkout", "webhook", "reserve", "commit"
                    ],
                    default="all", help='Which phase to run (default: all)')
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
    ap.add_argument("--csv", help="write results to csv")
    ap.add_argument("--tag", default='',
                    help="tag to write into csv's tag column")
    ap.add_argument("--accounting", choices=['pg', 'tb'], required=True,
                    help="accounting backend used (pg|tb)")
    ap.add_argument("--payments", choices=['pg', 'redis'], required=True,
                    help="accounting backend used (pg|redis)")
    ap.add_argument("--redis-max-conn", type=int, required=True,
                    help="redis max conn used in server")
    ap.add_argument("--db-pool-size", type=int, required=True,
                    help="db pool size used in server")

    args = ap.parse_args()

    sessions: List[CheckoutResult] = []
    accounting_sessions: List[AccountingResult] = []

    if args.phase in ("all", "checkout") and not args.load:
        sessions, checkout_summary = await phase_checkout(
            args.base, args.total, args.concurrency
        )
        if args.save:
            save_sessions(args.save, sessions)

    webhook_summary = None
    if args.phase in ("all", "webhook"):
        if args.load and not sessions:
            sessions = load_sessions(args.load)
        checkouts, webhook_summary = await phase_webhook(
            base=args.base,
            sessions=sessions,
            concurrency=args.concurrency,
            mode=args.webhook_mode,
            succeed_rate=args.succeed_rate,
            mock_webhook_url=args.webhook_url,
            mock_secret=args.secret,
        )
        # print it after trying for nicer output in logs
        if not sessions:
            print("No sessions available. Run checkout phase or "
                  "provide --load file.")

    if args.phase in ("all", "reserve"):
        # print it after trying for nicer output in logs
        accounting_sessions, reserve_summary = await phase_accounting_reserve(
            args.base, args.total, args.concurrency
        )

    commit_summary = None
    if args.phase in ("all", "commit"):
        if accounting_sessions:
            _, commit_summary = await phase_accounting_commit(
                base=args.base,
                sessions=accounting_sessions,
                conc=args.concurrency,
            )
        else:
            print("No accounting sessions available. Run reserve phase, too")

    if args.csv:
        write_csv(
            args.csv, args.tag, args.concurrency, args.webhook_mode,
            args.succeed_rate,
            checkout_summary, webhook_summary,
            reserve_summary, commit_summary,
            args.accounting, args.payments,
            args.db_pool_size, args.redis_max_conn,
        )

if __name__ == "__main__":
    asyncio.run(main())
