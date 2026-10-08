"""uzParser — MCP-сервер для Claude: поиск по OLX.uz, Uzum Market и avtoelon.uz.

OLX и Uzum собирает сервер парсеров (там headless Chromium): MCP ставит задание в его API, ждёт и отдаёт
Claude сводку и компактный список объявлений. Полная выгрузка остаётся на сервере, её можно
дочитать порциями через olx_get_offers.

Транспорт — stdio (JSON-RPC по строкам), только стандартная библиотека Python 3.9+.

Переменные окружения:
  UZPARSER_TOKEN  API-токен сервера парсеров (выдаёт его владелец) — нужен для OLX и Uzum
  (старые имена OLX_MCP_TOKEN / OLX_MCP_URL тоже понимаются)
  UZPARSER_URL    адрес сервера парсеров
  UZPARSER_PROGRESS_DIR  куда писать прогресс поисков (по умолчанию ~/.cache/uzparser/progress;
                  пустая строка — не писать). Его читает мод uzparser-progress для Claude Code.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import quote, urlencode, urlsplit

__version__ = "0.6.0"

DEFAULT_URL = "https://tools.oresh.in"  # сервер парсеров OLX/Uzum
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
PAGE = 50          # объявлений в одном ответе Claude
TEXT_CUT = 200     # символов описания в компактном виде
MULTI_CUT = 1500   # у объявлений с несколькими позициями описание нужно целиком — там цены
MULTI_NOTE = "несколько позиций или конфигураций с разными ценами — разбери по описанию; цена объявления — только одна из них"
JOB_TIMEOUT = 1800  # 1000 объявлений ≈ 40 с, 10 000 ≈ 7 минут, плюс очередь
SEARCH_TOOLS = ("olx_search", "uzum_search", "avtoelon_search")
DEFAULT_PROGRESS_DIR = os.path.join(os.path.expanduser("~"), ".cache", "uzparser", "progress")

SORTS = {
    "new": "created_at:desc",
    "cheap": "filter_float_price:asc",
    "expensive": "filter_float_price:desc",
}

UZUM_SORTS = ["popular", "cheap", "expensive", "rating", "new"]

try:  # запуск как пакета (uzparser.server) и как файла (python server.py)
    from uzparser import avtoelon
except ImportError:  # pragma: no cover
    import avtoelon

INSTRUCTIONS = (
    "Для любых запросов про объявления на OLX.uz (olx.uz) — найти, подобрать, сравнить цены, оценить рынок, "
    "проверить продавцов — используй инструменты olx_*, а для товаров Uzum Market (uzum.uz) — uzum_*, "
    "а не веб-поиск и не загрузку страниц этих сайтов: они блокируют прямые запросы, а парсеры отдают "
    "структурированные данные. OLX — частные объявления и б/у; Uzum — новые товары магазинов с ценой по карте, "
    "скидкой, рассрочкой и рейтингом. Если просят сравнить рынок, можно собрать с обеих площадок. "
    "Если пользователь прислал ссылку на список с фильтрами — передай её как есть. "
    "Для машин (купить авто, цены на Cobalt, Gentra и т.п.) используй avtoelon_* — крупнейшую площадку "
    "авто-объявлений Узбекистана; машины на OLX тоже есть (olx_search), для полной картины можно собрать с обеих."
)

TOOLS = [
    {
        "name": "olx_search",
        "description": (
            "Ищет объявления на OLX.uz (Узбекистан) через серверный парсер и возвращает сводку по ценам "
            "и список объявлений. Используй для ЛЮБЫХ запросов про OLX вместо веб-поиска. "
            "query — поисковый запрос («iphone 15 pro», «chevrolet cobalt») или ссылка на список olx.uz "
            "с уже выставленными фильтрами (категория, город, цена). Сбор занимает ~10 с на 100 объявлений. "
            "В ответе первые 50 объявлений; остальные — через olx_get_offers с dump_id. "
            "Объявления с полем multi — магазины с ассортиментом или «конфигурация на выбор»: в описании "
            "несколько товаров со своими ценами. Разбирай их по описанию и не считай цену объявления ценой "
            "всего, что в нём перечислено; медиана в сводке посчитана без них."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Поисковый запрос или ссылка https://www.olx.uz/…"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 100,
                          "description": "Сколько объявлений собрать, до 10 000. Сверх 1000 парсер дробит поиск "
                                         "по цене: такие объявления идут от дешёвых к дорогим, без цены — не попадают. "
                                         "~40 с на 1000"},
                "price_from": {"type": "number", "description": "Цена от, в сумах"},
                "price_to": {"type": "number", "description": "Цена до, в сумах"},
                "sort": {"type": "string", "enum": list(SORTS),
                         "description": "new — сначала новые, cheap — дешёвые, expensive — дорогие. "
                                        "По умолчанию — как на сайте (по релевантности)"},
                "photos": {"type": "boolean", "default": False, "description": "Включить ссылки на фото"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "olx_get_offers",
        "description": (
            "Дочитывает объявления из уже собранной выгрузки OLX (dump_id из olx_search или olx_list_dumps) "
            "порциями по 50. full=true — с полным текстом описаний и всеми параметрами."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "dump_id": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "count": {"type": "integer", "minimum": 1, "maximum": 100, "default": PAGE},
                "full": {"type": "boolean", "default": False},
            },
            "required": ["dump_id"],
        },
    },
    {
        "name": "uzum_search",
        "description": (
            "Ищет товары на Uzum Market (uzum.uz, Узбекистан) через серверный парсер и возвращает сводку по ценам "
            "и список товаров: цена, цена по карте Uzum, скидка от зачёркнутой цены, рассрочка, рейтинг, отзывы, "
            "доставка. Используй для ЛЮБЫХ запросов про Uzum вместо веб-поиска. query — поисковый запрос или "
            "ссылка на поиск/категорию uzum.uz с фильтрами. Сбор: ~10 с на проверку браузера + ~1 с на 100 товаров. "
            "В ответе первые 50; остальные — через uzum_get_items с dump_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Поисковый запрос или ссылка https://uzum.uz/ru/search?… / /category/…"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 100,
                          "description": "Сколько товаров собрать, до 10 000"},
                "sort": {"type": "string", "enum": UZUM_SORTS,
                         "description": "popular — по числу заказов, cheap / expensive — по цене, rating — по рейтингу, "
                                        "new — новинки. По умолчанию — как на сайте (по релевантности)"},
                "photos": {"type": "boolean", "default": False, "description": "Включить ссылки на фото"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "uzum_get_items",
        "description": "Дочитывает товары из собранной выгрузки Uzum (dump_id из uzum_search или uzum_list_dumps) порциями по 50.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "dump_id": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "count": {"type": "integer", "minimum": 1, "maximum": 100, "default": PAGE},
            },
            "required": ["dump_id"],
        },
    },
    {
        "name": "uzum_list_dumps",
        "description": "Список прошлых выгрузок Uzum на сервере: запрос, дата, число товаров, медиана цены.",
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}},
    },
    {
        "name": "avtoelon_search",
        "description": (
            "Ищет объявления о продаже машин на avtoelon.uz (Узбекистан): цена в у.е., год, пробег, двигатель, "
            "топливо, кузов, КПП, город, дата, «торг». Используй для ЛЮБЫХ запросов про покупку/цены машин. "
            "query — марка и модель латиницей («chevrolet cobalt», «byd song plus») или ссылка avtoelon.uz с фильтрами. "
            "Работает с компьютера пользователя: сайт показывает объявления только узбекским IP. "
            "~1 с на 20 объявлений. В ответе первые 50; остальные — avtoelon_get_items. "
            "Объявления с полем rent — аренда/лизинг: их цена не рыночная, не считай их выгодными."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Марка и модель латиницей или ссылка https://avtoelon.uz/avto/…"},
                "limit": {"type": "integer", "minimum": 1, "maximum": avtoelon.MAX_ITEMS, "default": 100},
                "price_from": {"type": "number", "description": "Цена от, у.е. (USD)"},
                "price_to": {"type": "number", "description": "Цена до, у.е. (USD)"},
                "year_from": {"type": "integer"}, "year_to": {"type": "integer"},
                "sort": {"type": "string", "enum": list(avtoelon.SORTS),
                         "description": "new — свежие, cheap / expensive — по цене, year_new / year_old — по году"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "avtoelon_get_items",
        "description": "Дочитывает объявления из сохранённой выгрузки avtoelon (dump_id) порциями по 50; full=true — с полным описанием.",
        "inputSchema": {"type": "object", "properties": {
            "dump_id": {"type": "string"}, "offset": {"type": "integer", "minimum": 0, "default": 0},
            "count": {"type": "integer", "minimum": 1, "maximum": 100, "default": PAGE},
            "full": {"type": "boolean", "default": False}}, "required": ["dump_id"]},
    },
    {
        "name": "avtoelon_list_dumps",
        "description": "Прошлые выгрузки avtoelon (хранятся на этом компьютере в ~/.olx-mcp/avtoelon).",
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}},
    },
    {
        "name": "olx_list_dumps",
        "description": "Список прошлых выгрузок OLX на сервере: запрос, дата, число объявлений, медиана цены.",
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}},
    },
]


class ToolError(Exception):
    """Ошибка, которую стоит показать Claude как результат инструмента."""


class Api:
    """Клиент API сервера парсеров."""

    def __init__(self, base: str, token: str, opener: Callable[..., Any] = urllib.request.urlopen):
        self.base, self.token, self.opener = base.rstrip("/"), token, opener

    def call(self, method: str, path: str, body: dict | None = None) -> Any:
        req = urllib.request.Request(
            self.base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json",
                     "Content-Type": "application/json", "User-Agent": f"uzparser/{__version__}"})
        try:
            with self.opener(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode()).get("detail")
            except Exception:
                detail = None
            if e.code == 401:
                raise ToolError("Сервер не принял токен: проверьте UZPARSER_TOKEN или попросите новый") from None
            raise ToolError(f"Сервер парсеров ответил {e.code}: {detail or e.reason}") from None
        except urllib.error.URLError as e:
            raise ToolError(f"Сервер парсеров недоступен: {e.reason}") from None


def build_query(query: str, price_from: float | None = None, price_to: float | None = None,
                sort: str | None = None) -> str:
    """Запрос или ссылка + фильтры → то, что понимает парсер (ссылка olx.uz или просто текст)."""
    q = query.strip()
    if not (price_from or price_to or sort):
        return q
    if "olx.uz" in q.lower():
        url = q if "://" in q else "https://" + q
    else:
        url = "https://www.olx.uz/list/q-" + quote("-".join(q.split())) + "/"
    extra = {}
    if price_from:
        extra["search[filter_float_price:from]"] = int(price_from)
    if price_to:
        extra["search[filter_float_price:to]"] = int(price_to)
    if sort:
        if sort not in SORTS:
            raise ToolError(f"sort: одно из {', '.join(SORTS)}")
        extra["search[order]"] = SORTS[sort]
    sep = "&" if urlsplit(url).query else "?"
    return url + sep + urlencode(extra)


def fmt_num(n: float) -> str:
    return f"{n:,.0f}".replace(",", " ")


def fmt_price(p: dict | None) -> str:
    if not p:
        return "—"
    if p.get("value") is None:
        return p.get("label") or "—"
    s = f"{fmt_num(p['value'])} {p['currency']}"
    if p.get("currency") != "UZS" and p.get("uzs"):
        s += f" (≈{fmt_num(p['uzs'])} сум)"
    return s + (", торг" if p.get("negotiable") else "")


def compact_offer(o: dict, full: bool = False) -> dict:
    text = o.get("text") or ""
    item = {
        "id": o["id"], "title": o["title"], "price": fmt_price(o.get("price")),
        "price_uzs": round((o.get("price") or {}).get("uzs") or 0) or None,
        "condition": o.get("condition"), "location": o.get("location"),
        "created": (o.get("created") or "")[:10],
        "seller": ("магазин " if o["seller"]["business"] else "частное лицо ") + (o["seller"].get("name") or "")
                  + (f", на OLX с {o['seller']['since'][:7]}" if o["seller"].get("since") else ""),
        "promoted": o.get("promoted") or None,
        "url": o["url"],
    }
    if o.get("multi"):
        item["multi"] = MULTI_NOTE
    if full:
        item.update(params=o.get("params"), text=text, photos=o.get("photos"))
    else:
        if o.get("params"):
            item["params"] = "; ".join(f"{k}: {v}" for k, v in list(o["params"].items())[:8])
        cut = MULTI_CUT if o.get("multi") else TEXT_CUT
        item["text"] = text if len(text) <= cut else text[:cut].rstrip() + "…"
    return {k: v for k, v in item.items() if v not in (None, "", {})}


def compact_item(i: dict) -> dict:
    """Товар Uzum для ответа Claude."""
    price = (fmt_num(i["price"]) + " сум") if i.get("price") else "—"
    item = {
        "id": i["id"], "title": i["title"], "price": price, "price_uzs": i.get("price"),
        "price_card": fmt_num(i["price_card"]) if i.get("price_card") else None,
        "discount": f"−{i['discount']}% от {fmt_num(i['price_full'])}" if i.get("discount") else None,
        "rating": f"{i['rating']} ({i['reviews']} отз.)" if i.get("rating") else None,
        "installment": i.get("installment"), "delivery": i.get("delivery"),
        "labels": ", ".join(i.get("labels") or []) or None, "url": i["url"], "photos": i.get("photos"),
    }
    return {k: v for k, v in item.items() if v not in (None, "", [])}


def compact_car(i: dict, full: bool = False) -> dict:
    """Объявление avtoelon для ответа Claude."""
    specs = ", ".join(x for x in [f"{i['engine_l']} л" if i.get("engine_l") else None, i.get("fuel"),
                                   i.get("gearbox"), i.get("body")] if x)
    desc = i.get("description") or ""
    item = {
        "id": i["id"], "title": i["title"], "price": i.get("price") or (f"{fmt_num(i['price_usd'])} y.e." if i.get("price_usd") else "—"),
        "price_usd": i.get("price_usd"), "year": i.get("year"),
        "mileage": f"{fmt_num(i['mileage_km'])} км" if i.get("mileage_km") else None, "specs": specs or None,
        "city": i.get("city"), "date": i.get("date"), "bargain": True if i.get("bargain") else None,
        "rent": f"аренда {fmt_num(i['rent_usd_month'])} y.e./мес — цена не рыночная" if i.get("rent_usd_month") else None,
        "badges": ", ".join(b for b in i.get("badges") or [] if "торг" not in b.lower()) or None,
        "description": desc if full or len(desc) <= TEXT_CUT else desc[:TEXT_CUT].rstrip() + "…",
        "url": i["url"], "photo": i.get("photo") if full else None,
    }
    return {k: v for k, v in item.items() if v not in (None, "", [])}


def uzum_summary(rep: dict, dump_id: str) -> dict:
    s = rep["stats"]
    pu = s.get("price_uzs")
    return {
        "dump_id": dump_id, "title": rep.get("title"), "query": rep["filters"].get("query"),
        "source_url": rep["url"], "fetched_at": rep["fetched_at"], "sort": rep["filters"].get("sort"),
        "items": s["items"], "total_on_site": s.get("total_on_site"),
        "price_uzs": {k: fmt_num(v) for k, v in pu.items()} if pu else None,
        "with_discount": s.get("with_discount"), "avg_rating": s.get("avg_rating"),
    }


def summary(rep: dict, dump_id: str) -> dict:
    s = rep["stats"]
    pu = s.get("price_uzs")
    return {
        "dump_id": dump_id,
        "title": rep.get("title"), "query": rep["filters"].get("query"), "source_url": rep["url"],
        "fetched_at": rep["fetched_at"],
        "offers": s["offers"],
        "total_on_site": f"{s.get('total_on_site')}+" if s.get("total_capped") else s.get("total_on_site"),
        "with_price": s["with_price"],
        "price_uzs": {k: fmt_num(v) for k, v in pu.items()} if pu else None,
        "currencies": s.get("currencies"), "business_sellers": s.get("business"),
        "multi_item_offers": (f"{s['multi']} — с несколькими позициями в описании, в медиану не входят"
                              if s.get("multi") else None),
        "dates": f"{s.get('from')} — {s.get('to')}",
    }


class ProgressFile:
    """Прогресс одного поиска в файле <dir>/<pid>-<n>.json — для мода uzparser-progress.

    Пишется атомарно (через os.replace), удаляется по завершении. Ошибки записи
    глотаются: прогресс — подсказка, поиск из-за него падать не должен.
    """

    def __init__(self, directory: str | None, tool: str, args: dict, n: int):
        self.path = os.path.join(directory, f"{os.getpid()}-{n}.json") if directory else None
        now = int(time.time() * 1000)
        self.data = {"tool": tool, "query": str(args.get("query", "")), "limit": args.get("limit"),
                     "fetched": 0, "goal": None, "message": "Запуск", "started": now, "updated": now,
                     "changed": now}
        if self.path:
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError:
                self.path = None
        self._write()

    def update(self, done: float, total: float | None, message: str) -> None:
        now = int(time.time() * 1000)
        if done != self.data["fetched"] or message != self.data["message"]:  # что-то сдвинулось
            self.data["changed"] = now
        self.data.update(fetched=done, goal=total, message=message, updated=now)
        self._write()

    def _write(self) -> None:
        if not self.path:
            return
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def remove(self) -> None:
        if self.path:
            try:
                os.remove(self.path)
            except OSError:
                pass


class Server:
    def __init__(self, api: Api | None, send: Callable[[dict], None],
                 sleep: Callable[[float], None] = time.sleep, progress_dir: str | None = None):
        self.api, self.send, self.sleep = api, send, sleep
        self.progress_dir = progress_dir  # None — прогресс в файлы не пишется
        self._calls = 0
        self.avtoelon_get = avtoelon.http_get  # в тестах подменяется

    # ---------- инструменты ----------

    def _need_api(self) -> Api:
        if self.api is None:
            raise ToolError("Не задан UZPARSER_TOKEN: попросите токен у владельца сервера и добавьте его "
                            "в настройки MCP-сервера uzparser (OLX и Uzum без него не работают; avtoelon — работает)")
        return self.api

    def _run(self, kind: str, label: str, body: dict, limit: int, progress) -> tuple[str, dict]:
        """Ставит задание парсеру и ждёт его, сообщая прогресс; возвращает dump_id и выгрузку."""
        api = self._need_api()
        job = api.call("POST", f"/api/{kind}/jobs", body)
        deadline = time.monotonic() + JOB_TIMEOUT
        while job["status"] in ("queued", "running"):
            if time.monotonic() > deadline:
                raise ToolError(f"Сбор не уложился в {JOB_TIMEOUT // 60} минут (задание {job['id']})")
            self.sleep(1.5)
            job = api.call("GET", f"/api/{kind}/jobs/{job['id']}")
            goal = min(limit, job.get("total") or limit)
            progress(job.get("fetched", 0), goal,
                     "В очереди" if job["status"] == "queued" else f"Собрано {job.get('fetched', 0)} из {goal}")
        if job["status"] == "error":
            raise ToolError(f"Парсер {label}: {job.get('error')}")
        return job["dump_id"], api.call("GET", f"/api/{kind}/dumps/{job['dump_id']}")

    def olx_search(self, args: dict, progress: Callable[[float, float | None, str], None]) -> dict:
        query = build_query(args["query"], args.get("price_from"), args.get("price_to"), args.get("sort"))
        limit = max(1, min(int(args.get("limit") or 100), 10000))
        dump_id, rep = self._run("olx", "OLX", {"url": query, "limit": limit, "photos": bool(args.get("photos"))},
                                 limit, progress)
        out = summary(rep, dump_id)
        out["shown"] = f"0–{min(PAGE, len(rep['offers']))} из {len(rep['offers'])}"
        if len(rep["offers"]) > PAGE:
            out["more"] = f"olx_get_offers(dump_id='{dump_id}', offset={PAGE})"
        out["offers_list"] = [compact_offer(o) for o in rep["offers"][:PAGE]]
        return out

    def olx_get_offers(self, args: dict, progress) -> dict:
        api = self._need_api()
        dump_id = str(args["dump_id"])
        if not all(c.isalnum() or c in "_-" for c in dump_id):
            raise ToolError("Некорректный dump_id")
        rep = api.call("GET", f"/api/olx/dumps/{dump_id}")
        off = max(0, int(args.get("offset") or 0))
        cnt = max(1, min(int(args.get("count") or PAGE), 100))
        part = rep["offers"][off:off + cnt]
        out = {"dump_id": dump_id, "title": rep.get("title"), "total": len(rep["offers"]),
               "shown": f"{off}–{off + len(part)}",
               "offers_list": [compact_offer(o, full=bool(args.get("full"))) for o in part]}
        if off + cnt < len(rep["offers"]):
            out["more"] = f"olx_get_offers(dump_id='{dump_id}', offset={off + cnt})"
        return out

    def uzum_search(self, args: dict, progress) -> dict:
        sort = args.get("sort")
        if sort and sort not in UZUM_SORTS:
            raise ToolError(f"sort: одно из {', '.join(UZUM_SORTS)}")
        limit = max(1, min(int(args.get("limit") or 100), 10000))
        dump_id, rep = self._run("uzum", "Uzum", {"url": args["query"].strip(), "limit": limit, "sort": sort,
                                                  "photos": bool(args.get("photos"))}, limit, progress)
        out = uzum_summary(rep, dump_id)
        out["shown"] = f"0–{min(PAGE, len(rep['items']))} из {len(rep['items'])}"
        if len(rep["items"]) > PAGE:
            out["more"] = f"uzum_get_items(dump_id='{dump_id}', offset={PAGE})"
        out["items_list"] = [compact_item(i) for i in rep["items"][:PAGE]]
        return out

    def uzum_get_items(self, args: dict, progress) -> dict:
        api = self._need_api()
        dump_id = str(args["dump_id"])
        if not all(c.isalnum() or c in "_-" for c in dump_id):
            raise ToolError("Некорректный dump_id")
        rep = api.call("GET", f"/api/uzum/dumps/{dump_id}")
        off = max(0, int(args.get("offset") or 0))
        cnt = max(1, min(int(args.get("count") or PAGE), 100))
        part = rep["items"][off:off + cnt]
        out = {"dump_id": dump_id, "title": rep.get("title"), "total": len(rep["items"]),
               "shown": f"{off}–{off + len(part)}", "items_list": [compact_item(i) for i in part]}
        if off + cnt < len(rep["items"]):
            out["more"] = f"uzum_get_items(dump_id='{dump_id}', offset={off + cnt})"
        return out

    def uzum_list_dumps(self, args: dict, progress) -> list:
        api = self._need_api()
        lim = max(1, min(int(args.get("limit") or 20), 100))
        return [{"dump_id": d["id"], "title": d.get("title"), "query": (d.get("filters") or {}).get("query"),
                 "fetched_at": d["fetched_at"], "items": d["stats"]["items"],
                 "median_uzs": fmt_num(d["stats"]["price_uzs"]["median"]) if d["stats"].get("price_uzs") else None,
                 "url": d.get("url")}
                for d in api.call("GET", "/api/uzum/dumps")[:lim]]

    def avtoelon_search(self, args: dict, progress) -> dict:
        try:
            url = avtoelon.build_url(args["query"], args.get("price_from"), args.get("price_to"),
                                     args.get("year_from"), args.get("year_to"), args.get("sort"))
            limit = max(1, min(int(args.get("limit") or 100), avtoelon.MAX_ITEMS))
            items, total = avtoelon.fetch(url, limit, sleep=self.sleep, get=self.avtoelon_get,
                                          progress=lambda n, t: progress(n, min(limit, t or limit),
                                                                         f"Собрано {n} из {min(limit, t or limit)}"))
        except avtoelon.AvtoelonError as e:
            raise ToolError(str(e)) from None
        rep = avtoelon.build_report(items, url, total)
        dump_id = avtoelon.save(rep)
        s = rep["stats"]
        out = {"dump_id": dump_id, "source_url": url, "items": s["items"], "total_on_site": total,
               "price_usd": {k: fmt_num(v) for k, v in s["price_usd"].items()} if s["price_usd"] else None,
               "years": s["years"], "with_bargain": s["with_bargain"], "with_rent": s.get("with_rent"),
               "shown": f"0–{min(PAGE, len(items))} из {len(items)}"}
        if len(items) > PAGE:
            out["more"] = f"avtoelon_get_items(dump_id='{dump_id}', offset={PAGE})"
        out["items_list"] = [compact_car(i) for i in items[:PAGE]]
        return out

    def avtoelon_get_items(self, args: dict, progress) -> dict:
        try:
            rep = avtoelon.load(str(args["dump_id"]))
        except avtoelon.AvtoelonError as e:
            raise ToolError(str(e)) from None
        off = max(0, int(args.get("offset") or 0))
        cnt = max(1, min(int(args.get("count") or PAGE), 100))
        part = rep["items"][off:off + cnt]
        out = {"dump_id": args["dump_id"], "total": len(rep["items"]), "shown": f"{off}–{off + len(part)}",
               "items_list": [compact_car(i, full=bool(args.get("full"))) for i in part]}
        if off + cnt < len(rep["items"]):
            out["more"] = f"avtoelon_get_items(dump_id='{args['dump_id']}', offset={off + cnt})"
        return out

    def avtoelon_list_dumps(self, args: dict, progress) -> list:
        return avtoelon.list_dumps(max(1, min(int(args.get("limit") or 20), 100)))

    def olx_list_dumps(self, args: dict, progress) -> list:
        api = self._need_api()
        lim = max(1, min(int(args.get("limit") or 20), 100))
        return [{"dump_id": d["id"], "title": d.get("title"), "query": (d.get("filters") or {}).get("query"),
                 "fetched_at": d["fetched_at"], "offers": d["stats"]["offers"],
                 "median_uzs": fmt_num(d["stats"]["price_uzs"]["median"]) if d["stats"].get("price_uzs") else None,
                 "url": d.get("url")}
                for d in api.call("GET", "/api/olx/dumps")[:lim]]

    # ---------- протокол ----------

    def handle(self, msg: dict) -> dict | None:
        method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if mid is None:  # уведомление (initialized, cancelled…) — ответа не требует
            return None
        try:
            if method == "initialize":
                ver = params.get("protocolVersion")
                return self._ok(mid, {
                    "protocolVersion": ver if ver in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "uzparser", "version": __version__},
                    "instructions": INSTRUCTIONS,
                })
            if method == "ping":
                return self._ok(mid, {})
            if method == "tools/list":
                return self._ok(mid, {"tools": TOOLS})
            if method == "tools/call":
                return self._ok(mid, self._call_tool(params))
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Нет метода {method}"}}
        except Exception as e:  # протокол не должен падать из-за одного запроса
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}

    def _call_tool(self, params: dict) -> dict:
        name, args = params.get("name"), params.get("arguments") or {}
        fn = getattr(self, name, None) if name in {t["name"] for t in TOOLS} else None
        if fn is None:
            return {"content": [{"type": "text", "text": f"Нет инструмента {name}"}], "isError": True}
        token = (params.get("_meta") or {}).get("progressToken")
        track = ProgressFile(self.progress_dir, name, args, self._next_call()) if name in SEARCH_TOOLS else None

        def progress(done: float, total: float | None, message: str) -> None:
            if track:
                track.update(done, total, message)
            if token is not None:
                p = {"progressToken": token, "progress": done, "message": message}
                if total:
                    p["total"] = total
                self.send({"jsonrpc": "2.0", "method": "notifications/progress", "params": p})

        try:
            result = fn(args, progress)
        except ToolError as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
        except (KeyError, ValueError, TypeError) as e:
            return {"content": [{"type": "text", "text": f"Неверные аргументы: {e}"}], "isError": True}
        finally:
            if track:
                track.remove()
        # без отступов: на 50 объявлениях это ~10% контекста
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, separators=(",", ":"))}]}

    def _next_call(self) -> int:
        self._calls += 1
        return self._calls

    @staticmethod
    def _ok(mid, result) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
        print(__version__)
        return
    env = lambda new, old, default="": (os.environ.get(new) or os.environ.get(old) or default).strip()
    token = env("UZPARSER_TOKEN", "OLX_MCP_TOKEN")
    api = Api(env("UZPARSER_URL", "OLX_MCP_URL", DEFAULT_URL), token) if token else None
    out = sys.stdout

    def send(msg: dict) -> None:
        out.write(json.dumps(msg, ensure_ascii=False) + "\n")
        out.flush()

    pdir = os.environ.get("UZPARSER_PROGRESS_DIR")
    server = Server(api, send, progress_dir=DEFAULT_PROGRESS_DIR if pdir is None else (pdir.strip() or None))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        for m in msg if isinstance(msg, list) else [msg]:
            resp = server.handle(m)
            if resp is not None:
                send(resp)


if __name__ == "__main__":
    main()
