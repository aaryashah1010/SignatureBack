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

    # ── External Integration ────────────────────────────────────────────────
    # SQL Server connection string for reading external users/mappings.
    # Example: mssql+pyodbc://user:pass@server/db?driver=ODBC+Driver+17+for+SQL+Server
    sqlserver_url: str = Field(default="")

    # Base URL of the external software's inbound callback endpoint.
    external_api_base_url: str = Field(default="")

    # Shared secret for authenticating outbound callback POST requests.
    external_api_auth_secret: str = Field(default="")

    # HMAC-HS256 secret used to validate signed launch tokens from the external software.
    integration_shared_secret: str = Field(default="change-integration-secret-in-production")

    # Maximum lifetime of a launch token in minutes (short-lived by design).
    launch_token_expire_minutes: int = Field(default=15)

    # How long used nonces are remembered in Redis to block replay attacks.
    nonce_ttl_seconds: int = Field(default=900)

    # Optional path-prefix allowlist for external document files (empty = disabled).
    allowed_document_path_prefixes: list[str] = Field(default_factory=list)

    # Optional security allowlist for DocumentMaster write-back.
    # DocumentMaster.PhysicalRelativePath contains ABSOLUTE paths on the CpaDesk server
    # (e.g. D:\NewtechGitProjects\CPADesk.API\...\contract.pdf).
    # If set, the write-back destination must start with this prefix (prevents writing
    # outside the intended directory tree). Leave empty to allow any absolute path.
    # Example: Path("D:/NewtechGitProjects/CPADesk.API")
    document_base_path: Path = Field(default=Path(""))

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
