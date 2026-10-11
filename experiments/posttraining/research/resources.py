"""Own and clean up only this run's isolated PostgreSQL and policy processes."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time

import requests
from ..dev_controller import free_port, save

PG_ROOT = Path("/nas/chenyi/posttraining-qwen/runtime/phase1d-tools/postgresql")


class ResearchResources:
    def __init__(self, runtime, config):
        self.runtime, self.config = Path(runtime), config
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.children, self.streams, self.pg_running = [], [], False
        self.pg_environment = dict(os.environ)
        self.pg_environment["LD_LIBRARY_PATH"] = (
            str(PG_ROOT / "usr/lib/x86_64-linux-gnu") + ":" + os.environ.get("LD_LIBRARY_PATH", "")
        )
        self.pg_bin = PG_ROOT / "usr/lib/postgresql/16/bin"
        self.pg_data = self.runtime / "pg/cluster"
        self.marker = self.runtime / "pg/OWNED_CLUSTER.json"

    def pg_command(self, binary, *args):
        result = subprocess.run(
            [str(self.pg_bin / binary), *map(str, args)],
            env=self.pg_environment,
            capture_output=True,
            text=True,
        )
        with (self.runtime / "logs/PG_CONTROL.log").open("a") as out:
            out.write(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError(f"Owned PostgreSQL {binary} failed; see PG_CONTROL.log")

    def start_pg(self):
        if self.marker.exists():
            marker = json.loads(self.marker.read_text())
            if Path(marker["data_directory"]).resolve() != self.pg_data.resolve():
                raise ValueError("Owned cluster marker mismatch")
            self.admin_dsn = marker["admin_dsn"]
            port = marker["port"]
        else:
            if self.pg_data.exists():
                raise ValueError("Unmarked PostgreSQL directory; refuse adoption")
            self.pg_command(
                "initdb",
                "-D",
                self.pg_data,
                "-L",
                PG_ROOT / "usr/share/postgresql/16",
                "--auth-local=trust",
                "--auth-host=trust",
                "--username=chenyi",
                "--no-locale",
                "--encoding=UTF8",
            )
            port = free_port()
            self.admin_dsn = (
                f"host=127.0.0.1 port={port} dbname=postgres user=chenyi connect_timeout=10"
            )
            save(
                self.marker,
                {
                    "data_directory": str(self.pg_data.resolve()),
                    "port": port,
                    "admin_dsn": self.admin_dsn,
                    "owner_run_id": self.config["run_id"],
                },
            )
        socket = self.runtime / "pg/socket"
        socket.mkdir(exist_ok=True)
        self.pg_command(
            "pg_ctl",
            "-D",
            self.pg_data,
            "-l",
            self.runtime / "logs/PG_SERVER.log",
            "-o",
            f"-h 127.0.0.1 -p {port} -k {socket}",
            "-w",
            "start",
        )
        self.pg_running = True
        import psycopg

        with psycopg.connect(self.admin_dsn) as conn:
            actual = conn.execute("SHOW data_directory").fetchone()[0]
            assert Path(actual).resolve() == self.pg_data.resolve()
            assert not conn.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE 'ptdev_%'"
            ).fetchall()
        return self.admin_dsn

    @contextmanager
    def backend(self, phase, adapter=None):
        # Refuse an occupied selected card, without signalling its processes.
        gpu = str(self.config["physical_gpu"])
        status = subprocess.check_output(
            [
                "nvidia-smi",
                "-i",
                gpu,
                "--query-gpu=memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            text=True,
        )
        used, utilization = [int(x.strip()) for x in status.strip().split(",")]
        if used > 100 or utilization > 0:
            raise RuntimeError("Selected GPU became occupied; no preemption")
        directory = self.runtime / "logs" / phase / ("attempt-" + secrets.token_hex(4))
        directory.mkdir(parents=True, exist_ok=False)
        config = dict(
            self.config["generation"],
            model_path=self.config["model_path"],
            directory=str(directory),
            port=free_port(),
            capability=secrets.token_hex(32),
            adapter=adapter,
            gpu_active_seconds=self.config.get("remaining_gpu_seconds", 8 * 3600),
        )
        path = directory / "BACKEND_CONFIG.json"
        save(path, config)
        path.chmod(0o600)
        stream = (directory / "BACKEND.log").open("a")
        child = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "experiments.posttraining.research.backend",
                "--config",
                str(path),
            ],
            env=dict(os.environ, CUDA_VISIBLE_DEVICES=gpu),
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.children.append(child)
        self.streams.append(stream)
        url = f"http://127.0.0.1:{config['port']}"
        client = requests.Session()
        client.trust_env = False
        try:
            deadline = time.monotonic() + 180
            while True:
                if child.poll() is not None:
                    raise RuntimeError("Policy backend exited; inspect owned BACKEND.log")
                try:
                    response = client.get(url + "/ready", timeout=2)
                    if response.status_code == 200:
                        break
                except requests.RequestException:
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError("Policy backend load timeout")
                time.sleep(0.5)
            yield {
                "policy_url": url,
                "policy_capability": config["capability"],
                "generation_directory": str(directory),
            }
        finally:
            client.close()
            self.stop_child(child)
            stream.close()

    @staticmethod
    def stop_child(child):
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGINT)
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    # Only a directly created owned process group is eligible.
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=10)

    def close(self):
        for child in reversed(self.children):
            self.stop_child(child)
        for stream in self.streams:
            if not stream.closed:
                stream.close()
        if self.pg_running:
            import psycopg

            with psycopg.connect(self.admin_dsn) as conn:
                remaining = conn.execute(
                    "SELECT datname FROM pg_database WHERE datname LIKE 'ptdev_%'"
                ).fetchall()
            save(
                self.runtime / "logs/ISOLATION_FINAL.json",
                {
                    "episode_databases_remaining": remaining,
                    "owned_cluster": str(self.pg_data),
                    "passed": not remaining,
                },
            )
            self.pg_command("pg_ctl", "-D", self.pg_data, "-m", "fast", "-w", "stop")
            self.pg_running = False
