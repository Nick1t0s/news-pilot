from __future__ import annotations

import asyncio
import logging

from tavily import TavilyClient

log = logging.getLogger("tavily")


class TavilyImageSearch:
    """Async wrapper around the sync tavily-python client."""

    def __init__(self, api_key: str, *, timeout: float = 30.0, retries: int = 3) -> None:
        self.enabled = bool(api_key)
        self._client = TavilyClient(api_key=api_key) if api_key else None
        self._timeout = timeout
        self._retries = retries

    async def search(self, query: str) -> list[str]:
        if not self.enabled or self._client is None:
            return []

        def run():
            return self._client.search(  # type: ignore[union-attr]
                query=query,
                search_depth="basic",
                include_images=True,
                include_answer=False,
                max_results=8,
            )

        try:
            result = await asyncio.wait_for(asyncio.to_thread(run), timeout=self._timeout + 10)
        except Exception as exc:  # noqa: BLE001
            log.warning("tavily search failed: %s", exc)
            return []
        images = result.get("images") or []
        urls: list[str] = []
        for image in images:
            url = image.get("url") if isinstance(image, dict) else image
            if url:
                urls.append(str(url))
        return urls
