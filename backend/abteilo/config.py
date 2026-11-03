"""Runtime configuration. Everything arrives through ABTEILO_* environment variables."""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ABTEILO_", env_file=_ENV_FILE, extra="ignore"
    )

    environment: str = "production"
    database_url: SecretStr
    migration_database_url: SecretStr | None = None

    session_ttl_hours: int = 12
    cookie_secure: bool = True
    login_max_failures: int = 5
    login_window_minutes: int = 15

    # Model providers. URLs and keys are server configuration only; model
    # profiles in the database can pick a provider but never a URL or key.
    ollama_url: str = "http://127.0.0.1:11434"
    openai_compatible_url: str | None = None
    openai_compatible_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    model_timeout_seconds: float = 120.0
    # Bounded inference: at most this many model generations run at once (one local GPU); further requests
    # wait up to the queue time, then get MODEL_BUSY instead of piling up.
    max_concurrent_generations: int = 2
    generation_queue_seconds: float = 30.0

    # Embeddings for hybrid document search (local Ollama model). Empty = keyword search only.
    embedding_model: str = "bge-m3"
    embedding_query_timeout_seconds: float = 8.0
    embedding_index_timeout_seconds: float = 120.0
    # Vector candidates farther than this cosine distance are dropped before rank fusion.
    vector_max_distance: float = 0.55

    # Suggest document-stated relationships for review (uses the department's general model).
    graph_extraction_enabled: bool = True

    # Demo seeding only (python -m abteilo.seed).
    demo_password: SecretStr | None = None

    # Absolute frontend build directory served at "/" when present.
    frontend_dist: str | None = None

    @property
    def is_development(self) -> bool:
        return self.environment == "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
