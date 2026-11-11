import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def load_config() -> dict:
    return yaml.safe_load((ROOT / "config" / "study.yaml").read_text())


def get_secret(name: str) -> str:
    load_dotenv(ROOT / ".env")  # does not override vars already set
    val = os.environ.get(name, "")
    if not val:
        raise RuntimeError(f"{name} is not set; add it to backend/.env")
    return val


def require_main_pass_ready(cfg: dict) -> None:
    missing = [k for k in ("ceiling", "end_page") if cfg.get(k) is None]
    if missing:
        raise RuntimeError(f"Set {', '.join(missing)} in config/study.yaml before the main pass")
