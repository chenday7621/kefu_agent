"""Planned TRAIN-only LoRA binding check; logits forwards, no generation/API."""

import argparse
import hashlib
import json
from pathlib import Path
import time


def verify(config, manifest, trained, output):
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, set_seed

    if torch.cuda.device_count() != 1:
        raise RuntimeError("One assigned GPU required for binding check")
    adapter = Path(trained["final_adapter"])
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in adapter.iterdir() if p.is_file()}
    if hashes != trained["adapter_hashes"]:
        raise RuntimeError("Final adapter hash changed")
    data = [json.loads(s) for s in Path(manifest["train"]["path"]).read_text().splitlines()]
    row = min(data, key=lambda r: r["prompt_length"])
    if row["split"] != "train" or row["episode_id"].startswith("PTDEV-"):
        raise ValueError("Binding check cannot use DEV")
    set_seed(42)
    start = time.monotonic()
    torch.cuda.reset_peak_memory_stats()
    base = AutoModelForCausalLM.from_pretrained(config["model_path"], local_files_only=True,
            torch_dtype=torch.bfloat16, device_map={"": "cuda:0"}, attn_implementation="sdpa")
    base.eval()
    inputs = torch.tensor([row["input_ids"][:row["prompt_length"]]], device="cuda:0")
    kwargs = dict(input_ids=inputs, attention_mask=torch.ones_like(inputs), use_cache=False, logits_to_keep=1)
    with torch.inference_mode():
        reference = base(**kwargs).logits.float().cpu()
    model = PeftModel.from_pretrained(base, adapter, is_trainable=False)
    model.eval()
    if model.active_adapters != ["default"]:
        raise RuntimeError("Unexpected active LoRA adapter")
    with model.disable_adapter(), torch.inference_mode():
        disabled = model(**kwargs).logits.float().cpu()
    torch.testing.assert_close(reference, disabled, rtol=1e-4, atol=1e-4)
    if any(p.requires_grad for p in model.parameters()):
        raise RuntimeError("Inference adapter has trainable parameters")
    nonzero, hooks, participating = [], [], set()
    def observe(name):
        def hook(module, args, result):
            if not module.disable_adapters and "default" in module.active_adapters:
                participating.add(name)
        return hook
    for name, module in model.named_modules():
        if hasattr(module, "lora_B") and "default" in module.lora_B:
            value = module.lora_B["default"].weight
            if not torch.isfinite(value).all():
                raise RuntimeError("Nonfinite final LoRA weight")
            if value.count_nonzero().item():
                nonzero.append(name)
                hooks.append(module.register_forward_hook(observe(name)))
    if not nonzero:
        raise RuntimeError("Final adapter is all zero")
    with torch.inference_mode():
        enabled = model(**kwargs).logits.float().cpu()
    for hook in hooks:
        hook.remove()
    if set(nonzero) != participating:
        raise RuntimeError("Nonzero LoRA modules did not participate in forward")
    model.config.use_cache = True
    if not torch.isfinite(enabled).all():
        raise RuntimeError("Nonfinite adapted inference logits")
    report = {"status": "ADAPTER_BINDING_PASS", "source_split": "train", "sample_id": row["sample_id"],
              "prompt_tokens": row["prompt_length"], "active_adapters": model.active_adapters,
              "adapter_hashes": hashes, "dtype": str(model.dtype), "device": str(model.device),
              "disabled_matches_base": True, "tolerance": {"rtol": 1e-4, "atol": 1e-4},
              "nonzero_lora_B_modules": len(nonzero), "participating_lora_modules": len(participating),
              "enabled_max_abs_logit_delta": (enabled-reference).abs().max().item(),
              "delta_required_for_pass": False, "generations": 0, "api_calls": 0,
              "elapsed_seconds": time.monotonic()-start,
              "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
              "peak_reserved_bytes": torch.cuda.max_memory_reserved(),
              "inference_mode": True, "eval": not model.training, "use_cache_restored": model.config.use_cache}
    Path(output).write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    manifest = json.loads(Path(config["data_manifest"]).read_text())
    trained = json.loads((Path(config["output_directory"]) / "TRAIN_RESULT.json").read_text())
    verify(config, manifest, trained, args.output)


if __name__ == "__main__":
    main()
