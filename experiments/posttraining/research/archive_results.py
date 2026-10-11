"""Archive actual stage coverage, paired metrics and NAS evidence references."""

from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import subprocess

from .budget import BudgetLedger
from .business_metrics import audit_rows
from .runner import write


def rows(path):
    return [json.loads(s) for s in path.read_text().splitlines()] if path.exists() else []


def metrics(data):
    groups = defaultdict(list)
    for row in data:
        groups[row["family"]].append(row)
    rewards = Counter(str(r["score"]["reward"]) for r in data)
    result = {"completed": len(data), "reward_counts": {str(t): rewards[str(t)] for t in (1.0, 0.4, 0.0, -1.0)},
              "task_success_count": sum(r["score"]["metrics"]["task_success"] for r in data),
              "task_success_rate": sum(r["score"]["metrics"]["task_success"] for r in data) / 30 if len(data) == 30 else None,
              "completed_task_success_rate": sum(r["score"]["metrics"]["task_success"] for r in data) / len(data) if data else None,
              "failure_types": dict(Counter(r["failure_type"] for r in data)),
              "attribution_all_pass": all(r["audit"]["passed"] for r in data)}
    for key in ("wrong_tool", "wrong_arguments", "backend_rejection", "unauthorized_write_attempt",
                "duplicate_tool_call", "native_gate_rejection", "turns", "tool_calls", "final_db_match"):
        result[key] = sum(r["score"]["metrics"][key] for r in data)
    result["policy_decisions"] = sum(r["audit"]["policy_decisions"] for r in data)
    for key in ("turns", "tool_calls", "policy_decisions"):
        result[key + "_per_task"] = result[key] / len(data) if data else None
    result["prompt_tokens"] = sum(r["model_metrics"]["prompt_tokens"] for r in data)
    result["output_tokens"] = sum(r["model_metrics"]["output_tokens"] for r in data)
    result["episode_latency_seconds"] = sum(r["latency_seconds"] for r in data)
    result["family"] = {name: {"completed": len(rs), "reward_counts": dict(Counter(str(r["score"]["reward"]) for r in rs)),
                                  "successes": sum(r["score"]["metrics"]["task_success"] for r in rs)}
                        for name, rs in groups.items()}
    return result


def archive(plan):
    review, runtime = Path(plan["review"]), Path(plan["runtime"])
    config = json.loads(Path(plan["experiment_config"]).read_text())
    repo = Path(config["repo"])
    state = json.loads((review / "RUN_STATE.json").read_text())
    stages = {"b0": rows(runtime / "b0/PER_TASK.jsonl"),
              "sft1": rows(runtime / "sft1_eval/PER_TASK.jsonl")}
    if plan.get("boundary_addendum"):
        from .boundary_continuation import combined_b0
        stages["b0"] = combined_b0(plan)
    budget = BudgetLedger(config["budget_directory"], config["authorization"], config["prices"]).summary()
    summary = {"status": state["status"], "version": config["version"], "budget": budget,
               "comparison_scope": "Shared business capabilities; causal-template and episode isolated limited DEV comparison",
               "clean_test": False, "second_sft": False, "rl": False,
               "b0": metrics(stages["b0"]), "sft1": metrics(stages["sft1"])}
    if plan.get("boundary_addendum"):
        summary["b0"]["planned_tasks"] = 30
        summary["b0"]["complete_execution_rate"] = len(stages["b0"]) / 30
        summary["b0"]["unscored_task_ids"] = ["PTDEV-F3-04"]
        summary["b0"]["original_23"] = metrics(rows(runtime / "b0/PER_TASK.jsonl"))
        summary["b0"]["post_patch_6"] = metrics(rows(runtime / "b0_post_boundary/PER_TASK.jsonl"))
        comparison_path = review / "EVAL_COMPARABILITY.json"
        comparison = json.loads(comparison_path.read_text()) if comparison_path.exists() else None
        if comparison:
            primary = set(comparison["primary_paired_task_ids"])
            summary["primary_comparison"] = {
                "fixed_task_ids": comparison["primary_paired_task_ids"],
                "denominator": len(primary),
                "b0": metrics([r for r in stages["b0"] if r["task_id"] in primary]),
                "sft1": metrics([r for r in stages["sft1"] if r["task_id"] in primary]),
                "frozen_before_training": True,
                "excluded_contract_affected": comparison["contract_affected_complete_task_ids"],
                "excluded_missing": comparison["missing_b0_task_ids"]}
    for name, data in stages.items():
        accounting = audit_rows(data)
        write(review / (name.upper() + "_BUSINESS_ACCOUNTING.json"), accounting)
        summary[name]["business_accounting"] = accounting["totals"]
    prior_audit = Path(plan.get("prior_review", str(review))) / "FIRST_THREE_READONLY_AUDIT.json"
    if prior_audit.exists():
        summary["existing_readonly_audit_reference"] = {
            "path": str(prior_audit), "sha256": hashlib.sha256(prior_audit.read_bytes()).hexdigest(),
            "audit_repeated": False,
            "previously_verified_provider_error": "F1-03: frozen response provider presented an order ID as an operation ID; no real user confirmation",
            "failure_blame_allocation": "No numerical causal allocation or paid counterfactual diagnosis"}
    write(review / "RESULT_SUMMARY.json", summary)
    for name, data in stages.items():
        write(review / (name.upper() + "_METRICS.json"), summary[name])
        path = review / (name.upper() + "_PER_TASK.jsonl")
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in data))
    paired = []
    after = {r["task_id"]: r for r in stages["sft1"]}
    for before in stages["b0"]:
        other = after.get(before["task_id"])
        row = {"task_id": before["task_id"], "family": before["family"],
               "b0_reward": before["score"]["reward"], "sft1_reward": other["score"]["reward"] if other else None,
               "b0_failure": before["failure_type"], "sft1_failure": other["failure_type"] if other else "NOT_MEASURED"}
        for key in ("task_success", "wrong_tool", "wrong_arguments", "backend_rejection", "unauthorized_write_attempt",
                    "duplicate_tool_call", "tool_calls", "turns", "final_db_match"):
            row["b0_" + key] = before["score"]["metrics"][key]
            row["sft1_" + key] = other["score"]["metrics"][key] if other else None
        row["primary_comparable"] = before["task_id"] in set(summary.get("primary_comparison", {}).get("fixed_task_ids", [])) if plan.get("boundary_addendum") else True
        paired.append(row)
    if paired:
        with (review / "PAIRED_PER_TASK.csv").open("w") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(paired[0]))
            writer.writeheader()
            writer.writerows(paired)
    references = {}
    for data in stages.values():
        for row in data:
            directory = Path(row["attempt_directory"])
            for path in directory.glob("trajectories/*.jsonl"):
                references[str(path)] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
            for name in ("SCORE.json", "EPISODE_EVIDENCE.json", "CLEANUP.json", "TRAJECTORY_AUDIT.json"):
                path = directory / name
                references[str(path)] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
    write(review / "EVIDENCE_REFERENCES.json", references)
    tracked = subprocess.check_output(["git", "ls-files", "experiments/posttraining", "apps/retail_demo", "src/parlant"], cwd=repo, text=True).splitlines()
    write(review / "SOURCE_HASHES.json", {n: hashlib.sha256((repo / n).read_bytes()).hexdigest() for n in tracked})
    (review / "GIT_STATUS.txt").write_text(subprocess.check_output(["git", "status", "--short"], cwd=repo, text=True))
    (review / "SOURCE_DIFF.diff").write_text(subprocess.check_output(["git", "diff", "17652d4c7508e05002de6a720332e7ac3db35957", "--", "experiments/posttraining", "apps/retail_demo/outbox.py"], cwd=repo, text=True))
    conclusion = "SFT复评缺测，不能评估提升。"
    if len(stages["b0"]) == len(stages["sft1"]) == 30:
        delta = summary["sft1"]["task_success_count"] - summary["b0"]["task_success_count"]
        conclusion = "任务成功数" + ("提升" if delta > 0 else "下降" if delta < 0 else "持平") + f"（变化{delta:+d}/30）；同时参考错误和逐题对照，不追加训练。"
    if summary.get("primary_comparison"):
        paired_summary = summary["primary_comparison"]
        if paired_summary["sft1"]["completed"] == paired_summary["denominator"]:
            delta = paired_summary["sft1"]["task_success_count"] - paired_summary["b0"]["task_success_count"]
            conclusion = f"固定可比题集{paired_summary['denominator']}题：成功数变化{delta:+d}；全30题SFT结果另报，契约变化/缺测不计纯SFT收益。"
    text = f"""# PT-B0-R1-CONTRACTFIX / PT-SFT1

状态：`{state['status']}`。

{conclusion}

- B0：{len(stages['b0'])}/30；reward分布 {summary['b0']['reward_counts']}。
- SFT1复评：{len(stages['sft1'])}/30；reward分布 {summary['sft1']['reward_counts']}。
- 累计费用估算/未知预留：USD{budget['estimated_or_reserved_usd']:.6f}；总授权USD10，至少USD2复评预留；实际账单未知。
- 未知用量请求：{budget['unknown_usage_requests']}；在途：{budget['in_flight']}。
- 完整运行/轨迹/数据库/数据/adapter留在 `{runtime}`，不进入Git。
- 旧0题完成attempt及其费用只读保留，未混入R1成绩。
- APP授权例外为Outbox错误ToolResult的两处类型检查；实验层另有显式版本化参数边界修复；确认、授权、快照、PG写入、幂等、事务、去重和DEV/scorer均未修改。
- 本轮共享广义业务能力，具体模板/episode隔离；30题为DEV，不能称clean test或广义family未见泛化。
- 不将环境错误契约修复收益归因于SFT；固定回复provider错误也不能全部归因于Qwen policy。
- 既有只读审计已确认F1-03的冻结回复provider错误展示operation_id；这里只引用原审计hash，不重复审计或追加收费诊断。
- BUSINESS_ACCOUNTING将policy proposal、会话检查拒绝、native ToolCaller dispatch、MCP wire请求、错误预览与正式申请分别计数；wire invocation_id仅计数，不用时间邻近猜因果关联。回复provider只登记可直接核验的文本标记，不声称穷尽其错误或据此分配失败因果比例。
- 无第二轮SFT、RL、legacy/holdout运行或演示部署。

阻塞（若有）：{state.get('blocker_type', '')} {state.get('blocker_reason', '')}

详细指标：RESULT_SUMMARY.json、PAIRED_PER_TASK.csv、DATA_QUALITY_REPORT和训练结果引用。未完成阶段明确为缺测。
"""
    if plan.get("boundary_addendum"):
        text += "\nB0不是全30题同版本：原23题/新契约6题分别保存，F3-04保持契约中断unscored，缺测不记0。原23题受影响单元按离线兼容审计单列，主对照题集在SFT前冻结。\n"
    (review / "REPORT.md").write_text(text)
    (review / "FINAL_REPORT.md").write_text(text)
    return summary
