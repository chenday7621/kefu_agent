"""Separate <=2-step sanity then one fresh frozen LoRA epoch; no generation."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import time

from .completion_loss import completion_loss, trainer_class
from .formatting import CompletionCollator
from .train import TARGET_MODULES, verified_rows
from ..policy import MODEL_REVISION


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def checkpoint_directories(output):
    """Native checkpoint directories only; adjacent audit files are evidence."""
    return sorted(
        (p for p in Path(output).glob("checkpoint-*")
         if not p.is_symlink() and p.is_dir()
         and re.fullmatch(r"checkpoint-[0-9]+", p.name)),
        key=lambda p: int(p.name.split("-")[1]),
    )


def model_and_tokenizer(config):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, set_seed
    from peft import get_peft_model, LoraConfig, TaskType

    if torch.cuda.device_count() != 1:
        raise RuntimeError("Exactly one assigned GPU required")
    if config["revision"] != MODEL_REVISION:
        raise ValueError("Locked revision required")
    set_seed(42)
    tokenizer = AutoTokenizer.from_pretrained(config["model_path"], local_files_only=True)
    start = time.monotonic()
    base = AutoModelForCausalLM.from_pretrained(config["model_path"], local_files_only=True,
            torch_dtype=torch.bfloat16, attn_implementation="sdpa", device_map={"": "cuda:0"})
    load_seconds = time.monotonic() - start
    present = {n.split(".")[-1] for n, _ in base.named_modules()}
    if not set(TARGET_MODULES).issubset(present):
        raise ValueError("LoRA target modules absent")
    model = get_peft_model(base, LoraConfig(task_type=TaskType.CAUSAL_LM, r=16,
            lora_alpha=32, lora_dropout=0.05, bias="none", target_modules=TARGET_MODULES))
    model.config.use_cache = False
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()
    names = {n: p.numel() for n, p in model.named_parameters() if p.requires_grad}
    if not names or not all("lora_" in n for n in names):
        raise ValueError("Only LoRA parameters may be trainable")
    return model, tokenizer, names, load_seconds


def memory():
    import torch

    return {"peak_allocated_bytes": torch.cuda.max_memory_allocated(),
            "peak_reserved_bytes": torch.cuda.max_memory_reserved()}


def sanity(config, rows, manifest):
    import torch
    from peft import PeftModel

    output = Path(config["sanity_directory"])
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    torch.cuda.reset_peak_memory_stats()
    model, tokenizer, names, load_seconds = model_and_tokenizer(config)
    model.train()
    longest = max(rows, key=lambda r: len(r["input_ids"]))
    batch = {k: v.to("cuda:0") for k, v in CompletionCollator(tokenizer.pad_token_id)([longest]).items()}
    parameters = {n: p for n, p in model.named_parameters() if p.requires_grad}
    original = {n: p.detach().cpu().clone() for n, p in parameters.items()}
    optimizer = torch.optim.AdamW(parameters.values(), lr=1e-4, weight_decay=0.01)
    losses, gradient_counts = [], []
    for _ in range(2):
        optimizer.zero_grad(set_to_none=True)
        loss, _ = completion_loss(model, batch)
        if not torch.isfinite(loss):
            raise RuntimeError("Nonfinite longest-sample sanity loss")
        loss.backward()
        gradients = [p.grad for p in parameters.values() if p.grad is not None]
        if not gradients or not all(torch.isfinite(g).all() for g in gradients):
            raise RuntimeError("Missing/nonfinite LoRA sanity gradients")
        if any(p.grad is not None for n, p in model.named_parameters() if n not in parameters):
            raise RuntimeError("Base parameter received a gradient")
        gradient_counts.append(sum(g.count_nonzero().item() for g in gradients))
        torch.nn.utils.clip_grad_norm_(parameters.values(), 1.0)
        optimizer.step()
        losses.append(loss.item())
    changed = [n for n, p in parameters.items() if not torch.equal(original[n], p.detach().cpu())]
    if not changed or not all(gradient_counts):
        raise RuntimeError("LoRA sanity weights did not change")
    model.eval()
    with torch.inference_mode():
        reference, _ = completion_loss(model, batch)
    adapter = output / "SANITY_ONLY_ADAPTER"
    model.save_pretrained(adapter, safe_serialization=True)
    saved = {n: p.detach().cpu().clone() for n, p in parameters.items()}
    del optimizer, original, parameters
    base = model.unload()
    del model
    model = PeftModel.from_pretrained(base, adapter, is_trainable=False)
    model.eval()
    for n, p in model.named_parameters():
        if n in saved and not torch.equal(saved[n], p.detach().cpu()):
            raise RuntimeError("Saved/reloaded LoRA parameter mismatch")
    with torch.inference_mode():
        reloaded, _ = completion_loss(model, batch)
    torch.testing.assert_close(reference, reloaded, rtol=1e-4, atol=1e-4)
    report = {"status": "SANITY_PASS", "optimizer_steps": 2, "episodes": [longest["episode_id"]],
              "split": "train", "sample_id": longest["sample_id"], "longest_input_tokens": len(longest["input_ids"]),
              "max_length": manifest["max_length"], "losses": losses, "gradient_nonzero_counts": gradient_counts,
              "changed_lora_parameters": len(changed), "trainable_parameters": names,
              "base_frozen": True, "save_reload_pass": True, "counts_as_formal_training": False,
              "load_seconds": load_seconds, "elapsed_seconds": time.monotonic() - started, **memory()}
    write(output / "SANITY_RESULT.json", report)
    return report


def formal(config, rows, manifest, resume=None):
    import torch
    from datasets import Dataset
    from transformers import TrainingArguments, TrainerCallback

    output = Path(config["output_directory"])
    report_path = Path(config["sanity_directory"]) / "SANITY_RESULT.json"
    if not report_path.exists() or json.loads(report_path.read_text())["status"] != "SANITY_PASS":
        raise ValueError("Separate longest-sample sanity required")
    if (output / "FINAL_ADAPTER").exists():
        raise RuntimeError("Completed epoch exists; second training forbidden")
    if output.exists() and any(output.iterdir()) and not resume:
        raise RuntimeError("Prior formal attempt exists; no restart from fresh weights")
    output.mkdir(parents=True, exist_ok=True)
    if resume:
        marker = output / "TRAIN_RESUMED_ONCE.json"
        if marker.exists() or not Path(resume).is_relative_to(output):
            raise RuntimeError("At most one owned checkpoint recovery")
        write(marker, {"checkpoint": str(resume), "created_utc": datetime.now(timezone.utc).isoformat()})
    torch.cuda.reset_peak_memory_stats()
    started = time.monotonic()
    model, tokenizer, names, load_seconds = model_and_tokenizer(config)
    write(output / "TRAINABLE_PARAMETERS.json", names)
    valid = [json.loads(s) for s in Path(manifest["sft_valid"]["path"]).read_text().splitlines()]
    for row in valid:
        n = row["prompt_length"]
        if (row["split"] != "sft_valid" or row["source_type"] != "synthetic_rule_expert"
                or row["labels"][:n] != [-100] * n or row["labels"][n:] != row["input_ids"][n:]):
            raise ValueError("Validation split/source/loss mask differs from frozen data")
    def dataset(data):
        return Dataset.from_list([{k: r[k] for k in ("input_ids", "attention_mask", "labels")} for r in data])

    class Guard(TrainerCallback):
        def on_pre_optimizer_step(self, args, state, control, model=None, **kwargs):
            if any(not torch.isfinite(p.grad).all() for p in model.parameters() if p.grad is not None):
                raise RuntimeError("Nonfinite formal gradient; no hidden numerical retry")
            return control

        def on_log(self, args, state, control, logs=None, **kwargs):
            if any(not math.isfinite(float(v)) for k, v in (logs or {}).items()
                   if "loss" in k and isinstance(v, (int, float))):
                raise RuntimeError("Nonfinite formal loss; preserve failed attempt")
            return control

        def on_step_end(self, args, state, control, **kwargs):
            if time.monotonic() - started > 6 * 3600:
                control.should_save = True
                control.should_training_stop = True
            return control

        def on_save(self, args, state, control, **kwargs):
            checkpoints = checkpoint_directories(output)
            for path in checkpoints:
                record = {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in path.rglob("*") if p.is_file()}
                write(output / (path.name + "-HASHES.json"), record)
            return control

    arguments = TrainingArguments(output_dir=str(output), num_train_epochs=1, seed=42,
            learning_rate=1e-4, per_device_train_batch_size=1, per_device_eval_batch_size=1,
            gradient_accumulation_steps=16, optim="adamw_torch", lr_scheduler_type="cosine",
            warmup_ratio=0.05, weight_decay=0.01, max_grad_norm=1.0, bf16=True,
            gradient_checkpointing=True, gradient_checkpointing_kwargs={"use_reentrant": False},
            report_to=[], save_strategy="steps", save_steps=50, save_total_limit=2,
            logging_steps=1, remove_unused_columns=False, load_best_model_at_end=False,
            push_to_hub=False, prediction_loss_only=True)
    trainer = trainer_class()(model=model, args=arguments, train_dataset=dataset(rows),
            eval_dataset=dataset(valid), data_collator=CompletionCollator(tokenizer.pad_token_id), callbacks=[Guard()])
    trainer.model_accepts_loss_kwargs = True
    optimizer = trainer.create_optimizer()
    allowed = {id(p) for p in model.parameters() if p.requires_grad}
    actual = {id(p) for g in optimizer.param_groups for p in g["params"]}
    if actual != allowed:
        raise RuntimeError("Optimizer includes unexpected parameters")
    before = trainer.evaluate(metric_key_prefix="initial_sft_valid") if not resume else None
    result = trainer.train(resume_from_checkpoint=str(resume) if resume else None)
    if trainer.state.epoch is None or abs(trainer.state.epoch - 1.0) > 1e-8:
        raise RuntimeError("Partial epoch is not the final SFT1")
    final = trainer.evaluate(metric_key_prefix="final_sft_valid")
    if not math.isfinite(final["final_sft_valid_loss"]):
        raise RuntimeError("Nonfinite validation loss; final adapter not published")
    adapter = output / "FINAL_ADAPTER"
    model.save_pretrained(adapter, safe_serialization=True)
    report = {"status": "SFT1_TRAINED", "epochs": trainer.state.epoch,
              "optimizer_steps": trainer.state.global_step, "train_samples": len(rows),
              "supervised_tokens": sum(sum(t != -100 for t in r["labels"]) for r in rows),
              "total_input_tokens": sum(len(r["input_ids"]) for r in rows),
              "initial_validation": before, "final_validation": final, "train_metrics": result.metrics,
              "load_seconds": load_seconds, "elapsed_seconds": time.monotonic() - started,
              "formal_initialization": "fresh seed42 adapter; never sanity adapter", "final_adapter": str(adapter),
              "adapter_hashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in adapter.iterdir() if p.is_file()},
              "selection": "final_epoch_1", "model_generations": 0, **memory()}
    write(output / "TRAIN_RESULT.json", report)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--stage", choices=["sanity", "train"], required=True)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    rows, manifest = verified_rows(config)
    if args.validate_only:
        print(json.dumps({"verified": True, "rows": len(rows), "model_loads": 0}))
        return
    storage = Path(config["large_storage"]).resolve()
    for path in (config["sanity_directory"], config["output_directory"]):
        if not Path(path).resolve().is_relative_to(storage):
            raise ValueError("Adapter/optimizer/runtime must remain in dedicated NAS")
    if shutil.disk_usage(storage).free < config["required_storage_bytes"] + 10 * 1024**3:
        raise RuntimeError("TRAIN_RESOURCE_BLOCKED: insufficient NAS plus10GiB reserve")
    try:
        if args.stage == "sanity":
            sanity(config, rows, manifest)
        else:
            formal(config, rows, manifest, args.resume)
    except BaseException as exc:
        path = Path(config["large_storage"]) / (args.stage.upper() + "_BLOCKER.json")
        write(path, {"status": "TRAIN_RESOURCE_BLOCKED" if "out of memory" in str(exc).lower() else "SFT_BLOCKED",
                     "type": type(exc).__name__, "reason": str(exc), "automatic_retry": False})
        raise


if __name__ == "__main__":
    main()
