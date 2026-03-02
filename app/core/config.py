from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Digital Signature Workflow Platform"
    api_prefix: str = "/api"
    debug: bool = False

    database_url: str = Field(default="postgresql+asyncpg://postgres:postgres@postgres:5432/signature_db")
    redis_url: str = Field(default="redis://redis:6379/0")

    jwt_secret_key: str = Field(default="change-me-in-production")
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24

    storage_root: Path = Path("/app/storage")
    original_dir_name: str = "original"
    signed_dir_name: str = "signed"
    signatures_dir_name: str = "signature_images"

    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    @property
    def original_storage_dir(self) -> Path:
        return self.storage_root / self.original_dir_name

    @property
    def signed_storage_dir(self) -> Path:
        return self.storage_root / self.signed_dir_name

    @property
    def signature_images_dir(self) -> Path:
        return self.storage_root / self.signatures_dir_name


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.original_storage_dir.mkdir(parents=True, exist_ok=True)
    settings.signed_storage_dir.mkdir(parents=True, exist_ok=True)
    settings.signature_images_dir.mkdir(parents=True, exist_ok=True)
    return settings
