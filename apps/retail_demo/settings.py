from dataclasses import dataclass, field
from pathlib import Path
import os
import json
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
APP = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    customer_id: str = "demo-alice"
    mcp_port: int = 8811
    parlant_port: int = 8810
    tool_port: int = 8812
    parlant_home: Path = ROOT / "runtime-data/retail-demo/parlant"
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    session_storage: str = "postgres"
    rollback_home: Path | None = None

    @property
    def mcp_url(self) -> str:
        # This Parlant version appends /mcp itself. Do not include a path.
        return f"http://127.0.0.1:{self.mcp_port}"


def load_settings() -> Settings:
    config = {**dotenv_values(APP / ".env"), **os.environ}
    url = config.get("DEMO_DATABASE_URL")
    if not url:
        raise RuntimeError("Copy apps/retail_demo/.env.example to .env; set DEMO_DATABASE_URL.")
    customer = config.get("DEMO_CUSTOMER_ID", "demo-alice")
    if customer not in ("demo-alice", "demo-bob"):
        raise RuntimeError("This local demo supports only the two seeded identities.")
    home = Path(config.get("DEMO_PARLANT_HOME", "runtime-data/retail-demo/parlant"))
    home = home if home.is_absolute() else ROOT / home
    state = home / "storage_mode.json"
    saved = json.loads(state.read_text()) if state.exists() else {}
    mode = config.get("DEMO_SESSION_STORAGE") or saved.get("mode", "postgres")
    if mode not in ("postgres", "local_rollback"):
        raise RuntimeError("DEMO_SESSION_STORAGE must be postgres or explicit local_rollback")
    rollback_home = Path(saved["rollback_home"]) if saved.get("rollback_home") else None
    if mode == "local_rollback" and not rollback_home:
        raise RuntimeError(
            "Run storage_migration rollback first; original local files are backups only"
        )
    return Settings(
        url,
        customer,
        int(config.get("DEMO_MCP_PORT", 8811)),
        int(config.get("DEMO_PARLANT_PORT", 8810)),
        int(config.get("DEMO_TOOL_PORT", 8812)),
        home,
        config.get("DEMO_EMBEDDING_MODEL", Settings.embedding_model),
        mode,
        rollback_home,
    )


def prepare_model_environment(settings: Settings) -> None:
    # Only copy the model credential, not GIT_TOKEN or unrelated root secrets.
    value = os.environ.get("DEEPSEEK_API_KEY") or dotenv_values(ROOT / ".env").get(
        "DEEPSEEK_API_KEY"
    )
    if not value:
        raise RuntimeError("DEEPSEEK_API_KEY unavailable; business/MCP can still run without it.")
    os.environ["DEEPSEEK_API_KEY"] = value
    home = (
        settings.rollback_home
        if settings.session_storage == "local_rollback"
        else settings.parlant_home
    )
    os.environ["PARLANT_HOME"] = str(home)
    home.mkdir(parents=True, exist_ok=True)
    config = {**dotenv_values(APP / ".env"), **os.environ}
    # Shared model cache only, never a benchmark's orders, snapshots or results.
    cache = Path(config.get("DEMO_HF_HOME", str(ROOT / "runtime-data/huggingface")))
    os.environ.setdefault("HF_HOME", str(cache))
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    os.environ.setdefault("MKL_NUM_THREADS", "2")
