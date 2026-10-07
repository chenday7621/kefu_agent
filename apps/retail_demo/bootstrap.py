"""Create an isolated additive venv without upgrading the project environment."""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
APP = Path(__file__).resolve().parent
source = ROOT / ".venv/bin/python"
if not source.exists():
    raise SystemExit("Install the project .venv first (see root README).")
subprocess.run([str(source), "-m", "venv", str(APP / ".venv")], check=True)
python = APP / ".venv/bin/python"
site = subprocess.check_output(
    [str(python), "-c", 'import sysconfig; print(sysconfig.get_path("purelib"))'], text=True
).strip()
parent_site = subprocess.check_output(
    [str(source), "-c", 'import sysconfig; print(sysconfig.get_path("purelib"))'], text=True
).strip()
(Path(site) / "project-runtime.pth").write_text(str(ROOT / "src") + "\n" + parent_site + "\n")
subprocess.run(
    [str(python), "-m", "pip", "install", "-r", str(APP / "requirements.txt")], check=True
)
print("Demo venv ready; project dependencies unchanged.")
