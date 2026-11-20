from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str = (
        "postgresql+psycopg://signalforge:signalforge_dev@localhost:5432/signalforge"
    )
    TEST_DATABASE_URL: str = (
        "postgresql+psycopg://signalforge:signalforge_dev@localhost:5432/signalforge_test"
    )
    APP_ENV: str = "development"
    LOG_LEVEL: str = "INFO"
    API_HOST: str = "127.0.0.1"
    API_PORT: int = 8000


@lru_cache
def get_settings() -> Settings:
    return Settings()