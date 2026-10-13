"""Verify completed epoch64, evaluate frozen VALID and publish identical weights.

This executable never enters training, creates an optimizer or initializes LoRA.
The original checkpoints, hashes, train configuration and freeze stay read-only.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import time

from ..policy import MODEL_REVISION
from .runner import write
from .train import verified_rows

VERSION = "PT-SFT1-FINALIZE-R1"
REQUIRED = {"adapter_config.json", "adapter_model.safetensors", "trainer_state.json",
            "optimizer.pt", "scheduler.pt", "rng_state.pth", "training_args.bin", "README.md"}


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024**2), b""):
            value.update(chunk)
    return value.hexdigest()


def expected_shapes(base, rank, modules):
    hidden, intermediate = base["hidden_size"], base["intermediate_size"]
    query = base["num_attention_heads"] * base["head_dim"]
    kv = base["num_key_value_heads"] * base["head_dim"]
    dimensions = {"q_proj": (hidden, query), "k_proj": (hidden, kv),
                  "v_proj": (hidden, kv), "o_proj": (query, hidden),
                  "gate_proj": (hidden, intermediate), "up_proj": (hidden, intermediate),
                  "down_proj": (intermediate, hidden)}
    result = {}
    for layer in range(base["num_hidden_layers"]):
        for module in modules:
            before, after = dimensions[module]
            section = "self_attn" if module.endswith("_proj") and module[0] in "qkvo" else "mlp"
            prefix = f"base_model.model.model.layers.{layer}.{section}.{module}"
            result[prefix + ".lora_A.weight"] = [rank, before]
            result[prefix + ".lora_B.weight"] = [after, rank]
    return result


def check_state(state):
    if (state.get("global_step") != 64 or state.get("max_steps") != 64
            or state.get("epoch") != 1.0):
        raise ValueError("Final checkpoint must record step64/max64/epoch1.0; no fallback")


def check_adapter(adapter, config):
    expected = {"r": config["lora_r"], "lora_alpha": config["lora_alpha"],
                "lora_dropout": config["lora_dropout"], "bias": config["lora_bias"],
                "peft_type": "LORA", "task_type": "CAUSAL_LM", "inference_mode": True,
                "base_model_name_or_path": config["model_path"]}
    if any(adapter.get(key) != value for key, value in expected.items()):
        raise ValueError("Adapter configuration differs from original training freeze")
    if set(adapter["target_modules"]) != set(config["target_modules"]):
        raise ValueError("LoRA modules differ from freeze")
    if adapter.get("revision") not in (None, MODEL_REVISION):
        raise ValueError("Adapter base revision differs from lock")
    if any(adapter.get(key) for key in ("rank_pattern", "alpha_pattern", "modules_to_save",
                                      "use_dora", "use_rslora", "use_qalora")):
        raise ValueError("Unexpected additional adapter behavior")


def verify(config_path, freeze_path, evidence_path, model_lock_path, model_hash_path):
    config = json.loads(Path(config_path).read_text())
    freeze = json.loads(Path(freeze_path).read_text())
    if config != freeze["training"] or config["revision"] != MODEL_REVISION:
        raise ValueError("Original training config/revision changed")
    if digest(config["data_manifest"]) != freeze["data_manifest_sha256"]:
        raise ValueError("Data manifest changed")
    rows, manifest = verified_rows(config)
    valid = [json.loads(s) for s in Path(manifest["sft_valid"]["path"]).read_text().splitlines()]
    if len(valid) != 320 or manifest["sft_valid"]["samples"] != 320:
        raise ValueError("Exactly the frozen320 VALID decisions required")
    for row in valid:
        length = row["prompt_length"]
        if (row["split"] != "sft_valid" or row["source_type"] != "synthetic_rule_expert"
                or row["labels"][:length] != [-100] * length
                or row["labels"][length:] != row["input_ids"][length:]
                or len(row["input_ids"]) > config["max_length"]):
            raise ValueError("VALID source/loss mask/length mismatch")
    output = Path(config["output_directory"])
    checkpoint = output / "checkpoint-64"
    if checkpoint.is_symlink() or not checkpoint.is_dir():
        raise ValueError("Missing real checkpoint64 directory")
    evidence = json.loads(Path(evidence_path).read_text())
    records = evidence["checkpoint_hashes"]
    for name, record in records.items():
        path = output / name
        if (not path.is_file() or path.is_symlink() or path.stat().st_size != record["bytes"]
                or digest(path) != record["sha256"]):
            raise ValueError("Preserved checkpoint/evidence hash mismatch: " + name)
    checkpoint_files = {p.name for p in checkpoint.iterdir() if p.is_file()}
    if not REQUIRED.issubset(checkpoint_files):
        raise ValueError("Checkpoint64 files incomplete")
    if any("checkpoint-64/" + name not in records for name in checkpoint_files):
        raise ValueError("Checkpoint64 file lacks pre-recovery hash")
    state = json.loads((checkpoint / "trainer_state.json").read_text())
    check_state(state)
    adapter = json.loads((checkpoint / "adapter_config.json").read_text())
    check_adapter(adapter, config)
    model_path = Path(config["model_path"])
    lock = json.loads(Path(model_lock_path).read_text())
    if (lock["revision"] != MODEL_REVISION or lock["repo_id"] != "Qwen/Qwen3-8B"
            or Path(lock["path"]).resolve() != model_path.resolve()):
        raise ValueError("Locked official model lineage differs")
    model_hashes = json.loads(Path(model_hash_path).read_text())
    for name, record in model_hashes.items():
        path = model_path / name
        if path.stat().st_size != record["size_bytes"] or digest(path) != record["sha256"]:
            raise ValueError("Locked model hash mismatch: " + name)
    base = json.loads((model_path / "config.json").read_text())
    shapes = expected_shapes(base, config["lora_r"], config["target_modules"])
    import torch
    from safetensors import safe_open

    with safe_open(checkpoint / "adapter_model.safetensors", framework="pt", device="cpu") as weights:
        if set(weights.keys()) != set(shapes):
            raise ValueError("Unexpected/missing adapter tensor")
        for name, shape in shapes.items():
            tensor = weights.get_tensor(name)
            if list(tensor.shape) != shape or tensor.dtype != torch.float32:
                raise ValueError("Adapter tensor shape/dtype mismatch: " + name)
            if not torch.isfinite(tensor).all():
                raise ValueError("NaN/Inf in adapter weights: " + name)
    parameters = sum(math.prod(s) for s in shapes.values())
    if parameters != config["estimated_lora_parameters"]:
        raise ValueError("Adapter parameter count differs from freeze")
    report = {"status": "CHECKPOINT64_VERIFIED", "version": VERSION,
              "global_step": 64, "max_steps": 64, "epoch": 1.0,
              "checkpoint_files": sorted(checkpoint_files), "old_evidence_unchanged": True,
              "tensor_count": len(shapes), "parameter_count": parameters, "all_weights_finite": True,
              "shape_dtype_verified": True, "serialized_adapter_revision": adapter.get("revision"),
              "resolved_revision": MODEL_REVISION, "revision_lineage": "original freeze and full locked base hashes",
              "model_files_hash_verified": len(model_hashes), "train_samples": len(rows),
              "valid_samples": len(valid), "additional_optimizer_steps": 0,
              "model_loads": 0, "model_generations": 0, "api_calls": 0,
              "old_checkpoint_sha256": {name: rec["sha256"] for name, rec in records.items()}}
    report["last_logged_training_metrics"] = next(
        (row for row in reversed(state["log_history"]) if "loss" in row), None
    )
    return config, manifest, valid, report


def evaluate(config, valid, checkpoint, output, audit, manifest, verification):
    import torch
    from datasets import Dataset
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer, TrainingArguments, set_seed
    from .completion_loss import trainer_class
    from .formatting import CompletionCollator

    if torch.cuda.device_count() != 1:
        raise RuntimeError("Exactly one assigned GPU required")
    if output.exists():
        raise RuntimeError("Finalization output exists; explicit audited continuation required")
    output.mkdir(parents=True)
    started = time.monotonic()
    torch.cuda.reset_peak_memory_stats()
    set_seed(config["seed"])
    tokenizer = AutoTokenizer.from_pretrained(config["model_path"], local_files_only=True)
    base = AutoModelForCausalLM.from_pretrained(config["model_path"], local_files_only=True,
            torch_dtype=torch.bfloat16, attn_implementation="sdpa", device_map={"": "cuda:0"})
    model = PeftModel.from_pretrained(base, checkpoint, is_trainable=False)
    model.config.use_cache = False
    model.eval()
    if any(p.requires_grad for p in model.parameters()):
        raise ValueError("Finalization model has trainable parameters")
    load_seconds = time.monotonic() - started
    arguments = TrainingArguments(output_dir=str(output / "evaluation-runtime"),
        num_train_epochs=1, seed=config["seed"], learning_rate=config["learning_rate"],
        per_device_train_batch_size=1, per_device_eval_batch_size=1,
        gradient_accumulation_steps=16, optim="adamw_torch", lr_scheduler_type="cosine",
        warmup_ratio=0.05, weight_decay=0.01, max_grad_norm=1.0, bf16=True,
        gradient_checkpointing=True, gradient_checkpointing_kwargs={"use_reentrant": False},
        report_to=[], save_strategy="steps", save_steps=50, save_total_limit=2,
        logging_steps=1, remove_unused_columns=False, load_best_model_at_end=False,
        push_to_hub=False, prediction_loss_only=True)
    trainer = trainer_class()(model=model, args=arguments,
        eval_dataset=Dataset.from_list([{k: row[k] for k in ("input_ids", "attention_mask", "labels")}
                                       for row in valid]),
        data_collator=CompletionCollator(tokenizer.pad_token_id))
    trainer.model_accepts_loss_kwargs = True
    metrics = trainer.evaluate(metric_key_prefix="final_sft_valid")
    if trainer.optimizer is not None or trainer.state.global_step != 0:
        raise RuntimeError("Evaluation created optimizer/update state")
    if not math.isfinite(metrics["final_sft_valid_loss"]):
        raise RuntimeError("Nonfinite frozen VALID loss")
    final = output / "FINAL_ADAPTER"
    final.mkdir()
    for name in ("adapter_config.json", "adapter_model.safetensors", "README.md"):
        shutil.copyfile(checkpoint / name, final / name)
        if digest(final / name) != digest(checkpoint / name):
            raise ValueError("Published adapter differs from final epoch checkpoint")
    hashes = {p.name: digest(p) for p in final.iterdir()}
    lock = {"version": VERSION, "source_checkpoint": str(checkpoint), "source_global_step": 64,
            "source_epoch": 1.0, "model_revision": MODEL_REVISION, "adapter_directory": str(final),
            "adapter_sha256": hashes, "identical_to_checkpoint64": True,
            "new_optimizer_steps": 0, "random_lora_initialized": False}
    write(audit / "FINAL_ADAPTER_LOCK.json", lock)
    report = {"status": "SFT1_TRAINED", "finalization_version": VERSION, "epochs": 1.0,
              "optimizer_steps": 64, "additional_optimizer_steps": 0,
              "train_samples": manifest["train"]["samples"], "valid_samples": len(valid),
              "initial_validation": None, "initial_validation_status": "unavailable",
              "formal_train_peak_memory": None, "formal_train_elapsed_seconds": None,
              "formal_train_missing_metrics": "unavailable; failed process did not persist these metrics",
              "train_metrics": {"last_logged_metrics": verification["last_logged_training_metrics"],
                                "source": "preserved checkpoint64 log_history"},
              "final_validation": metrics, "formal_initialization": "original fresh seed42 adapter; sanity excluded",
              "final_adapter": str(final), "adapter_hashes": hashes, "selection": "final_epoch_1",
              "finalization": {"load_seconds": load_seconds, "elapsed_seconds": time.monotonic() - started,
                               "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
                               "peak_reserved_bytes": torch.cuda.max_memory_reserved(),
                               "model_dtype": str(model.dtype), "device": str(model.device),
                               "eval": not model.training, "optimizer_created": False},
              "model_generations": 0, "api_calls": 0}
    write(output / "TRAIN_RESULT.json", report)
    write(audit / "VALID_RESULTS.json", report)
    verification["published_adapter_hashes"] = hashes
    write(audit / "CHECKPOINT64_VERIFICATION.json", verification)
    return report


def main():
    parser = argparse.ArgumentParser()
    for name in ("config", "freeze", "evidence", "model-lock", "model-hashes", "output", "audit"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    config, manifest, valid, verification = verify(args.config, args.freeze, args.evidence,
                                                 args.model_lock, args.model_hashes)
    args.audit.mkdir(parents=True, exist_ok=True)
    write(args.audit / "CHECKPOINT64_VERIFICATION.json", verification)
    if args.validate_only:
        print(json.dumps({k: verification[k] for k in ("status", "tensor_count", "valid_samples", "additional_optimizer_steps")}))
        return
    if not args.output.resolve().is_relative_to(Path(config["large_storage"]).resolve()):
        raise ValueError("Finalized adapter and runtime must remain on NAS")
    if shutil.disk_usage(config["large_storage"]).free < config["required_storage_bytes"] + 10 * 1024**3:
        raise RuntimeError("NAS reserve insufficient")
    evaluate(config, valid, Path(config["output_directory"]) / "checkpoint-64", args.output,
             args.audit, manifest, verification)


if __name__ == "__main__":
    main()
