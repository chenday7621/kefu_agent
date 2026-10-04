"""Freeze V0.1 implementation and environment before any formal model run."""

import hashlib
import json
from pathlib import Path
import subprocess


PROJECT = Path(__file__).resolve().parents[2]
REVIEW = PROJECT / "_reviews" / "20261002_0137_V01_RETAIL_REGRESSION"
BENCH = PROJECT / "benchmarks" / "tau2-bench"
FILES = [
    "benchmarks/parlant_retail/bridge.py",
    "benchmarks/parlant_retail/business_config.py",
    "benchmarks/parlant_retail/observe.py",
    "benchmarks/parlant_retail/run.py",
    "benchmarks/parlant_retail/audit.py",
    "benchmarks/parlant_retail/metrics.py",
    "benchmarks/parlant_retail/freeze.py",
    "benchmarks/parlant_retail/test_v01_synthetic.py",
    "benchmarks/parlant_retail/selection.json",
    "benchmarks/parlant_retail/README.md",
]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def package_snapshot(path: Path) -> str:
    return subprocess.check_output(["uv", "pip", "freeze", "--python", str(path)], text=True)


def main() -> None:
    destination = REVIEW / "V01_FROZEN.json"
    if destination.exists():
        raise SystemExit("V0.1 freeze already exists; refusing to replace")
    v0 = json.loads((REVIEW / "V0_SNAPSHOT_MANIFEST.json").read_text())
    benchmark_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=BENCH, text=True).strip()
    if benchmark_head != v0["benchmark_head"]:
        raise SystemExit("Benchmark HEAD differs from V0")
    data_root = BENCH / "data" / "tau2" / "domains" / "retail"
    for name, expected in v0["official_retail_data_sha256"].items():
        if digest((data_root / name).read_bytes()) != expected:
            raise SystemExit(f"Official Retail data differs from V0: {name}")
    packages = {
        "parlant": package_snapshot(PROJECT / ".venv" / "bin" / "python"),
        "tau2": package_snapshot(BENCH / ".venv" / "bin" / "python"),
    }
    for name, content in packages.items():
        if content != (REVIEW / f"V0_{name}_packages.txt").read_text():
            raise SystemExit(f"{name} dependencies differ from V0 snapshot")
    frozen = {
        "parlant_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=PROJECT, text=True).strip(),
        "benchmark_head": benchmark_head,
        "official_retail_data_sha256": v0["official_retail_data_sha256"],
        "code_and_config_sha256": {name: digest((PROJECT / name).read_bytes()) for name in FILES},
        "package_freeze_sha256": {name: digest(content.encode()) for name, content in packages.items()},
        "protocol": json.loads((PROJECT / "benchmarks/parlant_retail/selection.json").read_text()),
        "model": "deepseek/deepseek-chat",
        "local_embedder": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "note": "seed 42 controls benchmark randomness; external model responses are not guaranteed deterministic",
    }
    destination.write_text(json.dumps(frozen, ensure_ascii=False, indent=2) + "\n")
    print(destination.relative_to(PROJECT), digest(destination.read_bytes()))


if __name__ == "__main__":
    main()
