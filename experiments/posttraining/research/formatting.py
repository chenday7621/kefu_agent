"""Completion-only loss without masking genuine EOS when pad == EOS."""

import json


def completion_record(tokenizer, prompt_ids, target_json, maximum):
    # Prefix ids are retained from the actual inference boundary, not retemplated.
    target = json.dumps(target_json, ensure_ascii=False, separators=(",", ":"))
    target_ids = tokenizer.encode(target, add_special_tokens=False)
    eos = tokenizer.convert_tokens_to_ids("<|im_end|>")
    if not isinstance(eos, int) or eos < 0:
        raise ValueError("Qwen end marker unavailable")
    if target_ids and target_ids[-1] == eos:
        raise ValueError("Target already contains an end marker")
    target_ids.append(eos)
    if not prompt_ids or len(prompt_ids) + len(target_ids) > maximum:
        raise ValueError("Empty prompt or CONTEXT_LIMIT; truncation forbidden")
    return {
        "input_ids": list(prompt_ids) + target_ids,
        "attention_mask": [1] * (len(prompt_ids) + len(target_ids)),
        "labels": [-100] * len(prompt_ids) + target_ids,
        "prompt_length": len(prompt_ids),
    }


class CompletionCollator:
    def __init__(self, pad_token_id):
        self.pad_token_id = pad_token_id

    def __call__(self, rows):
        import torch

        width = max(len(row["input_ids"]) for row in rows)
        result = {k: [] for k in ("input_ids", "attention_mask", "labels")}
        for row in rows:
            n = len(row["input_ids"])
            assert len(row["labels"]) == len(row["attention_mask"]) == n
            assert any(label != -100 for label in row["labels"])
            result["input_ids"].append(row["input_ids"] + [self.pad_token_id] * (width - n))
            result["attention_mask"].append(row["attention_mask"] + [0] * (width - n))
            result["labels"].append(row["labels"] + [-100] * (width - n))
        return {k: torch.tensor(v) for k, v in result.items()}
