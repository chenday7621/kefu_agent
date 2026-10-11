"""Exact causal completion loss with the full prefix and selected LM projection.

Only ignored output positions are omitted from the vocabulary projection. All
attention inputs, completion tokens, EOS, gradients and the objective remain.
Uses the installed Qwen3 forward's public logits_to_keep tensor argument.
"""

import torch
from torch.nn import functional as F


def completion_loss(model, inputs, num_items_in_batch=None):
    labels = inputs["labels"]
    if labels.ndim != 2 or labels.shape[0] != 1 or labels[0, 0] != -100:
        raise ValueError("Frozen batch1 and masked nonempty prefix required")
    positions = torch.nonzero(labels[0, 1:] != -100, as_tuple=False).flatten()
    if not positions.numel():
        raise ValueError("No supervised completion tokens")
    targets = labels[:, positions + 1]
    forward = {k: v for k, v in inputs.items() if k != "labels"}
    outputs = model(**forward, labels=None, use_cache=False, logits_to_keep=positions)
    logits = outputs.logits.float()
    loss = F.cross_entropy(logits.reshape(-1, logits.shape[-1]), targets.reshape(-1), reduction="sum")
    denominator = num_items_in_batch if num_items_in_batch is not None else targets.numel()
    if torch.is_tensor(denominator):
        denominator = denominator.to(loss.device)
    loss = loss / denominator
    return loss, outputs


def trainer_class():
    from transformers import Trainer

    class CompletionTrainer(Trainer):
        def compute_loss(self, model, inputs, return_outputs=False, num_items_in_batch=None):
            loss, outputs = completion_loss(model, inputs, num_items_in_batch)
            return (loss, outputs) if return_outputs else loss

    return CompletionTrainer
