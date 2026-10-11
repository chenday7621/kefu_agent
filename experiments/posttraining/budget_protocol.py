"""Audited execution-budget overlay; registered V1 goals/scorer stay immutable."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone

from .dev_protocol import sha, verify_bundle

VERSION = "qwen.dev.v2"
BUDGET = 30
# Predeclared infrastructure probes, not baseline/teacher actions. Their task
# rewards must remain critical (-1) even when the real backend rejects them.
NEGATIVE_PROBES = {"PTDEV-F3-01": "submit_confirmed_return",
                   "PTDEV-F3-05": "check_return_eligibility"}


def f3_tools(task):
    names = ["get_order_details"]
    if task["initial_db_fixture"]["tables"]["return_operations"]:
        names.append("get_operation_result")
    if task["task_id"] in NEGATIVE_PROBES:
        names.append(NEGATIVE_PROBES[task["task_id"]])
    return names


def planned_counts(task):
    variant = task["variant"]
    if task["family_id"] == "F1":
        stages = ["read", "prepare", "confirmed_submit"]
        calls = 3
    elif task["family_id"] == "F3":
        stages = f3_tools(task)
        calls = len(stages)
    elif variant == "target_revision":
        stages = ["read_original", "prepare_original", "read_new_target", "replace", "confirmed_submit"]
        calls = 5
    elif variant == "withdraw_then_resume":
        stages = ["read", "prepare_original", "withdraw_no_call", "replace_after_resume", "confirmed_submit"]
        calls = 4
    elif variant in ("quantity_revision", "reason_revision"):
        stages = ["read", "prepare_original", "replace", "confirmed_submit"]
        calls = 4
    else:
        stages = ["clarify_no_call", "read", "prepare", "confirmed_submit"]
        calls = 3
    return {"task_id": task["task_id"], "family": task["family"], "variant": variant,
            "stages": stages, "joint_decision_rounds": len(stages),
            "candidate_decisions": 6 * len(stages), "expected_mcp_calls": calls,
            "expected_no_call_decisions": 6 * len(stages) - calls,
            "negative_probe": NEGATIVE_PROBES.get(task["task_id"])}


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def create_v2(v1, directory):
    v1, directory = Path(v1).resolve(), Path(directory).resolve()
    original = verify_bundle(v1)
    if max(p["candidate_decisions"] for p in map(planned_counts, original)) != BUDGET:
        raise ValueError("Budget is not the audited maximum")
    directory.mkdir(parents=True, exist_ok=False)
    tasks = deepcopy(original)
    for task in tasks:
        task["episode_budget"]["max_policy_decisions"] = BUDGET
    for name in ("DEV_SCENARIO_FAMILIES.json", "REWARD_SPEC.json", "SPLIT_FREEZE.json"):
        (directory / name).write_bytes((v1 / name).read_bytes())
    (directory / "DEV_TASKS.jsonl").write_text("".join(json.dumps(t, ensure_ascii=False, sort_keys=True,
                                                               separators=(",", ":")) + "\n" for t in tasks))
    protocol = {"protocol_version": VERSION, "task_definition_version": "qwen.dev.v1",
                "v1_bundle": str(v1), "v1_manifest_sha256": file_hash(v1 / "DEV_MANIFEST.json"),
                "uniform_max_policy_decisions": BUDGET, "previous_max_policy_decisions": 24,
                "only_task_change": "episode_budget.max_policy_decisions: 24 -> 30",
                "budget_unit": "candidate SingleToolBatchSchema evaluations including NO_CALL",
                "max_joint_rounds": 5, "candidate_count_per_round": 6,
                "negative_probes": NEGATIVE_PROBES,
                "probe_purpose": "fixed oracle infrastructure checks only; critical reward is not masked",
                "scoring_version": "qwen.reward.v1", "baseline_runs": 0,
                "selection": "maximum of all 30 predeclared controller paths, no margin or per-task exception"}
    (directory / "DEV_V2_PROTOCOL.json").write_text(json.dumps(protocol, indent=2) + "\n")
    manifest = json.loads((v1 / "DEV_MANIFEST.json").read_text())
    manifest.update(protocol_version=VERSION, derived_from_manifest_sha256=protocol["v1_manifest_sha256"],
                    created_utc=datetime.now(timezone.utc).isoformat(), baseline_runs=0)
    manifest["tasks"] = [{"task_id": t["task_id"], "family": t["family"], "seed": t["seed"],
                          "sha256_canonical": sha(t)} for t in tasks]
    manifest["asset_sha256"] = {p.name: file_hash(p) for p in directory.iterdir() if p.is_file()}
    manifest["source_sha256"][Path(__file__).name] = file_hash(__file__)
    (directory / "DEV_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    verify_execution_bundle(directory)
    return protocol


def verify_execution_bundle(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "DEV_MANIFEST.json").read_text())
    if manifest["protocol_version"] == "qwen.dev.v1":
        return verify_bundle(directory)
    if manifest["protocol_version"] != VERSION:
        raise ValueError("Unregistered execution protocol")
    protocol = json.loads((directory / "DEV_V2_PROTOCOL.json").read_text())
    if protocol["uniform_max_policy_decisions"] != BUDGET or protocol["negative_probes"] != NEGATIVE_PROBES:
        raise ValueError("Unregistered budget/probe plan")
    v1 = Path(protocol["v1_bundle"])
    if file_hash(v1 / "DEV_MANIFEST.json") != protocol["v1_manifest_sha256"]:
        raise ValueError("V1 manifest drift")
    original = verify_bundle(v1)
    expected = deepcopy(original)
    for task in expected:
        task["episode_budget"]["max_policy_decisions"] = BUDGET
    tasks = [json.loads(s) for s in (directory / "DEV_TASKS.jsonl").read_text().splitlines()]
    if tasks != expected:
        raise ValueError("V2 may change only the uniform decision budget")
    for name, digest in manifest["asset_sha256"].items():
        if file_hash(directory / name) != digest:
            raise ValueError("V2 asset drift: " + name)
    for name, digest in manifest["source_sha256"].items():
        if file_hash(Path(__file__).with_name(name)) != digest:
            raise ValueError("V2 source drift: " + name)
    for name in ("DEV_SCENARIO_FAMILIES.json", "REWARD_SPEC.json", "SPLIT_FREEZE.json"):
        if (directory / name).read_bytes() != (v1 / name).read_bytes():
            raise ValueError("Frozen family/reward/split changed")
    expected_entries = [{"task_id": t["task_id"], "family": t["family"], "seed": t["seed"],
                         "sha256_canonical": sha(t)} for t in tasks]
    if manifest["tasks"] != expected_entries:
        raise ValueError("V2 task manifest mismatch")
    return tasks
