"""Runtime settings — secrets from env / .env on server (scp), never from git."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py → repo root (sanskrit_srv/)
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_CANDIDATES = (
    Path.cwd() / ".env",
    _REPO_ROOT / ".env",
    Path("/opt/sanskrit_srv/.env"),
)


def _find_env_file() -> str | None:
    for path in _ENV_CANDIDATES:
        if path.is_file():
            return str(path)
    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_find_env_file(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = f"sqlite:///{_REPO_ROOT / 'data' / 'sanskrit_srv.db'}"
    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7
    storage_root: Path = _REPO_ROOT / "storage"
    cors_origins: str = "*"

    openai_api_key: str = ""
    openai_base_url: str = "https://api.proxyapi.ru/openai/v1"
    openai_model: str = "gpt-4o-mini"
    # Google AI Studio key (preferred for Gemini). Empty → ProxyAPI Google gateway + OPENAI_API_KEY.
    gemini_api_key: str = ""
    # Extra Studio keys, comma/newline: "sanskrit_srv_1:AQ.xxx,sanskrit_srv_2:AQ.yyy".
    # Daily 20-request quota is per key; exhausted keys fall back to the next.
    gemini_api_keys: str = ""
    # Optional labeled key file (same format as ~/keys/sanskrit_srv_gemini.txt). Not in git.
    gemini_keys_file: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com"
    gemini_model: str = "gemini-3.1-pro-preview"
    # Claude via ProxyAPI Anthropic gateway (same OPENAI_API_KEY).
    anthropic_base_url: str = "https://api.proxyapi.ru/anthropic"
    anthropic_model: str = ""  # e.g. claude-opus-5 — empty = skip Claude

    # OpenRouter (optional). No default model — ox-alpha is gone; experts type an id.
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = ""
    # Haimaker GLM-5.3 promo (same as LSE chat). Free through 2026-09-11 UTC.
    # Separate from OpenRouter so Gemini/OpenRouter keep their own keys and URLs.
    haimaker_api_key: str = ""
    haimaker_base_url: str = "https://api.haimaker.ai/v1"
    haimaker_model: str = "z-ai/glm-5v-turbo"
    openrouter_http_referer: str = "https://sanskrit-srv.local"
    openrouter_app_title: str = "sanskrit_srv"
    # Ceiling only; ox-alpha uses smaller per-task caps (see openrouter_ox.py).
    # A 32k budget lets default effort=max dump chain-of-thought for 10+ minutes.
    openrouter_max_tokens: int = 16384

    # Legacy: unused for upload (whole book is default). Kept for manual extract helpers.
    default_extract_max_pages: int = 0
    # Auto-run whole-book pipeline only if PDF has this many pages or fewer.
    # Above this, upload extracts stubs; author digitizes page-by-page (avoids breaking large scans).
    large_book_pages: int = 10
    # Consecutive unagreed pages per vision call. Ceiling only — Flash uses 3, Pro 6, Opus 4, GLM 1.
    # Capped at 8 — 1M input is fine, ~32k output is not (dense page HTML is 3–8k tokens).
    digitize_batch_pages: int = 6
    # Consecutive translation pages per text call. Ceiling only — Flash 3, Pro 6, Opus 4, GLM/ox 2.
    translate_batch_pages: int = 6
    # USD per 1M tokens: {"gemini:gemini-2.5-flash":{"in":0.1,"out":0.4}, ...}
    # JSON string in env LLM_PRICE_PER_1M; empty = no cost estimate.
    llm_price_per_1m: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
