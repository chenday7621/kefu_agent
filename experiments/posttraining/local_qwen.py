"""Explicit, single-generation local backend for the Phase 1-B smoke only."""

from time import perf_counter
from unittest.mock import patch

import torch
from transformers import AutoModelForCausalLM, GenerationConfig

from parlant.core.nlp.generation_info import UsageInfo
from .trajectory import digest


class LocalQwenSmokeBackend:
    origin = "local_qwen"

    def __init__(self, model_path, tokenizer, config, save):
        self.tokenizer, self.config, self.save = tokenizer, config, save
        self.calls = 0
        self.records = {}
        torch.manual_seed(config["seed"])
        torch.cuda.manual_seed_all(config["seed"])
        assert torch.cuda.device_count() == 1
        torch.cuda.reset_peak_memory_stats()
        started = perf_counter()
        self.model = AutoModelForCausalLM.from_pretrained(
            model_path,
            local_files_only=True,
            trust_remote_code=False,
            torch_dtype=torch.bfloat16,
            device_map={"": "cuda:0"},
            attn_implementation="sdpa",
        ).eval()
        torch.cuda.synchronize()
        assert not self.model.training
        assert all(p.device == torch.device("cuda:0") for p in self.model.parameters())
        assert all(p.dtype == torch.bfloat16 for p in self.model.parameters())
        self.memory = {
            "load_time_seconds": perf_counter() - started,
            "parameter_bytes": sum(p.numel() * p.element_size() for p in self.model.parameters()),
            "buffer_bytes": sum(p.numel() * p.element_size() for p in self.model.buffers()),
            "after_load_allocated_bytes": torch.cuda.memory_allocated(),
            "after_load_reserved_bytes": torch.cuda.memory_reserved(),
            "model_dtype": str(self.model.dtype),
            "device": str(self.model.device),
            "device_map": self.model.hf_device_map,
        }
        self.save("MEMORY_PROFILE.json", self.memory)

    async def complete(self, request):
        if self.calls:
            raise RuntimeError("Phase 1-B permits one generation; retries are forbidden")
        assert request["model_revision"] == self.config["revision"]
        assert request["enable_thinking"] is False
        assert digest(request["rendered_prompt"]) == self.config["frozen_rendered_prompt_hash"]
        pid = request["policy_step_id"]
        encoded = self.tokenizer(
            request["rendered_prompt"], return_tensors="pt", add_special_tokens=False
        ).to("cuda:0")
        self.save("PROMPT.json", dict(request, token_count=encoded.input_ids.shape[1]))
        record = {"policy_step_id": pid, "generation_origin": self.origin,
                  "input_tokens": encoded.input_ids.shape[1], "input_token_ids": encoded.input_ids[0].tolist()}
        self.records[pid] = record
        # Construct a fresh config so the downloaded sampling defaults cannot override greedy decoding.
        generation = GenerationConfig(
            do_sample=False,
            num_beams=1,
            max_new_tokens=self.config["max_new_tokens"],
            use_cache=True,
            pad_token_id=self.tokenizer.pad_token_id,
            eos_token_id=self.model.generation_config.eos_token_id,
        )
        # Transformers >=4.50 can treat explicit default-valued fields as unspecified
        # and overwrite do_sample=False from the model config unless this is disabled.
        effective, _ = self.model._prepare_generation_config(
            generation, use_model_defaults=False, do_sample=False
        )
        assert effective.do_sample is False and effective.num_beams == 1
        record["effective_generation_config"] = effective.to_dict()
        record["use_model_defaults"] = False
        original_sample = self.model._sample

        def checked_decode(instance, *args, **kwargs):
            assert instance is self.model
            active = kwargs["generation_config"]
            assert active.do_sample is False and active.num_beams == 1
            record["runtime_do_sample"] = active.do_sample
            record["runtime_num_beams"] = active.num_beams
            return original_sample(*args, **kwargs)
        self.calls += 1
        self.save("GENERATION_STARTED.json", {"calls": self.calls, "policy_step_id": pid})
        torch.cuda.synchronize()
        started = perf_counter()
        with torch.inference_mode(), patch.object(type(self.model), "_sample", checked_decode):
            generated = self.model.generate(
                **encoded, generation_config=effective,
                use_model_defaults=False, do_sample=False,
            )
        assert record["runtime_do_sample"] is False and record["runtime_num_beams"] == 1
        torch.cuda.synchronize()
        record["latency_seconds"] = perf_counter() - started
        output_ids = generated[0, encoded.input_ids.shape[1]:].tolist()
        raw = self.tokenizer.decode(output_ids, skip_special_tokens=True)
        record.update(output_tokens=len(output_ids), output_token_ids=output_ids, raw_model_output=raw)
        self.save("GENERATION.json", record)
        self.save("QWEN_RAW_OUTPUT.txt", raw)
        self.memory.update(
            peak_allocated_bytes=torch.cuda.max_memory_allocated(),
            peak_reserved_bytes=torch.cuda.max_memory_reserved(),
            after_generation_allocated_bytes=torch.cuda.memory_allocated(),
            after_generation_reserved_bytes=torch.cuda.memory_reserved(),
        )
        self.save("MEMORY_PROFILE.json", self.memory)
        return raw

    def usage_for(self, policy_step_id):
        record = self.records[policy_step_id]
        return UsageInfo(record["input_tokens"], record["output_tokens"], {"local_qwen": 1})
