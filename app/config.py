"""Runtime configuration.

Everything the app needs is read from the environment once, at import time, so
deployment is a matter of setting env vars (see .env.example).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

try:  # optional: load a local .env when present
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:  # pragma: no cover - dotenv is optional
    pass


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # --- LLM (Groq) -------------------------------------------------------
    groq_api_key: str = field(default_factory=lambda: os.getenv("GROQ_API_KEY", "").strip())
    groq_model: str = field(
        default_factory=lambda: os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip()
    )
    groq_temperature: float = field(
        default_factory=lambda: float(os.getenv("GROQ_TEMPERATURE", "0.1"))
    )
    groq_max_tokens: int = field(default_factory=lambda: int(os.getenv("GROQ_MAX_TOKENS", "1600")))
    groq_timeout_s: float = field(default_factory=lambda: float(os.getenv("GROQ_TIMEOUT_S", "60")))

    # --- Agent ------------------------------------------------------------
    max_agent_steps: int = field(default_factory=lambda: int(os.getenv("MAX_AGENT_STEPS", "8")))
    pending_action_ttl_s: int = field(
        default_factory=lambda: int(os.getenv("PENDING_ACTION_TTL_S", "900"))
    )

    # --- Data -------------------------------------------------------------
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", str(BASE_DIR / "data"))))

    # --- Misc -------------------------------------------------------------
    app_name: str = "ParcelPilot Internal Ops Copilot"
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    allow_deprecated_docs: bool = field(
        default_factory=lambda: _env_bool("ALLOW_DEPRECATED_DOCS", False)
    )

    @property
    def documents_dir(self) -> Path:
        return self.data_dir / "documents"

    @property
    def structured_dir(self) -> Path:
        return self.data_dir / "structured"

    @property
    def rules_dir(self) -> Path:
        return self.structured_dir / "rules"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.groq_api_key)


settings = Settings()
