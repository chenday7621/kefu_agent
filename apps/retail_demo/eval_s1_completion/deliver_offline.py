"""Read-only isolation verification and redacted offline-stage archive.

No server starts/stops, migration, mutation, conversation or model clients.
"""
import argparse
import difflib
import hashlib
import json
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values
from psycopg import sql
from ..business import plain
from ..db import connect
from ..settings import load_settings, ROOT
from .offline import read, save, sha


def run(audit, results):
    audit, results = Path(audit), Path(results)
    protected = read(audit / "protected_before.json")
    changed = [p for p, h in protected.items() if not Path(p).is_file() or sha(p) != h]
    assert not changed, "Protected historical/source/config file changed"
    before = read(audit / "demo_readonly_before.json")
    after = {}
    with connect(load_settings().database_url) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        for table in before["table_summaries"]:
            rows = plain(conn.execute(sql.SQL("SELECT * FROM {} ORDER BY 1").format(sql.Identifier(table))).fetchall())
            after[table] = {"count": len(rows), "sha256": hashlib.sha256(
                json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}
            if table in ("outbox_control", "schema_migrations"):
                after[table]["rows"] = rows
    demo_changes = [t for t, d in before["table_summaries"].items() if after[t] != d]
    # Report concurrent outside writes without overwriting any user data.
    runtime_before = read(audit / "runtime_before.json")
    processes = subprocess.check_output(["pgrep", "-af", "^.*python.*apps.retail_demo.(parlant_app|mcp_server)"], text=True)
    container = subprocess.check_output(["docker", "inspect", "--format", "{{.State.Status}}", "app-s1-20261006-165858"], text=True).strip()
    state = {"utc": datetime.now(timezone.utc).isoformat(), "protected_files_checked": len(protected),
             "protected_changes": changed, "demo_readonly_table_changes": demo_changes,
             "demo_after_summaries": after, "original_processes": processes,
             "original_processes_same": processes == runtime_before["application_processes"],
             "worker_enabled": after["outbox_control"]["rows"][0]["enabled"],
             "demo_migrations": after["schema_migrations"]["rows"],
             "test_container_state": container, "new_test_processes_started": 0,
             "new_fault_hooks_installed": 0, "new_model_dispatches": 0,
             "test_database_live_hook_check": "not rerun; stopped database unchanged",
             "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
             "git_status": subprocess.check_output(["git", "status", "--short"], text=True)}
    assert state["original_processes_same"] and container == "exited"
    save(audit / "FINAL_ISOLATION.json", state)
    added = [p for p in Path(__file__).parent.iterdir() if p.suffix in (".py", ".md")]
    added.append(Path("apps/retail_demo/APP_S1_COMPLETION.md"))
    appended = {"apps/retail_demo/README.md": "\n## APP-S1 评测收尾（2026-10-07）",
                "apps/retail_demo/VERIFICATION.md": "\n## APP-S1 收尾离线补充（2026-10-07，独立评分版本）",
                "REPRODUCE.md": "\n## APP-S1 completion：独立离线退款语境复核（2026-10-07）"}
    diff = ""
    for p in sorted(added):
        diff += "".join(difflib.unified_diff([], p.read_text().splitlines(True), fromfile="/dev/null", tofile=str(p)))
    for name, marker in appended.items():
        p = Path(name)
        new = p.read_text()
        old = new[:new.rindex(marker)]
        diff += "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), fromfile=name + " (before append)", tofile=name))
    (audit / "COMPLETION_ADDITIONS.diff").write_text(diff)
    save(audit / "completion_file_hashes.json", {str(p): sha(p) for p in sorted(added + [Path(p) for p in appended])})
    frozen = read("results/app_s1_confirmed_snapshot_20261006_165858/protocol_source_hashes.json")
    save(audit / "current_frozen_behavior_hashes.json", {p: sha(p) for p in frozen})
    # Explicitly label this interim package; it contains no new paid attempts.
    stage = audit / "upload_stage"
    stage.mkdir(exist_ok=True)
    secrets = []
    for p in (ROOT / ".env", ROOT / "apps/retail_demo/.env"):
        secrets.extend(str(v) for k, v in dotenv_values(p).items() if v and len(v) > 8
                       and any(n in k.upper() for n in ("KEY", "TOKEN", "PASSWORD", "DATABASE_URL")))
    private = Path("runtime-data/retail-demo/app_s1/20261006_165858/private.json")
    if private.exists():
        secrets.append(read(private)["password"])
    capabilities = set()
    for p in results.rglob("*.json"):
        data = read(p)
        if isinstance(data, dict) and isinstance(data.get("tool_session_contexts"), list):
            capabilities.update(str(x["id"]) for x in data["tool_session_contexts"])
    replacements = {v: "<REDACTED_SECRET>" for v in secrets}
    replacements.update({v: "redacted-capability-" + hashlib.sha256(v.encode()).hexdigest()[:16] for v in capabilities})
    replacements[str(ROOT)] = "<PROJECT_ROOT>"
    files = [(p, p) for p in results.rglob("*") if p.is_file()]
    files += [(p, p) for p in audit.iterdir() if p.is_file() and p.suffix in (".md", ".json", ".diff", ".txt")
              and p.name not in ("PACKAGE_CHECK.json", "UPLOAD_SHA256.txt")]
    files += [(p, p) for p in added + [Path(n) for n in appended]]
    for p, relative in files:
        assert p.name != ".env" and p.name != "private.json" and p.suffix not in (".key", ".pem", ".bin", ".safetensors")
        text = p.read_text()
        for secret, replacement in replacements.items():
            text = text.replace(secret, replacement)
        assert not any(v in text for v in secrets)
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    manifest = {str(p.relative_to(stage)): sha(p) for p in stage.rglob("*")
                if p.is_file() and p.name != "HASH_MANIFEST.json"}
    save(stage / "HASH_MANIFEST.json", manifest)
    archive = audit / "APP_S1_COMPLETION_OFFLINE_UPLOAD.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(stage, arcname="APP_S1_COMPLETION_OFFLINE")
    with tarfile.open(archive, "r:gz") as tar:
        for name, expected in manifest.items():
            assert hashlib.sha256(tar.extractfile("APP_S1_COMPLETION_OFFLINE/" + name).read()).hexdigest() == expected
    digest = sha(archive)
    (audit / "UPLOAD_SHA256.txt").write_text(digest + "  " + archive.name + "\n")
    save(audit / "PACKAGE_CHECK.json", {"sha256": digest, "bytes": archive.stat().st_size,
         "files_verified_inside_archive": len(manifest), "secret_values_excluded": True,
         "capabilities_redacted": len(capabilities), "stage": "WAITING_BUDGET",
         "private_config_model_weights_demo_database_excluded": True})
    print(json.dumps({"archive": str(archive), "sha256": digest, "protected_checked": len(protected),
                      "demo_changes": demo_changes, "original_processes_same": state["original_processes_same"],
                      "worker_enabled": state["worker_enabled"]}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", required=True)
    parser.add_argument("--results", required=True)
    args = parser.parse_args()
    run(args.audit, args.results)
