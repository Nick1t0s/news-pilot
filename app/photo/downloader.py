from __future__ import annotations

import logging

import httpx

from app.providers.retry import with_retries

log = logging.getLogger("images")

MAX_IMAGE_BYTES = 10 * 1024 * 1024


class PermanentImageError(RuntimeError):
    """Unrecoverable fetch failure (e.g. HTTP 4xx); never retried."""


def sniff_image_format(data: bytes) -> str | None:
    if len(data) < 12:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def mime_for_format(fmt: str) -> str:
    return {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[fmt]


async def download_image(
    http: httpx.AsyncClient,
    url: str,
    *,
    timeout: float = 20.0,
    retries: int = 2,
) -> bytes | None:
    """Download and validate an image; returns raw bytes or None.

    HTTP 4xx failures (forbidden, not found...) are not retried.
    """

    async def fetch() -> bytes:
        resp = await http.get(url, timeout=timeout, follow_redirects=True)
        if 400 <= resp.status_code < 500:
            raise PermanentImageError(f"HTTP {resp.status_code}")
        resp.raise_for_status()
        data = resp.content
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError(f"image too large: {len(data)} bytes")
        return data

    try:
        data = await with_retries(
            fetch,
            attempts=max(1, retries) + 1,
            what=f"image {url[:80]}",
            exceptions=(httpx.HTTPError, httpx.StreamError, TimeoutError, ValueError),
        )
    except PermanentImageError as exc:
        log.warning("image not available (no retry): url=%s: %s", url[:200], exc)
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("image download failed url=%s: %s", url[:200], exc)
        return None
    if sniff_image_format(data) is None:
        log.warning("unsupported image format url=%s", url[:200])
        return None
    return data
