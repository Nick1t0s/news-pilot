from __future__ import annotations

import logging

import httpx

from app.config import EmbeddingsConfig
from app.providers.retry import with_retries

log = logging.getLogger("embeddings")


class EmbeddingError(RuntimeError):
    pass


class EmbeddingProvider:
    """Ollama embeddings provider (independent from the LLM provider)."""

    def __init__(self, cfg: EmbeddingsConfig, http: httpx.AsyncClient) -> None:
        self._cfg = cfg
        self._http = http

    @property
    def max_chars(self) -> int:
        return self._cfg.max_chars

    @property
    def dimensions(self) -> int:
        return self._cfg.dimensions

    def truncate(self, text: str) -> str:
        return text[: self._cfg.max_chars]

    async def embed(self, text: str) -> list[float]:
        payload = {"model": self._cfg.model, "input": self.truncate(text)}

        async def call() -> list[float]:
            resp = await self._http.post(
                f"{self._cfg.base_url.rstrip('/')}/api/embed",
                json=payload,
                timeout=self._cfg.timeout_seconds,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("embeddings"):
                return [float(x) for x in data["embeddings"][0]]
            if "embedding" in data:
                return [float(x) for x in data["embedding"]]
            raise EmbeddingError(f"unexpected ollama embed response: {list(data.keys())}")

        return await with_retries(
            call, attempts=self._cfg.retries, what="ollama.embed", exceptions=(httpx.HTTPError, EmbeddingError, TimeoutError)
        )
