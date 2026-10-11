"""Explicitly authorized six-unit continuation; no replay of original B0 units.

Keeps the 23 original scores immutable and F3-04 unscored. This orchestration
adds no action selection, prompt changes, scorer changes or recovery generation.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
from uuid import uuid4

from ..budget_protocol import verify_execution_bundle
from ..episode_state import EpisodeDatabase
from .controller import run_episode
from .resources import ResearchResources
from .runner import preflight, write

PATCH_FILES = (
    "experiments/posttraining/policy.py",
    "experiments/posttraining/research/boundary.py",
    "experiments/posttraining/research/evidence.py",
)
MISSING_TASK = "PTDEV-F3-04"
REMAINING_TASKS = tuple(f"PTDEV-F3-{i:02d}" for i in range(5, 11))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_rows(path):
    return [json.loads(s) for s in Path(path).read_text().splitlines()] if Path(path).exists() else []


def combined_b0(plan):
    runtime = Path(plan["runtime"])
    return read_rows(runtime / "b0/PER_TASK.jsonl") + read_rows(runtime / "b0_post_boundary/PER_TASK.jsonl")


def verify_sources(plan):
    review = Path(plan["review"])
    config = json.loads(Path(plan["experiment_config"]).read_text())
    frozen = json.loads((Path(plan["prior_review"]) / "freezes/B0_FREEZE.json").read_text())
    amendment = json.loads((review / "freezes/BOUNDARY_RUN_FREEZE.json").read_text())
    if set(amendment["authorized_changed_sources"]) != set(PATCH_FILES):
        raise RuntimeError("Unexpected frozen-source exception")
    for name, expected in frozen["source_sha256"].items():
        actual = digest(Path(config["repo"]) / name)
        if name in PATCH_FILES:
            exception = amendment["authorized_changed_sources"][name]
            if exception["old_sha256"] != expected or exception["new_sha256"] != actual:
                raise RuntimeError("Authorized boundary source differs from addendum")
        elif actual != expected:
            raise RuntimeError("Unauthorized frozen-source change: " + name)
    if config["generation"] != frozen["generation"]:
        raise RuntimeError("Decoder/Prompt/provider configuration cannot change")
    if digest(Path(plan["dev_bundle"]) / "DEV_MANIFEST.json") != frozen["dev_manifest_sha256"]:
        raise RuntimeError("DEV manifest changed")
    return frozen


def require_b0_addendum(plan):
    verify_sources(plan)
    review = Path(plan["review"])
    comparison = json.loads((review / "EVAL_COMPARABILITY.json").read_text())
    rows = combined_b0(plan)
    if len(rows) != 29 or len({r["task_id"] for r in rows}) != 29:
        raise RuntimeError("Authorized B0 requires23 original +6 patched complete units")
    if not all(r["scoreable"] and r["audit"]["passed"] for r in rows):
        raise RuntimeError("B0 evidence incomplete")
    if MISSING_TASK in {r["task_id"] for r in rows} or comparison["missing_b0_task_ids"] != [MISSING_TASK]:
        raise RuntimeError("F3-04 must remain explicitly unscored")
    if any(t not in {r["task_id"] for r in rows} for t in comparison["primary_paired_task_ids"]):
        raise RuntimeError("Invalid pretraining paired denominator")
    return rows


def freeze_comparison(plan):
    review = Path(plan["review"])
    path = review / "EVAL_COMPARABILITY.json"
    if path.exists():
        raise RuntimeError("Comparison freeze already exists; never select after evaluation")
    rows = combined_b0(plan)
    impact = json.loads((review / "PATCH_IMPACT_AUDIT.json").read_text())
    if len(rows) != 29:
        raise RuntimeError("Six-unit continuation incomplete")
    eligible = set(impact["compatible_task_ids"]) | set(REMAINING_TASKS)
    value = {
        "version": "pt.eval.comparability.v2", "created_utc": datetime.now(timezone.utc).isoformat(),
        "primary_paired_task_ids": [r["task_id"] for r in rows if r["task_id"] in eligible],
        "primary_denominator": sum(r["task_id"] in eligible for r in rows),
        "missing_b0_task_ids": [MISSING_TASK],
        "contract_affected_complete_task_ids": [r["task_id"] for r in rows if r["task_id"] not in eligible],
        "sft_eval_order": [t["task_id"] for t in verify_execution_bundle(Path(plan["dev_bundle"]))],
        "planned_denominator": 30, "missing_reward": None,
        "original_batch_size": 23, "post_boundary_batch_size": 6,
        "mixed_b0_contract_versions": True,
        "impact_audit_sha256": digest(review / "PATCH_IMPACT_AUDIT.json"),
        "original_result_sha256": digest(Path(plan["runtime"]) / "b0/PER_TASK.jsonl"),
        "patched_result_sha256": digest(Path(plan["runtime"]) / "b0_post_boundary/PER_TASK.jsonl"),
        "selection_before_training_and_reevaluation": True,
        "interpretation": "Shared capabilities; template/episode isolated limited DEV comparison. Missing/contract-affected units separate; no pure-SFT attribution for contract repair."
    }
    write(path, value)
    return value


def run(plan):
    review, runtime = Path(plan["review"]), Path(plan["runtime"])
    if preflight(plan):
        raise RuntimeError("Continuation preflight blocked")
    verify_sources(plan)
    original = read_rows(runtime / "b0/PER_TASK.jsonl")
    if len(original) != 23:
        raise RuntimeError("Original23 immutable B0 results required")
    task_list = verify_execution_bundle(Path(plan["dev_bundle"]))
    selected = [t for t in task_list if t["task_id"] in REMAINING_TASKS]
    if [t["task_id"] for t in selected] != list(REMAINING_TASKS):
        raise RuntimeError("Remaining manifest order changed")
    if any((runtime / "b0" / t["task_id"]).exists() for t in selected):
        raise RuntimeError("Continuation only permits previously unstarted units")
    cfg = json.loads(Path(plan["experiment_config"]).read_text())
    directory = runtime / "b0_post_boundary"
    directory.mkdir(exist_ok=True)
    result_file = directory / "PER_TASK.jsonl"
    done = {r["task_id"] for r in read_rows(result_file)}
    state = json.loads((review / "RUN_STATE.json").read_text())
    state.update(status="B0_BOUNDARY_CONTINUATION_RUNNING", blocker_type=None, blocker_reason=None,
                 excluded_incomplete_task=MISSING_TASK)
    write(review / "RUN_STATE.json", state)
    resources = ResearchResources(runtime, cfg)
    try:
        dsn = resources.start_pg()
        with resources.backend("b0_post_boundary") as backend:
            shared = dict(cfg, **backend, phase="b0", admin_dsn=dsn,
                          owned_cluster_marker=str(resources.marker), bundle=plan["dev_bundle"])
            config_path = Path(backend["generation_directory"]) / "CONTROLLER_CONFIG.json"
            write(config_path, shared)
            config_path.chmod(0o600)
            write(review / "freezes/BOUNDARY_RESOURCE_BINDING.json", {
                "generation_directory": backend["generation_directory"],
                "controller_config_sha256": digest(config_path),
                "model_profile": json.loads((Path(backend["generation_directory"]) / "MODEL_PROFILE.json").read_text()),
                "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                "remaining_b0_gpu_seconds": cfg["remaining_gpu_seconds"]})
            for task in selected:
                if task["task_id"] in done:
                    continue
                d = directory / task["task_id"]
                if d.exists():
                    raise RuntimeError("Incomplete continuation attempt retained; no implicit replay")
                db = EpisodeDatabase(dsn, "ptdev_" + uuid4().hex[:16], task, d)
                try:
                    db.materialize()
                    result = run_episode(db, shared)
                finally:
                    db.cleanup()
                with result_file.open("a") as out:
                    out.write(json.dumps(result, ensure_ascii=False) + "\n")
                    out.flush()
                print(json.dumps({"task_id": task["task_id"], "reward": result["score"]["reward"],
                                  "scoreable": result["scoreable"]}), flush=True)
        freeze_comparison(plan)
        require_b0_addendum(plan)
        state.update(status="B0_COMPLETE_WITH_DECLARED_MISSING", completed_stages=
                     list(dict.fromkeys(state.get("completed_stages", []) + ["B0_COMPLETE_WITH_DECLARED_MISSING"])))
        write(review / "RUN_STATE.json", state)
    except BaseException as exc:
        state.update(status="B0_BOUNDARY_BLOCKED", blocker_type=type(exc).__name__, blocker_reason=str(exc))
        write(review / "RUN_STATE.json", state)
        raise
    finally:
        resources.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--plan", type=Path, required=True)
    run(json.loads(p.parse_args().plan.read_text()))
