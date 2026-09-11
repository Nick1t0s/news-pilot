from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass, field
from typing import Any

CITIES = ["Москве", "Казани", "Сочи", "Екатеринбурге", "Новосибирске", "Нижнем Новгороде", "Перми", "Тюмени"]
COMPANIES = ["Роботекс", "АйТиСфера", "Гравитон", "Синтекс", "Орбита Лаб", "Полюс Тех"]
PRODUCTS = ["дрон-доставщик", "промышленный робот", "нейросеть для проектирования", "беспилотный трактор", "медробот"]
TEAMS = ["Заря", "Спутник", "Метеор", "Авангард", "Торпедо", "Динамо"]


def _rand_range(rng: random.Random, low: int, high: int) -> int:
    return rng.randint(low, high)


@dataclass
class Article:
    uid: int
    topic_key: str
    params: dict[str, Any]
    title: str
    summary: str
    body: list[str]
    published_at: dt.datetime
    image: bool = False
    guid: str = ""
    link: str = ""

    def full_text(self) -> str:
        return "\n\n".join(self.body)


@dataclass
class EmulatorState:
    base_url: str
    scenario: str
    rng: random.Random = field(default_factory=random.Random)
    articles: list[Article] = field(default_factory=list)
    next_uid: int = 1


TOPICS: list[dict[str, Any]] = [
    {
        "key": "metro",
        "title": "В {city} открыли новую линию метро",
        "summary": "В {city} открыли новую линию метро длиной {length} км с {stations} станциями.",
        "body": [
            "В {city} открыли новую линию метро длиной {length} км с {stations} станциями. Строительство шло {months} месяцев.",
            "Пассажирам обещают экономию до {minutes} минут в пути. Проект обошёлся в {amount} млрд рублей.",
            "Ожидается, что новым транспортом будут пользоваться около {riders} тысяч человек в сутки.",
        ],
        "params": {
            "length": (5, 24),
            "stations": (3, 12),
            "months": (12, 48),
            "minutes": (10, 40),
            "amount": (20, 300),
            "riders": (30, 200),
        },
        "continuation_title": "{city}, линия метро: стали известны первые итоги работы",
        "continuation_summary": "Через {days} дней после запуска новой линии метро в {city} подвели первые итоги.",
        "continuation_body": [
            "Через {days} дней после запуска новой линии метро в {city} подвели первые итоги.",
            "Средний пассажиропоток составил {riders} тысяч человек в сутки — это {pct}% от прогноза.",
            "Смета выросла на {growth}% из-за удорожания материалов.",
        ],
    },
    {
        "key": "funding",
        "title": "{company} привлекла {amount} млн долларов инвестиций",
        "summary": "{company} привлекла {amount} млн долларов инвестиций при оценке {valuation} млн.",
        "body": [
            "{company} привлекла {amount} млн долларов инвестиций. Оценка компании достигла {valuation} млн долларов.",
            "Раунд возглавил фонд {fund}. Деньги пойдут на расширение производства.",
            "Компания планирует удвоить штат в течение {months} месяцев.",
        ],
        "params": {
            "amount": (5, 120),
            "valuation": (50, 900),
            "months": (6, 24),
        },
        "continuation_title": "{company}: что изменилось после раунда инвестиций",
        "continuation_summary": "{company} рассказала, на что потратит {amount} млн долларов, привлечённых в раунде.",
        "continuation_body": [
            "{company} раскрыла планы по потраченным {amount} млн долларов инвестиций.",
            "Компания открыла вторую площадку и наняла {hired} инженеров.",
            "Выручка за квартал выросла на {pct}%.",
        ],
    },
    {
        "key": "product",
        "title": "{company} представила {product}",
        "summary": "{company} показала {product} — продажи начнутся в {month}.",
        "body": [
            "{company} представила {product}. Устройство получило аккумулятор на {hours} часов автономности.",
            "Цена составит {price} тысяч рублей. Предзаказы откроются в {month}.",
            "Производитель рассчитывает поставить {units} тысяч единиц за первый год.",
        ],
        "params": {
            "hours": (4, 48),
            "price": (9, 300),
            "units": (5, 90),
        },
        "continuation_title": "{product} от {company}: предзаказы побили рекорды",
        "continuation_summary": "За первые {days} дней {company} собрала {units2} тысяч предзаказов на {product}.",
        "continuation_body": [
            "За первые {days} дней {company} собрала {units2} тысяч предзаказов на {product}.",
            "Производство загружено до конца года, сроки доставки сдвинулись.",
            "Часть функций отложили на следующее обновление.",
        ],
    },
    {
        "key": "sport",
        "title": "{team} обыграла {team2} со счётом {score}",
        "summary": "{team} победила {team2} {score} в матче чемпионата.",
        "body": [
            "{team} обыграла {team2} со счётом {score} в очередном туре чемпионата.",
            "Первый гол забили на {minute} минуте. Победный — в добавленное время.",
            "{team} поднялась на {place}-е место в турнирной таблице.",
        ],
        "params": {
            "minute": (2, 88),
            "place": (1, 16),
        },
        "continuation_title": "{team}: последствия матча с {team2}",
        "continuation_summary": "После победы над {team2} ({score}) {team} готовится к следующему туру.",
        "continuation_body": [
            "После победы над {team2} ({score}) {team} остаётся на {place}-м месте.",
            "Тренер подтвердил, что лучший игрок матча пропустит следующую игру из-за повреждения.",
            "Билеты на домашний матч распроданы за {hours} часов.",
        ],
    },
]

FUNDS = ["Северный рассвет", "Индустрия Капитал", "Технопарк Партнерс", "Вектор Групп"]
MONTHS_RU = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def fill(template: str, params: dict[str, Any]) -> str:
    return template.format(**params)


def make_new(state: EmulatorState) -> Article:
    rng = state.rng
    topic = rng.choice(TOPICS)
    city = rng.choice(CITIES)
    company = rng.choice(COMPANIES)
    team = rng.choice(TEAMS)
    team2 = rng.choice([t for t in TEAMS if t != team])
    params: dict[str, Any] = {
        "city": city,
        "company": company,
        "fund": rng.choice(FUNDS),
        "month": rng.choice(MONTHS_RU),
        "product": rng.choice(PRODUCTS),
        "team": team,
        "team2": team2,
    }
    for name, (low, high) in topic["params"].items():
        params[name] = _rand_range(rng, low, high)
    params["score"] = f"{rng.randint(0, 4)}:{rng.randint(0, 3)}"
    title = fill(topic["title"], params)
    summary = fill(topic["summary"], params)
    body = [fill(part, params) for part in topic["body"]]
    return _build(state, topic["key"], params, title, summary, body)


def make_duplicate(state: EmulatorState) -> Article | None:
    recent = state.articles[-5:]
    if not recent:
        return make_new(state)
    rng = state.rng
    source = rng.choice(recent)
    title = rephrase(source.title, rng)
    summary = rephrase(source.summary, rng)
    body = [rephrase(paragraph, rng) for paragraph in source.body]
    return _build(state, source.topic_key, dict(source.params), title, summary, body)


def make_developing(state: EmulatorState) -> Article | None:
    older = state.articles[:-2] if len(state.articles) > 2 else []
    if not older:
        return make_new(state)
    rng = state.rng
    source = rng.choice(older)
    topic = next(t for t in TOPICS if t["key"] == source.topic_key)
    params = dict(source.params)
    for name, (low, high) in topic["params"].items():
        params[name] = _rand_range(rng, low, high)
    params["days"] = rng.randint(3, 30)
    params["pct"] = rng.randint(40, 140)
    params["growth"] = rng.randint(2, 25)
    params["hired"] = rng.randint(10, 200)
    params["hours"] = rng.randint(2, 12)
    params["units2"] = rng.randint(5, 120)
    title = fill(topic["continuation_title"], params)
    summary = fill(topic["continuation_summary"], params)
    body = [fill(part, params) for part in topic["continuation_body"]]
    return _build(state, source.topic_key, params, title, summary, body)


def make_mixed(state: EmulatorState) -> Article:
    rng = state.rng
    roll = rng.random()
    if roll < 0.55:
        return make_new(state)
    if roll < 0.8:
        return make_duplicate(state)
    return make_developing(state)


SYNONYMS = [
    ("открыли", "ввели в эксплуатацию"),
    ("запустили", "запустили в работу"),
    ("представила", "показала"),
    ("обыграла", "победила"),
    ("обошёлся", "стоил"),
    ("обещают", "гарантируют"),
    ("рассчитывает", "ожидает"),
    ("составил", "достиг"),
    ("привлекла", "получила"),
    ("производитель", "компания-изготовитель"),
    ("пассажиропоток", "поток пассажиров"),
]


def rephrase(text: str, rng: random.Random) -> str:
    result = text
    for original, replacement in SYNONYMS:
        if original in result and rng.random() < 0.6:
            result = result.replace(original, replacement, 1)
    return result


def _build(state: EmulatorState, topic_key: str, params: dict, title: str, summary: str, body: list[str]) -> Article:
    uid = state.next_uid
    state.next_uid += 1
    with_images = state.scenario != "random" and uid % 4 == 0
    article = Article(
        uid=uid,
        topic_key=topic_key,
        params=params,
        title=title,
        summary=summary,
        body=body,
        published_at=dt.datetime.now(dt.timezone.utc),
        image=with_images,
        guid=f"{state.base_url}/article/{uid}",
        link=f"{state.base_url}/article/{uid}",
    )
    state.articles.append(article)
    return article
