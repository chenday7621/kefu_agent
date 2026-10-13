"""Authorized first-run stages, with guarded artifacts and no score retries.

Never starts another B0. Can wait for the separately owned frozen B0 driver.
Failure pauses at the recorded stage and archives partial results.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from .archive_results import archive
from .data_execution import run as acquire
from .runner import write
from .train import TARGET_MODULES
from .boundary_continuation import verify_sources, require_b0_addendum
from ..policy import MODEL_REVISION


def unchanged_b0(plan):
    review = Path(plan["review"])
    config = json.loads(Path(plan["experiment_config"]).read_text())
    if plan.get("boundary_addendum"):
        return verify_sources(plan)
    frozen = json.loads((review / "freezes/B0_FREEZE.json").read_text())
    for name, expected in frozen["source_sha256"].items():
        path = Path(config["repo"]) / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError("Frozen B0 consumed source changed: " + name)
    if config["generation"] != frozen["generation"]:
        raise RuntimeError("Frozen decoder/config changed")
    return frozen


def idle_gpu(config):
    value = subprocess.check_output(["nvidia-smi", "-i", str(config["physical_gpu"]),
            "--query-gpu=memory.used,utilization.gpu,name", "--format=csv,noheader,nounits"], text=True)
    used, active, name = [x.strip() for x in value.split(",")]
    if int(used) > 100 or int(active) != 0 or "A100" not in name:
        raise RuntimeError("TRAIN_RESOURCE_BLOCKED: selected A100 occupied; no preemption")


def checkpoint(helper, message, label, repo):
    if helper.resolve() not in (Path("/tmp/posttrain_safe_checkpoint_r1.py"), Path("/tmp/posttrain_safe_checkpoint_boundary.py")):
        raise ValueError("Only the reviewed secret-safe checkpoint helper is permitted")
    subprocess.run([sys.executable, str(helper), message, label], cwd=repo, check=True)


def run(plan_path, definitions, helper, wait):
    plan = json.loads(plan_path.read_text())
    review, runtime = Path(plan["review"]), Path(plan["runtime"])
    state_path = review / "RUN_STATE.json"
    config = json.loads(Path(plan["experiment_config"]).read_text())
    owned_marker = runtime / "logs/PIPELINE_OWNED.json"
    if owned_marker.exists():
        raise RuntimeError("Pipeline attempt already exists; explicit stage recovery required")
    write(owned_marker, {"pid": os.getpid(), "created_utc": datetime.now(timezone.utc).isoformat(),
                         "automatic_b0_retries": False, "training_epochs": 1})
    try:
        waiting_started = time.monotonic()
        while True:
            state = json.loads(state_path.read_text())
            if state["status"] in ("B0_COMPLETE", "B0_COMPLETE_WITH_DECLARED_MISSING") and not (runtime / "pg/cluster/postmaster.pid").exists():
                break
            if state["status"] not in ("B0_STARTING", "B0_COMPLETE", "B0_BOUNDARY_CONTINUATION_RUNNING", "B0_COMPLETE_WITH_DECLARED_MISSING") or not wait:
                raise RuntimeError("Prior B0 incomplete/blocked; subsequent stages forbidden")
            if time.monotonic() - waiting_started > 8.5 * 3600:
                raise RuntimeError("B0 wait resource limit reached; no automatic stage expansion")
            # This wait schedules no request, tool dispatch or model load.
            time.sleep(5)
        if plan.get("boundary_addendum"):
            require_b0_addendum(plan)
        frozen = unchanged_b0(plan)
        checkpoint(helper, "feat: add independent policy SFT data and single-epoch pipeline", "DATA_PIPELINE", config["repo"])
        archive(plan)
        manifest = acquire(plan, definitions)
        if manifest["status"] != "DATASET_VERIFIED":
            raise RuntimeError("DATASET_INSUFFICIENT: no training and no DEV backfill")
        unchanged_b0(plan)
        dataset_bytes = sum(Path(manifest[s]["path"]).stat().st_size for s in ("train", "sft_valid"))
        os.environ["HF_DATASETS_CACHE"] = str(runtime / "datasets/hf-datasets-cache")
        os.environ["TRITON_CACHE_DIR"] = str(runtime / "runtime/triton-cache")
        os.environ["CUDA_CACHE_PATH"] = str(runtime / "runtime/cuda-cache")
        os.environ["HF_DATASETS_OFFLINE"] = "1"
        output = runtime / "checkpoints/sft1"
        model_config = json.loads((Path(config["model_path"]) / "config.json").read_text())
        h, m, layers = (model_config[k] for k in ("hidden_size", "intermediate_size", "num_hidden_layers"))
        kv = model_config["num_key_value_heads"] * model_config["head_dim"]
        lora_count = layers * 16 * (4 * h + 2 * (h + kv) + 3 * (h + m))
        required = max(10 * 1024**3, dataset_bytes + lora_count * 4 * 8 + 2 * 1024**3)
        if shutil.disk_usage(runtime).free < required + 10 * 1024**3:
            raise RuntimeError("TRAIN_RESOURCE_BLOCKED: recovery storage and10GiB reserve insufficient")
        train = {"model_path": config["model_path"], "revision": MODEL_REVISION,
                 "data_manifest": str(review / "data/TRAIN_DATA_MANIFEST.json"),
                 "large_storage": str(runtime), "output_directory": str(output),
                 "sanity_directory": str(runtime / "checkpoints/sanity"),
                 "required_storage_bytes": required, "precision": "bf16", "quantization": None,
                 "max_length": manifest["max_length"], "num_train_epochs": 1, "seed": 42,
                 "learning_rate": 1e-4, "per_device_train_batch_size": 1,
                 "gradient_accumulation_steps": 16, "optimizer": "adamw_torch",
                 "lr_scheduler_type": "cosine", "warmup_ratio": 0.05, "weight_decay": 0.01,
                 "max_grad_norm": 1.0, "gradient_checkpointing_use_reentrant": False,
                 "lora_r": 16, "lora_alpha": 32, "lora_dropout": 0.05, "lora_bias": "none",
                 "target_modules": TARGET_MODULES,
                 "packing": False, "checkpoint_selection": "final_epoch_1",
                 "estimated_lora_parameters": lora_count, "actual_tokenized_bytes": dataset_bytes,
                 "loss_implementation": "Full causal context; exact completion loss; ignored LM projections omitted",
                 "b0_code_commit": frozen["code_commit"], "uses_sanity_adapter_as_formal_initialization": False}
        sft_review = review / "sft1"
        sft_review.mkdir(exist_ok=True)
        train_path = sft_review / "TRAIN_CONFIG.json"
        write(train_path, train)
        write(review / "freezes/SFT1_FREEZE.json", {"training": train,
              "data_manifest_sha256": hashlib.sha256(Path(train["data_manifest"]).read_bytes()).hexdigest(),
              "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=config["repo"], text=True).strip(),
              "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in Path(config["repo"]).joinpath("experiments/posttraining/research").glob("*.py")},
              "b0_source_exceptions_explicit": bool(plan.get("boundary_addendum")), "max_epochs": 1,
              "eval_comparability_sha256": hashlib.sha256((review / "EVAL_COMPARABILITY.json").read_bytes()).hexdigest() if plan.get("boundary_addendum") else None})
        environment = dict(os.environ, CUDA_VISIBLE_DEVICES=str(config["physical_gpu"]))
        for stage in ("sanity", "train"):
            idle_gpu(config)
            state = json.loads(state_path.read_text())
            state.update(status="SFT1_" + stage.upper() + "_RUNNING")
            write(state_path, state)
            with (runtime / "logs" / ("SFT1_" + stage.upper() + ".log")).open("x") as log:
                subprocess.run([sys.executable, "-m", "experiments.posttraining.research.sft_execution",
                                "--config", str(train_path), "--stage", stage],
                               cwd=config["repo"], env=environment, stdout=log, stderr=subprocess.STDOUT, check=True)
        result_path = output / "TRAIN_RESULT.json"
        trained = json.loads(result_path.read_text())
        if trained["status"] != "SFT1_TRAINED" or trained["epochs"] != 1:
            raise RuntimeError("No complete one-epoch final adapter")
        write(sft_review / "TRAIN_RESULT.json", trained)
        write(sft_review / "SANITY_RESULT.json", json.loads((Path(train["sanity_directory"]) / "SANITY_RESULT.json").read_text()))
        state.update(status="SFT1_TRAINED", completed_stages=list(dict.fromkeys(state.get("completed_stages", []) + ["SFT1_TRAINED"])))
        write(state_path, state)
        unchanged_b0(plan)
        idle_gpu(config)
        with (runtime / "logs/SFT1_ADAPTER_BINDING.log").open("x") as log:
            subprocess.run([sys.executable, "-m", "experiments.posttraining.research.adapter_binding",
                            "--config", str(train_path), "--output", str(sft_review / "ADAPTER_BINDING.json")],
                           cwd=config["repo"], env=environment, stdout=log, stderr=subprocess.STDOUT, check=True)
        config.update(final_adapter=trained["final_adapter"], remaining_gpu_seconds=8 * 3600)
        write(Path(plan["experiment_config"]), config)
        idle_gpu(config)
        with (runtime / "logs/SFT1_EVAL_DRIVER.log").open("x") as log:
            subprocess.run([sys.executable, "-m", "experiments.posttraining.research.execution", "--plan", str(plan_path),
                            "--stage", "sft_eval"], cwd=config["repo"], stdout=log, stderr=subprocess.STDOUT, check=True)
        state = json.loads(state_path.read_text())
        if state["status"] != "SFT_EVAL_COMPLETE":
            raise RuntimeError("Reevaluation incomplete")
        state.update(status="EXPERIMENT_COMPLETE", completed_stages=list(dict.fromkeys(state.get("completed_stages", []) + ["EXPERIMENT_COMPLETE"])))
        write(state_path, state)
    except BaseException as exc:
        state = json.loads(state_path.read_text())
        if state["status"] not in ("B0_BLOCKED", "DATA_COLLECTION_BLOCKED", "DATASET_INSUFFICIENT", "SFT_EVAL_BLOCKED"):
            state.update(status="EXPERIMENT_BLOCKED", blocker_type=type(exc).__name__, blocker_reason=str(exc))
            write(state_path, state)
        raise
    finally:
        summary = archive(plan)
        # A small public result summary is allowed; episode data and targets are
        # referenced only in the ignored audit/NAS locations.
        public = Path(config["repo"]) / "experiments/posttraining/research/LATEST_RESULT.md"
        public.write_text("# First policy SFT experiment\n\n" +
                f"Status: `{summary['status']}`. PT-B0-R1-CONTRACTFIX: {summary['b0']['completed']}/30; " +
                f"PT-SFT1: {summary['sft1']['completed']}/30. Estimated/reserved DeepSeek cost: " +
                f"USD{summary['budget']['estimated_or_reserved_usd']:.6f}; total authorization USD10.\n\n" +
                "Shared capabilities, concrete causal-template and episode isolation; limited DEV comparison. " +
                "Explicit authorized exceptions: two Outbox error-payload type checks and versioned experiment boundary rejection. No clean-test claim, " +
                "second SFT, RL or deployment. Full data/trajectories/models/database remain outside Git.\n")
        # An incomplete B0 may still need its single infrastructure recovery.
        # Do not change HEAD in that case and invalidate its original freeze.
        if summary["b0"]["completed"] == 30 or plan.get("boundary_addendum"):
            checkpoint(helper, "docs: archive first policy SFT experiment outcome", "FINAL_PIPELINE", config["repo"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--definitions", type=Path, required=True)
    parser.add_argument("--checkpoint-helper", type=Path, required=True)
    parser.add_argument("--wait-for-b0", action="store_true")
    args = parser.parse_args()
    run(args.plan, args.definitions, args.checkpoint_helper, args.wait_for_b0)


if __name__ == "__main__":
    main()
