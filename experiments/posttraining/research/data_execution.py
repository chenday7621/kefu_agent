"""Acquire registered TRAIN/VALID only after a complete audited B0 batch."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from .data_controller import collect
from .data_freeze import assemble
from .resources import ResearchResources
from .runner import preflight, write
from .split_policy import check_episode, register


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verified_definitions(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "SCENARIO_MANIFEST.json").read_text())
    registry = json.loads((directory / "SPLIT_REGISTRY.json").read_text())
    path = directory / "SCENARIOS.jsonl"
    if digest(path) != manifest["scenario_sha256"]:
        raise ValueError("Frozen synthetic scenario hash changed")
    if registry.get("version") != "split.policy.v2":
        raise ValueError("Explicit approved split V2 required")
    register(registry["groups"])
    tasks = [json.loads(s) for s in path.read_text().splitlines()]
    if [t["episode_id"] for t in tasks] != manifest["execution_order"]:
        raise ValueError("Frozen data execution order changed")
    for task in tasks:
        check_episode(task, registry["groups"])
        if task["split"] not in ("train", "sft_valid") or task["episode_id"].startswith("PTDEV-"):
            raise ValueError("DEV/holdout/oracle excluded from collection")
        if registry["episode_ownership"].get(task["episode_id"]) != task["split"]:
            raise ValueError("Unregistered episode split")
    return tasks, registry, manifest


def run(plan, definitions):
    review, runtime = Path(plan["review"]), Path(plan["runtime"])
    state_path = review / "RUN_STATE.json"
    state = json.loads(state_path.read_text())
    if plan.get("boundary_addendum"):
        from .boundary_continuation import require_b0_addendum
        require_b0_addendum(plan)
        if state["status"] != "B0_COMPLETE_WITH_DECLARED_MISSING":
            raise RuntimeError("Authorized B0 addendum not complete")
    else:
        results = runtime / "b0/PER_TASK.jsonl"
        b0 = [json.loads(s) for s in results.read_text().splitlines()] if results.exists() else []
        if state["status"] != "B0_COMPLETE" or len(b0) != 30 or not all(r["scoreable"] and r["audit"]["passed"] for r in b0):
            raise RuntimeError("All30 real B0 episodes must complete before acquisition")
    if preflight(plan):
        raise RuntimeError("Data resource/budget/provider/split preflight blocked")
    tasks, registry, definition = verified_definitions(definitions)
    approval = json.loads(Path(plan["split_approval"]).read_text())
    approved = json.loads(Path(approval["policy_path"]).read_text())
    if registry["groups"] != approved["groups"] or definition["policy_sha256"] != approval["policy_sha256"]:
        raise ValueError("Synthetic registry differs from explicitly approved split policy")
    config = json.loads(Path(plan["experiment_config"]).read_text())
    config.update(collection_directory=str(runtime / "datasets/verified-episodes"),
                  dataset_output=str(runtime / "datasets/sft1-frozen"))
    data_review = review / "data"
    data_review.mkdir(exist_ok=True)
    frozen_files = sorted(Path(config["repo"]).joinpath("experiments/posttraining/research").glob("*.py"))
    freeze = {"status": "DATA_COLLECTION_FROZEN", "created_utc": datetime.now(timezone.utc).isoformat(),
              "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=config["repo"], text=True).strip(),
              "source_sha256": {str(p.relative_to(config["repo"])): digest(p) for p in frozen_files},
              "app_outbox_sha256": digest(Path(config["repo"]) / "apps/retail_demo/outbox.py"),
              "scenario_sha256": definition["scenario_sha256"], "registry_sha256": digest(Path(definitions) / "SPLIT_REGISTRY.json"),
              "execution_order": definition["execution_order"], "budget": config["authorization"],
              "policy_source": "synthetic_rule_expert_current_visible_native_events_only",
              "reference_provider": "same native DemoDeepSeek as PT-B0-R1-CONTRACTFIX",
              "candidate_budget": 30, "user_turn_budget": 12, "native_tool_budget": 18,
              "qwen_generations": 0, "dev_supervision": False, "holdout_generated": False}
    freeze_path = data_review / "DATA_COLLECTION_FREEZE.json"
    if freeze_path.exists():
        raise RuntimeError("Existing data freeze requires explicit audited recovery; no automatic replay")
    if shutil.disk_usage(runtime).free < 10 * 1024**3:
        raise RuntimeError("NAS reserve below10GiB")
    write(freeze_path, freeze)
    resources = ResearchResources(runtime, config)
    try:
        config.update(admin_dsn=resources.start_pg(), owned_cluster_marker=str(resources.marker))
        config_path = runtime / "datasets/DATA_CONTROLLER_CONFIG.json"
        write(config_path, config)
        config_path.chmod(0o600)
        state.update(status="DATA_COLLECTION_RUNNING")
        write(state_path, state)
        collect(config, tasks)
        episodes = [json.loads(s) for s in (Path(config["collection_directory"]) / "EPISODES.jsonl").read_text().splitlines()]
        manifest = assemble(config, registry, episodes)
        write(data_review / "TRAIN_DATA_MANIFEST.json", manifest)
        write(data_review / "DATA_QUALITY_REPORT.json", {"episode_count": len(episodes),
              "business_verified": sum(e["business"]["passed"] for e in episodes),
              "all_attribution_pass": all(e["audit"]["passed"] for e in episodes),
              "counts_as_baseline": False, "manifest": manifest})
        state.update(status=manifest["status"])
        if manifest["status"] == "DATASET_VERIFIED":
            state["completed_stages"] = list(dict.fromkeys(state.get("completed_stages", []) + ["DATASET_VERIFIED"]))
        write(state_path, state)
        return manifest
    except BaseException as exc:
        state.update(status="DATA_COLLECTION_BLOCKED", blocker_type=type(exc).__name__,
                     blocker_reason=str(exc), stopped_utc=datetime.now(timezone.utc).isoformat())
        write(state_path, state)
        raise
    finally:
        resources.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--definitions", required=True, type=Path)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    if args.validate_only:
        tasks, _, manifest = verified_definitions(args.definitions)
        print(json.dumps({"definitions_verified": True, "episodes": len(tasks),
                          "scenario_sha256": manifest["scenario_sha256"], "model_calls": 0}))
        return
    run(json.loads(args.plan.read_text()), args.definitions)


if __name__ == "__main__":
    main()
