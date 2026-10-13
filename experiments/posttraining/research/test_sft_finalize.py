"""CPU contracts for directory discovery and evaluation-only finalization."""

import ast
import inspect
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from .sft_execution import checkpoint_directories
from . import sft_finalize


class CheckpointContracts(unittest.TestCase):
    def test_failed_directory_shape_and_invalid_entries(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("checkpoint-64", "checkpoint-50", "checkpoint-3", "checkpoint-tmp",
                         "checkpoint-2-HASHES.json", "checkpoint--1", "checkpoint-64-extra"):
                (root / name).mkdir()
            for name in ("checkpoint-50-HASHES.json", "checkpoint-64.json", "checkpoint-2"):
                (root / name).write_text('{}\n')
            (root / "checkpoint-1").symlink_to(root / "checkpoint-64", target_is_directory=True)
            old = (root / "checkpoint-50-HASHES.json").read_bytes()
            self.assertEqual([p.name for p in checkpoint_directories(root)],
                             ["checkpoint-3", "checkpoint-50", "checkpoint-64"])
            self.assertEqual((root / "checkpoint-50-HASHES.json").read_bytes(), old)
            with self.assertRaises(ValueError):
                sorted(root.glob("checkpoint-*"), key=lambda p: int(p.name.split('-')[-1]))

    def test_completed_state_only(self):
        good = {"global_step": 64, "max_steps": 64, "epoch": 1.0}
        sft_finalize.check_state(good)
        for key, value in (("global_step", 50), ("max_steps", 65), ("epoch", 0.99)):
            with self.assertRaises(ValueError):
                sft_finalize.check_state(dict(good, **{key: value}))

    def test_exact_adapter_config(self):
        config = {"lora_r": 16, "lora_alpha": 32, "lora_dropout": 0.05,
                  "lora_bias": "none", "model_path": "locked-local", "target_modules": ["q_proj"]}
        adapter = {"r": 16, "lora_alpha": 32, "lora_dropout": 0.05, "bias": "none",
                   "peft_type": "LORA", "task_type": "CAUSAL_LM", "inference_mode": True,
                   "base_model_name_or_path": "locked-local", "target_modules": ["q_proj"], "revision": None}
        sft_finalize.check_adapter(adapter, config)
        for key, value in (("r", 8), ("target_modules", ["k_proj"]), ("revision", "other"),
                           ("rank_pattern", {"q_proj": 4})):
            with self.assertRaises(ValueError):
                sft_finalize.check_adapter(dict(adapter, **{key: value}), config)

    def test_shape_mapping(self):
        base = {"hidden_size": 8, "intermediate_size": 12, "num_attention_heads": 4,
                "num_key_value_heads": 2, "head_dim": 2, "num_hidden_layers": 1}
        shapes = sft_finalize.expected_shapes(base, 2, ["q_proj", "k_proj", "down_proj"])
        self.assertEqual(shapes["base_model.model.model.layers.0.self_attn.k_proj.lora_B.weight"], [4, 2])
        self.assertEqual(shapes["base_model.model.model.layers.0.mlp.down_proj.lora_A.weight"], [2, 12])

    def test_no_training_or_random_adapter_entry(self):
        tree = ast.parse(inspect.getsource(sft_finalize))
        forbidden = {"train", "step", "backward", "create_optimizer", "get_peft_model", "model_and_tokenizer"}
        calls = {node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
                 for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, (ast.Attribute, ast.Name))}
        self.assertFalse(calls & forbidden)
        self.assertIn("evaluate", calls)
        self.assertNotIn("resume_from_checkpoint", inspect.getsource(sft_finalize))
        self.assertIn("is_trainable=False", inspect.getsource(sft_finalize))


if __name__ == "__main__":
    unittest.main()
