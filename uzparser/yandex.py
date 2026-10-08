"""Парсер Яндекс Маркета для Узбекистана (market.yandex.uz, «Market Yandex Go»).

Работает с машины пользователя, как avtoelon: узбекскому IP сайт отдаёт обычный HTML, а серверу в США
показывает капчу. Браузер не нужен — данные каждой карточки лежат JSON в атрибуте data-zone-data
(цена в сумах, старая цена, рейтинг, даты доставки, флаг isCrossBorder), характеристики — в тексте карточки.

Главное для покупателя — откуда едет товар. Большая часть ассортимента — продавцы из России
(isCrossBorder: доставка из-за рубежа, 1–3 недели, возможна пошлина); товары со складов в Узбекистане
приезжают за 1–3 дня. Отчёт делит выдачу на эти две группы.

Страницы ?page=N: первая отдаёт 8 карточек (остальные сайт подгружает скриптом), следующие — по 16.
"""
from __future__ import annotations

import html
import http.cookiejar
import json
import re
import secrets
import statistics
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    from uzparser.avtoelon import data_dir as _data_dir
except ImportError:  # запуск server.py напрямую
    from avtoelon import data_dir as _data_dir

BASE = "https://market.yandex.uz"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
MAX_ITEMS = 1000
SORTS = {"popular": "dpop", "cheap": "aprice", "expensive": "dprice", "rating": "rating"}
LOCAL_DAYS = 5  # не из-за рубежа и привезут за столько дней — «со склада в Узбекистане»


class YandexError(Exception):
    pass


def data_dir():
    return _data_dir("yandex")


def build_url(query: str, price_from=None, price_to=None, sort=None) -> str:
    """Запрос или ссылка market.yandex.uz → URL выдачи с фильтрами (цены — в сумах)."""
    q = query.strip()
    if not q:
        raise YandexError("Пустой запрос")
    if re.match(r"^(https?://)?market\.yandex\.uz(/|$)", q, re.I):
        u = urlsplit(q if "://" in q else "https://" + q)
        if u.path.startswith(("/card/", "/product")):
            raise YandexError("Это ссылка на один товар — нужна ссылка на поиск или категорию")
        path, params = u.path or "/search", dict(parse_qsl(u.query))
    elif "://" in q or re.match(r"^[\w.-]+\.[a-z]{2,}/", q, re.I):
        raise YandexError(f"Это не ссылка на market.yandex.uz: «{q}»")
    else:
        path, params = "/search", {"text": q}
    for key in ("page", "rs", "cpa"):
        params.pop(key, None)
    if price_from:
        params["pricefrom"] = str(int(price_from))
    if price_to:
        params["priceto"] = str(int(price_to))
    if sort:
        if sort not in SORTS:
            raise YandexError(f"sort: одно из {', '.join(SORTS)}")
        params["how"] = SORTS[sort]
    return urlunsplit(("https", "market.yandex.uz", path, urlencode(params), ""))


def _page_url(url: str, page: int) -> str:
    u = urlsplit(url)
    params = dict(parse_qsl(u.query))
    if page > 1:
        params["page"] = str(page)
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(params), ""))


def _int(v) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


SNIPPET_RE = re.compile(r'<div[^>]*data-zone-name="productSnippet"[^>]*>')
STOCK_RE = re.compile(r"Остал[аоси]+ь?\s+(\d+)\s*шт")


def _card_text(segment: str) -> list[str]:
    segment = re.sub(r"(?s)<(script|style|noframes)[^>]*>.*?</\1>", "", segment)
    text = html.unescape(re.sub(r"<[^>]+>", "\n", segment))
    return [x for x in (re.sub(r"[ \t   ]+", " ", line).strip() for line in text.split("\n")) if x]


def _specs(lines: list[str], title: str) -> dict:
    """«Процессор: | Intel N100 | Оперативная память: | 16 ГБ …» → {"Процессор": "Intel N100", …}."""
    out, i = {}, 0
    while i < len(lines) - 1:
        if lines[i].endswith(":") and lines[i] != title and not lines[i + 1].endswith(":"):
            out[lines[i][:-1].strip()] = lines[i + 1]
            i += 2
        else:
            i += 1
    return out


def parse_page(page_html: str, today: date | None = None) -> tuple[list[dict], int | None]:
    """Карточки страницы выдачи и общее число товаров по запросу."""
    today = today or date.today()
    starts = [m for m in SNIPPET_RE.finditer(page_html)]
    items = []
    for n, m in enumerate(starts):
        zm = re.search(r'data-zone-data="([^"]+)"', m.group(0))
        if not zm:
            continue
        try:
            z = json.loads(html.unescape(zm.group(1)))
        except ValueError:
            continue
        end = starts[n + 1].start() if n + 1 < len(starts) else m.start() + 60000
        seg = page_html[m.start():min(end, m.start() + 60000)]
        title = (z.get("title") or "").strip()
        lines = _card_text(seg)
        href = re.search(r'href="(/card/[^"?]+)', seg)
        sku = z.get("marketSku") or z.get("oskuId")
        url = BASE + href.group(1) if href else (f"{BASE}/card/x/{sku}" if sku else None)
        price_full = _int(z.get("price"))
        disc = next((p.get("priceValue") for p in z.get("additionalPrices") or []
                     if p.get("priceType") == "withDiscount"), None)
        price = _int(disc) or price_full
        deliveries = [d for d in z.get("availableDelivery") or [] if d.get("dateFrom")]
        d_from = min((d["dateFrom"] for d in deliveries), default=None)
        d_to = min((d.get("dateTo") or d["dateFrom"] for d in deliveries), default=None)
        days = (date.fromisoformat(d_from) - today).days if d_from else None
        cross = str(z.get("isCrossBorder")).lower() == "true"
        rating = z.get("rating") or {}
        stock = STOCK_RE.search(" ".join(lines))
        items.append({
            "id": str(sku or z.get("wareId")),
            "url": url,
            "title": title,
            "price": price,
            "price_full": price_full if price_full and price and price_full > price else None,
            "discount": round(100 * (1 - price / price_full)) if price and price_full and price_full > price else None,
            "cross_border": cross,
            "origin": "из-за рубежа" if cross else ("Узбекистан" if days is not None and days <= LOCAL_DAYS else "не указано"),
            "delivery": (f"{d_from} — {d_to}" if d_to and d_to != d_from else d_from),
            "delivery_days": days,
            "delivery_types": sorted({d.get("type") for d in deliveries if d.get("type")}),
            "express": bool(z.get("isExpress")),
            "rating": float(rating["rating"]) if rating.get("rating") else None,
            "reviews": _int(rating.get("gradesCount")),
            "bought": _int(rating.get("purchaseCount")),
            "stock_left": int(stock.group(1)) if stock else None,
            "specs": _specs(lines, title),
            "shop_id": z.get("shopId"),
            "sponsored": bool(z.get("sponsored")),
            "badges": [b.get("badgeTitle") for b in z.get("featureBadges") or [] if b.get("badgeTitle")],
        })
    total = re.search(r'"total":(\d+)', page_html)
    return items, (int(total.group(1)) if total else None)


class Http:
    """urllib с куками: Яндекс спокойнее к посетителю, который листает страницы с одними куками."""

    def __init__(self):
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def __call__(self, url: str, timeout: int = 30) -> tuple[int, str, str]:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9",
                                                   "Accept": "text/html,application/xhtml+xml"})
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return r.status, r.geturl(), r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, url, ""
        except urllib.error.URLError as e:
            raise YandexError(f"market.yandex.uz недоступен: {e.reason}") from None


def fetch(url: str, limit: int = 100, delay: float = 2.0, get: Callable[[str], tuple[int, str, str]] | None = None,
          sleep: Callable[[float], None] = time.sleep,
          progress: Callable[[int, int | None], None] | None = None) -> tuple[list[dict], int | None]:
    """Товары по URL выдачи, без повторов. Капча на первой странице — ошибка, на следующих — останавливаемся."""
    get = get or Http()
    limit = max(1, min(limit, MAX_ITEMS))
    res: dict[str, dict] = {}
    total = None
    page = 1
    while len(res) < limit:
        if page > 1:
            sleep(delay)
        status, final_url, body = get(_page_url(url, page))
        captcha = "showcaptcha" in final_url or "SmartCaptcha" in body[:20000]
        if page == 1 and (captcha or status != 200):
            raise YandexError(
                "Яндекс Маркет показал капчу — так бывает с не-узбекского IP (VPN) или после частых поисков. "
                "Откройте market.yandex.uz в браузере, пройдите проверку и повторите через несколько минут"
                if captcha else f"Яндекс Маркет ответил {status}")
        if captcha or status != 200:
            break
        items, t = parse_page(body)
        total = t if t is not None else total
        new = [i for i in items if i["id"] not in res]
        for i in new[: limit - len(res)]:
            res[i["id"]] = i
        if progress:
            progress(len(res), total)
        if not items or not new or (total is not None and len(res) >= total):
            break
        page += 1
    return list(res.values()), total


def _median(xs: list) -> float | None:
    return statistics.median(xs) if xs else None


def build_report(items: list[dict], url: str, total: int | None) -> dict:
    def group(rows: list[dict]) -> dict:
        prices = sorted(i["price"] for i in rows if i["price"])
        days = [i["delivery_days"] for i in rows if i["delivery_days"] is not None]
        return {"items": len(rows),
                "price_uzs": {"min": prices[0], "median": _median(prices), "max": prices[-1]} if prices else None,
                "delivery_days_median": _median(days)}

    cross = [i for i in items if i["cross_border"]]
    local = [i for i in items if not i["cross_border"]]
    q = dict(parse_qsl(urlsplit(url).query))
    return {
        "source": "yandex", "url": url, "query": q.get("text"),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stats": {
            "items": len(items), "total_on_site": total,
            **{k: v for k, v in group(items).items() if k != "items"},
            "local": group(local), "cross_border": group(cross),
            "with_discount": sum(1 for i in items if i["discount"]),
        },
        "items": items,
    }


_TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
               "a b v g d e e zh z i y k l m n o p r s t u f h ts ch sh sch _ y _ e yu ya".split()))


def _slug(url: str) -> str:
    q = dict(parse_qsl(urlsplit(url).query)).get("text") or urlsplit(url).path
    s = "".join(_TR.get(c, c) for c in q.lower())
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")[:40]
    return "yandex_" + (s or "all")


def save(rep: dict) -> str:
    # суффикс — чтобы два поиска за одну секунду не затёрли друг друга
    dump_id = f"{_slug(rep['url'])}-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
    (data_dir() / f"{dump_id}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    return dump_id


def load(dump_id: str) -> dict:
    if not re.fullmatch(r"yandex_[a-z0-9_]+-\d{8}-\d{6}(-[0-9a-f]{4})?", dump_id):
        raise YandexError("Некорректный dump_id")
    p = data_dir() / f"{dump_id}.json"
    if not p.exists():
        raise YandexError("Нет такой выгрузки")
    return json.loads(p.read_text(encoding="utf-8"))


def list_dumps(limit: int = 20) -> list[dict]:
    out = []
    for p in sorted(data_dir().glob("yandex_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            rep = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        s = rep["stats"]
        out.append({"dump_id": p.stem, "query": rep.get("query"), "url": rep["url"], "fetched_at": rep["fetched_at"],
                    "items": s["items"], "median_uzs": (s.get("price_uzs") or {}).get("median"),
                    "local": s["local"]["items"], "cross_border": s["cross_border"]["items"]})
    return out
