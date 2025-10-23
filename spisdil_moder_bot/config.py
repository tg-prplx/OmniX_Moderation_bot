from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BatchSettings(BaseModel):
    max_batch_size: int = Field(default=50, ge=1)
    max_delay_seconds: float = Field(default=0.5, gt=0)


class LayerSettings(BaseModel):
    regex_workers: int = Field(default=6, ge=1)
    omni_concurrency: int = Field(default=8, ge=1)
    chatgpt_concurrency: int = Field(default=2, ge=1)


class SchedulerSettings(BaseModel):
    concurrent_batches: int = Field(default=4, ge=1)


class OpenAISettings(BaseModel):
    api_key: str
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 15.0


class StorageSettings(BaseModel):
    sqlite_path: str = "moderation.db"


class LoggingSettings(BaseModel):
    level: str = Field(default="INFO", description="Logging level: DEBUG, INFO, WARNING, ERROR")
    use_json: bool = Field(default=False, description="Use JSON format instead of colored output")


class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SPISDIL_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
    )

    telegram_token: str = Field(..., description="Telegram bot token.")
    openai: OpenAISettings
    batch: BatchSettings = BatchSettings()
    layers: LayerSettings = LayerSettings()
    scheduler: SchedulerSettings = SchedulerSettings()
    storage: StorageSettings = StorageSettings()
    logging: LoggingSettings = LoggingSettings()
