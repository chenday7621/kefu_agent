"""Native reference DeepSeek for every non-policy schema, metered before send."""

from time import perf_counter
import json
from pathlib import Path

from apps.retail_demo.nlp import DemoDeepSeek
from parlant.core.engines.alpha.tool_calling.single_tool_batch import SingleToolBatchSchema

from .budget import BudgetLedger
from ..trajectory import redact


class ReferenceNLP(DemoDeepSeek):
    def __init__(self, logger, tracer, meter, policy, config):
        super().__init__(logger, tracer, meter)
        self.policy, self.config = policy, config
        self.ledger = BudgetLedger(
            config["budget_directory"], config["authorization"], config["prices"]
        )

    async def get_embedder(self, hints=None):
        if not hasattr(self, "reference_embedder"):
            raise RuntimeError("Original neural reference embedder was not bound")
        return self.reference_embedder

    async def get_schematic_generator(self, schema, hints=None):
        if schema is SingleToolBatchSchema:
            return self.policy
        generator = await super().get_schematic_generator(schema, hints or {})
        # Native policy decorators may retry. Each API invocation is accounted
        # independently; the underlying SDK cannot perform invisible retries.
        generator._client.max_retries = 0
        original = generator._client.chat.completions.create

        async def accounted(*args, **kwargs):
            if kwargs.get("model") != "deepseek-chat":
                raise RuntimeError("Frozen native reference model changed")
            request_id = self.ledger.reserve(
                self.config["phase"],
                schema.__name__,
                kwargs,
                {"episode_id": self.config.get("episode_id"), "run_id": self.config.get("run_id")},
            )
            started = perf_counter()
            directory = Path(self.config["budget_directory"]) / "provider_payloads"
            directory.mkdir(exist_ok=True)
            record = {
                "request_id": request_id,
                "schema": schema.__name__,
                "request": redact(kwargs),
            }
            try:
                response = await original(*args, **kwargs)
                usage = response.usage.model_dump(mode="json") if response.usage else None
                self.ledger.settle(
                    request_id,
                    usage=usage,
                    status="completed",
                    response_id=response.id,
                    returned_model=response.model,
                    system_fingerprint=getattr(response, "system_fingerprint", None),
                    latency_seconds=perf_counter() - started,
                )
                record["response"] = redact(response.model_dump(mode="json"))
                return response
            except BaseException as exc:
                self.ledger.settle(
                    request_id,
                    status="failed",
                    http_status=getattr(exc, "status_code", None),
                    error_type=type(exc).__name__,
                    latency_seconds=perf_counter() - started,
                )
                record["error_type"] = type(exc).__name__
                raise
            finally:
                (directory / (request_id + ".json")).write_text(
                    json.dumps(record, ensure_ascii=False) + "\n"
                )

        generator._client.chat.completions.create = accounted
        return generator
