"""Whitelist-only completion environment; reuse the frozen log/HTTP helpers."""
from psycopg import sql
from ..eval_s1.common import config, environment, activate, save, digest, service, phase, TABLES
from ..settings import load_settings
from ..db import connect
from ..business import plain


def assert_isolated():
    c, s = config(), load_settings()
    assert c["authorized_units"] == ["r2_R1", "r2_R6"]
    assert c["database"] in ("app_s1", "app_s1_completion_r6")
    assert c["container"] == "app-s1-20261006-165858" and c["db_port"] == 55434
    assert s.database_url == environment()["DEMO_DATABASE_URL"]
    assert (s.parlant_port, s.mcp_port, s.tool_port) == (8920, 8921, 8922)
    assert "app_s1_completion_20261007_084043" in str(s.parlant_home)
    with connect(s.database_url) as conn:
        assert conn.execute("SELECT current_database() AS d").fetchone()["d"] == c["database"]


def database_evidence():
    assert_isolated()
    with connect(load_settings().database_url) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        return plain({t: conn.execute(sql.SQL("SELECT * FROM {} ORDER BY 1").format(sql.Identifier(t))).fetchall()
                      for t in TABLES})
