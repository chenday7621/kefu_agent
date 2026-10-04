"""Supervise B0-R1 child runs and recover an unscored task at most once."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[2]
SELECTION_PATH = Path(__file__).parent / "selection.json"
FREEZE_PATH = PROJECT / "_reviews/20261002_211551_DAY1_B0_R1_RECOVERY/B0_R1_FROZEN.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as output:
        output.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        output.flush()
        os.fsync(output.fileno())


def scored(path: Path, task_id: str, freeze_sha: str, selection: dict) -> bool:
    if not path.exists():
        return False
    row = json.loads(path.read_text())
    if row.get("task_id") != task_id or row.get("freeze_sha256") != freeze_sha:
        raise RuntimeError(f"Mismatched scored result: {path}")
    if row.get("selection_sha256") != sha256(SELECTION_PATH) or row.get("protocol") != selection:
        raise RuntimeError(f"Mismatched selection/protocol: {path}")
    if (row.get("simulation") or {}).get("reward_info") is None:
        raise RuntimeError(f"Incomplete official score: {path}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-dir", type=Path, required=True)
    parser.add_argument("--only-task-id", default=None)
    parser.add_argument("--diagnostic", action="store_true")
    parser.add_argument("--max-recoveries", type=int, default=1)
    args = parser.parse_args()
    if not FREEZE_PATH.is_file():
        raise RuntimeError("B0-R1 freeze is required before any paid run")
    result_dir = args.result_dir.resolve()
    if not result_dir.is_relative_to(PROJECT / "results"):
        raise RuntimeError("Result directory must be inside results/")
    selection = json.loads(SELECTION_PATH.read_text())
    ids = selection["task_ids"]
    if args.only_task_id is not None:
        if args.only_task_id not in ids:
            raise RuntimeError("Task ID outside frozen selection")
        ids = [args.only_task_id]
    freeze_sha = sha256(FREEZE_PATH)
    invocation = 0
    while True:
        remaining = [task_id for task_id in ids if not scored(result_dir / f"task_{task_id}.json", task_id, freeze_sha, selection)]
        if not remaining:
            append_jsonl(result_dir / "supervision/events.jsonl", {
                "event": "all_selected_tasks_scored", "time_utc": datetime.now(timezone.utc).isoformat(),
                "count": len(ids),
            })
            return 0
        task_id = remaining[0]
        existing_attempts = list((result_dir / "attempts" / f"task_{task_id}").glob("*/"))
        if len(existing_attempts) > args.max_recoveries:
            append_jsonl(result_dir / "supervision/events.jsonl", {
                "event": "recovery_limit_reached", "task_id": task_id,
                "attempt_count": len(existing_attempts), "time_utc": datetime.now(timezone.utc).isoformat(),
            })
            return 2
        invocation += 1
        base = result_dir / "supervision" / f"invocation_{invocation:03d}"
        base.parent.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-u", str(Path(__file__).parent / "run.py"),
               "--result-dir", str(result_dir)]
        if args.only_task_id:
            cmd += ["--only-task-id", args.only_task_id]
        if args.diagnostic:
            cmd.append("--diagnostic")
        env = os.environ.copy()
        env["PYTHONFAULTHANDLER"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        started = datetime.now(timezone.utc).isoformat()
        append_jsonl(result_dir / "supervision/events.jsonl", {
            "event": "child_started", "task_id": task_id, "invocation": invocation,
            "time_utc": started, "command": cmd,
            "environment": {name: env.get(name, "unset") for name in (
                "OMP_NUM_THREADS", "MKL_NUM_THREADS", "TOKENIZERS_PARALLELISM", "PYTHONFAULTHANDLER")},
            "freeze_sha256": freeze_sha,
        })
        with (base.with_suffix(".stdout.log")).open("w") as stdout, \
             (base.with_suffix(".stderr.log")).open("w") as stderr:
            child = subprocess.Popen(cmd, cwd=PROJECT, env=env, stdout=stdout, stderr=stderr)
            code = child.wait()
            stdout.flush(); os.fsync(stdout.fileno())
            stderr.flush(); os.fsync(stderr.fileno())
        after = [item for item in ids if not scored(result_dir / f"task_{item}.json", item, freeze_sha, selection)]
        active = result_dir / "active_attempt.json"
        active_row = json.loads(active.read_text()) if active.exists() else None
        row = {"event": "child_exited", "invocation": invocation, "returncode": code,
               "signal": -code if code < 0 else None, "time_utc": datetime.now(timezone.utc).isoformat(),
               "first_unscored_task": after[0] if after else None,
               "active_attempt": active_row}
        append_jsonl(result_dir / "supervision/events.jsonl", row)
        (base.with_suffix(".exit.json")).write_text(json.dumps(row, indent=2) + "\n")
        if not after:
            return 0
        if code == 0:
            raise RuntimeError("Child exited without scoring all selected tasks")
        if after[0] != task_id:
            raise RuntimeError("Unexpected incomplete task order after child exit")
        attempts = list((result_dir / "attempts" / f"task_{task_id}").glob("*/"))
        if not attempts:
            return 3  # failure before any task attempt; do not blindly restart
        if len(attempts) > args.max_recoveries:
            return 2
        # The next child starts the same task with a fresh Parlant session and
        # a freshly built official Retail environment. No interrupted state is reused.


if __name__ == "__main__":
    raise SystemExit(main())
