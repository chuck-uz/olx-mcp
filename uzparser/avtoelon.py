"""Парсер avtoelon.uz — объявления о продаже машин в Узбекистане.

Работает прямо с машины пользователя, а не через сервер парсеров: avtoelon отдаёт объявления только
узбекским IP (с сервера в США — 404). Защиты от ботов нет, страницы — обычный HTML, поэтому хватает urllib.

Каждая карточка в выдаче — HTML (цена, год, описание, город, дата) плюс JSON в listing.items.push(...)
(марка, модель, цена в у.е., ссылка). По 20 объявлений на страницу, ?page=N, потолка нет.
"""
from __future__ import annotations

import html
import json
import os
import re
import statistics
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

BASE = "https://avtoelon.uz"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
PER_PAGE = 20
MAX_ITEMS = 5000
SORTS = {"new": "add_date-desc", "old": "add_date-asc", "cheap": "price-asc", "expensive": "price-desc",
         "year_new": "year-desc", "year_old": "year-asc"}


class AvtoelonError(Exception):
    pass


def data_dir() -> Path:
    base = os.environ.get("UZPARSER_DATA") or os.environ.get("OLX_MCP_DATA")
    if not base:
        base, old = Path.home() / ".uzparser", Path.home() / ".olx-mcp"
        if old.exists() and not base.exists():  # проект переименован: переносим старые выгрузки
            old.rename(base)
    d = Path(base) / "avtoelon"
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_url(query: str, price_from=None, price_to=None, year_from=None, year_to=None, sort=None) -> str:
    """Ссылка avtoelon или «марка модель» латиницей → URL выдачи с фильтрами (цена — в у.е.)."""
    q = query.strip()
    if not q:
        raise AvtoelonError("Пустой запрос")
    if re.match(r"^(https?://)?(www\.)?avtoelon\.uz(/|$)", q, re.I):
        u = urlsplit(q if "://" in q else "https://" + q)
        if u.path.startswith("/a/show/"):
            raise AvtoelonError("Это ссылка на одно объявление — нужна ссылка на список (марка, модель, фильтры)")
        path, params = u.path or "/avto/", dict(parse_qsl(u.query))
    elif "://" in q or re.match(r"^[\w.-]+\.[a-z]{2,}/", q, re.I):
        raise AvtoelonError(f"Это не ссылка на avtoelon.uz: «{q}»")
    else:
        words = re.findall(r"[a-z0-9]+", q.lower())
        if not words or re.search(r"[а-яё]", q.lower()):
            raise AvtoelonError("Укажите марку и модель латиницей («chevrolet cobalt», «kia k5») или ссылку avtoelon.uz: "
                                "поиск по произвольному тексту сайт не поддерживает")
        path = "/avto/" + words[0] + "/" + ("-".join(words[1:]) + "/" if len(words) > 1 else "")
        params = {}
    params.pop("page", None)
    for key, val in (("price[from]", price_from), ("price[to]", price_to), ("year[from]", year_from), ("year[to]", year_to)):
        if val:
            params[key] = str(int(val))
    if sort:
        if sort not in SORTS:
            raise AvtoelonError(f"sort: одно из {', '.join(SORTS)}")
        params["sort_by"] = SORTS[sort]
    return urlunsplit(("https", "avtoelon.uz", path, urlencode(params), ""))


def _page_url(url: str, page: int) -> str:
    u = urlsplit(url)
    params = dict(parse_qsl(u.query))
    if page > 1:
        params["page"] = str(page)
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(params), ""))


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _num(s: str) -> int | None:
    d = re.sub(r"\D", "", s or "")
    return int(d) if d else None


FUELS = ("газ-бензин", "бензин", "дизель", "гибрид", "электро", "газ", "метан", "пропан")
BODIES = ("седан", "хэтчбек", "универсал", "внедорожник", "кроссовер", "минивэн", "купе", "пикап", "лифтбек",
          "кабриолет", "фургон", "микроавтобус", "микровэн", "родстер", "тарга", "лимузин")


def parse_desc(desc: str) -> dict:
    """«1.5 л, Бензин, 97 000 км, Белый, Седан, КПП Механика, …» → поля; остальное — опции и текст продавца."""
    out = {"engine_l": None, "fuel": None, "mileage_km": None, "body": None, "gearbox": None}
    for part in [p.strip() for p in desc.split(",") if p.strip()]:
        low = part.lower()
        if out["engine_l"] is None and re.fullmatch(r"\d+(?:[.,]\d+)?\s*л", low):
            out["engine_l"] = float(low.rstrip("л ").replace(",", "."))
        elif out["mileage_km"] is None and re.fullmatch(r"[\d\s]+км", low):
            out["mileage_km"] = _num(low)
        elif out["fuel"] is None and low in FUELS:
            out["fuel"] = part
        elif out["body"] is None and low in BODIES:
            out["body"] = part
        elif out["gearbox"] is None and low.startswith("кпп "):
            out["gearbox"] = part[4:].strip()
    return out


PUSH_RE = re.compile(r"listing\.items\.push\((\{.*?\})\);", re.S)
CARD_RE = re.compile(r'<div\s+data-id="(\d+)"\s+id="advert-\d+"(.*?)(?=<div\s+data-id="\d+"\s+id="advert-|<script type="text/javascript">\s*listing\.items|$)', re.S)


def parse_page(page_html: str) -> tuple[list[dict], int | None]:
    """Объявления страницы и общее число по запросу (из разметки schema.org)."""
    meta = {}
    for m in PUSH_RE.finditer(page_html):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        aid = _num((d.get("url") or "").rsplit("/", 1)[-1])
        if aid:
            meta[aid] = d
    items = []
    for m in CARD_RE.finditer(page_html):
        aid, c = int(m.group(1)), m.group(2)
        d = meta.get(aid, {})
        attrs = d.get("attributes") or {}
        title = _text((re.search(r'class="js__advert-link"[^>]*>(.*?)</a>', c, re.S) or [None, ""])[1])
        price_raw = _text((re.search(r'<span class="price">(.*?)</span>\s*</div>', c, re.S) or [None, ""])[1])
        price_raw = price_raw.replace("Цена:", "").strip()
        badges = [_text(b) for b in re.findall(r'class="(?:payment-package-corner__badge-text|badge__text)"[^>]*>(.*?)</', c, re.S)]
        # в описании бывает вложенный блок с меткой «Торг есть» — берём всё до нижней строки карточки
        dm = re.search(r'<div class="desc">(.*?)<div class="a-info-bot">', c, re.S)
        desc_html = re.sub(r'(?s)<div class="badge[^"]*".*?</div>', " ", dm.group(1) if dm else "")
        year = _num((re.search(r'class="year">(.*?)</span>', desc_html, re.S) or [None, ""])[1])
        rest = re.sub(r"^\d{4}\s*г\.,?\s*", "", _text(re.sub(r'(?s)<span class="year">.*?</span>', " ", desc_html)))
        rest = re.sub(r"^Торг есть\s*", "", rest)
        specs = parse_desc(rest)
        # «Аренда 320 y.e./мес» — машина сдаётся/в лизинге: цена в объявлении не рыночная
        rent = re.search(r"Аренда\s*~?\s*([\d\s]+)\s*y\.e\./мес", rest)
        img = re.search(r'<img[^>]+src="(https://[^"]+\.webp)"', c)
        items.append({
            "id": aid,
            "url": f"{BASE}/a/show/{aid}",
            "title": title or " ".join(x for x in (attrs.get("brand"), attrs.get("model")) if x),
            "brand": attrs.get("brand"), "model": attrs.get("model"),
            "price_usd": d.get("unitPrice") or _num(price_raw),
            "price": price_raw or None,
            "price_converted": price_raw.startswith("~"),  # «~» — продавец указал цену в сумах
            "year": year,
            **specs,
            "description": rest,
            "city": _text((re.search(r'class="a-info-text__region"[^>]*>(.*?)</a>', c, re.S) or [None, ""])[1]) or None,
            "date": _text((re.search(r'class="date">(.*?)</span>', c, re.S) or [None, ""])[1]) or None,
            "updated": d.get("lastUpdate"),
            "views": _num((re.search(r'class="nb-views">(.*?)</span>', c, re.S) or [None, ""])[1]),
            "photos": d.get("photos"),
            "photo": re.sub(r"-\d+x\d+\.webp$", "-full.webp", img.group(1)) if img else None,
            "bargain": any("торг" in b.lower() for b in badges),
            "rent_usd_month": _num(rent.group(1)) if rent else None,
            "badges": badges,
        })
    total = re.search(r'"offerCount":"(\d+)"', page_html)
    return items, (int(total.group(1)) if total else None)


def http_get(url: str, timeout: int = 30) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except urllib.error.URLError as e:
        raise AvtoelonError(f"avtoelon.uz недоступен: {e.reason}") from None


def fetch(url: str, limit: int = 100, delay: float = 1.0, get: Callable[[str], tuple[int, str]] = http_get,
          sleep: Callable[[float], None] = time.sleep,
          progress: Callable[[int, int | None], None] | None = None) -> tuple[list[dict], int | None]:
    """Объявления по URL выдачи, без повторов (VIP-объявления повторяются на страницах)."""
    limit = max(1, min(limit, MAX_ITEMS))
    res: dict[int, dict] = {}
    total = None
    page = 1
    while len(res) < limit:
        if page > 1:
            sleep(delay)
        status, body = get(_page_url(url, page))
        if status == 404 and page == 1:
            raise AvtoelonError("avtoelon ответил 404: такой страницы нет или ваш IP не из Узбекистана "
                                "(сайт показывает объявления только узбекским адресам)")
        if status != 200:
            break  # за последней страницей сайт делает 301 на первую
        items, t = parse_page(body)
        total = t if t is not None else total
        new = [i for i in items if i["id"] not in res]
        for i in new[: limit - len(res)]:
            res[i["id"]] = i
        if progress:
            progress(len(res), total)
        if not items or not new or (total is not None and page * PER_PAGE >= total):
            break
        page += 1
    return list(res.values()), total


def build_report(items: list[dict], url: str, total: int | None) -> dict:
    prices = sorted(i["price_usd"] for i in items if i["price_usd"])
    years = [i["year"] for i in items if i["year"]]
    return {
        "source": "avtoelon", "url": url,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stats": {
            "items": len(items), "total_on_site": total,
            "price_usd": {"min": prices[0], "median": statistics.median(prices), "max": prices[-1]} if prices else None,
            "years": f"{min(years)}–{max(years)}" if years else None,
            "with_bargain": sum(1 for i in items if i["bargain"]),
            "with_rent": sum(1 for i in items if i.get("rent_usd_month")),
        },
        "items": items,
    }


def _slug(url: str) -> str:
    u = urlsplit(url)
    s = re.sub(r"[^a-z0-9]+", "_", u.path.lower().replace("/avto/", "")).strip("_")[:40]
    return "avtoelon_" + (s or "all")


def save(rep: dict) -> str:
    dump_id = f"{_slug(rep['url'])}-{datetime.now():%Y%m%d-%H%M%S}"
    (data_dir() / f"{dump_id}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    return dump_id


def load(dump_id: str) -> dict:
    if not re.fullmatch(r"avtoelon_[a-z0-9_]+-\d{8}-\d{6}", dump_id):
        raise AvtoelonError("Некорректный dump_id")
    p = data_dir() / f"{dump_id}.json"
    if not p.exists():
        raise AvtoelonError("Нет такой выгрузки")
    return json.loads(p.read_text(encoding="utf-8"))


def list_dumps(limit: int = 20) -> list[dict]:
    out = []
    for p in sorted(data_dir().glob("avtoelon_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            rep = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"dump_id": p.stem, "url": rep["url"], "fetched_at": rep["fetched_at"], **rep["stats"]})
    return out
