from __future__ import annotations

import argparse
import asyncio
import email.utils
import logging
import random
import sys
from pathlib import Path

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent))

from templates import (
    Article,
    EmulatorState,
    make_developing,
    make_duplicate,
    make_mixed,
    make_new,
)

log = logging.getLogger("emulator")

MEDIA_NS = "http://search.yahoo.com/mrss/"
TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x02\x00\x00\x00\x02\x08\x02"
    b"\x00\x00\x00\xf6\x0b\xea\x1e\x00\x00\x00\x1eIDATx\x9cc\xfc\xcf\x80\x1b\xfe"
    b"\xa3\x01\x06\x18\x90z\x08\x00\x2e\x8f\x05\x87\x9b\xc4\xd2\x01\x00\x00\x00"
    b"\x00IEND\xaeB`\x82"
)


def next_article(state: EmulatorState) -> Article:
    if state.scenario == "random":
        return make_new(state)
    if state.scenario == "duplicates":
        return make_duplicate(state)
    if state.scenario == "developing":
        return make_developing(state)
    return make_mixed(state)


def build_rss(state: EmulatorState) -> bytes:
    import xml.etree.ElementTree as ET

    ET.register_namespace("media", MEDIA_NS)
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Эмулятор новостей"
    ET.SubElement(channel, "link").text = state.base_url
    ET.SubElement(channel, "description").text = "Тестовый фид для news-pilot"
    for article in state.articles[-50:]:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = article.title
        ET.SubElement(item, "link").text = article.link
        ET.SubElement(item, "guid", {"isPermaLink": "true"}).text = article.guid
        ET.SubElement(item, "pubDate").text = email.utils.format_datetime(article.published_at)
        ET.SubElement(item, "description").text = article.summary
        if article.image:
            ET.SubElement(
                item,
                f"{{{MEDIA_NS}}}content",
                {"url": f"{state.base_url}/img/{article.uid}.png", "type": "image/png", "medium": "image"},
            )
    return ET.tostring(rss, encoding="utf-8", xml_declaration=True)


def build_article_page(article: Article) -> str:
    paragraphs = "".join(f"<p>{paragraph}</p>" for paragraph in article.body)
    return (
        "<!doctype html>"
        "<html lang='ru'><head><meta charset='utf-8'>"
        f"<title>{article.title}</title></head><body>"
        "<article>"
        f"<h1>{article.title}</h1>"
        f"<time datetime='{article.published_at.isoformat()}'>"
        f"{article.published_at:%d.%m.%Y %H:%M}</time>"
        + paragraphs
        + "</article></body></html>"
    )


async def rss_handler(request: web.Request) -> web.Response:
    state: EmulatorState = request.app["state"]
    return web.Response(body=build_rss(state), content_type="application/xml")


async def article_handler(request: web.Request) -> web.Response:
    state: EmulatorState = request.app["state"]
    try:
        uid = int(request.match_info["uid"])
    except ValueError:
        raise web.HTTPNotFound(text="not found")
    article = next((a for a in state.articles if a.uid == uid), None)
    if article is None:
        raise web.HTTPNotFound(text="article not found")
    return web.Response(text=build_article_page(article), content_type="text/html")


async def image_handler(request: web.Request) -> web.Response:
    return web.Response(body=TINY_PNG, content_type="image/png")


async def generator_task(app: web.Application) -> None:
    state: EmulatorState = app["state"]
    interval: float = app["interval"]
    while True:
        await asyncio.sleep(interval)
        article = next_article(state)
        log.info(
            "generated: uid=%d topic=%s title=%r",
            article.uid,
            article.topic_key,
            article.title[:60],
        )


async def start_generator(app: web.Application) -> None:
    task = asyncio.create_task(generator_task(app))
    yield
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="news-pilot news emulator")
    parser.add_argument("--scenario", choices=["random", "duplicates", "developing", "mixed"], default="random")
    parser.add_argument("--interval", type=float, default=60.0, help="seconds between generated items")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--seed", type=int, default=None)
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    args = parse_args()
    base_url = f"http://{args.host}:{args.port}"
    state = EmulatorState(base_url=base_url, scenario=args.scenario)
    if args.seed is not None:
        state.rng = random.Random(args.seed)

    app = web.Application()
    app["state"] = state
    app["interval"] = args.interval
    app.add_routes(
        [
            web.get("/rss", rss_handler),
            web.get("/article/{uid}", article_handler),
            web.get("/img/{uid}.png", image_handler),
        ]
    )
    app.cleanup_ctx.append(start_generator)
    log.info(
        "emulator running: scenario=%s interval=%.0fs url=%s/rss",
        args.scenario,
        args.interval,
        base_url,
    )
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
