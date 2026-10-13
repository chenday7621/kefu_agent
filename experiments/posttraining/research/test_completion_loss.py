"""CPU algebra check; no Qwen model, downloads or paid calls."""

from types import SimpleNamespace
import unittest

import torch
from torch.nn import functional as F

from .completion_loss import completion_loss


class TinyCausal(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.embed = torch.nn.Embedding(19, 8)
        self.head = torch.nn.Linear(8, 19, bias=False)

    def forward(self, input_ids, attention_mask, labels=None, use_cache=False, logits_to_keep=0):
        hidden = self.embed(input_ids).cumsum(dim=1)
        if not isinstance(logits_to_keep, int):
            hidden = hidden[:, logits_to_keep, :]
        return SimpleNamespace(logits=self.head(hidden))


class LossTests(unittest.TestCase):
    def test_full_masked_and_selected_losses_gradients_match(self):
        torch.manual_seed(42)
        model = TinyCausal()
        inputs = {"input_ids": torch.tensor([[2, 3, 4, 5, 6, 7, 0]]),
                  "attention_mask": torch.tensor([[1, 1, 1, 1, 1, 1, 0]]),
                  "labels": torch.tensor([[-100, -100, -100, 5, 6, 7, -100]])}
        logits = model(**inputs).logits[:, :-1]
        original = F.cross_entropy(logits.reshape(-1, 19), inputs["labels"][:, 1:].reshape(-1))
        original.backward()
        gradients = {n: p.grad.clone() for n, p in model.named_parameters()}
        model.zero_grad()
        selected, _ = completion_loss(model, inputs)
        selected.backward()
        torch.testing.assert_close(selected, original)
        for name, parameter in model.named_parameters():
            torch.testing.assert_close(parameter.grad, gradients[name])
        # Input-only prefix participates causally; it is not detached/truncated.
        self.assertGreater(model.embed.weight.grad[2].abs().sum().item(), 0)

    def test_accumulation_denominator_matches_sum_over_target_tokens(self):
        torch.manual_seed(42)
        model = TinyCausal()
        inputs = {"input_ids": torch.tensor([[2, 3, 4, 5]]),
                  "attention_mask": torch.ones(1, 4, dtype=torch.long),
                  "labels": torch.tensor([[-100, -100, 4, 5]])}
        logits = model(**inputs).logits[:, :-1]
        original = F.cross_entropy(logits.reshape(-1, 19), inputs["labels"][:, 1:].reshape(-1), reduction="sum") / 11
        selected, _ = completion_loss(model, inputs, torch.tensor(11))
        torch.testing.assert_close(selected, original)


if __name__ == "__main__":
    unittest.main()
