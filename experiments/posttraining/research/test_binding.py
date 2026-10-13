"""SDK configuration lifecycle and atomic native-round budget, model-free."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from lagom import Container
from parlant.core.engines.alpha.hooks import EngineHooks, EngineHookResult
from . import episode_app


class BindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_pre_nlp_container_is_lazy_and_thirty_is_atomic(self):
        with TemporaryDirectory() as d:
            runtime = SimpleNamespace(
                policy=object(),
                logger=SimpleNamespace(steps={}),
                reserved=0,
                iteration=0,
                errors=[],
                directory=Path(d),
                config={"max_policy_decisions": 30, "episode_id": "E"},
            )
            c = Container()
            c[EngineHooks] = EngineHooks()
            context = SimpleNamespace(
                session=SimpleNamespace(id="S"),
                interaction=SimpleNamespace(
                    last_customer_message_event=SimpleNamespace(id="T"), events=[]
                ),
            )
            with patch.object(episode_app, "RUNTIME", runtime):
                c = await episode_app.configure_container(c)
                hooks = c[EngineHooks]
                start = hooks.on_preparation_iteration_start[0]
                end = hooks.on_preparation_iteration_end[0]
                self.assertEqual(await start(context), EngineHookResult.CALL_NEXT)
                await end(context)
                self.assertEqual(runtime.reserved, 0)
                for round_id in range(5):
                    self.assertEqual(await start(context), EngineHookResult.CALL_NEXT)
                    for n in range(6):
                        runtime.logger.steps[f"{round_id}-{n}"] = {"tool_results": []}
                    await end(context)
                self.assertEqual(runtime.reserved, 30)
                self.assertEqual(await start(context), EngineHookResult.BAIL)
                self.assertEqual(len(runtime.logger.steps), 30)
                self.assertTrue((Path(d) / "BUDGET_STOP.json").exists())


if __name__ == "__main__":
    unittest.main()


class SafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_trusted_parameter_values_and_nested_keys_detected(self):
        from .boundary import has_trusted_fields

        self.assertTrue(
            has_trusted_fields({"argument_evaluations": [{"parameter_name": "session_token"}]})
        )
        self.assertTrue(has_trusted_fields({"wrapped": {"customer_id": "synthetic"}}))
        self.assertFalse(
            has_trusted_fields(
                {"parameter_name": "operation_id", "value_as_string": "known-visible"}
            )
        )

    async def test_native_dispatch_budget_blocks_before_service_call(self):
        from .boundary import BoundedToolCaller, NativeToolBudget

        with TemporaryDirectory() as d:
            caller = BoundedToolCaller.__new__(BoundedToolCaller)
            caller.directory = Path(d)
            caller.maximum = 18
            caller.started = 18
            with self.assertRaises(NativeToolBudget):
                await caller._run_tool(None, SimpleNamespace(id="gate-bound-undispatched"), None)
            self.assertEqual(caller.started, 18)
            self.assertTrue((Path(d) / "TOOL_BUDGET_STOP.json").exists())
