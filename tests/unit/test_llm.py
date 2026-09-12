from __future__ import annotations

from app.config import LLMConfig
from app.providers.llm import LLMProvider


def make_provider(**kwargs) -> LLMProvider:
    cfg = LLMConfig(
        base_url="https://llm.example.com/v1",
        api_key="test-key",
        model="test-model",
        retries=1,
        **kwargs,
    )
    return LLMProvider(cfg)


def test_default_no_extra_headers() -> None:
    provider = make_provider()
    headers = dict(provider._client.default_headers or {})
    assert "x-opencode-session" not in headers


def test_extra_headers_passed_to_client() -> None:
    provider = make_provider(extra_headers={"x-opencode-session": "session-123"})
    headers = dict(provider._client.default_headers or {})
    assert headers.get("x-opencode-session") == "session-123"
