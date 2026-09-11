from __future__ import annotations

import logging
from pathlib import Path

import httpx

from app.providers.retry import with_retries

log = logging.getLogger("images")

MAX_IMAGE_BYTES = 10 * 1024 * 1024


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
    dest_dir: Path,
    *,
    base_name: str,
    timeout: float = 20.0,
    retries: int = 2,
) -> Path | None:
    """Download and validate an image; returns local path or None."""

    async def fetch() -> bytes:
        resp = await http.get(url, timeout=timeout, follow_redirects=True)
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
    except Exception as exc:  # noqa: BLE001
        log.warning("image download failed url=%s: %s", url[:200], exc)
        return None
    fmt = sniff_image_format(data)
    if fmt is None:
        log.warning("unsupported image format url=%s", url[:200])
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"{base_name}.{fmt}"
    path.write_bytes(data)
    return path
