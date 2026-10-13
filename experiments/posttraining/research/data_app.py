"""TRAIN-only in-process visible expert, with the frozen native APP/reply path."""

import argparse
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from transformers import AutoTokenizer

from ..episode_evidence import CanonicalJournal
from . import episode_app as native_app
from . import evidence
from .data_expert import DataBatcher, DataRuleBackend


class SplitJournal(CanonicalJournal):
    def __init__(self, directory, split):
        super().__init__(directory)
        self.split = split

    def append(self, kind, event_id=None, **values):
        if kind in ("policy_proposal", "policy_failure"):
            values["split"] = self.split
        return super().append(kind, event_id, **values)


class ExpertTrajectory(evidence.ResearchTrajectory):
    def __init__(self, directory, config, task):
        super().__init__(directory, config, task)
        if task["split"] not in ("train", "sft_valid"):
            raise ValueError("DEV/oracle cannot enter expert collection")
        self.journal = SplitJournal(directory, task["split"])

    def begin(self, frame, tool_id, tool_name, messages, rendered, schema, model,
              revision, generation_origin="interface"):
        if generation_origin != "synthetic_rule_expert":
            raise ValueError("Independent collection requires the visible rule expert")
        # The frozen exact journal's execution-origin vocabulary uses fixture
        # for CPU generators. Detailed supervision provenance remains the
        # explicit synthetic_rule_expert in EXPERT_RECORDS and data manifests.
        return super().begin(frame, tool_id, tool_name, messages, rendered, schema,
                             model, revision, generation_origin="fixture")


async def run(config):
    if config["phase"] != "train" or config["mode"] != "training_visible_expert":
        raise ValueError("Explicit independent expert collection mode required")
    tokenizer = AutoTokenizer.from_pretrained(config["model_path"], local_files_only=True)
    backend = DataRuleBackend(tokenizer, config["directory"])
    # Scoped to this dedicated child process. Never changes a formal B0 process.
    with patch.object(native_app, "RemoteQwenBackend", lambda *_: backend), patch.object(
        native_app, "OnceBatcher", DataBatcher
    ), patch.object(evidence, "ResearchTrajectory", ExpertTrajectory):
        await native_app.run(config)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    asyncio.run(run(json.loads(parser.parse_args().config.read_text())))
