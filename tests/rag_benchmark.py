"""Standalone RAG search benchmark: how embedding model size affects ContextSearch.

Uses only the search-related modules of the project (EmbeddingProvider,
ContextSearch, repo, db.base) against a disposable pgvector database.
No RSS, LLM, Telegram or photo pipeline involved.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import (
    ContextConfig,
    DatabaseConfig,
    EmbeddingsConfig,
    Settings,
)
from app.context_search import ContextSearch
from app.db import repo
from app.db.base import _schema_ddl, create_pool
from app.db.entities import News, NewsStatus, PostStatus
from app.providers.embeddings import EmbeddingError, EmbeddingProvider
from app.providers.retry import with_retries

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
DB_DSN = "postgresql+asyncpg://rag:rag@localhost:55432/rag"
MODELS = {
    "qwen3-embedding:0.6b-q4_K_M": 1024,
    "qwen3-embedding:4b-q4_K_M": 2560,
    "qwen3-embedding:8b-q4_K_M": 4096,
}
INSTRUCT = "Instruct: Given a news article, retrieve related previously published channel posts\nQuery: "

TOPICS = {
    "crypto": [
        "Биткоин пробил отметку 120 тысяч долларов и обновил исторический максимум. Аналитики связывают рост с притоком средств в спотовые ETF и снижением продаж со стороны майнеров. Волатильность остаётся высокой: за сутки курс несколько раз менялся на несколько процентов.",
        "Регуляторы США одобрили заявку на спотовый ETF на Solana. Биржи готовятся к листингу нового инструмента в ближайшие недели, институциональные инвесторы уже формируют заявки. Ранее аналогичные продукты по эфириуму притянули более миллиарда долларов.",
        "В России предложили ввести лицензирование крупных майнинговых ферм. Авторы инициативы хотят облагать промышленный майнинг по повышенной ставке и учитывать потребление электроэнергии. Мелкие майнеры в частных домах останутся вне регулирования.",
    ],
    "space": [
        "SpaceX успешно вывела на орбиту очередную партию спутников Starlink. Ракета Falcon 9 совершила уже двадцатый полёт, посадка первой ступени прошла штатно на морскую платформу. Компания планирует увеличить частоту запусков до трёх в неделю.",
        "Роскосмос назвал сроки сборки лунной станции «Луна-27». Аппарат будет бурить грунт у южного полюса Луны и искать водяной лёд. Испытания двигателя завершены, запуск намечен на следующий год.",
        "Астрономы зафиксировали рекордно яркий болид над Уралом. Метеорит был виден в нескольких регионах, пострадавших нет. Учёные собрали фрагменты и начали лабораторный анализ состава.",
    ],
    "cyber": [
        "Хакеры атаковали цепочку поставок крупного разработчика ПО: вредоносный код попал в официальные обновления. Тысячи компаний по всему миру получили скомпрометированные сборки. Эксперты рекомендуют срочно проверить цифровые подписи обновлений.",
        "Международная операция Интерпола закрыла ботнет из 400 тысяч заражённых роутеров. Злоумышленники использовали их для скрытия трафика и DDoS-атак. Арестованы трое организаторов в Восточной Европе.",
        "Утечка данных банка затронула два миллиона клиентов: в сети появились паспортные данные и выписки по счетам. Банк подтвердил инцидент и заявил, что пароли не утекли. Центробанк требует отчёт о причинах произошедшего.",
    ],
    "energy": [
        "Цена нефти Brent опустилась ниже 60 долларов впервые за год на фоне роста запасов в США. Трейдеры ждут решения ОПЕК+ о продлении добровольных ограничений добычи. Аналитики допускают дальнейшее снижение котировок.",
        "«Газпром» раньше обычного полностью заполнил подземные хранилища газа в Европе. Летняя закачка шла темпами выше среднегодовых. Зимние риски для потребителей оценены как минимальные.",
        "Первый энергоблок АЭС «Эль-Дабаа» в Египте получил лицензию на физический пуск. Росатом завершает испытания систем безопасности перед загрузкой топлива. Станция закроет до 15% потребностей страны в электроэнергии.",
    ],
    "ai": [
        "OpenAI показала новую модель рассуждений, которая набирает лучший результат на олимпиадных задачах по математике. Компания обещает втрое снизить цену генерации и добавить глубокий поиск по документам. Доступ откроют через API в ближайшие недели.",
        "Nvidia представила ускоритель следующего поколения для обучения ИИ-моделей. Заявлен рост производительности вдвое при прежнем энергопотреблении и увеличенный объём памяти HBM. Крупные облачные провайдеры уже заказали первые партии.",
        "Европейский регулятор оштрафовал сервис машинного перевода за обучение на текстах без разрешения авторов. Компании придётся удалить часть датасета и выплатить десятки миллионов евро. Издатели назвали решение прецедентом для всей отрасли.",
    ],
    "sport": [
        "Мадридский «Реал» вырвал победу в дерби голом на 90-й минуте и вышел на первое место в чемпионате. Единственный мяч забил вышедший на замену нападающий. Соперник отстал от лидера на три очка.",
        "Сборная России по хоккею выиграла первый матч турнира в Праге со счётом 5:2. Дубль оформил молодой форвард, дебютировавший в национальной команде. В следующем туре россияне сыграют с хозяевами турнира.",
        "УЕФА утвердил новый формат Лиги чемпионов с этапом лиги вместо группового раунда. Каждая команда сыграет с восемью разными соперниками, а плей-офф расширится до 24 клубов. Изменения вступят в силу со следующего сезона.",
    ],
    "health": [
        "ВОЗ зафиксировала рост случаев нового варианта гриппа в Южной Азии. Вакцина текущего состава сохраняет эффективность, госпитализации единичны. Врачи рекомендуют ускорить прививочные кампании к началу сезона.",
        "Минздрав расширил программу бесплатной химиотерапии: новые препараты включили в перечень льготного обеспечения. Помощь получат дополнительно 40 тысяч пациентов в год. Закупки начнутся с первого квартала.",
        "Учёные успешно испытали ревакцинацию от COVID-19 в форме назального спрея. Местный иммунитет в носоглотке оказался выше, чем при инъекции. Массовые поставки возможны к осени после регистрации.",
    ],
    "auto": [
        "Tesla снизила цены на Model 3 в Китае на 5% из-за конкуренции с местными брендами. Это уже третье удешевление за квартал, маржинальность продолжает падать. Аналитики ждут ответных шагов от BYD и Xpeng.",
        "В ЕС утвердили единый стандарт обмена данными для систем автопилота. Автопроизводители обязаны открывать телеметрию сервисам страхования и экстренным службам. Правило заработает с 2027 года.",
        "Продажи электромобилей в Европе впервые превысили продажи дизельных машин. Лидером рынка остаётся Volkswagen с семейством ID. Гибриды сохраняют второе место по объёму.",
    ],
}

QUERIES: list[tuple[str | None, str, str, str]] = [
    ("crypto", "easy", "Биткоин снова дороже 123 тысяч долларов", "Курс биткоина превысил 123 тысячи долларов на фоне рекордных притоков в спотовые ETF. За неделю фонды привлекли более двух миллиардов долларов, а продажи со стороны майнеров замедлились."),
    ("crypto", "medium", "Coinbase отчиталась о рекордных комиссиях", "Биржа Coinbase сообщила о росте комиссионных доходов на фоне всплеска волатильности альткоинов. Компания анонсировала новые инструменты для институциональных клиентов и расширяет листинги."),
    ("space", "easy", "Очередная партия Starlink на орбите", "SpaceX вывела на орбиту 24 спутника Starlink, ступень Falcon 9 в двадцатый раз вернулась на морскую платформу. Компания наращивает темп запусков."),
    ("space", "medium", "НАСА отложило облёт Луны", "Агентство НАСА перенесло пилотируемый облёт Луны из-за обнаруженных дефектов теплозащиты корабля Orion. Новые даты назовут после дополнительных испытаний."),
    ("cyber", "easy", "Интерпол закрыл ботнет из заражённых роутеров", "Международная операция прекратила работу ботнета из сотен тысяч заражённых маршрутизаторов, который использовался для DDoS-атак и скрытия преступного трафика. Задержаны организаторы."),
    ("cyber", "medium", "Банк раскрыл детали взлома через подрядчика", "Кредитная организация сообщила о компрометации внутренних сервисов через аккаунт подрядчика. Злоумышленники выгрузили часть базы клиентов, данные появились в открытом доступе."),
    ("energy", "easy", "Нефть дешевеет на фоне роста запасов", "Brent упала ниже 60 долларов за баррель, обновив минимум за год. Запасы в США выросли сильнее прогнозов, а ОПЕК+ не спешит продлевать добровольные сокращения добычи."),
    ("energy", "medium", "Германия закрыла последние угольные станции", "Германия досрочно вывела из эксплуатации последние угольные ТЭС. Для зимнего резервирования страна нарастила закачку газа в хранилища и ставки на импорт электроэнергии."),
    ("ai", "easy", "OpenAI показала модель рассуждений нового поколения", "OpenAI представила новую модель рассуждений с рекордными результатами по математике и глубоким поиском по документам. Цена генерации снизится втрое, доступ откроют через API."),
    ("ai", "medium", "TSMC нарастила выпуск ИИ-ускорителей", "TSMC сообщила, что заказы на ускорители для обучения нейросетей занимают уже половину передовых мощностей. Компания расширяет производство упаковки чипов с высокой плотностью памяти."),
    ("sport", "easy", "«Реал» вырвал победу в дерби", "Мадридский «Реал» забил на последней минуте дерби и возглавил таблицу чемпионата Испании. Победный мяч провёл нападающий, вышедший на замену."),
    ("health", "easy", "ВОЗ предупредила о новом варианте гриппа", "Всемирная организация здравоохранения фиксирует быстрый рост заболеваемости новым вариантом гриппа в Южной Азии. Вакцины сохраняют эффективность, медики просят ускорить кампании вакцинации."),
    ("health", "medium", "Сезон гриппа начался раньше обычного", "Эпидемиологи предупреждают о раннем старте сезона гриппа: новый вариант доминирует сразу в нескольких странах Азии. Госпитализаций пока немного, состав вакцин признали подходящим."),
    ("auto", "medium", "BYD снижает цены в Европе", "BYD объявил о скидках на линейку электромобилей в Европе. Конкуренция с китайскими брендами обостряется на фоне роста продаж и новых пошлин. Аналитики ждут снижения маржинальности всего рынка."),
    ("crypto", "trap", "Цифровое золото подешевело после заявлений центробанка", "Цена главной цифровой валюты опустилась на 8% после жёстких комментариев регулятора. Инвесторы фиксируют прибыль, фонды сообщили об оттоке средств на прошлой неделе."),
    ("space", "trap", "Пуск носителя с телекоммуникационным аппаратом перенесён", "Стартовая команда отложила отправку на орбиту из-за замечаний по системе ориентации. Аппарат должен пополнить группировку связи на низкой орбите, новая дата уточняется."),
    ("cyber", "trap", "Злоумышленники зашифровали серверы водоканала", "Вирус-шифровальщик парализовал диспетчерские системы коммунального предприятия. Специалисты восстанавливают данные из резервных копий, следователи ищут путь проникновения."),
    ("sport", "trap", "Пенальти в компенсированное время принёс победу", "Гости вели в счёте, но хозяева отыгрались и вырвали победу с одиннадцати метров на последней добавленной минуте. Тренер проигравших остался недоволен судейством."),
    ("energy", "trap", "Котельные региона переходят на голубое топливо", "Коммунальные предприятия завершают перевод топливных котлов с угля на природный газ. Ожидается снижение выбросов и стоимости тепла для жителей микрорайонов."),
    ("ai", "trap", "Нейросеть научилась вести переговоры о скидках", "Компания внедрила языкового агента, который самостоятельно переписывается с поставщиками и выбивает лучшие условия. В пилоте система закрыла треть сделок без участия человека."),
    (None, "negative", "Пошаговый рецепт борща со свёклой", "Свёклу натереть на крупной тёрке, обжарить с томатной пастой и добавить в бульон вместе с капустой. Отдельно сварить картофель, в конце заправить толчёным чесноком и зеленью, дать настояться полчаса."),
    (None, "negative", "Выращивание рассады томатов на балконе", "Семена высевают в рыхлый грунт, после появления двух листьев делают пикировку. Рассаде нужна подсветка 14 часов в сутки и подкормка дрожжевым настоем раз в две недели."),
    (None, "negative", "Выбираем шпаклёвку для стен под покраску", "Гипсовые смеси дешевле и легко шлифуются, но боятся влаги. Полимерные шпаклёвки дают более гладкую поверхность и подходят под тонкослойную покраску."),
]


@dataclass
class TimingEmbedding(EmbeddingProvider):
    inner: EmbeddingProvider
    latencies: list[float] = field(default_factory=list)

    @property
    def max_chars(self) -> int:
        return self.inner.max_chars

    @property
    def dimensions(self) -> int:
        return self.inner.dimensions

    def truncate(self, text: str) -> str:
        return self.inner.truncate(text)

    async def embed(self, text: str) -> list[float]:
        started = time.perf_counter()
        result = await self.inner.embed(text)
        self.latencies.append((time.perf_counter() - started) * 1000)
        return result


def build_settings(model: str, dims: int) -> Settings:
    return Settings(
        embeddings=EmbeddingsConfig(
            base_url=OLLAMA_URL,
            model=model,
            dimensions=dims,
            max_chars=6000,
            timeout_seconds=300.0,
            retries=2,
        ),
        database=DatabaseConfig(dsn=DB_DSN),
        context=ContextConfig(window_days=14, top_k=3, min_similarity=0.7),
    )


class ThreadedEmbedding(EmbeddingProvider):
    """EmbeddingProvider with an explicit num_thread option for CPU-only instances."""

    def __init__(self, cfg: EmbeddingsConfig, http: httpx.AsyncClient, num_thread: int | None) -> None:
        super().__init__(cfg, http)
        self._thread = num_thread

    async def embed(self, text: str) -> list[float]:
        if not self._thread:
            return await super().embed(text)
        payload = {
            "model": self._cfg.model,
            "input": self.truncate(text),
            "options": {"num_thread": self._thread},
        }

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
            raise EmbeddingError(f"unexpected ollama embed response: {list(data.keys())}")

        return await with_retries(
            call, attempts=self._cfg.retries, what="ollama.embed", exceptions=(httpx.HTTPError, EmbeddingError)
        )


async def reset_schema(pool, dims: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DROP TABLE IF EXISTS processing_log, post_references, post_images, posts, news CASCADE"
        )
    ddl = _schema_ddl(dims)
    if dims > 2000:
        ddl = "\n".join(line for line in ddl.splitlines() if "USING hnsw" not in line)
    async with pool.acquire() as conn:
        await conn.execute(ddl)


async def unload_others(active: str) -> None:
    async with httpx.AsyncClient() as client:
        for _ in range(60):
            loaded = (await client.get(OLLAMA_URL + "/api/ps")).json().get("models", [])
            others = [m["name"] for m in loaded if m["name"] != active]
            if not others:
                return
            for name in others:
                await client.post(
                    OLLAMA_URL + "/api/generate", json={"model": name, "keep_alive": 0}
                )
            await asyncio.sleep(1)


async def warn_if_cpu_offload(client: httpx.AsyncClient, model: str) -> None:
    for entry in (await client.get(OLLAMA_URL + "/api/ps")).json().get("models", []):
        if entry["name"] == model and entry["size_vram"] < 0.95 * entry["size"]:
            print(
                f"WARNING: {model} offloaded to CPU "
                f"({entry['size_vram'] / 1e9:.1f} of {entry['size'] / 1e9:.1f} GB in VRAM)"
            )


async def run_model(model: str, dims: int, instruct: bool) -> dict:
    await unload_others(model)
    settings = build_settings(model, dims)
    pool = await create_pool(DB_DSN, min_size=1, max_size=2)
    await reset_schema(pool, dims)

    http = httpx.AsyncClient(timeout=httpx.Timeout(300.0))
    num_thread = int(os.environ.get("NUM_THREAD", "0")) or None
    timing = TimingEmbedding(inner=ThreadedEmbedding(settings.embeddings, http, num_thread))
    search = ContextSearch(settings, pool, timing)

    corpus_emb = time.perf_counter()
    post_ids: dict[int, str] = {}
    corpus_embeddings: dict[int, list[float]] = {}
    checked = False
    for topic, texts in TOPICS.items():
        for i, text in enumerate(texts):
            emb = await timing.embed(text)
            if not checked:
                await warn_if_cpu_offload(http, model)
                checked = True
            news_id = await repo.add_news(
                pool,
                source="bench",
                external_id=f"{topic}-{i}",
                title=text[:80],
                text=text,
                url="https://example.invalid/" + topic,
                full_text_fetched=True,
                rss_image_urls=None,
                published_at=None,
                status=NewsStatus.published,
            )
            post_id = await repo.insert_post(pool, news_id, text, PostStatus.published)
            await repo.update_post_published(
                pool, post_id, tg_message_id=0, tg_url=None, embedding=emb
            )
            post_ids[post_id] = topic
            corpus_embeddings[post_id] = emb
    corpus_seconds = time.perf_counter() - corpus_emb

    rows_out = []
    for topic, difficulty, title, text in QUERIES:
        body = (INSTRUCT + title + "\n" + text) if instruct else f"{title}\n{text}"
        started = time.perf_counter()
        emb = await timing.embed(body)
        query_seconds = time.perf_counter() - started
        news = News(
            id=0,
            source="bench",
            external_id="q",
            title=title,
            text=text,
            url="https://example.invalid/q",
            embedding=emb,
        )
        hits = await search.find(news)
        production = [post_ids[h.id] for h in hits]
        sims = {pid: cosine(emb, cemb) for pid, cemb in corpus_embeddings.items()}
        ranked = sorted(sims.items(), key=lambda kv: kv[1], reverse=True)
        rel_max = max((s for pid, s in ranked if post_ids[pid] == topic), default=0.0)
        dis_max = max((s for pid, s in ranked if post_ids[pid] != topic), default=0.0)
        raw_top = [post_ids[pid] for pid, _ in ranked[:3]]
        rows_out.append(
            {
                "topic": topic,
                "difficulty": difficulty,
                "raw_top3": raw_top,
                "top1_hit": bool(topic) and raw_top[0] == topic,
                "top3_hit": bool(topic) and topic in raw_top,
                "production_hit": bool(topic) and topic in production,
                "production_empty": len(production) == 0,
                "rel_max": rel_max,
                "dis_max": dis_max,
                "pass07": topic is not None and rel_max >= 0.7,
                "fp_count_07": sum(1 for s in sims.values() if s >= 0.7) if topic is None else None,
                "query_seconds": round(query_seconds, 3),
            }
        )

    lat = timing.latencies
    pos = [r for r in rows_out if r["topic"] is not None]
    neg = [r for r in rows_out if r["topic"] is None]
    summary = {
        "model": model,
        "dims": dims,
        "instruct": instruct,
        "top1_acc": mean([r["top1_hit"] for r in pos]),
        "top3_acc": mean([r["top3_hit"] for r in pos]),
        "production_hit_rate": mean([r["production_hit"] for r in pos]),
        "pass_07_rate": mean([r["pass07"] for r in pos]),
        "negatives_with_hits": sum(1 for r in neg if r["fp_count_07"]),
        "mean_rel_sim": mean([r["rel_max"] for r in pos]),
        "mean_dis_sim": mean([r["dis_max"] for r in pos]),
        "margin": mean([r["rel_max"] for r in pos]) - mean([r["dis_max"] for r in pos]),
        "embed_ms_median": round(statistics.median(lat), 1),
        "embed_ms_mean": round(statistics.mean(lat), 1),
        "corpus_embed_seconds": round(corpus_seconds, 2),
        "queries": rows_out,
    }
    await http.aclose()
    await pool.close()
    return summary


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def mean(xs: list[bool | float | None]) -> float:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 3) if xs else 0.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instruct", action="store_true")
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    async def run_all() -> list[dict]:
        results = []
        for model, dims in MODELS.items():
            print(f"=== {model} (dims={dims}, instruct={args.instruct}) ===", flush=True)
            results.append(await run_model(model, dims, args.instruct))
        return results

    results = asyncio.run(run_all())

    header = [
        "model", "top1", "top3(raw)", "module-hit", "pass@0.7", "rel-sim",
        "dis-sim", "margin", "embed-ms(med)", "fp@0.7(neg)",
    ]
    print("\n" + " | ".join(header))
    print("-" * 100)
    for r in results:
        print(
            f"{r['model']} | {r['top1_acc']:.2f} | {r['top3_acc']:.2f} | "
            f"{r['production_hit_rate']:.2f} | {r['pass_07_rate']:.2f} | "
            f"{r['mean_rel_sim']:.3f} | {r['mean_dis_sim']:.3f} | {r['margin']:.3f} | "
            f"{r['embed_ms_median']} | {r['negatives_with_hits']}/3"
        )
    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
