from __future__ import annotations

import asyncio
import logging

import httpx
import trafilatura

from app.providers.retry import with_retries

log = logging.getLogger("fetcher")


async def fetch_article(
    http: httpx.AsyncClient,
    url: str,
    *,
    timeout: float,
    retries: int,
) -> str | None:
    """Download the article page and extract its main text with trafilatura.

    `retries` = number of attempts after the first one. Returns None on failure.
    """

    async def get() -> str:
        resp = await http.get(url, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        return resp.text

    try:
        html = await with_retries(
            get,
            attempts=max(1, retries) + 1,
            what=f"fetch {url[:80]}",
            exceptions=(httpx.HTTPError, httpx.StreamError, TimeoutError),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("article fetch failed url=%s: %s", url[:200], exc)
        return None
    if not html:
        return None
    try:
        text = await asyncio.to_thread(
            trafilatura.extract, html, url=url, include_comments=False, include_tables=False
        )
    except Exception:
        log.exception("trafilatura extraction failed url=%s", url[:200])
        return None
    return text
