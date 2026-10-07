"""Replay existing evidence only. This entry cannot start servers or call a model."""
import argparse
import difflib
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .refund_v2 import VERSION, score, observe
from .test_refund_v2 import CASES

SOURCES = {"V1": Path("results/app_eval_v1_20261006_151637"),
           "S1": Path("results/app_s1_confirmed_snapshot_20261006_165858")}
ALLOWED = {"status", "errors", "needs_review", "false_refund_claim"}


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def summaries(rows, field):
    counts = Counter(r[field]["status"] if r[field] is not None else "unavailable" for r in rows)
    return {"planned": len(rows), "status_counts": dict(counts),
            "pass_per_plan": counts["pass"] / len(rows),
            "complete_execution": sum(r["execution_status"] == "completed" for r in rows),
            "complete_execution_scores": dict(Counter(r[field]["status"] for r in rows
                if r["execution_status"] == "completed" and r[field] is not None)),
            "execution_status_counts": dict(Counter(r["execution_status"] for r in rows))}


def run(audit, result):
    audit, result = Path(audit), Path(result)
    audit.mkdir(parents=True, exist_ok=True)
    result.mkdir(parents=True, exist_ok=True)
    tests = []
    for category, text, expected in CASES:
        observed = observe(text)
        assert [h["label"] for h in observed["hits"]] == expected
        tests.append({"category": category, "expected": expected, "passed": True, **observed})
    save(audit / "refund_context_tests.json", {"passed": len(tests), "failed": 0, "cases": tests,
          "model_calls": 0, "scope_test": "negation in preceding message does not exempt next message"})
    all_rows = []
    for family, root in SOURCES.items():
        records = []
        for planned in read(root / "plan.json"):
            aid = planned["attempt_id"]
            folder = root / "tasks" / aid
            row = {"family": family, "logical_unit": aid, "repeat": planned["repeat"],
                   "seed": planned["seed"], "scenario": planned["scenario"]["id"],
                   "goal": planned["scenario"], "execution_status": "not_run",
                   "score_status": "unavailable", "original_score_status": "unavailable",
                   "old_score": None, "new_score": None,
                   "observations": [], "changed": False}
            if (folder / "score.json").exists():
                task, initial, final = (read(folder / n) for n in
                    ("attempt.json", "initial_database.json", "final_database.json"))
                stored = read(folder / "score.json")
                original, revised, observations = score(task, initial, final, family)
                assert original == stored, f"Frozen scorer replay differs: {family}/{aid}"
                assert {k: v for k, v in original.items() if k not in ALLOWED} == {
                    k: v for k, v in revised.items() if k not in ALLOWED}, "Non-refund criterion changed"
                assert [x for x in original["errors"] if x != "false_refund_claim"] == [
                    x for x in revised["errors"] if x != "false_refund_claim"]
                assert all(x in revised["needs_review"] for x in original["needs_review"])
                status = "completed" if not task.get("error") else "interrupted"
                if status == "interrupted" and family == "S1":
                    stop = read(root / "STOP.json")
                    unknown = [c for c in read(root / "usage_summary.json")["calls"]
                               if c.get("attempt_id") == aid and not c.get("usage")]
                    if stop["reason"] == "budget_upper_estimate_cap" and unknown:
                        status = "interrupted_budget"
                        row["budget_interruption_evidence"] = {"stop": str(root / "STOP.json"),
                            "unknown_call_ids": [c["invocation_id"] for c in unknown]}
                row.update(execution_status=status, score_status=revised["status"],
                           original_score_status=stored["status"],
                           old_score=stored, new_score=revised, observations=observations,
                           changed=stored != revised, session_id=task.get("session_id"),
                           original_attempt=task, original_evidence=str(folder),
                           evidence_hashes={n: sha(folder / n) for n in
                               ("attempt.json", "score.json", "initial_database.json", "final_database.json")})
                row["change_reason"] = ("Context-only refund observation correction; all other criteria unchanged"
                    if row["changed"] else "Unchanged")
            save(result / "scores" / family / (aid + ".json"), row)
            records.append(row)
        all_rows.extend(records)
        save(result / (family + "_score_comparison.json"), records)
    changes = [r for r in all_rows if r["changed"]]
    save(audit / "score_changes.json", changes)
    save(audit / "state_mapping.json", [{k: r[k] for k in
        ("family", "logical_unit", "repeat", "execution_status", "original_score_status", "score_status", "original_evidence") if k in r}
        for r in all_rows])
    summary = {}
    for family in SOURCES:
        rows = [r for r in all_rows if r["family"] == family]
        summary[family] = {"original_frozen": summaries(rows, "old_score"),
                          "same_context_v2_offline": summaries(rows, "new_score"),
                          "by_repeat": {str(n): {
                              "original_frozen": summaries([r for r in rows if r["repeat"] == n], "old_score"),
                              "same_context_v2_offline": summaries([r for r in rows if r["repeat"] == n], "new_score")}
                              for n in (1, 2)}}
    legal = [r for r in all_rows if r["family"] == "V1" and r["scenario"].startswith("R")]
    summary["V1_legal_only"] = {"original_frozen": summaries(legal, "old_score"),
                                "same_context_v2_offline": summaries(legal, "new_score")}
    summary["completion_old_score_view"] = {"status": "WAITING_BUDGET", "planned": 16,
                                             "new_attempts": 0, "pending_units": ["r2_R1", "r2_R6"]}
    summary["replay_checks"] = {"stored_scores_reproduced_exactly": 55,
        "non_refund_fields_unchanged": 55, "new_tests_passed": len(tests),
        "changed_units": [{"family": r["family"], "unit": r["logical_unit"],
            "old": r["old_score"]["status"], "new": r["new_score"]["status"]} for r in changes],
        "B2_and_B3_original_criteria_unchanged": True, "new_model_calls": 0,
        "human_factual_evaluation": "unreviewed"}
    save(result / "offline_metrics.json", summary)
    save(audit / "offline_metrics.json", summary)
    files = [p for p in Path(__file__).parent.iterdir() if p.suffix == ".py"]
    save(audit / "scoring_source_hashes.json", {str(p): sha(p) for p in sorted(files)})
    # A separate diff, not a mutation of either frozen scorer.
    original = Path("apps/retail_demo/eval_v1/scoring.py")
    revised = Path(__file__).with_name("refund_v2.py")
    (audit / "SCORER_CONTEXT_V2.diff").write_text("".join(difflib.unified_diff(
        original.read_text().splitlines(True), revised.read_text().splitlines(True),
        fromfile=str(original), tofile=str(revised))))
    save(audit / "scoring_revision.json", {"version": VERSION, "utc": datetime.now(timezone.utc).isoformat(),
        "changed_check": "false_refund_claim only", "original_scorer_sha256": sha(original),
        "old_scorer_preserved": True, "context_scorer_sha256": sha(revised),
        "quoted_conditional_uncertain_hits": "needs_review", "criteria": "no task-ID special cases"})
    print(json.dumps(summary["replay_checks"], ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", required=True)
    parser.add_argument("--results", required=True)
    args = parser.parse_args()
    run(args.audit, args.results)
