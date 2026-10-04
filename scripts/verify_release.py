"""Check the showcase publication using only Python stdlib and tracked Git files.

No project modules are imported, no benchmark is executed, and no API is called.
This is release validation, not the full upstream Parlant test suite.
"""

from pathlib import Path
import json
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def git(*args: str, **kwargs: object) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT, **kwargs)


def main() -> None:
    files = {p.decode() for p in git("ls-files", "-z").split(b"\0") if p}
    errors: list[str] = []
    forbidden_dirs = {
        "results", "_reviews", "runtime-data", "__pycache__", ".venv",
        "hf_download_cache", "huggingface_cache", ".cache",
    }
    forbidden_suffixes = (
        ".key", ".pem", ".pyc", ".bin", ".safetensors", ".npy", ".npz",
        ".pkl", ".tar.gz", ".pt", ".pth", ".gguf", ".log",
    )
    credentials = re.compile(
        rb"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}"
        rb"|github_pat_[A-Za-z0-9_]{30,}|(?:AKIA|ASIA)[A-Z0-9]{16})"
        rb"|-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
    )
    python_files = 0
    for name in sorted(files):
        path = Path(name)
        if (
            forbidden_dirs.intersection(path.parts)
            or name.endswith(forbidden_suffixes)
            or (path.name.startswith(".env") and path.name != ".env.example")
            or (name.startswith("benchmarks/") and {"data", "model", "index", "tau2-bench", "upstream_metadata"}.intersection(path.parts))
        ):
            errors.append(f"Forbidden publication asset: {name}")
        content = (ROOT / name).read_bytes()
        if credentials.search(content):
            errors.append(f"Possible credential in {name} (value redacted)")
        if name.endswith(".py"):
            try:
                compile(content, name, "exec")
                python_files += 1
            except SyntaxError:
                errors.append(f"Python syntax error: {name}")

    # Check exclusion rules even when private assets are absent on a fresh clone.
    probes = [
        ".env", "private.key", "private.pem", "runtime-data/probe.json",
        "results/probe.json", "_reviews/probe.md", "probe.tar.gz",
        "probe.bin", "probe.safetensors", "__pycache__/probe.pyc",
        "benchmarks/parlant_rag/data/probe.jsonl",
        "benchmarks/parlant_rag_r1/index/probe.npy",
        "benchmarks/parlant_rag_r1/model/config.json", "hf_download_cache/probe",
    ]
    ignored = git("check-ignore", "--no-index", "--stdin", input=("\n".join(probes) + "\n").encode()).decode().splitlines()
    errors.extend(f"Missing ignore rule: {name}" for name in set(probes) - set(ignored))

    defaults = json.loads((ROOT / "configs/release_defaults.json").read_text())
    summary = json.loads((ROOT / "configs/results_summary.json").read_text())
    if defaults["transaction"]["default"] != "O1-B" or defaults["rag"]["default"] != "R1-B":
        errors.append("Showcase defaults must remain O1-B / R1-B")
    if summary["retail"]["final"] != "O1-B" or summary["r2_ablation"]["default"]:
        errors.append("Results metadata disagrees with the frozen showcase selection")

    for name in ["README.md", "docs/transaction_agent.md", "docs/rag_agent.md", "docs/reproduce.md"]:
        for link in re.findall(r"\]\(([^)]+)\)", (ROOT / name).read_text()):
            if "://" in link or link.startswith("#"):
                continue
            target = ((ROOT / name).parent / link.split("#")[0]).resolve()
            if not target.is_relative_to(ROOT) or target.relative_to(ROOT).as_posix() not in files:
                errors.append(f"Broken public link in {name}: {link}")

    if errors:
        raise SystemExit("\n".join(errors))
    print(f"Release verification passed: {len(files)} tracked files, {python_files} Python syntax checks")
    print("No benchmark, model execution, or API request; upstream integration tests are not included.")


if __name__ == "__main__":
    main()
