"""Freeze verified exact candidate samples; no model calls, no truncation."""

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import random
import re

from transformers import AutoTokenizer

from .data_audit import admit_sample, load_latest
from .formatting import completion_record


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def normalized_sample(sample, rendered):
    text = json.dumps({"prompt": rendered, "candidate": sample["candidate_tool"],
                       "target": sample["target_json"]}, ensure_ascii=False, sort_keys=True)
    identities = {}

    def replace(match):
        identity = match.group(0)
        if identity not in identities:
            identities[identity] = "<PUBLIC_ID_" + str(len(identities)) + ">"
        return identities[identity]

    text = re.sub(r"(?:ORD|ITEM|PTTRAIN|PTVALID)-[A-Za-z0-9_-]+|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", replace, text)
    return hashlib.sha256(text.encode()).hexdigest()


def assemble(config, registry, episodes):
    output = Path(config["dataset_output"])
    output.mkdir(parents=True, exist_ok=False)
    tokenizer = AutoTokenizer.from_pretrained(config["model_path"], local_files_only=True)
    samples = []
    exclusion = Counter()
    stats = Counter()
    dedup, normalized_owners = set(), {}
    for episode in episodes:
        if not episode["business"]["passed"]:
            exclusion["episode_business_validation_failed"] += 1
            continue
        directory = Path(episode["directory"])
        task = json.loads((directory / "TASK.json").read_text())
        evidence = json.loads((directory / "EPISODE_EVIDENCE.json").read_text())
        policies = load_latest(directory)
        experts = {r["policy_step_id"]: r for r in map(json.loads, (directory / "EXPERT_RECORDS.jsonl").read_text().splitlines())}
        grouped = defaultdict(list)
        for pid, policy in policies.items():
            stats[task["split"] + ":raw:" + str(policy["proposed_action"])] += 1
            try:
                row = admit_sample(task, policy, experts[pid], evidence["events"], episode["business"], registry)
                rendered = experts[pid]["rendered_prompt"]
                if tokenizer.encode(rendered, add_special_tokens=False) != row["prompt_token_ids"]:
                    raise ValueError("Actual runtime prefix token IDs differ from tokenizer")
                key = normalized_sample(row, rendered)
                if key in normalized_owners and normalized_owners[key] != row["split"]:
                    raise ValueError("Cross-split derived trajectory duplicate")
                normalized_owners[key] = row["split"]
                if key in dedup:
                    exclusion["normalized_duplicate"] += 1
                    continue
                dedup.add(key)
                row["normalized_dedup_hash"] = key
                row["lineage_path"] = str(directory / "EXPERT_RECORDS.jsonl")
                grouped[row["joint_round"]].append(row)
            except (ValueError, KeyError) as exc:
                integrity_markers = ("Cross-split", "Future or", "Target differs", "prompt differs",
                                     "prefix token IDs differ", "evidence was modified", "source span",
                                     "trusted server field", "cross-split proposal", "Prompt hash mismatch")
                if any(marker in str(exc) for marker in integrity_markers):
                    raise
                exclusion[str(exc)] += 1
        # Deterministic selection, fixed seed; never duplicates CALL for balance.
        rng = random.Random(42)
        for round_id in sorted(grouped):
            rows = grouped[round_id]
            essential = [r for r in rows if r["action"] == "CALL" or r["hard_no_call"]]
            simple = sorted([r for r in rows if r not in essential], key=lambda r: r["candidate_tool"])
            rng.shuffle(simple)
            exclusion["simple_no_call_cap"] += max(0, len(simple) - 2)
            samples.extend(essential + simple[:2])
    if len(samples) > 6000:
        raise RuntimeError("Dataset exceeds predeclared6000 cap; no automatic expansion")
    lengths = []
    for row in samples:
        encoded = completion_record(tokenizer, row["prompt_token_ids"], row["target_json"], 10**9)
        lengths.append(len(encoded["input_ids"]))
    maximum = next((n for n in (4096, 8192, 16384) if sum(x > n for x in lengths) <= 0.05 * len(lengths)), None)
    if maximum is None:
        raise RuntimeError("DATA_CONTEXT_BLOCKED: over5% exceed16384; truncation forbidden")
    retained = []
    for row, length in zip(samples, lengths):
        if length > maximum:
            exclusion["context_excluded"] += 1
            continue
        row.update(completion_record(tokenizer, row["prompt_token_ids"], row["target_json"], maximum))
        retained.append(row)
    def critical_strata(rows):
        return {(r["split"], r["family_id"], r["action"])
                for r in rows if r["action"] == "CALL" or r["hard_no_call"]}
    lost = critical_strata(samples) - critical_strata(retained)
    if lost:
        raise RuntimeError("DATA_CONTEXT_BLOCKED: context exclusion removes an entire critical family/action stratum")
    manifest = {"status": "DATASET_VERIFIED", "split_approved": True, "source_type": "synthetic_rule_expert",
                "split_policy": "split.policy.v2", "max_length": maximum, "truncation": False,
                "raw_distribution": dict(stats), "exclusions": dict(exclusion),
                "selected_samples": len(retained), "length_max": max(lengths, default=None),
                "selection": "CALL and all hard NO_CALL retained; simple NO_CALL <=2 per round; seed42",
                "models_loaded_for_assembly": 0, "oracle_or_dev_supervision": False,
                "critical_context_strata_preserved": True,
                "length_distribution": {str(n): sum(x <= n for x in lengths) for n in (4096, 8192, 16384)},
                "assembly_source_sha256": digest(__file__)}
    for split in ("train", "sft_valid"):
        rows = [r for r in retained if r["split"] == split]
        path = output / (split + ".jsonl")
        with path.open("w") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        manifest[split] = {"path": str(path), "sha256": digest(path), "samples": len(rows),
                           "episodes": len({r["episode_id"] for r in rows}),
                           "calls": sum(r["action"] == "CALL" for r in rows),
                           "hard_no_call": sum(r["hard_no_call"] for r in rows),
                           "families": dict(Counter(r["family_id"] for r in rows)),
                           "tools": dict(Counter(r["candidate_tool"] for r in rows))}
    if (manifest["train"]["episodes"] < 100 or manifest["sft_valid"]["episodes"] < 20
            or manifest["train"]["samples"] < 1000 or manifest["train"]["calls"] < 200
            or manifest["train"]["hard_no_call"] < 200):
        manifest["status"] = "DATASET_INSUFFICIENT"
    (output / "TRAIN_DATA_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest
