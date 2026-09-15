from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass

import httpx

from app.config import Settings
from app.photo.downloader import (
    MAX_IMAGE_BYTES,
    download_image,
    mime_for_format,
    sniff_image_format,
)
from app.photo.tavily_client import TavilyImageSearch
from app.providers.llm import LLMError

log = logging.getLogger("photo_agent")

MAX_INLINE_IMAGES = 2

SYSTEM_PROMPT = (
    "Ты — фото-редактор новостного Telegram-канала. Твоя задача — подобрать 1–4 фотографии к новости.\n"
    "Правила:\n"
    "- Лучший вариант — кадр, напрямую связанный с сутью новости (участники, место, предмет события).\n"
    "- Если точных кадров найти не удалось — подбери тематически близкое фото: тот же объект, тема или\n"
    "  окружение события. Пост без фото — хуже, чем пост с уместным тематическим кадром.\n"
    "- Запрещено: NSFW, кадры с жестокостью, крупные логотипы СМИ, водяные знаки, постеры с текстовым спамом.\n"
    "- Запрещены изображения с украинским флагом или любой украинской символикой (герб, ленты, эмблемы, надписи).\n"
    "- Запрещены изображения с явным упоминанием украинских СМИ/медиа или их логотипами — за исключением случая,\n"
    "  когда такое изображение непосредственно необходимо для понимания сути новости.\n"
    "- Горизонтальные кадры предпочтительны вертикальных.\n"
    "Порядок работы:\n"
    "1. Ищи кадры через tavily_image_search (не более разрешённого числа поисков).\n"
    "2. Начинай с точных запросов; если точных кадров нет — постепенно обобщай запрос (тема, объект, место), а не отказывайся сразу.\n"
    "3. Каждого кандидата перед выбором оценивай через inspect_image.\n"
    "4. Заверши работу вызовом select_images со списком id выбранных кандидатов (1–4).\n"
    "5. Пустой список в select_images — крайний случай: только если даже тематические фото не подходят."
)

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "tavily_image_search",
            "description": "Поиск изображений в интернете по текстовому запросу. Возвращает список кандидатов (id и URL).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Поисковый запрос на русском или английском"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_image",
            "description": "Загрузить изображение и получить его для визуальной оценки: релевантность, качество, водяные знаки.",
            "parameters": {
                "type": "object",
                "properties": {
                    "image": {"type": "string", "description": "id кандидата (например img_0) или прямой URL"},
                },
                "required": ["image"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select_images",
            "description": "Финальный выбор фотографий для поста (1–4 id). Пустой список — публиковать без фото.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 0,
                        "maxItems": 10,
                    },
                },
                "required": ["ids"],
                "additionalProperties": False,
            },
        },
    },
]


@dataclass
class PhotoRecord:
    source_url: str
    data: bytes | None


class PhotoAgent:
    """Tool-calling agent that searches and selects photos for a post."""

    def __init__(
        self,
        cfg: Settings,
        llm,
        tavily: TavilyImageSearch,
        http: httpx.AsyncClient,
    ) -> None:
        self._cfg = cfg
        self._llm = llm
        self._tavily = tavily
        self._http = http

    async def collect(self, item) -> list[PhotoRecord]:
        try:
            return await self._collect_inner(item)
        except LLMError as exc:
            log.error("photo agent failed: source=%s error=%s", item.source, exc)
            return []
        except Exception:
            log.exception("photo agent failed: source=%s", item.source)
            return []

    async def _collect_inner(self, item) -> list[PhotoRecord]:
        max_images = self._cfg.photo_agent.max_images
        max_searches = self._cfg.photo_agent.max_searches
        candidates: dict[str, dict] = {}

        def register(url: str) -> str:
            cid = f"img_{len(candidates)}"
            candidates[cid] = {"url": url, "bytes": None}
            return cid

        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _news_prompt(item, max_searches)},
        ]

        searches_used = 0
        selection: list[str] | None = None
        failed_urls: set[str] = set()
        iterations = self._cfg.photo_agent.max_iterations

        for _ in range(iterations):
            _prune_inline_images(messages, MAX_INLINE_IMAGES)
            completion = await self._llm.chat(messages, tools=TOOLS, temperature=0.2)
            message = completion.choices[0].message
            tool_calls = list(getattr(message, "tool_calls", None) or [])
            if not tool_calls:
                messages.append(
                    {
                        "role": "user",
                        "content": "Пользуйся инструментами. Заверши вызовом select_images (список id или пустой список).",
                    }
                )
                continue
            messages.append(message.model_dump(exclude_none=True))
            for tool_call in tool_calls:
                image_message = None
                name = tool_call.function.name
                arguments = json.loads(tool_call.function.arguments or "{}")
                if name == "tavily_image_search":
                    reply, searches_used = await self._handle_search(
                        arguments, searches_used, max_searches, register
                    )
                elif name == "inspect_image":
                    reply, image_message = await self._handle_inspect(
                        arguments, candidates, failed_urls
                    )
                elif name == "select_images":
                    selection, reply = self._handle_select(arguments, candidates, max_images)
                    image_message = None
                else:
                    reply, image_message = f"Неизвестный инструмент: {name}", None
                messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": reply})
                if image_message is not None:
                    messages.append(image_message)
            if selection is not None:
                break
        else:
            log.warning("photo agent hit iteration limit: source=%s", item.source)

        if not selection:
            log.info("no photos selected: source=%s", item.source)
            return []

        records: list[PhotoRecord] = []
        for cid in selection[:max_images]:
            candidate = candidates.get(cid)
            if candidate is None:
                continue
            url = candidate["url"]
            data = candidate["bytes"]
            if url in failed_urls:
                log.info("selected photo previously failed download, dropped: url=%s", url[:200])
                continue
            if data is None:
                data = await download_image(self._http, url, timeout=20.0, retries=2)
            if data is not None:
                records.append(PhotoRecord(source_url=url, data=data))
            else:
                log.warning("selected photo failed validation, dropped: url=%s", url[:200])
        return records

    async def _handle_search(self, arguments: dict, searches_used: int, max_searches: int, register) -> tuple[str, int]:
        if searches_used >= max_searches:
            return f"Лимит поисковых запросов исчерпан ({max_searches}). Выбери из доступных кандидатов.", searches_used
        query = str(arguments.get("query") or "").strip()
        if not query:
            return "Пустой поисковый запрос.", searches_used
        searches_used += 1
        urls = await self._tavily.search(query)
        if not urls:
            return f"По запросу {query!r} ничего не найдено.", searches_used
        lines = []
        for url in urls:
            cid = register(url)
            lines.append(f"{cid}: {url}")
        return "Найденные кандидаты (id: URL):\n" + "\n".join(lines), searches_used

    async def _handle_inspect(self, arguments: dict, candidates: dict, failed_urls: set[str]) -> tuple[str, dict | None]:
        ref = str(arguments.get("image") or "").strip()
        candidate = candidates.get(ref)
        url = ref if candidate is None else candidate["url"]
        if candidate is None and not ref.startswith(("http://", "https://")):
            return f"Кандидат {ref!r} не найден.", None
        if url in failed_urls:
            return (
                f"Изображение {ref} уже пробовали загрузить и получили отказ сервера. "
                "Не выбирай и не оценивай его повторно, возьми другого кандидата."
            ), None
        data = await self._fetch_image_bytes(url)
        if data is None:
            failed_urls.add(url)
            if candidate is not None:
                candidate["bytes"] = None
            return f"Не удалось загрузить изображение {ref}. Выбери другого кандидата.", None
        fmt = sniff_image_format(data)
        if fmt is None:
            failed_urls.add(url)
            return f"Изображение {ref} не распознано как jpg/png/webp. Выбери другого кандидата.", None
        if candidate is None:
            candidates[ref] = {"url": url, "bytes": data}
        else:
            candidate["bytes"] = data
        mime = mime_for_format(fmt)
        b64 = base64.b64encode(data).decode()
        data_url = f"data:{mime};base64,{b64}"
        reply = f"Изображение {ref} загружено ({fmt}, {len(data)} байт), оно приложено в следующем сообщении."
        image_message = {
            "role": "user",
            "content": [
                {"type": "text", "text": f"Изображение {ref} для визуальной оценки:"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
        return reply, image_message

    def _handle_select(self, arguments: dict, candidates: dict, max_images: int) -> tuple[list[str] | None, str]:
        ids = [str(x) for x in (arguments.get("ids") or [])][:max_images]
        known = [cid for cid in ids if cid in candidates]
        unknown = [cid for cid in ids if cid not in candidates]
        reply = f"Выбрано: {', '.join(known) if known else 'ничего (публикация без фото)'}"
        if unknown:
            reply += f"; игнорированы неизвестные id: {', '.join(unknown)}"
        return known, reply

    async def _fetch_image_bytes(self, url: str) -> bytes | None:
        try:
            resp = await self._http.get(url, timeout=20.0, follow_redirects=True)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            log.warning("image inspect download failed url=%s: %s", url[:200], exc)
            return None
        data = resp.content
        if len(data) > MAX_IMAGE_BYTES:
            return None
        return data


def _prune_inline_images(messages: list[dict], keep: int) -> None:
    indices = [
        index
        for index, message in enumerate(messages)
        if isinstance(message.get("content"), list)
        and any(isinstance(part, dict) and part.get("type") == "image_url" for part in message["content"])
    ]
    for index in indices[:-keep]:
        messages[index]["content"] = "Изображение было приложено ранее и уже оценено."


def _news_prompt(item, max_searches: int) -> str:
    return "\n".join([
        "Суть новости:",
        item.title,
        item.text[:1200],
        "",
        f"Тебе доступно не более {max_searches} поисковых запросов.",
        "Найди фото через tavily_image_search.",
    ])
