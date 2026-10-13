"""Opt-in PARLANT_SDK_MODULE extension; importing this module starts nothing.

Never set this module for historical Retail/O1-B evaluation launchers.
"""

import os
from pathlib import Path

from .integration import install_container
from .policy import QwenPolicyProvider
from .trajectory import TrajectoryLogger


async def configure_container(container):
    if os.environ.get("POSTTRAIN_APP_POLICY") != "phase1a-interface":
        raise RuntimeError("Explicit independent APP experiment flag required")
    from transformers import AutoTokenizer

    model = Path("/mnt/nvme3/chenyi/posttraining/models/Qwen3-8B")
    tokenizer = AutoTokenizer.from_pretrained(model, local_files_only=True)
    trajectory = TrajectoryLogger()
    qwen = QwenPolicyProvider(tokenizer, trajectory)
    return install_container(container, qwen, trajectory)
