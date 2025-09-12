#!/usr/bin/env python3
"""
TigerFans load client (async)

Simulates the browser flow against the server:
  1) POST /api/checkout  (class, email) -> {order_id, redirect_url}
  2) Extract psid from redirect_url (/mockpay/{psid})
  3) POST /mockpay/{psid}/emit  (t=succeeded|failed|canceled)
     - follows 303 to /demo/success?order_id=...
  4) Poll GET /api/orders/{order_id} until status != PENDING (or timeout)

It records timings per order and prints an aggregate report.

Usage:
  python load_client.py --base http://localhost:8000 \
                        --total 200 --concurrency 50

  python load_client.py --base https://your.domain \
                        --total 100 --concurrency 20 --fail-rate 0.1

Notes:
- This targets the MockPay flow.
- Keep server workers=1 with SQLite to avoid lock contention artifacts.
"""

import asyncio
import random
import string
import time
import argparse
from dataclasses import dataclass, field
from typing import Optional, List, Dict

import httpx


def _rand_email() -> str:
    name = ''.join(
        random.choices(string.ascii_lowercase + string.digits, k=10)
    )
    return f"{name}@example.com"


@dataclass
class Result:
    ok: bool
    cls: str
    outcome: str  # PAID/FAILED/CANCELED/TIMEOUT/ERROR
    t_checkout: float = 0.0
    t_emit: float = 0.0
    t_observed: float = 0.0  # time until non-PENDING observed
    err: Optional[str] = None


@dataclass
class Stats:
    results: List[Result] = field(default_factory=list)

    def add(self, r: Result):
        self.results.append(r)

    def summary(self) -> Dict[str, float]:
        done = [
            r for r in self.results
            if r.outcome in ("PAID", "FAILED", "CANCELED")
        ]
        paid = [r for r in self.results if r.outcome == "PAID"]
        lat = [r.t_observed for r in done if r.t_observed > 0]

        def pct(p):
            if not lat:
                return 0.0
            x = sorted(lat)
            k = int(max(0, min(len(x)-1, round(p/100*(len(x)-1)))))
            return x[k]
        return {
            "total": len(self.results),
            "ok": sum(1 for r in self.results if r.ok),
            "paid": len(paid),
            "failed": sum(1 for r in self.results if r.outcome == "FAILED"),
            "canceled": sum(
                1 for r in self.results if r.outcome == "CANCELED"
            ),
            "timeout": sum(1 for r in self.results if r.outcome == "TIMEOUT"),
            "error": sum(1 for r in self.results if r.outcome == "ERROR"),
            "p50_s": pct(50),
            "p90_s": pct(90),
            "p99_s": pct(99),
            "avg_s": (sum(lat)/len(lat)) if lat else 0.0,
        }

    def print(self, elapsed_s: float):
        s = self.summary()
        print("\n=== Load Summary ===")
        print(
            f"Total: {int(s['total'])}   OK: {int(s['ok'])}   "
            f"PAID: {int(s['paid'])}   FAILED: {int(s['failed'])}   "
            f"CANCELED: {int(s['canceled'])}   TIMEOUT: {int(s['timeout'])}"
            f"   ERROR: {int(s['error'])}"
        )
        print(
            f"Latency (observed order resolution): "
            f"avg {s['avg_s']:.3f}s   p50 {s['p50_s']:.3f}s   "
            f"p90 {s['p90_s']:.3f}s   p99 {s['p99_s']:.3f}s"
        )
        print(
            f"Wall time: {elapsed_s:.3f}s   "
            f"Throughput: {s['total']/elapsed_s:.1f} ops/s"
        )


async def one_order(
    client: httpx.AsyncClient,
    base: str,
    cls_choice: str,
    emit_kind: str,
    poll_interval_s: float,
    poll_timeout_s: float,
) -> Result:
    r = Result(ok=False, cls=cls_choice, outcome="ERROR")
    email = _rand_email()

    # 1) checkout
    t0 = time.perf_counter()
    try:
        resp = await client.post(
            f"{base}/api/checkout",
            json={"cls": cls_choice, "customer_email": email},
            timeout=30.0,
        )
        resp.raise_for_status()
        j = resp.json()
        order_id = j["order_id"]
        redirect_url = j["redirect_url"]
    except Exception as e:
        r.err = f"checkout: {e}"
        return r
    r.t_checkout = time.perf_counter() - t0

    # 2) extract psid
    # redirect_url is like "/mockpay/{psid}"
    try:
        parts = redirect_url.strip("/").split("/")
        psid = parts[1] if len(parts) >= 2 else None
        if not psid:
            r.err = f"bad redirect_url: {redirect_url}"
            return r
    except Exception as e:
        r.err = f"parse redirect: {e}"
        return r

    # 3) emit outcome (simulate clicking the button on the MockPay page)
    t1 = time.perf_counter()
    try:
        # POST form: t=succeeded|failed|canceled, follow 303
        resp = await client.post(
            f"{base}/mockpay/{psid}/emit",
            data={"t": emit_kind},
            follow_redirects=True,
            timeout=30.0,
        )
        if resp.status_code >= 400:
            r.err = f"emit HTTP {resp.status_code}"
            return r
    except Exception as e:
        r.err = f"emit: {e}"
        return r
    r.t_emit = time.perf_counter() - t1

    # 4) poll order status until non-PENDING or timeout
    t2 = time.perf_counter()
    deadline = t2 + poll_timeout_s
    status = "PENDING"
    try:
        while time.perf_counter() < deadline:
            g = await client.get(f"{base}/api/orders/{order_id}", timeout=10.0)
            if g.status_code != 200:
                await asyncio.sleep(poll_interval_s)
                continue
            jo = g.json()
            status = jo.get("status", status)
            if status in ("PAID", "FAILED", "CANCELED"):
                break
            await asyncio.sleep(poll_interval_s)
    except Exception as e:
        r.err = f"poll: {e}"
        return r

    r.t_observed = time.perf_counter() - t2
    r.ok = True
    if status in ("PAID", "FAILED", "CANCELED"):
        r.outcome = status
    else:
        r.outcome = "TIMEOUT"
    return r


async def run_load(
    base: str,
    total: int,
    concurrency: int,
    cls_ratio_a: float,
    fail_rate: float,
    cancel_rate: float,
    poll_interval_s: float,
    poll_timeout_s: float,
    http2: bool,
) -> Stats:
    sem = asyncio.Semaphore(concurrency)
    stats = Stats()

    limits = httpx.Limits(
        max_keepalive_connections=concurrency, max_connections=concurrency
    )
    async with httpx.AsyncClient(
        limits=limits, http2=http2, headers={"User-Agent": "TigerFansLoad/1.0"}
    ) as client:

        async def worker(n: int):
            async with sem:
                cls_choice = "A" if random.random() < cls_ratio_a else "B"
                # choose outcome
                rnd = random.random()
                if rnd < fail_rate:
                    emit_kind = "failed"
                elif rnd < fail_rate + cancel_rate:
                    emit_kind = "canceled"
                else:
                    emit_kind = "succeeded"

                res = await one_order(
                    client, base, cls_choice, emit_kind,
                    poll_interval_s, poll_timeout_s
                )
                stats.add(res)

        tasks = [asyncio.create_task(worker(i)) for i in range(total)]
        await asyncio.gather(*tasks)

    return stats


def main():
    ap = argparse.ArgumentParser(description="TigerFans load client")
    ap.add_argument("--base", default="http://localhost:8000",
                    help="Base URL of the app")
    ap.add_argument("--total", type=int, default=100,
                    help="Total orders to run")
    ap.add_argument("--concurrency", type=int, default=20,
                    help="Concurrent workers")
    ap.add_argument("--ratio-a", type=float, default=0.5,
                    help="Probability of class A (0..1)")
    ap.add_argument("--fail-rate", type=float, default=0.0,
                    help="Fraction of orders to mark as failed")
    ap.add_argument("--cancel-rate", type=float, default=0.0,
                    help="Fraction of orders to mark as canceled")
    ap.add_argument("--poll-interval", type=float, default=0.05,
                    help="Seconds between status polls")
    ap.add_argument("--poll-timeout", type=float, default=10.0,
                    help="Max seconds to wait for non-PENDING")
    ap.add_argument("--http2", action="store_true",
                    help="Enable HTTP/2 if server supports it")
    args = ap.parse_args()

    if args.fail_rate + args.cancel_rate > 0.95:
        print(
            "Warning: combined fail+cancel rate is very high; "
            "few PAID outcomes will occur."
        )

    t_start = time.perf_counter()
    stats = asyncio.run(run_load(
        base=args.base,
        total=args.total,
        concurrency=args.concurrency,
        cls_ratio_a=args.ratio_a,
        fail_rate=args.fail_rate,
        cancel_rate=args.cancel_rate,
        poll_interval_s=args.poll_interval,
        poll_timeout_s=args.poll_timeout,
        http2=args.http2,
    ))
    elapsed = time.perf_counter() - t_start
    stats.print(elapsed)


if __name__ == "__main__":
    main()
