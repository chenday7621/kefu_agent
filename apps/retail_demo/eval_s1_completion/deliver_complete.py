"""Verify protected assets/demo and archive completed evaluation; no model access."""
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
from .offline import read, save, sha
from ..settings import load_settings, ROOT
from ..db import connect
from ..business import plain


def run(audit, results):
    audit, results = Path(audit), Path(results)
    protected = read(audit / "protected_before.json")
    changes = [p for p, h in protected.items() if not Path(p).is_file() or sha(p) != h]
    assert not changes, "Protected old result/source/config changed"
    freeze = read(audit / "COMPLETION_EXECUTION_FREEZE.json")
    assert all(sha(p) == h for p, h in freeze["completion_source_hashes"].items())
    assert sha(audit / "BUDGET_AUTHORIZATION.json") == freeze["authorization_sha256"]
    baseline = read(audit / "demo_readonly_before.json")
    after = {}
    with connect(load_settings().database_url) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        for table in baseline["table_summaries"]:
            rows = plain(conn.execute(sql.SQL("SELECT * FROM {} ORDER BY 1").format(sql.Identifier(table))).fetchall())
            after[table] = {"count": len(rows), "sha256": hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}
            if table in ("outbox_control", "schema_migrations"):
                after[table]["rows"] = rows
    demo_changes = [t for t in after if after[t] != baseline["table_summaries"][t]]
    # Never overwrite external demo writes; make any concurrent changes explicit.
    processes = subprocess.check_output(["pgrep", "-af", "^.*python.*apps.retail_demo.(parlant_app|mcp_server)"], text=True)
    original_same = processes == read(audit / "runtime_before.json")["application_processes"]
    assert original_same
    stop = read(audit / "TEST_STOP_STATE.json")
    assert stop["container_state"] == "exited" and all(not p["running"] for p in stop["test_processes"])
    final = {"utc": datetime.now(timezone.utc).isoformat(), "protected_files_checked": len(protected), "protected_changes": changes,
             "frozen_recovery_and_scorer_sources_unchanged": True, "demo_readonly_table_changes": demo_changes,
             "demo_after_summaries": after, "demo_processes": processes, "demo_processes_same": original_same,
             "worker_enabled": after["outbox_control"]["rows"][0]["enabled"], "demo_005_not_applied": not any(x["version"].startswith("005") for x in after["schema_migrations"]["rows"]),
             "test_stop": stop, "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
             "git_status": subprocess.check_output(["git", "status", "--short"], text=True), "deployment_executed": False}
    save(audit / "FINAL_ISOLATION_OFFLINE_BEFORE_AUTH.json", read(audit / "FINAL_ISOLATION.json"))
    save(audit / "FINAL_ISOLATION.json", final)
    added = sorted(p for p in Path(__file__).parent.iterdir() if p.suffix in (".py", ".md"))
    added.append(Path("apps/retail_demo/APP_S1_COMPLETION.md"))
    appended = {"apps/retail_demo/README.md": "\n## APP-S1 评测收尾（2026-10-07）",
                "apps/retail_demo/VERIFICATION.md": "\n## APP-S1 收尾离线补充（2026-10-07，独立评分版本）",
                "REPRODUCE.md": "\n## APP-S1 completion：独立离线退款语境复核（2026-10-07）"}
    diff = ""
    for p in added:
        diff += "".join(difflib.unified_diff([], p.read_text().splitlines(True), fromfile="/dev/null", tofile=str(p)))
    for name, marker in appended.items():
        new = Path(name).read_text()
        old = new[:new.rindex(marker)]
        diff += "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), fromfile=name + " (before completion append)", tofile=name))
    (audit / "COMPLETION_FINAL_ADDITIONS.diff").write_text(diff)
    save(audit / "completion_final_file_hashes.json", {str(p): sha(p) for p in added + [Path(p) for p in appended]})
    current = [p for p in Path("apps/retail_demo").rglob("*") if p.is_file()
               and ".venv" not in p.parts and "__pycache__" not in p.parts and p.suffix in (".py", ".sql")]
    save(audit / "CURRENT_APP_SOURCE_SHA256.json", {str(p): sha(p) for p in sorted(current)})
    save(audit / "COMPLETION_STATUS.json", {"status": "COMPLETED", "plan_units": 16, "completion_units": ["r2_R1", "r2_R6"],
         "deployment_recommendation": "suggest controlled local demo deployment after separate approval", "deployment_executed": False})
    # A new stage/archive leaves the authorization-before offline package intact.
    stage = audit / "complete_upload_stage"
    assert not stage.exists(), "Completed archive stage exists; preserve it"
    stage.mkdir()
    secrets = []
    for p in (ROOT / ".env", ROOT / "apps/retail_demo/.env"):
        secrets.extend(str(v) for k, v in dotenv_values(p).items() if v and len(v) > 8
                       and any(n in k.upper() for n in ("KEY", "TOKEN", "PASSWORD", "DATABASE_URL")))
    private = Path("runtime-data/retail-demo/app_s1_completion/20261007_084043/private.json")
    secrets.append(read(private)["password"])
    capabilities = set()
    for p in results.rglob("*.json"):
        data = read(p)
        if isinstance(data, dict) and isinstance(data.get("tool_session_contexts"), list):
            capabilities.update(str(x["id"]) for x in data["tool_session_contexts"])
    replacements = {v: "<REDACTED_SECRET>" for v in secrets}
    replacements.update({v: "redacted-capability-" + hashlib.sha256(v.encode()).hexdigest()[:16] for v in capabilities})
    replacements[str(ROOT)] = "<PROJECT_ROOT>"
    files = []
    for p in results.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(results)
        if p.suffix in (".safetensors", ".bin", ".pyc", ".key", ".pem") or "__pycache__" in rel.parts:
            continue
        if rel.parts[0] == "parlant" and p.name not in ("api_calls.jsonl", "model_calls.jsonl", "parlant.log"):
            continue
        files.append((p, p))
    files += [(p, p) for p in audit.iterdir() if p.is_file() and p.suffix in (".json", ".md", ".diff", ".txt", ".html")
              and p.name not in ("PACKAGE_CHECK.json", "UPLOAD_SHA256.txt")]
    files += [(p, p) for p in added + [Path(p) for p in appended] + [Path("apps/retail_demo/MIGRATION.md")]]
    for p, relative in files:
        assert p.name not in (".env", "private.json")
        text = p.read_text()
        for original, replacement in replacements.items():
            text = text.replace(original, replacement)
        assert not any(value in text for value in secrets)
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    manifest = {str(p.relative_to(stage)): sha(p) for p in stage.rglob("*") if p.is_file() and p.name != "HASH_MANIFEST.json"}
    save(stage / "HASH_MANIFEST.json", manifest)
    archive = audit / "APP_S1_COMPLETION_UPLOAD.tar.gz"
    assert not archive.exists(), "Completed upload archive already exists"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(stage, arcname="APP_S1_COMPLETION")
    with tarfile.open(archive, "r:gz") as tar:
        for name, h in manifest.items():
            assert hashlib.sha256(tar.extractfile("APP_S1_COMPLETION/" + name).read()).hexdigest() == h
    digest = sha(archive)
    (audit / "UPLOAD_SHA256.txt").write_text(digest + "  " + archive.name + "\n")
    save(audit / "PACKAGE_CHECK_OFFLINE_BEFORE_AUTH.json", read(audit / "PACKAGE_CHECK.json"))
    save(audit / "PACKAGE_CHECK.json", {"sha256": digest, "bytes": archive.stat().st_size,
         "actual_tar_files_hash_verified": len(manifest), "secret_values_excluded": True,
         "capabilities_redacted": len(capabilities), "private_config_backup_and_weights_excluded": True,
         "old_offline_archive_preserved": True})
    print(json.dumps({"archive": str(archive), "sha256": digest, "bytes": archive.stat().st_size,
         "protected_files_checked": len(protected), "demo_changes": demo_changes, "demo_processes_same": original_same,
         "worker_enabled": final["worker_enabled"], "demo_005_not_applied": final["demo_005_not_applied"]}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", required=True)
    parser.add_argument("--results", required=True)
    args = parser.parse_args()
    run(args.audit, args.results)
