"""Schema-aware provider with a disabled-by-default Qwen text backend."""

from dataclasses import dataclass
import json
from time import perf_counter
from typing import cast

from parlant.core.engines.alpha.tool_calling.single_tool_batch import SingleToolBatchSchema
from parlant.core.nlp.generation import SchematicGenerator, SchematicGenerationResult
from parlant.core.nlp.generation_info import GenerationInfo, UsageInfo
from parlant.core.nlp.service import NLPService
from parlant.core.nlp.tokenization import EstimatingTokenizer

from .trajectory import CANDIDATE, FRAME, TRUSTED, redact

MODEL_NAME = "Qwen/Qwen3-8B"
MODEL_REVISION = "b968826d9c46dd6066d109eabc6255188de91218"


def visible_tool_schema(tool):
    properties = {}
    for name, (descriptor, options) in tool.parameters.items():
        if name.lower() in TRUSTED or options.hidden:
            continue
        entry = redact(dict(descriptor))
        kind = entry.pop("item_type", None)
        if entry["type"] == "array":
            entry["items"] = {"type": kind or "string"}
        elif entry["type"] not in {"string", "integer", "number", "boolean"}:
            entry["type"] = "string"
        properties[name] = entry
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": [n for n in tool.required if n in properties],
                "additionalProperties": False,
            },
        },
    }


class DisabledBackend:
    async def complete(self, request):
        raise RuntimeError(
            "Phase 1-A prohibits Qwen inference; explicitly install a Phase 1-B backend"
        )


def reject_trusted_keys(value):
    if isinstance(value, dict):
        if any(str(key).lower() in TRUSTED for key in value):
            raise ValueError("Model output contains a trusted server field")
        for item in value.values():
            reject_trusted_keys(item)
    elif isinstance(value, list):
        for item in value:
            reject_trusted_keys(item)


class QwenEstimatingTokenizer(EstimatingTokenizer):
    def __init__(self, tokenizer):
        self.native = tokenizer

    async def estimate_token_count(self, prompt):
        return len(self.native.encode(prompt, add_special_tokens=False))


@dataclass(frozen=True)
class PolicyGenerationResult(SchematicGenerationResult):
    policy_step_id: str


class QwenPolicyProvider(SchematicGenerator[SingleToolBatchSchema]):
    def __init__(self, tokenizer, logger, backend=None):
        self.native_tokenizer = tokenizer
        self.trajectory = logger
        self.backend = backend or DisabledBackend()
        self._tokenizer = QwenEstimatingTokenizer(tokenizer)

    @property
    def schema(self):
        return SingleToolBatchSchema

    @property
    def id(self):
        return f"{MODEL_NAME}@{MODEL_REVISION}"

    @property
    def max_tokens(self):
        return 32768

    @property
    def tokenizer(self):
        return self._tokenizer

    async def generate(self, prompt, hints={}):
        frame, candidate = FRAME.get(), CANDIDATE.get()
        if frame is None or candidate is None:
            raise RuntimeError("Explicit APP decision frame and real candidate required")
        tool_id, tool = candidate
        tools = visible_tool_schema(tool)
        source_prompt = prompt if isinstance(prompt, str) else prompt.build()
        # Bind identity on the server; also remove secrets in embedded observation JSON.
        safe_prompt = redact(source_prompt)
        messages = [
            {
                "role": "system",
                "content": "Return ONLY a JSON object matching the supplied SingleToolBatchSchema. Tools describe the bound candidate. Propose actions; do not execute tools.\n"
                + json.dumps(SingleToolBatchSchema.model_json_schema()),
            },
            {"role": "user", "content": safe_prompt},
        ]
        rendered = self.native_tokenizer.apply_chat_template(
            messages,
            tools=[tools],
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )
        pid = self.trajectory.begin(
            frame,
            tool_id.to_string(),
            tool.name,
            messages,
            rendered,
            tools,
            MODEL_NAME,
            MODEL_REVISION,
            "disabled"
            if isinstance(self.backend, DisabledBackend)
            else getattr(self.backend, "origin", "unverified_backend"),
        )
        request = {
            "policy_step_id": pid,
            "messages": messages,
            "rendered_prompt": rendered,
            "tools": [tools],
            "model_name": MODEL_NAME,
            "model_revision": MODEL_REVISION,
            "generation_hints": dict(hints),
            "enable_thinking": False,
        }
        started = perf_counter()
        try:
            raw = await self.backend.complete(request)
        except Exception:
            self.trajectory.update(
                pid,
                parse_status="not_attempted",
                failure_type="GENERATION_DISABLED"
                if isinstance(self.backend, DisabledBackend)
                else "GENERATION_ERROR",
            )
            raise
        self.trajectory.update(pid, raw_model_output=raw)
        try:
            # Use the same native Pydantic model as Parlant's original provider.
            reject_trusted_keys(json.loads(raw))
            parsed = SingleToolBatchSchema.model_validate_json(raw)
            visible = tools["function"]["parameters"]["properties"]
            for call in parsed.tool_calls_for_candidate_tool:
                for argument in call.argument_evaluations or []:
                    if argument.parameter_name not in visible:
                        raise ValueError("Model proposed a hidden, trusted, or unknown parameter")
            proposals = [
                {
                    "tool_name": tool_id.to_string(),
                    "is_applicable": c.is_applicable,
                    "arguments": {
                        a.parameter_name: a.value_as_string for a in c.argument_evaluations or []
                    },
                }
                for c in parsed.tool_calls_for_candidate_tool
            ]
            action = (
                "CALL"
                if any(c.is_applicable for c in parsed.tool_calls_for_candidate_tool)
                else "NO_CALL"
            )
            self.trajectory.update(
                pid,
                parsed_schema_output=parsed.model_dump(mode="json"),
                parse_status="passed",
                proposed_action=action,
                proposed_tool_name=tool_id.to_string(),
                proposed_calls=proposals,
                proposed_arguments=proposals[0]["arguments"] if len(proposals) == 1 else None,
            )
        except Exception:
            self.trajectory.update(
                pid, parse_status="failed", failure_type="PARSE_OR_BOUNDARY_ERROR"
            )
            raise
        usage = (
            self.backend.usage_for(pid)
            if hasattr(self.backend, "usage_for")
            else UsageInfo(input_tokens=0, output_tokens=0, extra={"fixture_or_interface_only": True})
        )
        return PolicyGenerationResult(
            content=parsed,
            policy_step_id=pid,
            info=GenerationInfo(
                schema_name="SingleToolBatchSchema",
                model=self.id,
                duration=perf_counter() - started,
                usage=usage,
            ),
        )


class SchemaRouter(NLPService):
    """Install ONLY in the independent APP experiment configuration."""

    def __init__(self, frozen, qwen):
        self.frozen, self.qwen = frozen, qwen

    @property
    def supports_streaming(self):
        return self.frozen.supports_streaming

    async def get_schematic_generator(self, t, hints={}):
        if t is SingleToolBatchSchema:
            return cast(SchematicGenerator, self.qwen)
        return await self.frozen.get_schematic_generator(t, hints)

    async def get_streaming_text_generator(self, hints={}):
        return await self.frozen.get_streaming_text_generator(hints)

    async def get_embedder(self, hints={}):
        return await self.frozen.get_embedder(hints)

    async def get_moderation_service(self):
        return await self.frozen.get_moderation_service()
