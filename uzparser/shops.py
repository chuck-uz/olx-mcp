"""Магазины техники Узбекистана одним поиском: idea.uz, alifshop.uz, texnomart.uz, mediapark.uz, olcha.uz, asaxiy.uz.

У первых пяти открытый JSON API (тот же, что у их сайтов и приложений); asaxiy отдаёт только HTML-страницы
поиска, и то лишь узбекским IP (серверу за границей — проверка Cloudflare). Браузер и сервер парсеров не нужны —
запросы идут с машины пользователя через urllib, параллельно по магазинам.

Поиск в этих API нечёткий: на «mac mini» idea отдаёт зарядки «Mini», texnomart — «Яндекс Станцию Мини»,
alifshop ставит Mac mini на вторую страницу после MacBook. Поэтому магазин листается до max_pages страниц,
а в выдачу попадают только товары, в названии которых есть все слова запроса (strict=False — без фильтра).

Все товары — со складов и магазинов в Узбекистане: официальная гарантия, рассрочка, доставка за 1–3 дня.
"""
from __future__ import annotations

import html
import json
import re
import secrets
import statistics
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Iterator
from urllib.parse import quote, quote_plus

try:
    from uzparser.avtoelon import data_dir as _data_dir
except ImportError:  # запуск server.py напрямую
    from avtoelon import data_dir as _data_dir

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
MAX_PAGES = 5        # страниц на магазин по умолчанию: релевантное у них часто не на первой
PER_SHOP_MAX = 200   # совпавших товаров с одного магазина
SHOPS = ("idea", "alifshop", "texnomart", "mediapark", "olcha", "asaxiy")
NAMES = {"idea": "idea.uz", "alifshop": "alifshop.uz", "texnomart": "texnomart.uz",
         "mediapark": "mediapark.uz", "olcha": "olcha.uz", "asaxiy": "asaxiy.uz"}

Getter = Callable[..., "dict | str"]  # (url, json-тело для POST или None, raw=False) → JSON или текст (raw=True)


class ShopsError(Exception):
    pass


def data_dir():
    return _data_dir("shops")


def http_json(url: str, body: dict | None = None, timeout: int = 25, raw: bool = False) -> dict | str:
    headers = {"User-Agent": UA, "Accept": "text/html" if raw else "application/json", "Accept-Language": "ru"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode("utf-8", "replace")
            return text if raw else json.loads(text)
    except urllib.error.HTTPError as e:
        if e.code == 403 and b"Just a moment" in (e.read() or b"")[:20000]:
            raise ShopsError("закрыт проверкой Cloudflare: работает только с узбекского IP, без VPN") from None
        raise ShopsError(f"ответил {e.code}") from None
    except urllib.error.URLError as e:
        raise ShopsError(f"недоступен: {e.reason}") from None
    except ValueError:
        raise ShopsError("вернул не JSON") from None


def _int(v) -> int | None:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    return n or None


def _item(shop: str, **kw) -> dict:
    price, old = kw.get("price"), kw.get("price_old")
    if not (old and price and old > price):
        old = None
    return {"shop": shop, "id": str(kw["id"]), "title": (kw.get("title") or "").strip(), "price": price,
            "price_old": old, "discount": round(100 * (1 - price / old)) if old else None,
            "in_stock": kw.get("in_stock"), "stock_qty": kw.get("stock_qty"),
            "installment": kw.get("installment"), "rating": kw.get("rating"), "reviews": kw.get("reviews"),
            "seller": kw.get("seller"), "warranty": kw.get("warranty"), "brand": kw.get("brand"),
            "category": kw.get("category"), "specs": kw.get("specs"), "url": kw.get("url")}


# ---------- магазины: каждый — генератор страниц (товары страницы, всего найдено) ----------

def pages_idea(q: str, get: Getter, max_pages: int) -> Iterator[tuple[list[dict], int | None]]:
    for page in range(1, max_pages + 1):
        d = get(f"https://api.idea.uz/api/v2/products?search={quote(q)}&page={page}", None)
        rows = []
        for p in d.get("data") or []:
            inst = None
            if p.get("min_price_per_month_duration"):
                inst = f"от {p['min_price_per_month']:,} сум × {p['min_price_per_month_duration']} мес".replace(",", " ")
            stock = _int(p.get("in_stock"))
            rows.append(_item("idea", id=p["id"], title=p.get("title_name") or p.get("name"),
                              price=_int(p.get("current_price")), price_old=_int(p.get("old_price")),
                              in_stock=bool(stock), stock_qty=stock, installment=inst,
                              rating=float(p["rating"]) if p.get("rating") else None, reviews=_int(p.get("rating_count")),
                              brand=p.get("brand_name"), category=p.get("category_name"), url=p.get("url")))
        meta = d.get("meta") or {}
        yield rows, meta.get("total")
        if page >= (meta.get("last_page") or 0):
            return


def pages_alifshop(q: str, get: Getter, max_pages: int) -> Iterator[tuple[list[dict], int | None]]:
    for page in range(max_pages):
        d = get("https://gw.alifshop.uz/web/client/search/full-text", {"query": q, "page": page})
        rows = []
        for i in d.get("items") or []:
            cond = i.get("condition") or {}
            qty = _int(i.get("quantity"))
            rows.append(_item("alifshop", id=i["id"], title=(i.get("product") or {}).get("name"),
                              price=(_int(i.get("price")) or 0) // 100 or None,  # цены — в тийинах
                              price_old=(_int(i.get("old_price")) or 0) // 100 or None,
                              in_stock=bool(qty), stock_qty=qty,
                              installment=f"рассрочка Alif до {cond['duration']} мес" if cond.get("duration") else None,
                              seller=(i.get("partner") or {}).get("slug"),
                              warranty="есть" if i.get("has_guarantee") else None, brand=i.get("brand_name"),
                              url=f"https://alifshop.uz/ru/moderated-offer/{i['slug']}" if i.get("slug") else None))
        yield rows, d.get("n_items")
        if page + 1 >= (d.get("n_pages") or 0):
            return


def pages_texnomart(q: str, get: Getter, max_pages: int) -> Iterator[tuple[list[dict], int | None]]:
    for page in range(1, max_pages + 1):
        d = (get(f"https://gw.texnomart.uz/api/common/v1/search/result?q={quote(q)}&page={page}&limit=40", None)
             .get("data") or {})
        rows = []
        for p in d.get("products") or []:
            specs = {c["name"]: c["value"] for c in p.get("main_characters") or [] if c.get("name")}
            rows.append(_item("texnomart", id=p["id"], title=p.get("name"), price=_int(p.get("sale_price")),
                              price_old=_int(p.get("old_price")),
                              in_stock={"openToCart": True}.get(p.get("availability"), None),
                              installment=p.get("axiom_monthly_price"),
                              rating=float(p["reviews_average"]) if p.get("reviews_average") else None,
                              reviews=_int(p.get("reviews_count")), specs=specs or None,
                              url=f"https://texnomart.uz/ru/product/detail/{p['id']}/"))
        pg = d.get("pagination") or {}
        yield rows, pg.get("total_count")
        if page >= (pg.get("total_page") or 0):
            return


def pages_mediapark(q: str, get: Getter, max_pages: int) -> Iterator[tuple[list[dict], int | None]]:
    for page in range(1, max_pages + 1):
        d = get(f"https://api.v2.mediapark.uz/v1/diginetica/search?query={quote(q)}&page={page}", None)
        rows = []
        for p in d.get("products") or []:
            inst = ((p.get("meta") or {}).get("installmentPrices") or [None])[0]
            cats = [c.get("name") for c in p.get("categories") or [] if c.get("name")]
            rows.append(_item("mediapark", id=p["id"], title=p.get("name"), price=_int(p.get("price")),
                              price_old=_int(p.get("oldPrice")), in_stock=bool(p.get("available")),
                              installment=f"от {int(inst):,} сум/мес".replace(",", " ") if _int(inst) else None,
                              brand=p.get("brand"), category=cats[0] if cats else None,
                              url="https://mediapark.uz" + p["linkUrl"] if p.get("linkUrl") else p.get("webUrl")))
        yield rows, _int(d.get("totalHits"))
        if page >= (_int(d.get("totalPages")) or 0):
            return


def pages_olcha(q: str, get: Getter, max_pages: int) -> Iterator[tuple[list[dict], int | None]]:
    for page in range(1, max_pages + 1):
        d = (get(f"https://mobile.olcha.uz/api/v2/products?q={quote(q)}&page={page}&per_page=40", None)
             .get("data") or {})
        rows = []
        for p in d.get("products") or []:
            total, disc = _int(p.get("total_price")), _int(p.get("discount_price"))
            price = disc if p.get("discount") and disc else total
            plan = p.get("plan") or {}
            inst = None
            if p.get("has_installment") and p.get("monthly_repayment"):
                inst = f"от {int(p['monthly_repayment']):,} сум/мес".replace(",", " ") + (
                    f", до {plan['max_period']} мес" if plan.get("max_period") else "")
            rows.append(_item("olcha", id=p["id"], title=p.get("name_ru") or p.get("name"), price=price,
                              price_old=total if price != total else None,
                              in_stock=bool(p.get("in_stock")) and not p.get("out_of_stock"),
                              stock_qty=_int(p.get("quantity")) if p.get("in_stock") else None, installment=inst,
                              rating=float(p["comments_rating"]) if _int(float(p.get("comments_rating") or 0)) else None,
                              reviews=_int(p.get("comments_count")),
                              warranty=f"{p['warranty_month']} мес" if _int(p.get("warranty_month")) else None,
                              seller=f"магазин {p['store_id']}" if p.get("store_id") else None,
                              category=((p.get("category") or {}).get("name_ru")),
                              url=f"https://olcha.uz/ru/product/view/{p['alias']}" if p.get("alias") else None))
        pg = d.get("paginator") or {}
        yield rows, pg.get("total")
        if page >= (pg.get("last_page") or 0):
            return


ASAXIY_PAGE = 24
_AX_CARD = re.compile(r'<div class="product__item d-flex')


def _ax_text(seg: str, cls: str) -> str | None:
    m = re.search(r'class="[^"]*\b' + re.escape(cls) + r'\b[^"]*"[^>]*>(.*?)</', seg, re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))).strip() if m else None


def parse_asaxiy(page: str) -> tuple[list[dict], int | None]:
    """Карточки результатов поиска asaxiy.uz (HTML) и общее число найденного (totalCount)."""
    start = page.find("loading-more-product-list")
    body = page[start:] if start >= 0 else page
    starts = [m.start() for m in _AX_CARD.finditer(body)] + [len(body)]
    rows = []
    for a, b in zip(starts, starts[1:]):
        seg = body[a:b]
        pid = re.search(r'data-product-id="(\d+)"', seg)
        href = re.search(r'<a href="(/ru/product/[^"]+)"', seg)
        title = _ax_text(seg, "product__item__info-title")
        if not (pid and title):
            continue
        inst = _ax_text(seg, "installment__price")
        reviews = re.search(r"(\d+)\s+отзыв", seg)
        stars = len(re.findall(r'class="fas fa-star"', seg)) + 0.5 * len(re.findall(r"fa-star-half", seg))
        out = "Нет в наличии" in seg or "Предзаказ" in seg
        rows.append(_item("asaxiy", id=pid.group(1), title=title,
                          price=_int(re.sub(r"\D", "", _ax_text(seg, "product__item-price") or "")),
                          price_old=_int(re.sub(r"\D", "", _ax_text(seg, "product__item-old--price") or "")),
                          in_stock=not out, installment=("от " + inst.replace(" x ", " × ")) if inst else None,
                          # без отзывов сайт рисует пять звёзд-заглушек — такой рейтинг не считаем
                          rating=stars if reviews and _int(reviews.group(1)) and stars else None,
                          reviews=_int(reviews.group(1)) if reviews else None,
                          specs={"Статус": "предзаказ"} if "Предзаказ" in seg else None,
                          url="https://asaxiy.uz" + href.group(1) if href else None))
    total = re.search(r'"totalCount":(\d+)', page)
    return rows, int(total.group(1)) if total else None


def pages_asaxiy(q: str, get: Getter, max_pages: int) -> Iterator[tuple[list[dict], int | None]]:
    """totalCount у asaxiy ненадёжен (на «iphone 15» — 6 при 24 карточках), поэтому конец выдачи узнаём иначе:
    за последней страницей сайт перенаправляет на первую, и новых товаров на ней нет."""
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        path = "/ru/product" if page == 1 else f"/ru/product/page={page}"
        rows, total = parse_asaxiy(get(f"https://asaxiy.uz{path}?key={quote_plus(q)}", None, raw=True))
        new = [r for r in rows if r["id"] not in seen]
        seen.update(r["id"] for r in rows)
        yield new, total
        if len(new) < ASAXIY_PAGE:
            return


PAGERS = {"idea": pages_idea, "alifshop": pages_alifshop, "texnomart": pages_texnomart,
          "mediapark": pages_mediapark, "olcha": pages_olcha, "asaxiy": pages_asaxiy}


# ---------- релевантность ----------

def _norm(s: str) -> str:
    return re.sub(r"[^\w]+", " ", (s or "").lower().replace("ё", "е")).strip()


def query_words(q: str) -> list[str]:
    return [w for w in _norm(q).split() if len(w) > 1 or w.isdigit()]


def matches(title: str, words: list[str]) -> bool:
    """Все слова запроса есть в названии (как отдельные слова или начало слова: «mini» ≈ «mini-пк»)."""
    t = " " + _norm(title) + " "
    return all(re.search(r"(?<!\w)" + re.escape(w), t) for w in words)


# ---------- поиск ----------

def search_shop(shop: str, q: str, limit: int, get: Getter, max_pages: int = MAX_PAGES, strict: bool = True,
                delay: float = 0.5, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Один магазин: листаем страницы, пока не наберём limit совпадений или не кончится max_pages."""
    words = query_words(q)
    found, seen, scanned, total, others = [], set(), 0, None, []
    try:
        for n, (rows, t) in enumerate(PAGERS[shop](q, get, max_pages)):
            total = t if t is not None else total
            scanned += len(rows)
            for r in rows:
                if r["id"] in seen or not r["price"]:
                    continue
                seen.add(r["id"])
                if not strict or matches(r["title"], words):
                    found.append(r)
                elif len(others) < 5:
                    others.append(r["title"])
            if len(found) >= limit or not rows:
                break
            sleep(delay)
    except ShopsError as e:
        return {"shop": shop, "error": f"{NAMES[shop]} {e}", "items": found[:limit], "scanned": scanned,
                "total": total, "others": others}
    return {"shop": shop, "error": None, "items": found[:limit], "scanned": scanned, "total": total, "others": others}


def search(q: str, shops: list[str] | None = None, limit: int = 50, max_pages: int = MAX_PAGES, strict: bool = True,
           price_from: float | None = None, price_to: float | None = None, get: Getter = http_json,
           sleep: Callable[[float], None] = time.sleep,
           progress: Callable[[int, int, str], None] | None = None) -> dict:
    """Все выбранные магазины параллельно. Возвращает отчёт: товары от дешёвых к дорогим + сводка по магазинам."""
    q = (q or "").strip()
    if not query_words(q):
        raise ShopsError("Пустой запрос")
    shops = list(shops or SHOPS)
    bad = [s for s in shops if s not in SHOPS]
    if bad:
        raise ShopsError(f"Неизвестные магазины: {', '.join(bad)}. Есть: {', '.join(SHOPS)}")
    limit = max(1, min(int(limit), PER_SHOP_MAX))
    done: list[dict] = []
    lock = threading.Lock()  # прогресс уходит в stdout MCP — из потоков по одному

    def run(shop: str) -> dict:
        res = search_shop(shop, q, limit, get, max_pages, strict, sleep=sleep)
        with lock:
            done.append(res)
            if progress:
                progress(len(done), len(shops), f"Готово {len(done)} из {len(shops)} магазинов")
        return res

    with ThreadPoolExecutor(max_workers=len(shops)) as ex:
        results = list(ex.map(run, shops))
    items = [i for r in results for i in r["items"]
             if (not price_from or i["price"] >= price_from) and (not price_to or i["price"] <= price_to)]
    items.sort(key=lambda i: (not i["in_stock"] if i["in_stock"] is not None else False, i["price"]))
    return build_report(q, items, results, strict)


def build_report(q: str, items: list[dict], results: list[dict], strict: bool) -> dict:
    def stats(rows):
        prices = sorted(i["price"] for i in rows)
        return {"min": prices[0], "median": statistics.median(prices), "max": prices[-1]} if prices else None

    per_shop = []
    for r in results:
        rows = [i for i in items if i["shop"] == r["shop"]]
        per_shop.append({"shop": NAMES[r["shop"]], "matched": len(rows), "scanned": r["scanned"],
                         "total_on_site": r["total"], "in_stock": sum(1 for i in rows if i["in_stock"]),
                         "price_uzs": stats(rows), "error": r["error"],
                         # ничего не совпало — что магазин нашёл вместо этого: подсказка переформулировать запрос
                         "unmatched_examples": r.get("others") if not rows and r.get("others") else None})
    in_stock = [i for i in items if i["in_stock"]]
    return {
        "source": "shops", "query": q, "strict": strict,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stats": {"items": len(items), "in_stock": len(in_stock), "price_uzs": stats(items),
                  "price_in_stock_uzs": stats(in_stock), "shops": per_shop},
        "items": items,
    }


_TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
               "a b v g d e e zh z i y k l m n o p r s t u f h ts ch sh sch _ y _ e yu ya".split()))


def _slug(q: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", "".join(_TR.get(c, c) for c in q.lower())).strip("_")[:40]
    return "shops_" + (s or "all")


def save(rep: dict) -> str:
    dump_id = f"{_slug(rep['query'])}-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
    (data_dir() / f"{dump_id}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    return dump_id


def load(dump_id: str) -> dict:
    if not re.fullmatch(r"shops_[a-z0-9_]+-\d{8}-\d{6}-[0-9a-f]{4}", dump_id):
        raise ShopsError("Некорректный dump_id")
    p = data_dir() / f"{dump_id}.json"
    if not p.exists():
        raise ShopsError("Нет такой выгрузки")
    return json.loads(p.read_text(encoding="utf-8"))


def list_dumps(limit: int = 20) -> list[dict]:
    out = []
    for p in sorted(data_dir().glob("shops_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            rep = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        s = rep["stats"]
        out.append({"dump_id": p.stem, "query": rep["query"], "fetched_at": rep["fetched_at"], "items": s["items"],
                    "in_stock": s["in_stock"], "median_uzs": (s.get("price_uzs") or {}).get("median")})
    return out
