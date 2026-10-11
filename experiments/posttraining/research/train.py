"""One frozen LoRA epoch; refuses unverified/DEV/undersized datasets."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil

from ..policy import MODEL_REVISION
from .formatting import CompletionCollator

TARGET_MODULES = ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]


def verified_rows(config):
    manifest = json.loads(Path(config["data_manifest"]).read_text())
    if manifest.get("status") != "DATASET_VERIFIED" or not manifest.get("split_approved"):
        raise ValueError("Independent data/split verification required before any model load")
    for split in ("train", "sft_valid"):
        entry = manifest[split]
        path = Path(entry["path"])
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("Tokenized dataset hash mismatch")
    rows = [json.loads(x) for x in Path(manifest["train"]["path"]).read_text().splitlines()]
    if len(rows) < 1000 or len({r["episode_id"] for r in rows}) < 100:
        raise ValueError("DATASET_INSUFFICIENT")
    if manifest["sft_valid"]["episodes"] < 20:
        raise ValueError("SFT-VALID episode minimum not met")
    if (
        sum(r["action"] == "CALL" for r in rows) < 200
        or sum(r.get("hard_no_call", False) for r in rows) < 200
    ):
        raise ValueError("CALL/hard NO_CALL minimum not met")
    for row in rows:
        if row["split"] != "train" or row["source_type"] != "synthetic_rule_expert":
            raise ValueError("DEV/oracle/non-training sample excluded")
        n = row["prompt_length"]
        if row["labels"][:n] != [-100] * n or row["labels"][n:] != row["input_ids"][n:]:
            raise ValueError("Completion-only labels mismatch")
    return rows, manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    rows, manifest = verified_rows(config)
    if args.validate_only:
        print(json.dumps({"verified": True, "rows": len(rows)}))
        return
    if config["revision"] != MODEL_REVISION or config.get("sanity_status") != "SANITY_PASS":
        raise ValueError("Revision and separate longest-sample/2-step sanity evidence required")
    output = Path(config["output_directory"]).resolve()
    if not output.is_relative_to(Path(config["large_storage"]).resolve()):
        raise ValueError("Training writes must remain on the configured large storage")
    if shutil.disk_usage(output.parent).free < config["required_storage_bytes"] + 10 * 1024**3:
        raise RuntimeError("TRAIN_RESOURCE_BLOCKED: 10GiB reserve required")
    if (output / "FINAL_ADAPTER").exists():
        raise RuntimeError("One completed epoch already exists; second run forbidden")
    import torch
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        Trainer,
        TrainingArguments,
        TrainerCallback,
        set_seed,
    )
    from peft import LoraConfig, TaskType, get_peft_model
    from datasets import Dataset

    if torch.cuda.device_count() != 1:
        raise RuntimeError("Exactly one idle assigned GPU is required")
    set_seed(42)
    tokenizer = AutoTokenizer.from_pretrained(config["model_path"], local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(
        config["model_path"],
        local_files_only=True,
        torch_dtype=torch.bfloat16,
        attn_implementation="sdpa",
    )
    present = {name.split(".")[-1] for name, _ in model.named_modules()}
    if not set(TARGET_MODULES).issubset(present):
        raise ValueError("Declared LoRA modules missing from the actual locked model")
    lora = LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=16,
        lora_alpha=32,
        lora_dropout=0.05,
        bias="none",
        target_modules=TARGET_MODULES,
    )
    model = get_peft_model(model, lora)
    model.config.use_cache = False
    trainable = {n: p.numel() for n, p in model.named_parameters() if p.requires_grad}
    assert trainable and all("lora_" in n for n in trainable)
    output.mkdir(parents=True, exist_ok=True)
    (output / "TRAINABLE_PARAMETERS.json").write_text(json.dumps(trainable, indent=2) + "\n")
    from time import monotonic

    class TimeLimit(TrainerCallback):
        def __init__(self):
            self.started = monotonic()

        def on_step_end(self, args, state, control, **kwargs):
            if monotonic() - self.started > 6 * 3600:
                control.should_save = True
                control.should_training_stop = True
            return control

    arguments = TrainingArguments(
        output_dir=str(output),
        num_train_epochs=1,
        seed=42,
        learning_rate=1e-4,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=16,
        optim="adamw_torch",
        lr_scheduler_type="cosine",
        warmup_ratio=0.05,
        weight_decay=0.01,
        max_grad_norm=1.0,
        bf16=True,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        report_to=[],
        save_strategy="steps",
        save_steps=50,
        save_total_limit=2,
        logging_steps=1,
        remove_unused_columns=False,
        load_best_model_at_end=False,
        push_to_hub=False,
    )
    trainer = Trainer(
        model=model,
        args=arguments,
        train_dataset=Dataset.from_list(
            [{k: r[k] for k in ("input_ids", "attention_mask", "labels")} for r in rows]
        ),
        data_collator=CompletionCollator(tokenizer.pad_token_id),
        callbacks=[TimeLimit()],
    )
    result = trainer.train(resume_from_checkpoint=str(args.resume) if args.resume else None)
    if trainer.state.epoch is None or trainer.state.epoch < 1:
        raise RuntimeError("Partial epoch: not PT-SFT1; no final adapter published")
    model.save_pretrained(output / "FINAL_ADAPTER", safe_serialization=True)
    (output / "TRAIN_RESULT.json").write_text(json.dumps(result.metrics, indent=2) + "\n")


if __name__ == "__main__":
    main()
