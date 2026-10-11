import unittest
from types import SimpleNamespace
from .completion_wait import post_native, PENDING


class WaitTests(unittest.TestCase):
    def test_pending_snapshot_does_not_resubmit_policy_or_user(self):
        calls = []
        waits = []
        responses = [409, 409, 200]

        def post(url, **kwargs):
            calls.append(url)
            status = responses.pop(0)
            return SimpleNamespace(
                status_code=status,
                json=lambda: {"error": PENDING} if status == 409 else {"ready": True},
                raise_for_status=lambda: None,
            )

        self.assertEqual(
            post_native(
                SimpleNamespace(post=post),
                "http://owned",
                "/demo/sessions/S/snapshot",
                {},
                lambda: waits.append(1),
            ),
            {"ready": True},
        )
        self.assertEqual(len(waits), 2)
        self.assertEqual(calls, ["http://owned/demo/sessions/S/snapshot"] * 3)

    def test_other_conflict_is_never_replayed(self):
        calls = []

        def post(url, **kwargs):
            calls.append(url)

            def error():
                raise RuntimeError("business conflict")

            return SimpleNamespace(
                status_code=409, json=lambda: {"error": PENDING}, raise_for_status=error
            )

        with self.assertRaises(RuntimeError):
            post_native(SimpleNamespace(post=post), "http://owned", "/confirm", {}, lambda: None)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
