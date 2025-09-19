# statscollector.py
from __future__ import annotations
import json
import gzip
import threading
import time
from typing import Dict, List, Iterable

import httpx
import uvicorn
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import JSONResponse


def start_collector_in_thread(host: str, port: int):
    """
    Starts a minimal FastAPI+uvicorn collector in a background thread.

    POST /v1/metric/flush
      - Body: NDJSON (optionally gzipped). Each line is a JSON object.
      - Headers:
          x-run-id     (required)
          x-worker-id  (optional; stored with each row)
          content-encoding: gzip (optional)
      - Behavior: Append lines verbatim under RUNS[run_id].
      - Response: {"accepted": N}

    GET /v1/metric/kind_dump?run_id=ALL|r1&run_id=r2&clear=0|1
      - Returns {"runs": {"r1":[...], "r2":[...], ...}}
      - If clear=1, the returned runs are removed atomically from memory.

    GET /health
    POST /__shutdown  (internal; used by stop() below)
    """
    app = FastAPI()
    lock = threading.Lock()

    # In-memory store:
    # RUNS[run_id] = [ { "run_id":..., "worker_id":..., <raw fields> }, ... ]
    RUNS: Dict[str, List[dict]] = {}

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post("/v1/metric/flush")
    async def flush(request: Request):
        run_id = request.headers.get("x-run-id")
        if not run_id:
            raise HTTPException(
                status_code=400,
                detail="missing x-run-id header"
            )
        worker_id = request.headers.get("x-worker-id") or "unknown-worker"

        raw = await request.body()
        if request.headers.get("content-encoding", "").lower() == "gzip":
            try:
                raw = gzip.decompress(raw)
            except Exception:
                raise HTTPException(status_code=400, detail="bad gzip body")

        try:
            text = raw.decode("utf-8")
        except Exception:
            raise HTTPException(
                status_code=400,
                detail="invalid body encoding"
            )

        to_add: List[dict] = []
        for ln in text.splitlines():
            s = ln.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception:
                raise HTTPException(
                    status_code=400,
                    detail=f"invalid JSON line: {s[:120]}"
                )
            obj["run_id"] = run_id
            obj["worker_id"] = worker_id
            to_add.append(obj)

        if not to_add:
            return JSONResponse({"accepted": 0})

        with lock:
            RUNS.setdefault(run_id, []).extend(to_add)
            accepted = len(to_add)

        return JSONResponse({"accepted": accepted})

    @app.get("/v1/metric/kind_dump")
    async def kind_dump(
        run_id: List[str] = Query(default=["ALL"]),
        clear: int = Query(default=0),
    ):
        """
        Returns all stored lines for one or many runs in one shot.

        Examples:
          /v1/metric/kind_dump?run_id=ALL
          /v1/metric/kind_dump?run_id=run-001&run_id=run-002
          /v1/metric/kind_dump?run_id=run-003&clear=1
        """
        with lock:
            if len(run_id) == 1 and run_id[0] == "ALL":
                selected_ids = list(RUNS.keys())
            else:
                selected_ids = [r for r in run_id if r in RUNS]

            out: Dict[str, List[dict]] = {}
            for rid in selected_ids:
                out[rid] = list(RUNS.get(rid, []))  # copy

            if clear and selected_ids:
                for rid in selected_ids:
                    RUNS.pop(rid, None)

        if not out:
            return JSONResponse({"runs": {}}, status_code=200)

        return {"runs": out}

    @app.post("/__shutdown")
    async def _shutdown():
        return {"ok": True}

    # --- run in thread ---
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)

    def _run():
        server.run()

    t = threading.Thread(target=_run, name="collector", daemon=True)
    t.start()

    # Wait until server is ready
    base = f"http://{host}:{port}"
    for _ in range(50):
        try:
            with httpx.Client(timeout=0.8) as c:
                r = c.get(f"{base}/health")
                if r.status_code == 200:
                    break
        except Exception:
            time.sleep(0.1)

    def stop():
        server.should_exit = True
        try:
            with httpx.Client(timeout=1.0) as c:
                c.post(f"{base}/__shutdown")
        except Exception:
            pass
        time.sleep(0.2)

    return base, stop
