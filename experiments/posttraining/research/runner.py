"""Persisted execution preflight/status. Blocked inputs never load a GPU model."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil


STAGES = [
    "PREFLIGHT_READY",
    "INTEGRATION_READY",
    "B0_FROZEN",
    "B0_COMPLETE",
    "DATASET_VERIFIED",
    "SFT1_FROZEN",
    "SFT1_TRAINED",
    "SFT1_EVAL_COMPLETE",
    "EXPERIMENT_COMPLETE",
]


def write(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)


def preflight(config):
    from dotenv import dotenv_values
    from ..budget_protocol import verify_execution_bundle

    blockers = []
    verify_execution_bundle(Path(config["dev_bundle"]))
    key = os.environ.get("DEEPSEEK_API_KEY") or dotenv_values(config["repo_env"]).get(
        "DEEPSEEK_API_KEY"
    )
    if not key:
        blockers.append(
            {"status": "PROVIDER_BINDING_BLOCKED", "reason": "DEEPSEEK_API_KEY missing"}
        )
    split = Path(config["split_approval"])
    if not split.exists() or json.loads(split.read_text()).get("status") != "SPLIT_APPROVED":
        blockers.append(
            {
                "status": "WAITING_SPLIT_DECISION",
                "reason": "Independent TRAIN/SFT-VALID family registry not approved",
            }
        )
    else:
        approval = json.loads(split.read_text())
        policy = Path(approval["policy_path"])
        if hashlib.sha256(policy.read_bytes()).hexdigest() != approval["policy_sha256"]:
            raise ValueError("Approved split policy hash changed")
        from .split_policy import register

        definition = json.loads(policy.read_text())
        if definition["version"] != "split.policy.v2":
            raise ValueError("Approved V2 template isolation required")
        register(definition["groups"])
    if shutil.disk_usage(config["runtime"]).free < 10 * 1024**3:
        blockers.append({"status": "RESOURCE_BLOCKED", "reason": "NAS below 10GiB reserve"})
    return blockers


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--stop-after", choices=STAGES)
    args = parser.parse_args()
    config = json.loads(os.path.expandvars(args.plan.read_text()))
    state_path = Path(config["review"]) / "RUN_STATE.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"completed_stages": []}
    if args.status:
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return
    blockers = preflight(config)
    state.update(
        last_checked_utc=datetime.now(timezone.utc).isoformat(),
        blockers=blockers,
        plan_sha256=hashlib.sha256(Path(config["source_plan"]).read_bytes()).hexdigest(),
    )
    if blockers:
        state["status"] = blockers[0]["status"]
        write(state_path, state)
        print(json.dumps({"status": state["status"], "blockers": blockers}, ensure_ascii=False))
        return
    # Actual B0 cannot begin until the installed component binding and its
    # native gate/scorer regression evidence have been frozen, not guessed.
    freeze = Path(config["review"]) / "freezes/B0_FREEZE.json"
    if not freeze.exists():
        state.update(
            status="INTEGRATION_VALIDATION_REQUIRED",
            blockers=[{"reason": "Real provider/embedding binding and B0 commit not yet frozen"}],
        )
        write(state_path, state)
        print(json.dumps(state, ensure_ascii=False))
        return
    if args.stop_after == "B0_FROZEN":
        print("B0_FROZEN")
        return
    from .controller import run_batch

    run_batch(json.loads(freeze.read_text())["controller_config"])
    state.update(status="B0_COMPLETE")
    write(state_path, state)
    # Dataset acquisition depends on the explicit family registry; it is never
    # replaced by the DEV oracle. Subsequent stage artifacts are mandatory.
    manifest = Path(config["review"]) / "data/TRAIN_DATA_MANIFEST.json"
    if not manifest.exists():
        state.update(status="DATA_COLLECTION_REQUIRED")
        write(state_path, state)
        print(json.dumps(state, ensure_ascii=False))
        return
    if args.stop_after == "B0_COMPLETE":
        return
    train_config = Path(config["review"]) / "sft1/TRAIN_CONFIG.json"
    if not train_config.exists():
        state.update(status="SFT_SANITY_REQUIRED")
        write(state_path, state)
        print(json.dumps(state, ensure_ascii=False))
        return
    # Training is a separate one-epoch executable guarded by verification and
    # sanity artifact checks. This blocked-run entry does not invent its output.
    print(
        json.dumps(
            {"status": state["status"], "next_train_config": str(train_config)}, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
