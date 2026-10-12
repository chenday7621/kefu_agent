"""Explicit DATA-only order addendum and first fully verified prefix stop."""

import hashlib
import json
from pathlib import Path

from .data_freeze import assemble
from .runner import write


def scheduled_tasks(config, tasks):
    if not config.get("data_collection_addendum"):
        return tasks
    path = Path(config["data_collection_addendum"])
    if hashlib.sha256(path.read_bytes()).hexdigest() != config["data_collection_addendum_sha256"]:
        raise ValueError("DATA collection addendum hash changed")
    addendum = json.loads(path.read_text())
    if addendum["version"] != "data.collection.addendum.v1" or not addendum["user_authorized"]:
        raise ValueError("Explicit DATA order/stop authorization required")
    by_id = {t["episode_id"]: t for t in tasks}
    order = addendum["execution_order"]
    if len(order) != len(tasks) or len(set(order)) != len(order) or set(order) != set(by_id):
        raise ValueError("DATA addendum may only reorder the same registered tasks")
    result = [by_id[eid] for eid in order]
    for split in ("train", "sft_valid"):
        if [t["episode_id"] for t in result if t["split"] == split] != [t["episode_id"] for t in tasks if t["split"] == split]:
            raise ValueError("DATA order within a split changed")
    return result


class VerifiedPrefixStop:
    def __init__(self, config, registry, review):
        self.config, self.registry, self.review = config, registry, Path(review)
        self.manifest = None

    def __call__(self):
        root = Path(self.config["collection_directory"])
        episodes = [json.loads(s) for s in (root / "EPISODES.jsonl").read_text().splitlines()]
        qualified = [e for e in episodes if e["complete"] and e["business"]["passed"] and e["audit"]["passed"]]
        counts = {s: sum(e["split"] == s for e in qualified) for s in ("train", "sft_valid")}
        write(self.review / "DATA_PREFIX_PROGRESS.json", {"complete": len(episodes), "qualified": counts,
             "first_fully_verified_prefix_stop": True})
        if counts["train"] < 100 or counts["sft_valid"] < 20:
            return False
        config = dict(self.config, dataset_output=str(Path(self.config["dataset_output"]).parent
                      / "data-verified-prefixes" / str(len(episodes))))
        self.manifest = assemble(config, self.registry, episodes)
        self.manifest.update(collection_addendum_sha256=config["data_collection_addendum_sha256"],
                             prefix_episode_count=len(episodes), planned_maximum=240)
        write(self.review / f"DATA_PREFIX_QUALITY_{len(episodes)}.json", self.manifest)
        # No relaxed count proxy: original per-step, provenance, token, length,
        # deduplication, split and CALL/hard NO_CALL checks determine this exit.
        return self.manifest["status"] == "DATASET_VERIFIED"
