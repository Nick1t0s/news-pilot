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
    proxy: str = ""
    extra_headers: dict[str, str] = Field(default_factory=dict)


class EmbeddingsConfig(ExtraForbid):
    base_url: str = "http://localhost:11434"
    model: str = "qwen3-embedding:0.6b-q4_K_M"
    dimensions: int = 1024
    max_chars: int = 6000
    timeout_seconds: float = 60.0
    retries: int = 3
    proxy: str = ""


class FeedConfig(ExtraForbid):
    name: str
    url: str


class RssConfig(ExtraForbid):
    poll_interval_seconds: int = 300
    clear_run: bool = False
    feeds: list[FeedConfig] = Field(default_factory=list)


class FetcherConfig(ExtraForbid):
    timeout_seconds: float = 30.0
    retries: int = 2
    min_text_length: int = 100
    proxy: str = ""


class DatabaseConfig(ExtraForbid):
    dsn: str = "postgresql+asyncpg://USER:PASSWORD@localhost:5432/DBNAME"


class TavilyConfig(ExtraForbid):
    api_key: str = ""
    timeout_seconds: float = 30.0
    retries: int = 3
    proxy: str = ""


class TelegramConfig(ExtraForbid):
    bot_token: str = ""
    channel_id: str = "@channel"
    admin_id: int = 0
    proxy: str = ""

    @field_validator("channel_id", mode="before")
    @classmethod
    def _coerce_channel_id(cls, value: object) -> object:
        # numeric channel ids (-100...) come from YAML as int
        return str(value) if isinstance(value, int) else value


class DedupConfig(ExtraForbid):
    window_days: int = 3
    min_similarity: float = 0.75
    top_k: int = 5
    on_error: Literal["review", "pass", "drop"] = "review"


class PhotoAgentConfig(ExtraForbid):
    max_iterations: int = 10
    max_searches: int = 5
    max_images: int = 4
    proxy: str = ""


class ContextConfig(ExtraForbid):
    window_days: int = 14
    top_k: int = 3
    min_similarity: float = 0.5


class PipelineConfig(ExtraForbid):
    retries: int = 2


class PublishConfig(ExtraForbid):
    mode: Literal["auto", "moderation"] = "moderation"
    moderation_timeout_hours: float = 24.0
    notify_admin: bool = False
    append_source: bool = False
    proxy: str = ""


class LimitsConfig(ExtraForbid):
    daily_posts: int = 0  # 0 = no hard limit, counter not queried
    reserve_posts: int = 5
    timezone: str = "Europe/Moscow"

    @field_validator("daily_posts", "reserve_posts")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must be >= 0")
        return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.environ.get("ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        env_ignore_empty=True,
        extra="forbid",
    )

    log_level: str = "INFO"

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
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    publish: PublishConfig = Field(default_factory=PublishConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)

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
            _FilteredDotenvSource(settings_cls, dotenv_settings),
            yaml_source,
            file_secret_settings,
        )


def _yaml_file_path(settings_cls: type[BaseSettings]) -> Path | None:
    raw = os.environ.get("CONFIG")
    if raw is None:
        # CONFIG may live in .env, which is not exported to the environment
        env_file = Path(os.environ.get("ENV_FILE", ".env"))
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                if name.strip() == "CONFIG":
                    raw = value.strip()
                    break
    if raw:
        return Path(raw)
    return None


class _FilteredDotenvSource(PydanticBaseSettingsSource):
    """Dotenv source that drops keys unknown to the model (e.g. CONFIG, TEST_DSN)."""

    def __init__(self, settings_cls: type[BaseSettings], inner: PydanticBaseSettingsSource) -> None:
        super().__init__(settings_cls)
        self._inner = inner

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return self._inner.get_field_value(field, field_name)

    def __call__(self) -> dict[str, Any]:
        data = self._inner() or {}
        return {k: v for k, v in data.items() if k in self.settings_cls.model_fields}


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
