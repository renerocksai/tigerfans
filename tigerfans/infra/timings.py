# tigerfans/infra/timings.py
from __future__ import annotations
import gzip
import json
import os
import socket
import time
from typing import Dict, Optional, List
import statistics
from fastapi import FastAPI

import httpx

# ------------ hot path: append only ------------
# one list per kind; no locks, single-threaded event loop
_TIMINGS: Dict[str, List[float]] = {}


def now_ts() -> float:
    # monotonic for durations
    return time.perf_counter()


def record_timing(kind: str, value: float) -> None:
    # absolutely minimal work: append a float to a list
    lst = _TIMINGS.get(kind)
    if lst is None:
        lst = []
        _TIMINGS[kind] = lst
    lst.append(float(value))


class timeit:
    """async usage:
        async with timeit("acct.hold"):
            await fn()
    """
    __slots__ = ("_kind", "_t0")

    def __init__(self, kind: str):
        self._kind = kind
        self._t0 = 0.0

    async def __aenter__(self):
        self._t0 = now_ts()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        record_timing(self._kind, now_ts() - self._t0)


# ------------ stats only at flush ------------

def _mean_std(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    return (
        statistics.mean(values),
        statistics.stdev(values) if len(values) > 1 else 0.0
    )


def _to_ndjson_aggregates() -> bytes:
    # one NDJSON line per kind: {"kind","n","mean","std"}
    lines = []
    for kind, vals in _TIMINGS.items():
        n = len(vals)
        mean, std = _mean_std(vals)
        rec = {"kind": kind, "n": n, "mean": mean, "std": std}
        lines.append(json.dumps(rec, separators=(",", ":")) + "\n")
    return ("".join(lines)).encode("utf-8")


async def flush_to_bench(
    bench_url: str,
    run_id: str,
    worker_id: Optional[str] = None,
    compress: bool = False,
    timeout: float = 10.0,
) -> Dict[str, int]:
    """
    Compute aggregates and POST them to the bench collector.
    Body: NDJSON (gzipped if compress=True)
    Headers: x-run-id, x-worker-id
    Response expected: {"accepted": <int>}
    """
    if not _TIMINGS:
        return {"accepted": 0}

    # compute aggregates *now* (no locks needed; single-threaded event loop)
    raw = _to_ndjson_aggregates()

    worker_id = worker_id or f"{os.getpid()}@{socket.gethostname()}"
    headers = {
        "content-type": "application/x-ndjson",
        "x-run-id": run_id,
        "x-worker-id": worker_id,
    }

    body = gzip.compress(raw) if compress else raw
    if compress:
        headers["content-encoding"] = "gzip"

    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(
            f"{bench_url.rstrip('/')}/v1/metric/flush",
            content=body,
            headers=headers,
        )
        r.raise_for_status()
        ack = r.json()

    # clear after successful send
    _TIMINGS.clear()
    return {"accepted": int(ack.get("accepted", 0))}


def install_shutdown_flush(
    app: FastAPI,
    bench_url_env: str = "BENCH_URL",
    run_id_env: str = "BENCH_RUN_ID",
    fallback_dump_env: str = "BENCH_FALLBACK_DUMP",
):
    """
    Env expected per run:
      BENCH_URL    = http://127.0.0.1:7071
      BENCH_RUN_ID = unique string per repetition (benchctl sets it)
    Optional:
      BENCH_FALLBACK_DUMP = /tmp/timings.ndjson.gz  (if POST fails)
    """
    from fastapi import FastAPI
    if not hasattr(app, "on_event") or not isinstance(app, FastAPI):
        return

    @app.on_event("shutdown")
    async def _flush_on_shutdown():
        bench_url = os.getenv(bench_url_env, "")
        run_id = os.getenv(run_id_env, "")
        print(f"SHUTDOWN with {bench_url_env}={bench_url} and {run_id_env}={run_id}")
        if not bench_url or not run_id:
            return
        try:
            await flush_to_bench(bench_url=bench_url, run_id=run_id)
        except Exception:
            dump = os.getenv(fallback_dump_env, "")
            if not dump:
                return
            try:
                # write the same aggregate NDJSON we would have sent
                raw = _to_ndjson_aggregates()
                with gzip.open(dump, "ab") as f:
                    f.write(raw)
            finally:
                _TIMINGS.clear()
