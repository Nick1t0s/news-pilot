from __future__ import annotations

import hashlib
import json
import re
from types import SimpleNamespace

from app.providers.llm import LLMError


def hashed_vector(text: str, dimensions: int = 768) -> list[float]:
    """Binary bag-of-words vector; reworded same-topic texts stay reasonably close."""
    vec = [0.0] * dimensions
    for token in set(re.findall(r"[а-яёa-z0-9]+", text.lower())):
        if len(token) < 3:
            continue
        index = int(hashlib.md5(token.encode()).hexdigest(), 16) % dimensions
        vec[index] = 1.0
    return vec


class FakeEmbeddings:
    """Deterministic bag-of-words embeddings; reworded same-topic texts stay close."""

    def __init__(self, dimensions: int = 768) -> None:
        self.dimensions = dimensions
        self.calls: list[str] = []

    def truncate(self, text: str) -> str:
        return text

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return hashed_vector(text)


class FakeLLM:
    def __init__(self) -> None:
        self.json_results: list[dict] = []
        self.chat_results: list = []
        self.json_calls: list[dict] = []
        self.chat_calls: list[list] = []

    def push_json(self, result: dict) -> None:
        self.json_results.append(result)

    def push_chat(self, completion) -> None:
        self.chat_results.append(completion)

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict,
        schema_name: str = "response",
        temperature: float | None = None,
    ) -> dict:
        self.json_calls.append({"system": system, "user": user, "schema": schema, "name": schema_name})
        if not self.json_results:
            raise LLMError("FakeLLM: no scripted json result")
        return self.json_results.pop(0)

    async def chat(self, messages: list, *, tools: list | None = None, temperature: float | None = None):
        self.chat_calls.append(messages)
        if not self.chat_results:
            raise LLMError("FakeLLM: no scripted chat completion")
        return self.chat_results.pop(0)


def completion(content=None, tool_calls=None):
    message_dict = {"role": "assistant", "content": content}
    message = SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        model_dump=lambda exclude_none=True: message_dict,
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def tool_call(call_id: str, name: str, arguments: dict):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))


class FakeSender:
    def __init__(self) -> None:
        self._counter = 100
        self.published: list[dict] = []
        self.drafts: list[dict] = []
        self.admin_texts: list[str] = []

    async def send_to_channel(self, text: str, photos: list[str], reply_to: int | None = None) -> tuple[int, str]:
        self._counter += 1
        self.published.append(
            {"text": text, "photos": list(photos), "message_id": self._counter, "reply_to": reply_to}
        )
        return self._counter, f"https://t.me/testchannel/{self._counter}"

    async def send_moderation_draft(self, text: str, photos: list[str], keyboard):
        self._counter += 1
        self.drafts.append({"text": text, "photos": list(photos)})
        return SimpleNamespace(chat=SimpleNamespace(id=1), message_id=self._counter)

    async def send_admin_text(self, text: str):
        self.admin_texts.append(text)
        return SimpleNamespace(chat=SimpleNamespace(id=1), message_id=0)

    async def edit_message(self, chat_id, message_id: int, text: str) -> None:
        return None
