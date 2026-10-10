"""Парсер uybor.uz — объявления о недвижимости в Узбекистане (почти всё — Ташкент).

У сайта открытый JSON API (тот же, что у его фронтенда): /api/v1/listings с фильтрами вида operationType__eq,
category__eq, room__in, price__gte… и embed=…, чтобы вместо ID пришли названия района, махалли, улицы и метро.
Работает с любого IP, браузер и токен не нужны.

Особенности, которые учтены:
- справочник мест у сайта с дублями (у «Мирабадского района» десятки ID, «Яшнабадский» пишут и «Яшнободский»),
  поэтому фильтр по району — по названию у нас, с нормализацией написания, а не через API;
- фильтр цены работает только вместе с валютой (priceCurrency__eq): объявления в другой валюте в него не попадают;
- сортировка у API «наоборот»: -priceEquivalent — сначала дешёвые, views — сначала популярные;
- цена бывает за сотку (priceType=sot) или за м² (sqm), а не за весь объект; в данных встречается мусор
  (квартира за 9 млрд сум), поэтому в сводке — медиана.
"""
from __future__ import annotations

import json
import re
import secrets
import statistics
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlencode

try:
    from uzparser.avtoelon import data_dir as _data_dir
except ImportError:  # запуск server.py напрямую
    from avtoelon import data_dir as _data_dir

API = "https://api.uybor.uz/api/v1/listings"
SITE = "https://uybor.uz/ru/listings/"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
PAGE_SIZE = 100
MAX_ITEMS = 2000
DISTRICT_SCAN_PAGES = 40  # при фильтре по району листаем до 4000 объявлений: район фильтруется у нас
EMBED = "category,subCategory,region,city,district,zone,street,metro"

CATEGORIES = {  # название для Claude → (category, subCategory)
    "apartment": (7, None), "house": (8, None), "private_house": (8, 28), "cottage": (8, 9), "dacha": (8, 27),
    "commercial": (10, None), "office": (10, 12), "warehouse": (10, 21), "land": (11, None), "room": (26, None),
}
SORTS = {"new": None, "cheap": "-priceEquivalent", "expensive": "priceEquivalent", "popular": "views"}
REPAIRS = {"evro": "евроремонт", "sredniy": "средний", "custom": "дизайнерский", "kapital": "требует ремонта",
           "chernovaya": "черновая отделка"}
FOUNDATIONS = {"kirpich": "кирпич", "monolit": "монолит", "panel": "панель", "blok": "блок", "other": "другое"}
REGIONS = {  # основной ID региона (у сайта бывают дубли с единичными объявлениями)
    "город Ташкент": 13, "Ташкентская область": 12, "Самаркандская область": 9, "Бухарская область": 4,
    "Ферганская область": 14, "Кашкадарьинская область": 6, "Джизакская область": 510, "Навоийская область": 7,
    "Хорезмская область": 15, "Сырдарьинская область": 521, "Республика Каракалпакстан": 2,
    "Андижанская область": 3, "Наманганская область": 8, "Сурхандарьинская область": 10,
}


class UyborError(Exception):
    pass


def data_dir():
    return _data_dir("uybor")


def _norm(s: str) -> str:
    """Для сравнения названий мест: «Яшнободский» = «Яшнабадский», «Олмазор» = «Алмазар», «Мирзо» = «Мирза»."""
    s = (s or "").lower().replace("ё", "е").replace("й", "и").replace("о", "а")
    return re.sub(r"[^\w]+", " ", s).strip()


def _stems(text: str) -> list[str]:
    stop = {"раиан", "раион", "г", "город", "гарад", "область", "абласть", "улица", "массив", "махалля", "ташкент"}
    return [w[:6] for w in _norm(text).split() if len(w) > 2 and w not in stop]


def region_id(name: str) -> int:
    key = _norm(name)
    if key in ("ташкент", "tashkent", "тошкент", "город ташкент", "г ташкент"):
        return 13
    for full, rid in REGIONS.items():
        stems = _stems(full)
        if stems and all(any(w.startswith(st[:5]) or st.startswith(w[:5]) for w in key.split()) for st in stems):
            return rid
    raise UyborError(f"Не знаю регион «{name}». Есть: {', '.join(REGIONS)}")


def build_params(operation: str = "sale", category: str = "apartment", rooms: str | None = None,
                 price_from: float | None = None, price_to: float | None = None, currency: str = "usd",
                 square_from: float | None = None, square_to: float | None = None,
                 new_building: bool | None = None, repair: str | None = None, region: str | None = None,
                 sort: str | None = None) -> dict:
    if operation not in ("sale", "rent"):
        raise UyborError("operation: sale (продажа) или rent (аренда)")
    if category not in CATEGORIES:
        raise UyborError(f"category: одно из {', '.join(CATEGORIES)}")
    cat, sub = CATEGORIES[category]
    p: dict = {"operationType__eq": operation, "category__eq": cat}
    if sub:
        p["subCategory__eq"] = sub
    if rooms:
        vals = [r.strip().lower() for r in str(rooms).replace(" ", ",").split(",") if r.strip()]
        vals = ["studio" if v in ("studio", "студия", "0") else ("6+" if v in ("6+", "6", "7", "8") else v) for v in vals]
        bad = [v for v in vals if v not in ("studio", "1", "2", "3", "4", "5", "6+")]
        if bad:
            raise UyborError("rooms: 1–5, 6+ или studio через запятую («2,3»)")
        p["room__in"] = ",".join(dict.fromkeys(vals))
    if price_from or price_to:
        if currency not in ("usd", "uzs"):
            raise UyborError("currency: usd или uzs")
        p["priceCurrency__eq"] = currency
        if price_from:
            p["price__gte"] = int(price_from)
        if price_to:
            p["price__lte"] = int(price_to)
    if square_from:
        p["square__gte"] = int(square_from)
    if square_to:
        p["square__lte"] = int(square_to)
    if new_building is not None:
        p["isNewBuilding__eq"] = "true" if new_building else "false"
    if repair:
        if repair not in REPAIRS:
            raise UyborError(f"repair: одно из {', '.join(REPAIRS)}")
        p["repair__eq"] = repair
    if region:
        p["region__eq"] = region_id(region)
    if sort:
        if sort not in SORTS:
            raise UyborError(f"sort: одно из {', '.join(SORTS)}")
        if SORTS[sort]:
            p["order"] = SORTS[sort]
    return p


def _name(obj) -> str | None:
    if isinstance(obj, dict):
        n = obj.get("name")
        return (n.get("ru") or n.get("uz") or n.get("en")) if isinstance(n, dict) else n
    return None


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f == int(f) else round(f, 2)


def normalize(r: dict) -> dict:
    prices = r.get("prices") or {}
    usd, square = _num(prices.get("usd")), _num(r.get("square"))
    per_unit = {"sot": "за сотку", "sqm": "за м²"}.get(r.get("priceType"))
    district = _name(r.get("district")) or _name(r.get("city"))
    return {
        "id": r["id"], "url": f"{SITE}{r['id']}",
        "operation": r.get("operationType"), "category": _name(r.get("category")),
        "subcategory": _name(r.get("subCategory")),
        "rooms": r.get("room"), "square": square, "land_sotka": _num(r.get("squareGround")), "floor": r.get("floor"), "floor_total": r.get("floorTotal"),
        "price": _num(r.get("price")), "currency": r.get("priceCurrency"), "price_usd": usd,
        "price_uzs": _num(prices.get("uzs")), "price_per": per_unit,
        "rent_period": {"month": "в месяц", "day": "в сутки"}.get(r.get("pricePeriodUnit")),
        "usd_per_m2": round(usd / square) if usd and square and not per_unit and r.get("operationType") == "sale" else None,
        "bargain": bool(r.get("isPriceAuction")), "new_building": r.get("isNewBuilding"),
        "repair": REPAIRS.get(r.get("repair"), r.get("repair")),
        "foundation": FOUNDATIONS.get(r.get("foundation"), r.get("foundation")),
        "region": _name(r.get("region")), "district": district, "zone": _name(r.get("zone")),
        "street": _name(r.get("street")), "address": r.get("address"), "metro": _name(r.get("metro")),
        "lat": r.get("lat"), "lng": r.get("lng"),
        "description": (r.get("description") or "").strip(),
        "photos": len(r.get("media") or []) or None,
        "views": r.get("views"), "created": (r.get("createdAt") or "")[:10], "up": (r.get("upAt") or "")[:10],
        "promoted": bool(r.get("isVip") or r.get("isPremium") or r.get("isUrgently")),
    }


def place_matches(item: dict, where: str) -> bool:
    """Все значимые слова из where есть в районе, махалле, улице, адресе или метро объявления."""
    hay = " " + _norm(" ".join(str(item.get(k) or "") for k in ("district", "zone", "street", "address", "metro"))) + " "
    words = _stems(where)
    return all(re.search(r"(?<!\w)" + re.escape(w[:5]), hay) for w in words) if words else True


def http_get(url: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", "Accept-Language": "ru"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise UyborError(f"uybor.uz ответил {e.code}") from None
    except urllib.error.URLError as e:
        raise UyborError(f"uybor.uz недоступен: {e.reason}") from None
    except ValueError:
        raise UyborError("uybor.uz вернул не JSON") from None


def fetch(params: dict, limit: int = 100, district: str | None = None,
          get: Callable[[str], dict] = http_get, sleep: Callable[[float], None] = time.sleep, delay: float = 0.3,
          progress: Callable[[int, int | None], None] | None = None) -> tuple[list[dict], int | None, int]:
    """Объявления по фильтрам; при district — листаем дальше и оставляем только этот район.
    Возвращает (объявления, всего по фильтрам API, сколько просмотрено)."""
    limit = max(1, min(int(limit), MAX_ITEMS))
    pages = DISTRICT_SCAN_PAGES if district else -(-limit // PAGE_SIZE)
    out: dict[int, dict] = {}
    total, scanned = None, 0
    for page in range(1, pages + 1):
        if page > 1:
            sleep(delay)
        d = get(f"{API}?{urlencode({**params, 'limit': PAGE_SIZE, 'page': page, 'embed': EMBED})}")
        rows = d.get("results") or []
        total = d.get("total", total)
        scanned += len(rows)
        for r in rows:
            item = normalize(r)
            if item["id"] not in out and (not district or place_matches(item, district)):
                out[item["id"]] = item
        if progress:
            progress(len(out), min(limit, total or limit) if not district else None)
        if len(out) >= limit or not rows or (total is not None and page * PAGE_SIZE >= total):
            break
    return list(out.values())[:limit], total, scanned


def build_report(items: list[dict], params: dict, district: str | None, total: int | None, scanned: int) -> dict:
    whole = [i for i in items if i["price_usd"] and not i["price_per"]]
    prices = sorted(i["price_usd"] for i in whole)
    per_m2 = sorted(i["usd_per_m2"] for i in items if i["usd_per_m2"])
    med = lambda xs: statistics.median(xs) if xs else None  # noqa: E731
    return {
        "source": "uybor", "params": params, "district": district,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stats": {
            "items": len(items), "total_on_site": total, "scanned": scanned,
            "price_usd": {"min": prices[0], "median": med(prices), "max": prices[-1]} if prices else None,
            "usd_per_m2_median": med(per_m2),
            "rooms": dict(Counter(i["rooms"] or "—" for i in items).most_common()),
            "districts": dict(Counter(i["district"] or "—" for i in items).most_common(12)),
            "new_building": sum(1 for i in items if i["new_building"]),
            "with_bargain": sum(1 for i in items if i["bargain"]),
            "price_per_unit": sum(1 for i in items if i["price_per"]),
        },
        "items": items,
    }


_TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
               "a b v g d e e zh z i y k l m n o p r s t u f h ts ch sh sch _ y _ e yu ya".split()))


def _slug(params: dict, district: str | None) -> str:
    cat = next((k for k, v in CATEGORIES.items() if v[0] == params.get("category__eq")
                and v[1] == params.get("subCategory__eq")), "all")
    parts = [params.get("operationType__eq", ""), cat, params.get("room__in", "").replace(",", "_").replace("+", "p"),
             "".join(_TR.get(c, c) for c in (district or "").lower())]
    s = re.sub(r"[^a-z0-9]+", "_", "_".join(p for p in parts if p)).strip("_")[:50]
    return "uybor_" + (s or "all")


def save(rep: dict) -> str:
    dump_id = f"{_slug(rep['params'], rep.get('district'))}-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
    (data_dir() / f"{dump_id}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    return dump_id


def load(dump_id: str) -> dict:
    if not re.fullmatch(r"uybor_[a-z0-9_]+-\d{8}-\d{6}-[0-9a-f]{4}", dump_id):
        raise UyborError("Некорректный dump_id")
    p = data_dir() / f"{dump_id}.json"
    if not p.exists():
        raise UyborError("Нет такой выгрузки")
    return json.loads(p.read_text(encoding="utf-8"))


def list_dumps(limit: int = 20) -> list[dict]:
    out = []
    for p in sorted(data_dir().glob("uybor_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            rep = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        s = rep["stats"]
        out.append({"dump_id": p.stem, "params": rep["params"], "district": rep.get("district"),
                    "fetched_at": rep["fetched_at"], "items": s["items"],
                    "median_usd": (s.get("price_usd") or {}).get("median"), "usd_per_m2_median": s.get("usd_per_m2_median")})
    return out
