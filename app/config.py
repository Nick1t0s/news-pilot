from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


class ExtraForbid(BaseModel):
    model_config = {"extra": "forbid"}


class LLMConfig(ExtraForbid):
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    temperature: float = 0.4
    timeout_seconds: float = 120.0
    retries: int = 3


class EmbeddingsConfig(ExtraForbid):
    base_url: str = "http://localhost:11434"
    model: str = "nomic-embed-text"
    dimensions: int = 768
    max_chars: int = 6000
    timeout_seconds: float = 60.0
    retries: int = 3


class FeedConfig(ExtraForbid):
    name: str
    url: str


class RssConfig(ExtraForbid):
    poll_interval_seconds: int = 300
    feeds: list[FeedConfig] = Field(default_factory=list)


class FetcherConfig(ExtraForbid):
    timeout_seconds: float = 30.0
    retries: int = 2
    min_text_length: int = 100


class DatabaseConfig(ExtraForbid):
    dsn: str = "postgresql+asyncpg://news:news@localhost:5432/news"


class TavilyConfig(ExtraForbid):
    api_key: str = ""
    timeout_seconds: float = 30.0
    retries: int = 3


class TelegramConfig(ExtraForbid):
    bot_token: str = ""
    channel_id: str = "@channel"
    admin_id: int = 0


class DedupConfig(ExtraForbid):
    window_days: int = 3
    min_similarity: float = 0.75
    top_k: int = 5
    on_error: Literal["review", "pass", "drop"] = "review"


class PhotoAgentConfig(ExtraForbid):
    max_iterations: int = 10
    max_searches: int = 5
    max_images: int = 4


class ContextConfig(ExtraForbid):
    window_days: int = 14
    top_k: int = 3
    min_similarity: float = 0.7


class PublishConfig(ExtraForbid):
    mode: Literal["auto", "moderation"] = "moderation"
    max_per_hour: int = 5
    quiet_hours: str | None = None
    timezone: str = "UTC"
    moderation_timeout_hours: float = 24.0

    @field_validator("quiet_hours")
    @classmethod
    def _validate_quiet_hours(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = value.split("-")
        if len(parts) != 2:
            raise ValueError(f"quiet_hours must look like '01:00-07:00', got {value!r}")
        for part in parts:
            hour, minute = part.strip().split(":")
            if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
                raise ValueError(f"bad time component in quiet_hours: {part!r}")
        return value


class PipelineConfig(ExtraForbid):
    workers: int = 2


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.environ.get("ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="forbid",
    )

    log_level: str = "INFO"
    data_dir: str = "data"

    llm: LLMConfig = Field(default_factory=LLMConfig)
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    rss: RssConfig = Field(default_factory=RssConfig)
    fetcher: FetcherConfig = Field(default_factory=FetcherConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    tavily: TavilyConfig = Field(default_factory=TavilyConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    dedup: DedupConfig = Field(default_factory=DedupConfig)
    photo_agent: PhotoAgentConfig = Field(default_factory=PhotoAgentConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    publish: PublishConfig = Field(default_factory=PublishConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        yaml_source = _YamlFileSource(settings_cls, _yaml_file_path(settings_cls))
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            yaml_source,
            file_secret_settings,
        )


def _yaml_file_path(settings_cls: type[BaseSettings]) -> Path | None:
    raw = os.environ.get("CONFIG")
    if raw:
        return Path(raw)
    return None


class _YamlFileSource(PydanticBaseSettingsSource):
    def __init__(self, settings_cls: type[BaseSettings], path: Path | None) -> None:
        super().__init__(settings_cls)
        self._data: dict[str, Any] = {}
        if path is not None and path.exists():
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            if loaded is not None:
                if not isinstance(loaded, dict):
                    raise ValueError(f"config file {path} must contain a mapping")
                self._data = loaded

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        if field_name not in self._data:
            return None, field_name, False
        return self._data[field_name], field_name, False

    def __call__(self) -> dict[str, Any]:
        return {k: v for k, v in self._data.items() if k in self.settings_cls.model_fields}


@lru_cache
def get_settings() -> Settings:
    return Settings()


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]
