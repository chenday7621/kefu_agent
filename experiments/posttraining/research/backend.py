"""One resident Qwen model with serialized candidate generations; no MCP access."""

import asyncio
from time import perf_counter
import json
from pathlib import Path

from parlant.core.nlp.generation_info import UsageInfo
from ..policy import MODEL_REVISION


class ContextLimit(RuntimeError):
    pass


class ResidentQwenBackend:
    origin = "local_qwen"

    def __init__(self, model_path, tokenizer, config, directory, adapter=None):
        import torch
        from transformers import AutoModelForCausalLM, GenerationConfig

        self.torch, self.tokenizer, self.config = torch, tokenizer, config
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock, self.records = asyncio.Lock(), {}
        if config["revision"] != MODEL_REVISION or config["max_new_tokens"] > 1024:
            raise ValueError("Revision or decoding budget mismatch")
        if torch.cuda.device_count() != 1:
            raise ValueError("Exactly one visible GPU required")
        torch.manual_seed(42)
        torch.cuda.manual_seed_all(42)
        torch.cuda.reset_peak_memory_stats()
        start = perf_counter()
        self.model = AutoModelForCausalLM.from_pretrained(
            model_path,
            local_files_only=True,
            trust_remote_code=False,
            torch_dtype=torch.bfloat16,
            device_map={"": "cuda:0"},
            attn_implementation="sdpa",
        )
        if adapter:
            from peft import PeftModel

            self.model = PeftModel.from_pretrained(self.model, adapter, is_trainable=False)
        self.model.eval()
        self.model.config.use_cache = True
        self.generation = GenerationConfig(
            do_sample=False,
            num_beams=1,
            max_new_tokens=config["max_new_tokens"],
            use_cache=True,
            pad_token_id=tokenizer.pad_token_id,
            eos_token_id=self.model.generation_config.eos_token_id,
        )
        effective, _ = self.model._prepare_generation_config(
            self.generation, use_model_defaults=False, do_sample=False
        )
        assert not effective.do_sample and effective.num_beams == 1
        self.generation = effective
        torch.cuda.synchronize()
        self.started = perf_counter()
        self.profile = {
            "load_seconds": self.started - start,
            "dtype": str(self.model.dtype),
            "device": str(self.model.device),
            "revision": MODEL_REVISION,
            "adapter": str(adapter) if adapter else None,
            "allocated_weight_bytes": torch.cuda.memory_allocated(),
            "effective_generation_config": effective.to_dict(),
        }
        (self.directory / "MODEL_PROFILE.json").write_text(
            json.dumps(self.profile, indent=2) + "\n"
        )

    async def complete(self, request):
        # Queue serialization never incorporates another candidate's output.
        async with self.lock:
            pid = request["policy_step_id"]
            if pid in self.records:
                raise RuntimeError("Duplicate policy request; no replay generation")
            if (
                request["model_revision"] != MODEL_REVISION
                or request["enable_thinking"] is not False
            ):
                raise ValueError("Policy request boundary mismatch")
            if perf_counter() - self.started > self.config["gpu_active_seconds"]:
                raise RuntimeError("Stage GPU active-time limit reached")
            ids = self.tokenizer.encode(request["rendered_prompt"], add_special_tokens=False)
            if len(ids) + self.config["max_new_tokens"] > self.config["context_limit"]:
                raise ContextLimit("No prompt truncation allowed")
            self.records[pid] = {"status": "started", "input_tokens": len(ids)}
            return await asyncio.to_thread(self._generate, request, ids)

    def _generate(self, request, ids):
        torch = self.torch
        pid = request["policy_step_id"]
        encoded = torch.tensor([ids], device="cuda:0")
        start = perf_counter()
        with torch.inference_mode():
            output = self.model.generate(
                input_ids=encoded,
                attention_mask=torch.ones_like(encoded),
                generation_config=self.generation,
                use_model_defaults=False,
                do_sample=False,
            )
        torch.cuda.synchronize()
        out = output[0, len(ids) :].tolist()
        raw = self.tokenizer.decode(out, skip_special_tokens=True)
        record = {
            "policy_step_id": pid,
            "generation_origin": "local_qwen",
            "status": "completed",
            "prompt_token_ids": ids,
            "output_token_ids": out,
            "input_tokens": len(ids),
            "output_tokens": len(out),
            "latency_seconds": perf_counter() - start,
            "raw_model_output": raw,
            "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
            "peak_reserved_bytes": torch.cuda.max_memory_reserved(),
        }
        self.records[pid] = record
        with (self.directory / "GENERATIONS.jsonl").open("a") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        return raw

    def usage_for(self, policy_step_id):
        row = self.records[policy_step_id]
        return UsageInfo(row["input_tokens"], row["output_tokens"], {"local_qwen": 1})


class RemoteQwenBackend:
    origin = "local_qwen"

    def __init__(self, url, capability):
        from urllib.parse import urlparse

        if urlparse(url).hostname != "127.0.0.1":
            raise ValueError("Policy server must be the owned loopback process")
        self.url, self.capability, self.records = url, capability, {}

    async def complete(self, request):
        from .boundary import LAST_REQUEST

        LAST_REQUEST.set(request)
        import httpx

        async with httpx.AsyncClient(trust_env=False, timeout=600) as client:
            response = await client.post(
                self.url + "/generate",
                json=request,
                headers={"X-Policy-Capability": self.capability},
            )
            response.raise_for_status()
            data = response.json()
        self.records[request["policy_step_id"]] = data["usage"]
        return data["raw"]

    def usage_for(self, policy_step_id):
        r = self.records[policy_step_id]
        return UsageInfo(r["input_tokens"], r["output_tokens"], {"local_qwen": 1})


def main():
    import argparse
    import hmac
    import uvicorn
    from fastapi import FastAPI, HTTPException, Request
    from transformers import AutoTokenizer

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    config = json.loads(parser.parse_args().config.read_text())
    tokenizer = AutoTokenizer.from_pretrained(config["model_path"], local_files_only=True)
    backend = ResidentQwenBackend(
        config["model_path"], tokenizer, config, config["directory"], config.get("adapter")
    )
    api = FastAPI()

    @api.get("/ready")
    def ready():
        return {"ready": True, "revision": MODEL_REVISION}

    @api.post("/generate")
    async def generate(request: Request):
        if not hmac.compare_digest(
            request.headers.get("X-Policy-Capability", ""), config["capability"]
        ):
            raise HTTPException(403)
        payload = await request.json()
        raw = await backend.complete(payload)
        row = backend.records[payload["policy_step_id"]]
        return {"raw": raw, "usage": {k: row[k] for k in ("input_tokens", "output_tokens")}}

    uvicorn.run(api, host="127.0.0.1", port=config["port"], access_log=False)


if __name__ == "__main__":
    main()
