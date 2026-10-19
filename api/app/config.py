from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://patentcompass:patentcompass_dev@127.0.0.1:15432/patentcompass"
    redis_url: str = "redis://127.0.0.1:16379/0"
    storage_root: Path = Path(__file__).resolve().parents[2] / "storage"
    dev_owner_id: str = "local-dev"
    dispatch_interval: float = 2
    lease_seconds: int = 90
    heartbeat_seconds: int = 15
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    model_timeout: float = 60

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    @property
    def embedding_enabled(self) -> bool:
        return bool(self.embedding_base_url and self.embedding_api_key and self.embedding_model)


@lru_cache
def get_settings() -> Settings:
    return Settings()
