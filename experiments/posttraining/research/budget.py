"""Cross-process append-only usage reservations, including unknown outcomes."""

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import fcntl
import json
import os
from pathlib import Path
from uuid import uuid4


class BudgetStop(RuntimeError):
    pass


class BudgetLedger:
    def __init__(self, directory, authorization, prices):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.authorization, self.prices = authorization, prices
        self.path = self.directory / "BUDGET_LEDGER.jsonl"
        if Decimal(str(authorization["approved_total_usd"])) != Decimal("10"):
            raise ValueError("This run is authorized for exactly USD10")
        if Decimal(str(authorization["eval_reserve_usd"])) < Decimal("2"):
            raise ValueError("At least USD2 must remain reserved for post-SFT evaluation")

    @contextmanager
    def locked(self):
        fd = os.open(self.directory / "budget.lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def latest(self):
        rows = {}
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                row = json.loads(line)
                rows[row["request_id"]] = row
        return rows

    def append(self, row):
        row = dict(row, timestamp=datetime.now(timezone.utc).isoformat())
        with self.path.open("a") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def amount(row):
        value = row.get("estimated_usd")
        return Decimal(str(row["reserved_usd"] if value is None else value))

    def reserve(self, phase, schema, request):
        if phase not in ("startup", "b0", "train", "sft_eval", "anomaly"):
            raise ValueError("Unknown paid phase")
        maximum = request.get("max_tokens")
        if not isinstance(maximum, int) or maximum <= 0:
            raise BudgetStop("Cannot reserve an unknown output bound")
        inputs = len(json.dumps(request.get("messages", []), ensure_ascii=False).encode())
        amount = (
            Decimal(inputs) * Decimal(str(self.prices["miss"]))
            + Decimal(maximum) * Decimal(str(self.prices["output"]))
        ) / Decimal(10**6)
        with self.locked():
            rows = self.latest().values()
            total = sum((self.amount(r) for r in rows), Decimal(0))
            non_eval = sum((self.amount(r) for r in rows if r["phase"] != "sft_eval"), Decimal(0))
            cap = Decimal(str(self.authorization["approved_total_usd"]))
            reserve = Decimal(str(self.authorization["eval_reserve_usd"]))
            failures = sorted(self.latest().values(), key=lambda r: r["timestamp"], reverse=True)
            streak = 0
            for row in failures:
                if row["status"] == "completed":
                    break
                if row["status"] == "failed":
                    streak += 1
            if streak >= 3 or any(r.get("http_status") in (401, 402, 403) for r in failures):
                raise BudgetStop("Persistent provider authentication/balance/error stop")
            if total + amount > cap or (phase != "sft_eval" and non_eval + amount > cap - reserve):
                raise BudgetStop("USD10 cap or USD2 evaluation reserve reached")
            row = {
                "request_id": uuid4().hex,
                "phase": phase,
                "schema": schema,
                "requested_model": request.get("model"),
                "status": "in_flight",
                "input_byte_bound": inputs,
                "output_token_bound": maximum,
                "reserved_usd": float(amount),
                "estimated_usd": None,
                "usage": None,
                "actual_bill_usd": None,
                "physical_sdk_retries": 0,
            }
            self.append(row)
            return row["request_id"]

    def settle(self, request_id, *, usage=None, **metadata):
        with self.locked():
            row = self.latest()[request_id]
            if row["status"] != "in_flight":
                raise ValueError("A request can be settled only once")
            estimated = None
            if (
                usage
                and usage.get("prompt_tokens") is not None
                and usage.get("completion_tokens") is not None
            ):
                hit, miss = (
                    usage.get("prompt_cache_hit_tokens"),
                    usage.get("prompt_cache_miss_tokens"),
                )
                if hit is None or miss is None:
                    hit, miss = 0, usage["prompt_tokens"]
                estimated = (
                    Decimal(hit) * Decimal(str(self.prices["hit"]))
                    + Decimal(miss) * Decimal(str(self.prices["miss"]))
                    + Decimal(usage["completion_tokens"]) * Decimal(str(self.prices["output"]))
                ) / Decimal(10**6)
            row.update(
                metadata,
                usage=usage,
                estimated_usd=float(estimated) if estimated is not None else None,
            )
            self.append(row)

    def summary(self):
        with self.locked():
            rows = list(self.latest().values())
            return {
                "unique_requests": len(rows),
                "estimated_or_reserved_usd": float(sum((self.amount(r) for r in rows), Decimal(0))),
                "unknown_usage_requests": sum(r.get("estimated_usd") is None for r in rows),
                "in_flight": sum(r["status"] == "in_flight" for r in rows),
                "actual_bill_usd": None,
                "automatic_increase": False,
            }
