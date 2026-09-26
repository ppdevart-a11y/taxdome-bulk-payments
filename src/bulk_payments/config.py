from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/bulk_payments"
    db_pool_size: int = 10
    db_max_overflow: int = 10
    # How long a request waits for a free connection before answering 503.
    db_pool_timeout_s: float = 5.0

    # Upper bound on how long a request may wait for a firm's row lock. A payee
    # that receives many payments at once is a hot row; failing fast with 503
    # beats piling up requests (and pool connections) behind it.
    lock_timeout_ms: int = 5_000
    statement_timeout_ms: int = 10_000


@lru_cache
def get_settings() -> Settings:
    return Settings()
