"""Start/stop only this application's local MCP and Parlant processes.

PostgreSQL is managed separately by the documented Compose commands. A stop
preserves its volume, native Parlant data and model usage logs.
"""

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from .settings import ROOT, load_settings

RUN = ROOT / "runtime-data/retail-demo/services"
MODULES = ("apps.retail_demo.mcp_server", "apps.retail_demo.parlant_app")


def running(record):
    try:
        args = Path(f"/proc/{record['pid']}/cmdline").read_bytes().split(b"\0")
        return record["module"].encode() in args
    except (FileNotFoundError, ProcessLookupError):
        return False


def listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


@contextmanager
def lock():
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN / "lock").open("w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def records():
    path = RUN / "processes.json"
    return json.loads(path.read_text()) if path.exists() else []


def stop(items):
    for item in reversed(items):
        if running(item):
            os.killpg(item["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 20
    while any(running(x) for x in items) and time.monotonic() < deadline:
        time.sleep(0.2)
    for item in items:
        if running(item):
            os.killpg(item["pid"], signal.SIGKILL)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["start", "stop", "status"])
    args = parser.parse_args()
    s = load_settings()
    with lock():
        existing = records()
        if args.command == "status":
            for item in existing:
                print(item["module"], "running" if running(item) else "stopped", "pid", item["pid"])
            if not existing:
                print("No managed demo processes.")
            return
        if args.command == "stop":
            stop(existing)
            print("Demo MCP/Parlant stopped; database and persisted data retained.")
            return
        if any(running(x) for x in existing):
            raise SystemExit("Managed demo processes already running; use status or stop first.")
        for port in [s.mcp_port, s.parlant_port, s.tool_port]:
            if listening(port):
                raise SystemExit(
                    f"Port {port} is occupied; this launcher will not stop another service."
                )
        started = []
        try:
            for module, port in zip(MODULES, [s.mcp_port, s.parlant_port]):
                log = RUN / (module.rsplit(".", 1)[-1] + ".log")
                with log.open("a") as stream:
                    child = subprocess.Popen(
                        [sys.executable, "-m", module],
                        cwd=ROOT,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                started.append(
                    {"module": module, "pid": child.pid, "log": str(log.relative_to(ROOT))}
                )
                (RUN / "processes.json").write_text(json.dumps(started, indent=2) + "\n")
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    if child.poll() is not None:
                        raise RuntimeError(f"{module} exited; see {log.relative_to(ROOT)}")
                    if listening(port):
                        break
                    time.sleep(0.2)
                else:
                    raise TimeoutError(f"{module} startup timed out; see {log.relative_to(ROOT)}")
            print(f"Chat: http://127.0.0.1:{s.parlant_port}/chat/")
            print(f"Select customer {s.customer_id}; local single-customer demonstration.")
            print(f"MCP: {s.mcp_url}/mcp; logs: {RUN.relative_to(ROOT)}")
        except BaseException:
            stop(started)
            raise


if __name__ == "__main__":
    main()
