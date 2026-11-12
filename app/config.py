from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    api_key: str = "change-me"
    database_url: str = "postgresql+asyncpg://payments:payments@localhost:5432/payments"
    rabbit_url: str = "amqp://payments:payments@localhost:5672/"
    outbox_poll_seconds: float = 1.0
    webhook_timeout_seconds: float = 10.0
    retry_base_seconds: float = 1.0


settings = Settings()
