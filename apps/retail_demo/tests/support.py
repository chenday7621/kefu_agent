"""Real service process helpers and uniquely named, insert-only test orders."""

from contextlib import contextmanager
from pathlib import Path
import os
import signal
import subprocess
import sys
import time
import socket
from uuid import uuid4
from ..settings import ROOT
from ..db import connect


def fixture_order(settings, quantity=2, customer_id=None):
    oid = "VERIFY-" + uuid4().hex[:12].upper()
    item = oid + "-ITEM"
    with connect(settings.database_url) as conn:
        conn.execute(
            "INSERT INTO orders(id,customer_id,status,delivered_at) VALUES (%s,%s,'delivered',now()-interval '1 day')",
            (oid, customer_id or settings.customer_id),
        )
        conn.execute(
            "INSERT INTO order_items(id,order_id,product_name,variant,quantity,unit_price_cents) VALUES (%s,%s,'验证耳机','黑色',%s,12900)",
            (item, oid, quantity),
        )
    return oid, item


@contextmanager
def process(module, port, folder, args=(), env=None):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            raise RuntimeError(
                f"Port {port} is already occupied; stop the demo before verification."
            )
    except OSError:
        pass
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    log = (folder / (module.rsplit(".", 1)[-1] + ".log")).open("a")
    command = [sys.executable, "-m", module, *args]
    proc = subprocess.Popen(
        command,
        cwd=ROOT,
        env=env or os.environ.copy(),
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"{module} failed to start; see {log.name}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            raise TimeoutError(f"{module} did not bind its port; see {log.name}")
        yield proc
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        log.close()


async def confirm_prepared(settings, preview):
    """An explicit offline fixture offer plus real native customer event.

    The offer is marked as a fixture, never reported as an LLM-generated reply.
    Native PostgreSQL authorization validates it exactly as a persisted offer.
    """
    from ..postgres_stores import open_stores
    from parlant.core.customers import CustomerId
    from parlant.core.agents import AgentId
    from parlant.core.sessions import EventKind, EventSource

    async with open_stores(settings.database_url) as (store, _, __):
        session = await store.create_session(
            CustomerId(settings.customer_id), AgentId("retail-persistent-demo"), title="MCP确认夹具"
        )
        await store.create_event(
            session.id,
            EventSource.AI_AGENT,
            EventKind.MESSAGE,
            "offline-fixture",
            {
                "message": preview["confirmation_phrase"],
                "participant": {"id": "retail-persistent-demo", "display_name": "离线验证夹具"},
            },
            metadata={"fixture_offer": True},
        )
        event = await store.create_event(
            session.id,
            EventSource.CUSTOMER,
            EventKind.MESSAGE,
            "offline-fixture",
            {
                "message": preview["confirmation_phrase"],
                "participant": {"id": settings.customer_id, "display_name": "验证用户"},
            },
        )
        return {"ok": True, "event_id": str(event.id), "session_id": str(session.id)}
