#!/usr/bin/env python3
import argparse
import os
import sys
import signal
import time
from datetime import datetime, timezone
import subprocess
from typing import Dict, List, Tuple, Optional

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

    return env_dict, args


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
    cooldown_s: int,
):
    rc = 0

    for repetition in range(repeats):
        print(f"\n=== Repetition {repetition+1}/{repeats} ===")
        server: Optional[subprocess.Popen] = None
        try:
            print(f"==> Starting server: {server_cmd}")
            server = start_server(server_cmd, extra_env)
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

    return rc


def collect_env_files(args) -> list[str]:
    files = []
    for ef in args.env_file or []:
        if ef == "-":
            files.extend(line.strip() for line in sys.stdin if line.strip())
        else:
            files.append(ef)
    return sorted(set(files))


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
        "--load-cmd", default=f"{sys.executable} -m tigerfans.load_phases",
        help='Load test command (default: "python -m tigerfans.load_phases")'
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

    try:
        for env_file in collect_env_files(args):
            print(f"\n=== ENV FILE {env_file} ===", flush=True)
            extra_env = read_env_file(env_file)
            extra_env, load_args = env_to_args(
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

            print(f'server_cmd: {server_cmd}')
            print(f'load_cmd: {load_cmd}', flush=True)
            rc = orchestrate_repeated(
                server_cmd, load_cmd, extra_env, repeats=args.repeat,
                warmup_s=args.warmup, cooldown_s=args.cooldown,
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

    finally:
        if current["proc"]:
            print("==> Stopping server...", flush=True)
            terminate_server(current['proc'])
            current["proc"] = None

        print('\n')
        print('*' * 64)
        print(f'** Shutting down at {human_iso(now_ts()).replace("T", " ")}')
        print('*' * 64)
        print('\n')
    sys.exit(rc)


if __name__ == "__main__":
    main()
