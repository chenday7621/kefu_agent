"""Owned-resource formal batch CLI; resumable completed tasks are never rerun."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

from .runner import preflight, write
from .resources import ResearchResources
from .controller import run_batch
from ..budget_protocol import verify_execution_bundle


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--stage", choices=["b0", "sft_eval"], required=True)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    review = Path(plan["review"])
    runtime = Path(plan["runtime"])
    blockers = preflight(plan)
    if blockers:
        raise RuntimeError("Preflight blocked: " + json.dumps(blockers))
    source = json.loads(Path(plan["experiment_config"]).read_text())
    stage = args.stage
    if (
        stage == "sft_eval"
        and not Path(source.get("final_adapter", "")).joinpath("adapter_config.json").is_file()
    ):
        raise ValueError("No completed first LoRA adapter; reevaluation forbidden")
    resources = ResearchResources(runtime, source)
    state = json.loads((review / "RUN_STATE.json").read_text())
    state.update(
        status=stage.upper() + "_STARTING",
        split_decision_pending=False,
        blockers=[],
        overall_status="EXPERIMENT_RUNNING",
    )
    write(review / "RUN_STATE.json", state)
    try:
        dsn = resources.start_pg()
        with resources.backend(
            stage, source.get("final_adapter") if stage == "sft_eval" else None
        ) as backend:
            controller = dict(
                source,
                **backend,
                phase=stage,
                admin_dsn=dsn,
                owned_cluster_marker=str(resources.marker),
                bundle=plan["dev_bundle"],
                output_directory=str(runtime / ("b0" if stage == "b0" else "sft1_eval")),
            )
            path = Path(backend["generation_directory"]) / "CONTROLLER_CONFIG.json"
            write(path, controller)
            path.chmod(0o600)
            freeze_path = (
                review
                / "freezes"
                / (
                    source.get("b0_freeze_name", "B0_FREEZE.json")
                    if stage == "b0"
                    else "SFT1_EVAL_FREEZE.json"
                )
            )
            if not freeze_path.exists():
                repo = Path(source["repo"])
                head = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=repo, text=True
                ).strip()
                tracked = subprocess.check_output(
                    [
                        "git",
                        "ls-tree",
                        "-r",
                        "--name-only",
                        "HEAD",
                        "experiments/posttraining",
                        "apps/retail_demo",
                        "src/parlant",
                    ],
                    cwd=repo,
                    text=True,
                ).splitlines()
                freeze = {
                    "status": stage.upper() + "_FROZEN",
                    "created_utc": datetime.now(timezone.utc).isoformat(),
                    "code_commit": head,
                    "controller_config_path": str(path),
                    "controller_config_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "dev_manifest_sha256": hashlib.sha256(
                        (Path(plan["dev_bundle"]) / "DEV_MANIFEST.json").read_bytes()
                    ).hexdigest(),
                    "execution_order": [
                        t["task_id"] for t in verify_execution_bundle(Path(plan["dev_bundle"]))
                    ],
                    "source_sha256": {
                        n: hashlib.sha256((repo / n).read_bytes()).hexdigest() for n in tracked
                    },
                    "generation": source["generation"],
                    "model_profile": json.loads(
                        (Path(backend["generation_directory"]) / "MODEL_PROFILE.json").read_text()
                    ),
                    "physical_gpu": source["physical_gpu"],
                    "reference_provider": "native DemoDeepSeek/deepseek-chat; SDK physical retries=0",
                    "embedding_lock": source["embedding_lock"],
                    "budget": source["authorization"],
                    "oracle_actions_enabled": False,
                    "negative_probes_enabled": False,
                    "first_task_is_counted_canary": True,
                    "recovery_rule": "incomplete infrastructure unit <=1, batch <=3; completed/normal model failures never rerun",
                    "integration_cpu_tests_passed": True,
                    "real_canary_verified": False,
                    "infrastructure_recovery": source.get("infrastructure_recovery"),
                }
                write(freeze_path, freeze)
            else:
                # An incomplete prior batch needs audited recovery with its
                # original freeze; never silently replace a version/config.
                existing = json.loads(freeze_path.read_text())
                if (
                    existing["code_commit"]
                    != subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
                ):
                    raise RuntimeError("B0_VERSION_SPLIT: code commit differs from existing freeze")
            run_batch(controller)
            rows = [
                json.loads(s)
                for s in (Path(controller["output_directory"]) / "PER_TASK.jsonl")
                .read_text()
                .splitlines()
            ]
            if len(rows) != 30 or not all(r["scoreable"] and r["audit"]["passed"] for r in rows):
                raise RuntimeError("Formal batch has incomplete scoreable coverage")
            state.update(
                status=stage.upper() + "_COMPLETE",
                completed_stages=list(
                    dict.fromkeys(state.get("completed_stages", []) + [stage.upper() + "_COMPLETE"])
                ),
            )
            write(review / "RUN_STATE.json", state)
    except BaseException as exc:
        state.update(
            status=stage.upper() + "_BLOCKED",
            blocker_type=type(exc).__name__,
            blocker_reason=str(exc),
            stopped_utc=datetime.now(timezone.utc).isoformat(),
        )
        write(review / "RUN_STATE.json", state)
        raise
    finally:
        resources.close()


if __name__ == "__main__":
    main()
