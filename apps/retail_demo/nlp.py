"""Official DeepSeek generation and the existing local multilingual embedding adapter."""

import dataclasses
import json
import os
import time
from pathlib import Path
import parlant.sdk as p
from parlant.adapters.nlp.deepseek_service import DeepSeekService
from parlant.adapters.nlp.hugging_face import HuggingFaceEmbedder
from parlant.core.meter import Meter


class LocalEmbedder(HuggingFaceEmbedder):
    def __init__(self, logger, tracer, meter):
        super().__init__(logger, tracer, meter, model_name=os.environ["DEMO_EMBEDDING_MODEL"])

    @property
    def dimensions(self):
        return 384

    @property
    def max_tokens(self):
        return 512


class DemoDeepSeek(DeepSeekService):
    async def get_embedder(self, hints=None):
        return LocalEmbedder(self._logger, self._tracer, self._meter)

    async def get_schematic_generator(self, t, hints=None):
        generator = await super().get_schematic_generator(t, hints or {})
        api_create = generator._client.chat.completions.create

        async def observed_api(*args, **kwargs):
            if os.environ.get("DEMO_DISABLE_LLM") == "1":
                raise RuntimeError("LLM API disabled for offline verification")
            start = time.monotonic()
            record = {"schema": t.__name__, "requested_model": kwargs.get("model")}
            try:
                response = await api_create(*args, **kwargs)
                usage = response.usage
                record.update(
                    status="completed",
                    returned_model=response.model,
                    input_tokens=usage.prompt_tokens if usage else None,
                    output_tokens=usage.completion_tokens if usage else None,
                    cache_hit_tokens=getattr(usage, "prompt_cache_hit_tokens", None),
                    cache_miss_tokens=getattr(usage, "prompt_cache_miss_tokens", None),
                )
                return response
            except Exception as exc:
                record.update(status="failed", error_type=type(exc).__name__)
                raise
            finally:
                record["seconds"] = time.monotonic() - start
                with (Path(os.environ["PARLANT_HOME"]) / "api_calls.jsonl").open("a") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

        generator._client.chat.completions.create = observed_api
        original = generator.generate

        async def observed(*args, **kwargs):
            if os.environ.get("DEMO_DISABLE_LLM") == "1":
                raise RuntimeError(
                    "LLM generation disabled during persistence/integration verification"
                )
            started = time.monotonic()
            record = {"schema": t.__name__, "requested_model": generator.id}
            try:
                result = await original(*args, **kwargs)
                record.update(status="completed", generation=dataclasses.asdict(result.info))
                return result
            except Exception as exc:
                record.update(status="failed", error_type=type(exc).__name__)
                raise
            finally:
                record["seconds"] = time.monotonic() - started
                path = Path(os.environ["PARLANT_HOME"]) / "model_calls.jsonl"
                with path.open("a") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                    stream.flush()

        generator.generate = observed
        return generator


def nlp_factory(container):
    container[LocalEmbedder] = LocalEmbedder(
        container[p.Logger], container[p.Tracer], container[Meter]
    )
    return DemoDeepSeek(container[p.Logger], container[p.Tracer], container[Meter])
