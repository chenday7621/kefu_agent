"""Versioned migrations and insert-only, repeatable synthetic seed."""

import argparse
import hashlib
import psycopg
from psycopg.rows import dict_row
from .settings import load_settings, APP


def connect(url: str):
    return psycopg.connect(
        url,
        row_factory=dict_row,
        options="-c search_path=retail_demo,public -c timezone=UTC",
        connect_timeout=5,
    )


def initialize(url: str) -> None:
    with connect(url) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(8810, 1)")
        conn.execute("CREATE SCHEMA IF NOT EXISTS retail_demo")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS retail_demo.schema_migrations (version text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        for p in sorted((APP / "migrations").glob("*.sql")):
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
            row = conn.execute(
                "SELECT sha256 FROM schema_migrations WHERE version=%s", (p.name,)
            ).fetchone()
            if row:
                if row["sha256"] != digest:
                    raise RuntimeError("Applied migration changed; add a new version instead.")
                continue
            conn.execute(p.read_text())
            conn.execute(
                "INSERT INTO schema_migrations(version,sha256) VALUES (%s,%s)", (p.name, digest)
            )


def seed(url: str) -> None:
    with connect(url) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(8810, 2)")
        for cid, name in [("demo-alice", "演示客户 Alice"), ("demo-bob", "演示客户 Bob")]:
            conn.execute("INSERT INTO customers VALUES (%s,%s) ON CONFLICT DO NOTHING", (cid, name))
        for oid, cid, status, days in [
            ("DEMO-1001", "demo-alice", "delivered", 3),
            ("DEMO-1002", "demo-alice", "pending", None),
            ("DEMO-1003", "demo-alice", "delivered", 45),
            ("DEMO-2001", "demo-bob", "delivered", 2),
        ]:
            conn.execute(
                "INSERT INTO orders(id,customer_id,status,delivered_at) VALUES (%s,%s,%s, CASE WHEN %s::integer IS NULL THEN NULL ELSE now()-(%s::integer*interval '1 day') END) ON CONFLICT DO NOTHING",
                (oid, cid, status, days, days),
            )
        for row in [
            ("DEMO-1001-HEADSET", "DEMO-1001", "无线耳机", "黑色 / 4小时续航", 3, 12900, True),
            ("DEMO-1001-MUG", "DEMO-1001", "陶瓷杯", "白色 / 350ml", 2, 3900, True),
            ("DEMO-1001-DIGITAL", "DEMO-1001", "数字下载券", "电子交付", 1, 1900, False),
            ("DEMO-1002-KEYBOARD", "DEMO-1002", "机械键盘", "有线 / 白色", 1, 25900, True),
            ("DEMO-1003-MOUSE", "DEMO-1003", "鼠标", "黑色", 1, 8900, True),
            ("DEMO-2001-MUG", "DEMO-2001", "陶瓷杯", "蓝色", 2, 3900, True),
        ]:
            conn.execute(
                "INSERT INTO order_items(id,order_id,product_name,variant,quantity,unit_price_cents,returnable) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                row,
            )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init", "seed", "setup"])
    command = parser.parse_args().command
    settings = load_settings()
    if command in ["init", "setup"]:
        initialize(settings.database_url)
    if command in ["seed", "setup"]:
        seed(settings.database_url)
    print("Database ready; existing orders, applications and logs were not overwritten.")


if __name__ == "__main__":
    main()
