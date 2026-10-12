"""Installed PEFT toggle semantics on fixed CPU parameters, without a model."""

from contextlib import contextmanager
from types import SimpleNamespace, MethodType
import unittest

import torch
from peft.tuners.tuners_utils import BaseTunerLayer

from .adapter_binding import frozen_adapter_comparison, require_frozen_parameters


class FixedParameters(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.base = torch.nn.Parameter(torch.tensor([3.0, 4.0]), requires_grad=False)
        self.adapters = torch.nn.ParameterDict({
            "default": torch.nn.Parameter(torch.tensor([1.0, 2.0]), requires_grad=False),
            "inactive": torch.nn.Parameter(torch.tensor([5.0]), requires_grad=False),
        })
        self.layer = SimpleNamespace(adapter_layer_names=["adapters"], adapters=self.adapters,
                                     active_adapters=["default"])
        self.layer.set_adapter = MethodType(BaseTunerLayer.set_adapter, self.layer)
        self.entered = 0
        self.flags_after_native_exit = None
        self.eval()

    @contextmanager
    def disable_adapter(self):
        self.entered += 1
        BaseTunerLayer.enable_adapters(self.layer, False)
        try:
            yield
        finally:
            BaseTunerLayer.enable_adapters(self.layer, True)
            self.flags_after_native_exit = {n: p.requires_grad for n, p in self.named_parameters()}


class BindingFlags(unittest.TestCase):
    def test_normal_exit_and_multiple_parameters(self):
        model = FixedParameters()
        values = {n: p.detach().clone() for n, p in model.named_parameters()}
        with frozen_adapter_comparison(model):
            self.assertFalse(any(p.requires_grad for p in model.parameters()))
        self.assertTrue(model.flags_after_native_exit["adapters.default"])
        self.assertFalse(model.flags_after_native_exit["adapters.inactive"])
        require_frozen_parameters(model)
        for name, parameter in model.named_parameters():
            self.assertTrue(torch.equal(parameter, values[name]))
        self.assertFalse(model.training)

    def test_exception_exit_restores_and_preserves_exception(self):
        model = FixedParameters()
        original = ValueError("fixture comparison failure")
        with self.assertRaises(ValueError) as caught:
            with frozen_adapter_comparison(model):
                raise original
        self.assertIs(caught.exception, original)
        self.assertTrue(model.flags_after_native_exit["adapters.default"])
        require_frozen_parameters(model)

    def test_initial_trainable_parameter_rejected_without_masking(self):
        model = FixedParameters()
        model.base.requires_grad_(True)
        with self.assertRaisesRegex(RuntimeError, "before comparison"):
            with frozen_adapter_comparison(model):
                self.fail("must not enter")
        self.assertEqual(model.entered, 0)
        self.assertTrue(model.base.requires_grad)
        with self.assertRaisesRegex(RuntimeError, "trainable"):
            require_frozen_parameters(model)

    def test_replaced_parameter_identity_rejected(self):
        model = FixedParameters()
        original = model.base
        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            with frozen_adapter_comparison(model):
                model.base = torch.nn.Parameter(torch.tensor([9.0]), requires_grad=True)
        self.assertFalse(original.requires_grad)
        self.assertTrue(model.base.requires_grad)
        self.assertFalse(model.adapters["default"].requires_grad)

    def test_added_parameter_rejected(self):
        model = FixedParameters()
        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            with frozen_adapter_comparison(model):
                model.extra = torch.nn.Parameter(torch.tensor([0.0]), requires_grad=True)
        self.assertTrue(model.extra.requires_grad)
        self.assertFalse(model.adapters["default"].requires_grad)

    def test_identity_change_with_exception_keeps_cause_and_restores(self):
        model = FixedParameters()
        original = ValueError("fixture comparison failure with changed parameters")
        with self.assertRaisesRegex(RuntimeError, "identity changed") as caught:
            with frozen_adapter_comparison(model):
                model.extra = torch.nn.Parameter(torch.tensor([0.0]), requires_grad=True)
                raise original
        self.assertIs(caught.exception.__context__, original)
        self.assertFalse(model.adapters["default"].requires_grad)
        self.assertTrue(model.extra.requires_grad)

    def test_removed_parameter_rejected_and_known_flags_restored(self):
        model = FixedParameters()
        original = model.base
        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            with frozen_adapter_comparison(model):
                del model.base
        self.assertFalse(original.requires_grad)
        self.assertFalse(model.adapters["default"].requires_grad)

    def test_postcondition_assertion_remains_effective(self):
        model = FixedParameters()
        with frozen_adapter_comparison(model):
            pass
        model.adapters["inactive"].requires_grad_(True)
        with self.assertRaisesRegex(RuntimeError, "trainable"):
            require_frozen_parameters(model)


if __name__ == "__main__":
    unittest.main()
