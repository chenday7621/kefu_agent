"""Heldout dual-configuration worker: Parlant decisions, attempt-scoped observations, atomic scores."""

import argparse
import faulthandler
from collections import defaultdict
from contextvars import ContextVar
from datetime import datetime
import hashlib
import json
import os
import sys
from pathlib import Path
import stat
import subprocess
import threading
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

PROJECT = Path(__file__).resolve().parents[2]
BENCH = PROJECT / "benchmarks" / "tau2-bench"
_pre_parser = argparse.ArgumentParser(add_help=False)
_pre_parser.add_argument("--group", choices=("BASE", "OPT"), default="BASE")
GROUP = _pre_parser.parse_known_args()[0].group
sys.path.insert(0, str(Path(__file__).parent / "configs" / GROUP))
os.environ["RETAIL_PARLANT_PORT"] = "18800" if GROUP == "BASE" else "18801"
os.environ["RETAIL_TOOL_SERVICE_PORT"] = "18818" if GROUP == "BASE" else "18819"
RUNTIME = PROJECT / "runtime-data" / "tau3-retail-heldout" / GROUP / f"{datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
faulthandler.enable(all_threads=True)
os.environ["PARLANT_HOME"] = str(RUNTIME)
os.environ["HF_HOME"] = str(PROJECT / "runtime-data" / "huggingface")
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["PARLANT_DATA_COLLECTION"] = "false"
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")

from dotenv import dotenv_values  # noqa: E402
from parlant.adapters.nlp.deepseek_service import DeepSeekSchematicGenerator  # noqa: E402
from openai.resources.chat.completions import AsyncCompletions  # noqa: E402
from tau2.user import user_simulator as user_module  # noqa: E402
from tau2.data_model.message import UserMessage  # noqa: E402
from tau2.data_model.simulation import TerminationReason  # noqa: E402
from tau2.domains.retail.environment import get_tasks_split  # noqa: E402
from tau2.evaluator import evaluator_nl_assertions as judge_module  # noqa: E402
from tau2.evaluator.evaluator import EvaluationType  # noqa: E402
from tau2.orchestrator.orchestrator import Orchestrator  # noqa: E402
from tau2.runner import build_environment, build_user, get_tasks, run_simulation  # noqa: E402

from bridge import AdapterError, ParlantHost, ParlantRetailAgent, ToolBridge  # noqa: E402
from observe import Observer, install_parlant_hooks, utc_now  # noqa: E402


from protocol_support import PROTOCOL, PLAN, FREEZE, REVIEW, scored, verify, unit_root
from policy_config import ASSOCIATIONS
SELECTION = PROTOCOL
MODEL = "deepseek/deepseek-chat"


def load_secret() -> None:
    path = PROJECT / ".env"
    if not path.is_file():
        raise RuntimeError("Project .env with DEEPSEEK_API_KEY is required")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise RuntimeError("Project .env must have permissions 600")
    key = dotenv_values(path).get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY is missing from project .env")
    os.environ["DEEPSEEK_API_KEY"] = key


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()




class Meter:
    def __init__(self, observer: Observer) -> None:
        self.lock = threading.Lock()
        self.observer = observer
        self.values = defaultdict(lambda: defaultdict(lambda: {"api_calls": 0, "successes": 0, "input_tokens": 0, "output_tokens": 0, "seconds": 0.0}))

    def add(self, role: str, *, success: bool, input_tokens: int = 0, output_tokens: int = 0, seconds: float = 0) -> None:
        context = self.observer.context()
        key = (context["task_id"], context["attempt_id"])
        with self.lock:
            item = self.values[key][role]
            item["api_calls"] += 1
            item["successes"] += int(success)
            item["input_tokens"] += input_tokens
            item["output_tokens"] += output_tokens
            item["seconds"] += seconds

    def task(self, task_id: str, attempt_id: str) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.values[(task_id, attempt_id)]))


def install_usage_meter(meter: Meter, observer: Observer) -> None:
    raw_response_context: ContextVar[dict | None] = ContextVar("retail_deepseek_response", default=None)
    original_create = AsyncCompletions.create

    async def observed_create(self, *args, **kwargs):
        context = raw_response_context.get()
        if context is None:
            return await original_create(self, *args, **kwargs)
        if kwargs.get("model") != "deepseek-chat":
            raise AdapterError("Parlant requested a non-DeepSeek model")
        observer.record("provider_request_started", role="customer_parlant",
                        request_model=kwargs.get("model"), request_sha256=hashlib.sha256(
                            json.dumps(kwargs.get("messages", []), sort_keys=True, default=str).encode()
                        ).hexdigest())
        response = await original_create(self, *args, **kwargs)
        context["response_model"] = getattr(response, "model", None) or "unavailable"
        context["raw_usage"] = response.usage.model_dump() if response.usage else None
        observer.record("provider_response_received", role="customer_parlant",
                        request_model=kwargs.get("model"), response_model=context["response_model"],
                        raw_usage=context["raw_usage"])
        return response

    AsyncCompletions.create = observed_create
    original = DeepSeekSchematicGenerator._do_generate
    seen_prompts: dict[tuple[str, str, str], str] = {}

    async def tracked(self, prompt, hints={}):
        started = time.monotonic()
        start_utc = utc_now()
        trace_id = self.tracer.trace_id
        schema = self.schema.__name__
        prompt_text = prompt.build() if hasattr(prompt, "build") else str(prompt)
        prompt_sha = hashlib.sha256(prompt_text.encode()).hexdigest()
        key = (observer.context()["attempt_id"], trace_id, prompt_sha)
        previous = seen_prompts.get(key)
        call_id = uuid4().hex
        seen_prompts[key] = call_id
        observer.record("model_call_started", role="customer_parlant", call_id=call_id,
                        trace_id=trace_id, schema=schema, request_model=self.model_name,
                        start_utc=start_utc, prompt_sha256=prompt_sha)
        raw: dict = {}
        token = raw_response_context.set(raw)
        try:
            result = await original(self, prompt, hints)
        except BaseException as exc:
            usage = raw.get("raw_usage") or {}
            meter.add("customer_parlant", success=False,
                      input_tokens=int(usage.get("prompt_tokens") or 0),
                      output_tokens=int(usage.get("completion_tokens") or 0),
                      seconds=time.monotonic() - started)
            observer.record("model_call", role="customer_parlant", call_id=call_id,
                            trace_id=trace_id, module=self.__class__.__module__, schema=schema,
                            request_model=self.model_name, response_model=raw.get("response_model", "unavailable"),
                            start_utc=start_utc, end_utc=utc_now(), seconds=time.monotonic()-started,
                            input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
                            success=False, error_type=type(exc).__name__, prompt_sha256=prompt_sha,
                            same_prompt_previous_call_id=previous or "unavailable", sdk_internal_retries="unavailable")
            raise
        finally:
            raw_response_context.reset(token)
        usage = result.info.usage
        observer.record("model_call", role="customer_parlant", call_id=call_id,
                        trace_id=trace_id, module=self.__class__.__module__, schema=schema,
                        request_model=self.model_name, response_model=raw.get("response_model", "unavailable"),
                        start_utc=start_utc, end_utc=utc_now(), seconds=time.monotonic()-started,
                        input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                        success=True, error_type=None, prompt_sha256=prompt_sha,
                        same_prompt_previous_call_id=previous or "unavailable", sdk_internal_retries="unavailable")
        meter.add(
            "customer_parlant", success=True,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            seconds=time.monotonic() - started,
        )
        return result

    DeepSeekSchematicGenerator._do_generate = tracked

    original_user = user_module.generate

    def tracked_user(*args, **kwargs):
        model = kwargs.get("model", args[0] if args else None)
        if model != MODEL:
            raise AdapterError(f"Simulated user provider is not DeepSeek: {model}")
        started = time.monotonic()
        start_utc = utc_now()
        call_id = uuid4().hex
        observer.record("model_call_started", role="simulated_user", call_id=call_id,
                        request_model=model, start_utc=start_utc)
        current_turn = observer.context()["turn"]
        next_turn = current_turn + 1 if isinstance(current_turn, int) else 1
        try:
            result = original_user(*args, **kwargs)
        except BaseException as exc:
            observer.record("model_call", role="simulated_user", turn=next_turn, call_id=call_id,
                            module="tau2.user.user_simulator", schema=kwargs.get("call_name", "unavailable"),
                            request_model=model, response_model="unavailable", start_utc=start_utc,
                            end_utc=utc_now(), seconds=time.monotonic()-started, input_tokens=None,
                            output_tokens=None, success=False, error_type=type(exc).__name__,
                            sdk_internal_retries="unavailable")
            raise
        usage = result.usage or {}
        observer.record("model_call", role="simulated_user", turn=next_turn, call_id=call_id,
                        module="tau2.user.user_simulator", schema=kwargs.get("call_name", "unavailable"),
                        request_model=model, response_model=(result.raw_data or {}).get("model") or "unavailable",
                        start_utc=start_utc, end_utc=utc_now(), seconds=time.monotonic()-started,
                        input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
                        success=True, error_type=None, sdk_internal_retries="unavailable")
        return result

    user_module.generate = tracked_user

    # The official NL assertion scorer imports a GPT default at module load.
    # Change only its provider configuration; retain its scoring implementation.
    judge_module.DEFAULT_LLM_NL_ASSERTIONS = MODEL
    judge_module.DEFAULT_LLM_NL_ASSERTIONS_ARGS = {"temperature": 0, "num_retries": 0, "timeout": 90}
    original_judge = judge_module.generate

    def tracked_judge(*args, **kwargs):
        model = kwargs.get("model", args[0] if args else None)
        if model != MODEL:
            raise AdapterError(f"Judge provider is not DeepSeek: {model}")
        started = time.monotonic()
        start_utc = utc_now()
        call_id = uuid4().hex
        observer.record("model_call_started", role="judge", call_id=call_id,
                        request_model=model, start_utc=start_utc)
        try:
            result = original_judge(*args, **kwargs)
        except BaseException as exc:
            meter.add("judge", success=False, seconds=time.monotonic() - started)
            observer.record("model_call", role="judge", turn="unavailable", call_id=call_id,
                            module="tau2.evaluator.evaluator_nl_assertions", schema="nl_assertions",
                            request_model=model, response_model="unavailable", start_utc=start_utc,
                            end_utc=utc_now(), seconds=time.monotonic()-started,
                            input_tokens=None, output_tokens=None, success=False,
                            error_type=type(exc).__name__, sdk_internal_retries="unavailable")
            raise
        usage = result.usage or {}
        observer.record("model_call", role="judge", turn="unavailable", call_id=call_id,
                        module="tau2.evaluator.evaluator_nl_assertions", schema="nl_assertions",
                        request_model=model, response_model=(result.raw_data or {}).get("model") or "unavailable",
                        start_utc=start_utc, end_utc=utc_now(), seconds=time.monotonic()-started,
                        input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
                        success=True, error_type=None, sdk_internal_retries="unavailable")
        meter.add(
            "judge", success=True,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            seconds=time.monotonic() - started,
        )
        return result

    judge_module.generate = tracked_judge


def user_usage(simulation) -> dict:
    messages = [m for m in simulation.messages if isinstance(m, UserMessage) and m.usage]
    return {
        "api_calls": len(messages),
        "successes": len(messages),
        "input_tokens": sum(int(m.usage.get("prompt_tokens") or 0) for m in messages),
        "output_tokens": sum(int(m.usage.get("completion_tokens") or 0) for m in messages),
        "seconds": sum(float(m.generation_time_seconds or 0) for m in messages),
        "note": "Successful simulator messages only; failed API attempts may not be observable",
    }


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def write_json_atomic_new(path: Path, value) -> None:
    """Publish a completed score once; retain any prior official score."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    with temporary.open("x") as output:
        output.write(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
        output.flush()
        os.fsync(output.fileno())
    try:
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def classify(simulation) -> str:
    if simulation.reward_info is None:
        return "unscored_termination"
    return "scored_task_success" if simulation.reward_info.reward == 1 else "scored_task_failure"


def run_task(task, host: ParlantHost, bridge: ToolBridge, meter: Meter, observer: Observer,
             result_dir: Path, freeze_sha: str, unit: dict) -> dict:
    task_id = task.id
    attempt_id = uuid4().hex
    attempt_dir = result_dir / "attempts" / f"task_{task_id}" / attempt_id
    attempt_dir.mkdir(parents=True, exist_ok=False)
    scope_token = observer.begin_attempt(task_id, attempt_id, attempt_dir / "events.jsonl")
    write_json(result_dir / "active_attempt.json", {"task_id": task_id, "attempt_id": attempt_id,
                                                    "attempt_dir": str(attempt_dir), "pid": os.getpid(), "unit": unit})
    observer.record("attempt_started", attempt_id=attempt_id, freeze_sha256=freeze_sha,
                    protocol_sha256=sha256(Path(__file__).parent / "protocol.json"), unit=unit)
    started = time.monotonic()
    agent = None
    orchestrator = None
    closed = False
    try:
        environment = build_environment("retail")
        agent = ParlantRetailAgent(environment.get_tools(), environment.get_policy(), bridge, observer)
        user = build_user(
            "user_simulator", environment, task,
            llm=MODEL,
            llm_args={"temperature": 0, "num_retries": 0, "timeout": 90},
        )
        orchestrator = Orchestrator(
            domain="retail", agent=agent, user=user, environment=environment,
            task=task, max_steps=SELECTION["max_steps"],
            max_errors=SELECTION["max_errors"],
            seed=unit["seed"],
            timeout=SELECTION["timeout_seconds_per_task"],
            validate_communication=True,
        )
        simulation = run_simulation(orchestrator, evaluation_type=EvaluationType.ALL)
        if simulation.reward_info is None:
            raise AdapterError("Official evaluation returned no reward_info")
        observer.set_phase(task_id, attempt_id, "cleanup")
        host.finish_session(orchestrator.agent_state.session_id)
        closed = True
        usage = meter.task(task_id, attempt_id)
        usage["simulated_user"] = user_usage(simulation)
        usage.setdefault("judge", {"api_calls": 0, "successes": 0, "input_tokens": 0, "output_tokens": 0, "seconds": 0})
        raw = {
            "task_id": task_id,
            "attempt_id": attempt_id,
            "freeze_sha256": freeze_sha,
            "protocol_sha256": sha256(Path(__file__).parent / "protocol.json"),
            "unit": unit,
            "protocol": SELECTION,
            "classification": classify(simulation),
            "elapsed_seconds": time.monotonic() - started,
            "usage": usage,
            "simulation": simulation.model_dump(mode="json"),
            "parlant_events": "unavailable",
            "observations": observer.task_records(task_id, attempt_id),
        }
        try:
            raw["parlant_events"] = agent.raw_events(orchestrator.agent_state)
        except Exception as exc:
            raw["parlant_events_error"] = f"{type(exc).__name__}: {exc}"
        observer.record("official_score_received", reward=simulation.reward_info.model_dump(mode="json"))
        raw["observations"] = observer.task_records(task_id, attempt_id)
        write_json_atomic_new(result_dir / f"task_{task_id}.json", raw)
        write_json(attempt_dir / "completed.json", {"task_id": task_id, "attempt_id": attempt_id,
                                                     "result_sha256": sha256(result_dir / f"task_{task_id}.json")})
        print(f"task {task_id}: {raw['classification']} reward={simulation.reward_info.reward if simulation.reward_info else None} elapsed={raw['elapsed_seconds']:.1f}s", flush=True)
        return {k: raw[k] for k in ("task_id", "classification", "elapsed_seconds", "usage") } | {"reward": simulation.reward_info.reward if simulation.reward_info else None, "reward_info": simulation.reward_info.model_dump(mode="json") if simulation.reward_info else None}
    except BaseException as exc:
        cleanup_error = None
        if not closed and orchestrator is not None and getattr(orchestrator, "agent_state", None) is not None:
            observer.set_phase(task_id, attempt_id, "cleanup")
            try:
                host.finish_session(orchestrator.agent_state.session_id)
                closed = True
            except BaseException as cleanup_exc:
                cleanup_error = f"{type(cleanup_exc).__name__}: {cleanup_exc}"
        record = {
            "task_id": task_id,
            "attempt_id": attempt_id,
            "classification": "environment_or_adapter_error",
            "unit": unit,
            "elapsed_seconds": time.monotonic() - started,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "usage": meter.task(task_id, attempt_id),
            "observations": observer.task_records(task_id, attempt_id),
            "cleanup_error": cleanup_error,
        }
        if agent is not None and orchestrator is not None and hasattr(orchestrator, "agent_state"):
            try:
                record["parlant_events"] = agent.raw_events(orchestrator.agent_state)
            except Exception:
                pass
        write_json(attempt_dir / "error.json", record)
        observer.record("attempt_error", attempt_id=attempt_id, error_type=type(exc).__name__,
                        error=str(exc))
        print(f"task {task_id}: adapter/environment error ({type(exc).__name__})", flush=True)
        raise
    finally:
        observer.set_phase(task_id, attempt_id, "closed")
        observer.record("attempt_closed", cleanup_completed=closed)
        observer.end_attempt(scope_token)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-dir", type=Path, required=True)
    parser.add_argument("--group", choices=("BASE", "OPT"), required=True)
    parser.add_argument("--control-dir", type=Path, required=True)
    args = parser.parse_args()
    result_dir = args.result_dir.resolve()
    if not result_dir.is_relative_to(PROJECT / "results") or not result_dir.name.startswith("tau3_retail_heldout_"):
        raise RuntimeError("Independent heldout results directory required")
    freeze_sha = verify()  # before official test task loading or API requests
    load_secret()
    observer = Observer()
    meter = Meter(observer)
    startup_dir = result_dir / "startup" / GROUP / args.control_dir.name
    observer.set_startup_journal(startup_dir / "events.jsonl")
    observer.set_unattributed_journal(startup_dir / "unattributed.jsonl")
    import torch, transformers, tokenizers
    write_json(startup_dir / "environment.json", {
        "group": GROUP, "pid": os.getpid(), "runtime_home": str(RUNTIME),
        "omp_num_threads": os.getenv("OMP_NUM_THREADS"), "mkl_num_threads": os.getenv("MKL_NUM_THREADS"),
        "torch_threads": torch.get_num_threads(), "torch_interop_threads": torch.get_num_interop_threads(),
        "torch": torch.__version__, "transformers": transformers.__version__, "tokenizers": tokenizers.__version__,
        "freeze_sha256": freeze_sha, "port": os.environ["RETAIL_PARLANT_PORT"],
        "tool_service_port": os.environ["RETAIL_TOOL_SERVICE_PORT"],
    })
    install_usage_meter(meter, observer)
    install_parlant_hooks(observer)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    environment = build_environment("retail")
    bridge = ToolBridge(PROTOCOL["adapter_tool_wait_seconds"], observer)
    host = ParlantHost(environment.get_tools(), environment.get_policy(), bridge)
    try:
        host.start()
        # Read back actual condition/action and tool associations. No decisions modified.
        import httpx
        import bridge as bridge_module
        actual=[]
        for entry in host.policy_map:
            response=httpx.get(f"{bridge_module.BASE_URL}/guidelines/{entry['guideline_id']}", timeout=30)
            response.raise_for_status()
            row=response.json()
            expected=next((x for x in ASSOCIATIONS if x['key']==entry.get('key')), None)
            guideline=row['guideline']
            if expected:
                assert guideline['condition']==expected['condition'] and guideline['action']==expected['action']
                assert sorted(t['tool_id']['tool_name'] for t in row['tool_associations'])==sorted(expected['tools'])
            else:
                section=next(x for x in bridge_module.policy_sections(environment.get_policy()) if x['heading']==entry['heading'])
                assert guideline['action']==section['text']
            actual.append(entry | {"actual":row})
        write_json(startup_dir / "actual_rules.json", actual)
        write_json(startup_dir / "policy_map.json",host.policy_map)
        write_json(startup_dir / "tool_coverage.json",host.coverage_report)
        write_json_atomic_new(args.control_dir / "ready.json", {"group":GROUP,"freeze_sha256":freeze_sha,"actual_rules_verified":True})
        for line in sys.stdin:
            command=json.loads(line)
            if command.get("stop"):break
            unit=command["unit"]
            if unit not in PLAN or unit["group"]!=GROUP:raise RuntimeError("Command outside frozen plan")
            if scored(result_dir,unit):raise RuntimeError("Scored execution must not be run again")
            tasks=get_tasks("retail",task_ids=[unit["task_id"]],task_split_name="test")
            assert len(tasks)==1 and tasks[0].id==unit["task_id"]
            root=unit_root(result_dir,unit)
            run_task(tasks[0],host,bridge,meter,observer,root,freeze_sha,unit)
            write_json_atomic_new(args.control_dir / (unit["unit_id"]+".ack.json"),{"unit":unit,"cleanup_returned":True})
    finally:
        host.stop()
        # Persist final usage after host shutdown; attempt journals remain open and attributed.
        write_json(startup_dir / "usage.json",meter.task("startup","startup"))
        write_json(startup_dir / "observations.json",observer.task_records("startup","startup"))


if __name__ == "__main__":
    main()
