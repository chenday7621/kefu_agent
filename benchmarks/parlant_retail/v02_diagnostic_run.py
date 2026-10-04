"""One separate post-regression diagnostic; never merges with formal V0.2 results."""

import argparse
from collections import defaultdict
from contextvars import ContextVar
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import threading
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

PROJECT = Path(__file__).resolve().parents[2]
BENCH = PROJECT / "benchmarks" / "tau2-bench"
RUNTIME = PROJECT / "runtime-data" / "tau3-retail-v02-diagnostic" / f"{datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
os.environ["PARLANT_HOME"] = str(RUNTIME)
os.environ["HF_HOME"] = str(PROJECT / "runtime-data" / "huggingface")
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["PARLANT_DATA_COLLECTION"] = "false"
os.environ["OMP_NUM_THREADS"] = "2"
os.environ["MKL_NUM_THREADS"] = "2"

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

from v02_bridge import AdapterError, ParlantHost, ParlantRetailAgent, ToolBridge  # noqa: E402
from v02_observe import Observer, install_parlant_hooks, utc_now  # noqa: E402
from v02_guard import ExecutionGuard, TOOL_POLICY  # noqa: E402


SELECTION = json.loads((Path(__file__).parent / "selection.json").read_text())
MODEL = "deepseek/deepseek-chat"
REVIEW = PROJECT / "_reviews" / "20261002_0137_V01_RETAIL_REGRESSION"
V0_MANIFEST = REVIEW / "V0_SNAPSHOT_MANIFEST.json"
V01_FREEZE = REVIEW / "V01_FROZEN.json"
V02_FREEZE = PROJECT / "_reviews" / "20261002_131819_V02_CORRECTNESS_REGRESSION" / "V02_POSTRUN_FIX_FROZEN.json"


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


def verify_frozen_selection() -> None:
    selected = SELECTION["task_ids"]
    actual = get_tasks_split()["train"][:5]
    if selected != actual:
        raise AdapterError("Frozen task IDs no longer match the official train split")
    if SELECTION["retry_count"] != 0:
        raise AdapterError("Unexpected retry setting")
    v0 = json.loads(V0_MANIFEST.read_text())
    actual_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=BENCH, text=True).strip()
    if actual_head != v0["benchmark_head"]:
        raise AdapterError("Benchmark git HEAD differs from frozen V0")
    data_root = BENCH / "data" / "tau2" / "domains" / "retail"
    for name, expected in v0["official_retail_data_sha256"].items():
        if sha256(data_root / name) != expected:
            raise AdapterError(f"Official Retail data differs from frozen V0: {name}")
    frozen = json.loads(V01_FREEZE.read_text())
    actual_parlant_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=PROJECT, text=True).strip()
    if actual_parlant_head != frozen["parlant_head"]:
        raise AdapterError("Parlant git HEAD differs from V0.1 freeze")
    for relative, expected in frozen["code_and_config_sha256"].items():
        if sha256(PROJECT / relative) != expected:
            raise AdapterError(f"V0.1 code/config changed after freeze: {relative}")
    for name, python_path in (("parlant", PROJECT / ".venv/bin/python"),
                              ("tau2", BENCH / ".venv/bin/python")):
        packages = subprocess.check_output(["uv", "pip", "freeze", "--python", str(python_path)])
        if hashlib.sha256(packages).hexdigest() != frozen["package_freeze_sha256"][name]:
            raise AdapterError(f"{name} dependencies changed after freeze")
    if not V02_FREEZE.exists():
        raise AdapterError("V0.2 code/test freeze is required before paid execution")
    v02 = json.loads(V02_FREEZE.read_text())
    for relative, expected in v02["code_and_config_sha256"].items():
        if sha256(PROJECT / relative) != expected:
            raise AdapterError(f"V0.2 code/config changed after offline freeze: {relative}")


class Meter:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active_task = "startup"
        self.values = defaultdict(lambda: defaultdict(lambda: {"api_calls": 0, "successes": 0, "input_tokens": 0, "output_tokens": 0, "seconds": 0.0}))

    def set_task(self, task_id: str) -> None:
        with self.lock:
            self.active_task = task_id

    def add(self, role: str, *, success: bool, input_tokens: int = 0, output_tokens: int = 0, seconds: float = 0) -> None:
        with self.lock:
            item = self.values[self.active_task][role]
            item["api_calls"] += 1
            item["successes"] += int(success)
            item["input_tokens"] += input_tokens
            item["output_tokens"] += output_tokens
            item["seconds"] += seconds

    def task(self, task_id: str) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.values[task_id]))


def install_usage_meter(meter: Meter, observer: Observer) -> None:
    raw_response_context: ContextVar[dict | None] = ContextVar("retail_deepseek_response", default=None)
    original_create = AsyncCompletions.create

    async def observed_create(self, *args, **kwargs):
        context = raw_response_context.get()
        if context is None:
            return await original_create(self, *args, **kwargs)
        if kwargs.get("model") != "deepseek-chat":
            raise AdapterError("Parlant requested a non-DeepSeek model")
        response = await original_create(self, *args, **kwargs)
        context["response_model"] = getattr(response, "model", None) or "unavailable"
        context["raw_usage"] = response.usage.model_dump() if response.usage else None
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
        key = (observer.context()["task_id"], trace_id, prompt_sha)
        previous = seen_prompts.get(key)
        call_id = uuid4().hex
        seen_prompts[key] = call_id
        raw: dict = {}
        token = raw_response_context.set(raw)
        try:
            result = await original(self, prompt, hints)
        except BaseException as exc:
            meter.add("customer_parlant", success=False, seconds=time.monotonic() - started)
            usage = raw.get("raw_usage") or {}
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
        current_turn = observer.context()["turn"]
        next_turn = current_turn + 1 if isinstance(current_turn, int) else 1
        try:
            result = original_user(*args, **kwargs)
        except BaseException as exc:
            observer.record("model_call", role="simulated_user", turn=next_turn, call_id=uuid4().hex,
                            module="tau2.user.user_simulator", schema=kwargs.get("call_name", "unavailable"),
                            request_model=model, response_model="unavailable", start_utc=start_utc,
                            end_utc=utc_now(), seconds=time.monotonic()-started, input_tokens=None,
                            output_tokens=None, success=False, error_type=type(exc).__name__,
                            sdk_internal_retries="unavailable")
            raise
        usage = result.usage or {}
        observer.record("model_call", role="simulated_user", turn=next_turn, call_id=uuid4().hex,
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
        try:
            result = original_judge(*args, **kwargs)
        except BaseException as exc:
            meter.add("judge", success=False, seconds=time.monotonic() - started)
            observer.record("model_call", role="judge", turn="unavailable", call_id=uuid4().hex,
                            module="tau2.evaluator.evaluator_nl_assertions", schema="nl_assertions",
                            request_model=model, response_model="unavailable", start_utc=start_utc,
                            end_utc=utc_now(), seconds=time.monotonic()-started,
                            input_tokens=None, output_tokens=None, success=False,
                            error_type=type(exc).__name__, sdk_internal_retries="unavailable")
            raise
        usage = result.usage or {}
        observer.record("model_call", role="judge", turn="unavailable", call_id=uuid4().hex,
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


def classify(simulation) -> str:
    if simulation.termination_reason in {TerminationReason.AGENT_STOP, TerminationReason.USER_STOP}:
        return "scored_task_success" if simulation.reward_info and simulation.reward_info.reward == 1 else "scored_task_failure"
    return "premature_termination"


def run_task(task, host: ParlantHost, bridge: ToolBridge, meter: Meter, observer: Observer, result_dir: Path) -> dict:
    task_id = task.id
    meter.set_task(task_id)
    observer.set_task(task_id)
    started = time.monotonic()
    agent = None
    orchestrator = None
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
            seed=SELECTION["seed"],
            timeout=SELECTION["timeout_seconds_per_task"],
            validate_communication=True,
        )
        simulation = run_simulation(orchestrator, evaluation_type=EvaluationType.ALL)
        usage = meter.task(task_id)
        usage["simulated_user"] = user_usage(simulation)
        usage.setdefault("judge", {"api_calls": 0, "successes": 0, "input_tokens": 0, "output_tokens": 0, "seconds": 0})
        raw = {
            "task_id": task_id,
            "classification": classify(simulation),
            "elapsed_seconds": time.monotonic() - started,
            "usage": usage,
            "simulation": simulation.model_dump(mode="json"),
            "parlant_events": agent.raw_events(orchestrator.agent_state),
            "observations": observer.task_records(task_id),
            "guard_events": bridge.guard.state(orchestrator.agent_state.session_id).events,
        }
        write_json(result_dir / f"task_{task_id}.json", raw)
        print(f"task {task_id}: {raw['classification']} reward={simulation.reward_info.reward if simulation.reward_info else None} elapsed={raw['elapsed_seconds']:.1f}s", flush=True)
        return {k: raw[k] for k in ("task_id", "classification", "elapsed_seconds", "usage") } | {"reward": simulation.reward_info.reward if simulation.reward_info else None, "reward_info": simulation.reward_info.model_dump(mode="json") if simulation.reward_info else None}
    except BaseException as exc:
        record = {
            "task_id": task_id,
            "classification": "environment_or_adapter_error",
            "elapsed_seconds": time.monotonic() - started,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "usage": meter.task(task_id),
            "observations": observer.task_records(task_id),
        }
        if agent is not None and orchestrator is not None and hasattr(orchestrator, "agent_state"):
            try:
                record["parlant_events"] = agent.raw_events(orchestrator.agent_state)
                record["guard_events"] = bridge.guard.state(orchestrator.agent_state.session_id).events
            except Exception:
                pass
        write_json(result_dir / f"task_{task_id}.json", record)
        print(f"task {task_id}: adapter/environment error ({type(exc).__name__})", flush=True)
        return record
    finally:
        if orchestrator is not None and hasattr(orchestrator, "agent_state"):
            bridge.unregister(orchestrator.agent_state.session_id)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["diagnostic"], required=True)
    parser.add_argument("--task-id", choices=SELECTION["task_ids"], required=True)
    parser.add_argument("--result-dir", type=Path, required=True)
    args = parser.parse_args()
    result_dir = args.result_dir.resolve()
    if not result_dir.is_relative_to(PROJECT / "results"):
        raise RuntimeError("Result directory must be inside project results/")
    if result_dir.exists() and any(result_dir.iterdir()):
        raise RuntimeError("Result directory is not empty; refusing to overwrite prior results")
    load_secret()
    verify_frozen_selection()
    meter = Meter()
    observer = Observer()
    install_usage_meter(meter, observer)
    environment = build_environment("retail")
    guard = ExecutionGuard(environment.get_tools())
    install_parlant_hooks(observer, guard)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    ids = SELECTION["task_ids"]
    if any(result_dir.glob("task_*.json")):
        raise RuntimeError("Formal task results already exist; refusing to rerun")
    target_ids = [args.task_id]
    tasks = {t.id: t for t in get_tasks("retail", task_ids=target_ids, task_split_name="train")}
    if set(tasks) != set(target_ids):
        raise RuntimeError("Selected official train tasks were not found")
    bridge = ToolBridge(SELECTION["adapter_tool_wait_seconds"], observer, guard)
    host = ParlantHost(environment.get_tools(), environment.get_policy(), bridge)
    started_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    data_root = BENCH / "data" / "tau2" / "domains" / "retail"
    write_json(result_dir / "provenance.json", {
        "started_at": started_at,
        "benchmark_commit": "fc0055dc4e0a316c3f83133267fbd6faaa770992",
        "verified_benchmark_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=BENCH, text=True).strip(),
        "parlant_config_store": str(RUNTIME),
        "v01_freeze_sha256": sha256(V01_FREEZE),
        "v02_freeze_sha256": sha256(V02_FREEZE),
        "tool_policy": {name: {"class": value[0], "auth_required": value[1]} for name, value in TOOL_POLICY.items()},
        "selection": SELECTION,
        "data_sha256": {name: sha256(data_root / name) for name in ("tasks.json", "db.json", "policy.md", "split_tasks.json")},
        "providers": {"customer": "DeepSeek official Parlant adapter", "simulated_user": MODEL, "judge": MODEL},
        "evaluation_type": "ALL (official reward_basis; required NL assertions use DeepSeek)",
    })
    try:
        host.start()
        write_json(result_dir / "policy_map.json", host.policy_map)
        write_json(result_dir / "tool_coverage.json", host.coverage_report)
        summaries = []
        for task_id in target_ids:
            summary = run_task(tasks[task_id], host, bridge, meter, observer, result_dir)
            summaries.append(summary)
            write_json(result_dir / f"summary_{args.phase}.json", summaries)
            if summary["classification"] == "environment_or_adapter_error":
                break
        write_json(result_dir / "startup_usage.json", meter.task("startup"))
        write_json(result_dir / "startup_observations.json", observer.task_records("startup"))
    finally:
        host.stop()


if __name__ == "__main__":
    main()
