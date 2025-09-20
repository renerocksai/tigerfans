#!/usr/bin/env python3
import argparse
import os
import sys
import signal
import time
from datetime import datetime, timezone
import subprocess
from typing import Dict, List, Tuple, Optional

# for collecting stats
import httpx
from .statscollector import start_collector_in_thread
from pathlib import Path
import csv

import traceback

# mutable server process container for access from global signal handlers
# mutable holder so the handler always sees latest proc
current = {'proc': None}


# global signal hander
def _handle_sig(signum, frame):
    print("\n==> Caught signal; terminating server...", flush=True)
    terminate_server(current["proc"])
    sys.exit(128 + signum)


# install signal handlers
if os.name == "posix":
    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)


def now_ts() -> float:
    return time.time()


def human_iso(ts: float | None) -> Optional[str]:
    if ts is None:
        return None
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        .replace('T', ' ')[:len('2025-09-17 11:58:00')]
    )


# ----- simple .env reader (KEY=VALUE, ignore blanks and # comment lines)
def read_env_file(path: str) -> Dict[str, str]:
    env = {}
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def env_to_args(
    env: Dict, env_file: str, csv_file: str
) -> Tuple[Dict, List[str]]:
    """
    We abuse the env file to also put in --arg=value settings meant for the
    load tester.
    In here, we split into the real env vars and the args
    """
    env_dict = {}
    args_dict = {}
    args = []

    # set or overwrite cmdline args for load test
    for k, v in env.items():
        if k.startswith('--'):
            args_dict[k] = v
        else:
            env_dict[k] = v

        if k == 'ACCT_BACKEND':
            args_dict['--accounting'] = v
        if k == 'PAYSESSION_BACKEND':
            args_dict['--payments'] = v
        if k == 'REDIS_MAX_CONN':
            args_dict['--redis-max-conn'] = v
        if k == 'DB_POOL_SIZE':
            args_dict['--db-pool-size'] = v

    args_dict['--tag'] = env_file
    args_dict['--csv'] = csv_file

    for k, v in args_dict.items():
        args.append(f'{k}={v}')

    return env_dict, args, args_dict


def start_server(
        cmd: list[str], extra_env: Dict[str, str]
) -> subprocess.Popen:
    env = os.environ.copy()
    env.update(extra_env)

    # Inherit stdout/stderr so server logs stream directly to the terminal
    # Start in its own process group/session so we can kill the whole tree
    # later
    if os.name == "posix":
        p = subprocess.Popen(
            cmd,
            env=env,
            stdout=None,
            stderr=None,
            stdin=None,
            preexec_fn=os.setsid,  # new session (POSIX)
        )
    else:
        # Windows: CREATE_NEW_PROCESS_GROUP lets us send CTRL_BREAK_EVENT
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        p = subprocess.Popen(
            cmd,
            env=env,
            stdout=None,
            stderr=None,
            stdin=None,
            creationflags=CREATE_NEW_PROCESS_GROUP,
        )
    return p


def terminate_server(
        proc: subprocess.Popen, grace_seconds: float = 5.0
) -> None:
    if proc.poll() is not None:
        return  # already exited

    try:
        if os.name == "posix":
            # send SIGTERM to the whole process group
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            # Windows: politely send CTRL_BREAK_EVENT to the group
            proc.send_signal(signal.CTRL_BREAK_EVENT)
    except Exception:
        # fall back to terminate
        proc.terminate()

    t0 = time.time()
    while proc.poll() is None and (time.time() - t0) < grace_seconds:
        time.sleep(0.1)

    if proc.poll() is None:
        # hard kill
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except Exception:
            pass


def run_load(cmd: list[str], extra_env: Dict[str, str]) -> int:
    env = os.environ.copy()
    env.update(extra_env)
    # Inherit stdout/stderr so we see the load test output live
    return subprocess.call(cmd, env=env)


def orchestrate_repeated(
    server_cmd, load_cmd, extra_env, repeats: int, warmup_s: int,
    cooldown_s: int, bench_url: str, run_id_maker
):
    rc = 0
    used_run_ids = []

    for repetition in range(repeats):
        print(f"\n=== Repetition {repetition+1}/{repeats} ===")
        run_id = run_id_maker(repetition)

        env_with_bench = dict(extra_env)
        env_with_bench["BENCH_URL"] = bench_url
        env_with_bench["BENCH_RUN_ID"] = run_id

        server: Optional[subprocess.Popen] = None
        try:
            print(f"==> Starting server: {server_cmd}")
            server = start_server(server_cmd, env_with_bench)
            current["proc"] = server

            # Warmup loop with early-exit detection
            print(f"==> Warming up for {warmup_s}s ...", flush=True)
            end = time.time() + warmup_s
            while time.time() < end:
                time.sleep(0.1)
                if server.poll() is not None:
                    raise RuntimeError(
                        f"Server exited early with code {server.returncode}"
                    )

            # Run load
            print(f"==> Running load test: {load_cmd}", flush=True)
            rc = run_load(load_cmd, extra_env)
            print(f"==> Load test exited with code {rc}", flush=True)

            # optional small cooldown between reps
            # time.sleep(1)

        except KeyboardInterrupt:
            print("\n==> KeyboardInterrupt; terminating server...", flush=True)
            terminate_server(server)
            sys.exit(130)

        except Exception as e:
            print(f"!! Orchestrator error: {e}", file=sys.stderr, flush=True)
            terminate_server(server)
            current["proc"] = None
            raise

        finally:
            print("==> Stopping server...", flush=True)
            terminate_server(server)
            current["proc"] = None  # clear so handler doesn’t try twice
            print(f"==> Cooling down for {cooldown_s}s ...", flush=True)
            time.sleep(cooldown_s)
            # no process to check while waiting, so we can really just sleep

    return rc, used_run_ids


def collect_env_files(args) -> list[str]:
    files = []
    for ef in args.env_file or []:
        if ef == "-":
            files.extend(line.strip() for line in sys.stdin if line.strip())
        else:
            files.append(ef)
    return sorted(set(files))


def append_rows_to_csv(
        csv_path: str, run_id: str, rows: list[dict], ctx: dict
):
    """
    Append each 'kind' row to existing CSV. We map:
      phase       = f"kind:{row['kind']}"
      total       = row['n']
      ok/errors   = 0  (not meaningful for per-kind timings)
      walltime    = row['std']      (store std just to keep it)
      throughput  = row['mean']     (store mean as 'throughput')
    and we keep the env context (accounting, payments, etc).
    """
    # ensure header exists: reuse existing header
    header = [
        "timestamp", "tag", "phase", "accounting", "payments", "concurrency",
        "db_pool_size", "redis_max_conn", "webhook_mode",
        "arg_succeed_rate", "total", "ok", "errors", "walltime", "throughput"
    ]
    path = Path(csv_path)
    file_exists = path.exists()

    ts = datetime.now(timezone.utc).isoformat()

    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        if not file_exists:
            w.writeheader()

        for r in rows:
            w.writerow({
                "timestamp": ts,
                "tag": ctx.get("tag", ""),
                "phase": f"kind:{r.get('kind', 'unknown')}",
                "accounting": ctx.get("accounting", ""),
                "payments": ctx.get("payments", ""),
                "concurrency": ctx.get("concurrency", ""),
                "db_pool_size": ctx.get("db_pool_size", ""),
                "redis_max_conn": ctx.get("redis_max_conn", ""),
                "webhook_mode": ctx.get("webhook_mode", ""),
                "arg_succeed_rate": "1.0",
                "total": int(r.get("n", 0)),
                "ok": 0,
                "errors": 0,
                # store std in 'walltime' to reuse existing column,
                # and mean in 'throughput' so dashboards can show bars/whiskers
                "walltime": float(r.get("std", 0.0)),
                "throughput": float(r.get("mean", 0.0)),
            })


def main():
    ap = argparse.ArgumentParser(
        description="Wrapper: set env, start server, run load test,"
                    " stop server"
    )
    ap.add_argument(
        "--env-file", action="append",
        help="Env file(s). Allowed multiple times. Use '-' for stdin list."
    )
    ap.add_argument(
        "--csv-file", required=True, help="What CSV file to write to"
    )
    ap.add_argument(
        "--repeat", required=True, type=int, help="how many times to repeat"
    )
    ap.add_argument(
        "--warmup", type=int, default=5,
        help="how long (seconds) to wait after server start"
    )
    ap.add_argument("--cooldown", default=3,
                    help="cooldown time in seconds after each test")
    ap.add_argument(
        "--server-cmd", default="make server",
        help='Server command (default: "make server")'
    )
    ap.add_argument(
        "--load-cmd", default=f"{sys.executable} -m bench.load_phases",
        help='Load test command (default: "python -m bench.load_phases")'
    )

    # everything after -- goes to the load test command
    ap.add_argument(
        "load_args", nargs=argparse.REMAINDER,
        help="Arguments passed through to the load test (use -- to separate)"
    )
    args = ap.parse_args()

    print('\n')
    print('*' * 64)
    print(f'** Starting at {human_iso(now_ts()).replace("T", " ")}')
    print('*' * 64)
    print('\n', flush=True)   # don't forget to flush, for tee

    # default: return code = 1  -->  sth is wrong
    rc = 1

    # start the collector server
    collector_base, stop_collector = start_collector_in_thread(
        "127.0.0.1", 7071
    )

    try:
        for env_file in collect_env_files(args):
            run_ids_for_this_env = []
            print(f"\n=== ENV FILE {env_file} ===", flush=True)
            extra_env = read_env_file(env_file)
            extra_env, load_args, load_args_dict = env_to_args(
                extra_env, env_file, args.csv_file
            )

            # Split commands like a shell, but without using shell=True
            def split_cmd(s: str) -> list[str]:
                # very simple split on spaces;
                # if we need real quoting later, maybe use shlex.split
                import shlex
                return shlex.split(s)

            server_cmd = split_cmd(args.server_cmd)
            load_cmd = split_cmd(args.load_cmd) + load_args + args.load_args

            # If user wrote "... -- ..." argparse keeps the leading '--' in
            # load_args --> drop it
            if load_cmd and load_cmd[-len(args.load_args):][:1] == ["--"]:
                # not typical, but just in case; usually argparse strips it
                # already
                load_cmd = load_cmd[:-len(args.load_args)] + args.load_args[1:]

            # prepare for run_id making
            tag = load_args_dict.get("--tag", Path(env_file).stem)

            def run_id_maker(rep_idx: int) -> str:
                # e.g. "./load_tests/...env" -> a safe id
                safe_tag = tag.replace("/", "_")
                return f"{safe_tag}-rep{rep_idx+1}"

            print(f'server_cmd: {server_cmd}')
            print(f'load_cmd: {load_cmd}', flush=True)
            rc, run_ids_for_this_env = orchestrate_repeated(
                server_cmd, load_cmd, extra_env, repeats=args.repeat,
                warmup_s=args.warmup, cooldown_s=args.cooldown,
                bench_url=collector_base, run_id_maker=run_id_maker,
            )

            # after all repetitions: fetch the per-kind aggregates for all runs
            # build context once from env_file
            ctx = {
                "tag": tag,
                "accounting": load_args_dict.get(
                    "--accounting", extra_env.get("ACCT_BACKEND", "")
                ),
                "payments": load_args_dict.get(
                    "--payments", extra_env.get("PAYSESSION_BACKEND", "")
                ),
                "concurrency": load_args_dict.get("--concurrency", "0"),
                "db_pool_size": load_args_dict.get(
                    "--db-pool-size", extra_env.get("DB_POOL_SIZE", "")
                ),
                "redis_max_conn": load_args_dict.get(
                    "--redis-max-conn", extra_env.get("REDIS_MAX_CONN", "")
                ),
                "webhook_mode": load_args_dict.get(
                    "--webhook-mode", extra_env.get("WEBHOOK_MODE", "")
                ),
            }

            # one request returns all selected runs; clear them from memory
            params = [("run_id", rid) for rid in run_ids_for_this_env]
            params.append(("clear", "1"))

            try:
                with httpx.Client(timeout=10.0) as c:
                    resp = c.get(
                        f"{collector_base}/v1/metric/kind_dump", params=params
                    )
                    resp.raise_for_status()
                    payload = resp.json()
            except Exception as e:
                print(f"!! Failed to fetch rows: {e}", file=sys.stderr)
                payload = {"runs": {}}

            runs = payload.get("runs", {})
            for rid, rows in runs.items():
                append_rows_to_csv(args.csv_file, rid, rows, ctx)
                print(
                    f"==> wrote {len(rows)} kind rows for {rid} into "
                    f"{args.csv_file}"
                )
    except KeyboardInterrupt:
        print("\n==> KeyboardInterrupt", flush=True)
        if current["proc"]:
            print("terminating server...", flush=True)
            terminate_server(current["proc"])
            current["proc"] = None
        sys.exit(130)

    except Exception as e:
        print(f"!! Orchestrator error: {e}", file=sys.stderr, flush=True)
        if current["proc"]:
            print("terminating server...", flush=True)
            terminate_server(current["proc"])
            current["proc"] = None
            print("traceback:")
            traceback.print_exc(file=sys.stderr)   # full stacktrace

    finally:
        if current["proc"]:
            print("==> Stopping server...", flush=True)
            terminate_server(current['proc'])
            current["proc"] = None

        # stop collector last
        try:
            stop_collector()
        except Exception:
            pass
        print('\n')
        print('*' * 64)
        print(f'** Shutting down at {human_iso(now_ts()).replace("T", " ")}')
        print('*' * 64)
        print('\n')
    sys.exit(rc)


if __name__ == "__main__":
    main()
