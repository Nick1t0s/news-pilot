from __future__ import annotations

from types import SimpleNamespace

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


class _CaptureCompletions:
    """Stub for client.chat.completions.create that records the call kwargs."""

    def __init__(self, content: str = "") -> None:
        self.calls: list[dict] = []
        self._content = content

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        message = SimpleNamespace(content=self._content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _install_capture(provider: LLMProvider, content: str) -> _CaptureCompletions:
    completions = _CaptureCompletions(content=content)
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return completions


def test_default_no_extra_headers() -> None:
    provider = make_provider()
    headers = dict(provider._client.default_headers or {})
    assert "x-opencode-session" not in headers


def test_extra_headers_passed_to_client() -> None:
    provider = make_provider(extra_headers={"x-opencode-session": "session-123"})
    headers = dict(provider._client.default_headers or {})
    assert headers.get("x-opencode-session") == "session-123"


async def test_chat_passes_reasoning_params() -> None:
    provider = make_provider(reasoning_effort="low", extra_body={"thinking": {"type": "disabled"}})
    completions = _install_capture(provider, content="ok")

    await provider.chat([{"role": "user", "content": "hi"}])

    kwargs = completions.calls[0]
    assert kwargs["reasoning_effort"] == "low"
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


async def test_chat_omits_reasoning_params_when_unset() -> None:
    provider = make_provider()
    completions = _install_capture(provider, content="ok")

    await provider.chat([{"role": "user", "content": "hi"}])

    kwargs = completions.calls[0]
    assert kwargs["reasoning_effort"] is None
    assert kwargs["extra_body"] is None


async def test_complete_json_passes_reasoning_params() -> None:
    provider = make_provider(reasoning_effort="low", extra_body={"thinking": {"type": "disabled"}})
    completions = _install_capture(provider, content='{"ok": true}')
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}

    result = await provider.complete_json(system="s", user="u", schema=schema)

    assert result == {"ok": True}
    kwargs = completions.calls[0]
    assert kwargs["reasoning_effort"] == "low"
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


async def test_complete_json_omits_reasoning_params_when_unset() -> None:
    provider = make_provider()
    completions = _install_capture(provider, content='{"ok": true}')
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}

    await provider.complete_json(system="s", user="u", schema=schema)

    kwargs = completions.calls[0]
    assert kwargs["reasoning_effort"] is None
    assert kwargs["extra_body"] is None
