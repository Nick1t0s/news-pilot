from __future__ import annotations

import html
import math
from collections.abc import Sequence
from html.parser import HTMLParser

ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "del", "code", "pre", "blockquote", "tg-spoiler"}
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6"}


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.open_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _BLOCK_TAGS:
            self.out.append("\n")
            return
        if tag == "a":
            href = dict(attrs).get("href", "")
            if _safe_href(href):
                self.out.append(f'<a href="{html.escape(href, quote=True)}">')
                self.open_tags.append("a")
            return
        if tag in ALLOWED_TAGS:
            self.out.append(f"<{tag}>")
            self.open_tags.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag in _BLOCK_TAGS:
            self.out.append("\n")
        elif tag == "a" and _safe_href(dict(attrs).get("href", "")):
            href = dict(attrs)["href"]
            text = html.escape(href, quote=True)
            self.out.append(f'<a href="{html.escape(href, quote=True)}">{text}</a>')

    def handle_endtag(self, tag: str) -> None:
        if tag in ALLOWED_TAGS and tag in self.open_tags:
            while self.open_tags:
                open_tag = self.open_tags.pop()
                self.out.append(f"</{open_tag}>")
                if open_tag == tag:
                    break
        elif tag in _BLOCK_TAGS:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        self.out.append(html.escape(data, quote=False))

    def result(self) -> str:
        while self.open_tags:
            self.out.append(f"</{self.open_tags.pop()}>")
        text = "".join(self.out)
        lines = [line.rstrip() for line in text.splitlines()]
        return "\n".join(lines).strip()


def _safe_href(href: str) -> bool:
    lowered = href.lower()
    return lowered.startswith(("http://", "https://", "tg://"))


def sanitize_telegram_html(raw: str) -> str:
    """Rebuild arbitrary LLM-produced HTML into a safe Telegram-allowed subset."""
    if not raw:
        return ""
    sanitizer = _Sanitizer()
    try:
        sanitizer.feed(raw)
        sanitizer.close()
    except Exception:  # noqa: BLE001
        return html.escape(raw, quote=False)
    return sanitizer.result()


class _Stripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def plain_text(raw: str) -> str:
    """Strip all markup, keeping readable text and line breaks."""
    if not raw:
        return ""
    stripper = _Stripper()
    try:
        stripper.feed(raw)
        stripper.close()
        text = "".join(stripper.parts)
    except Exception:  # noqa: BLE001
        text = raw
    text = html.unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    a = a.tolist() if hasattr(a, "tolist") else list(a)
    b = b.tolist() if hasattr(b, "tolist") else list(b)
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / math.sqrt(na * nb)
