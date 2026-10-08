import html
import json
import os
import tempfile
import unittest
from datetime import date

from uzparser import server as S
from uzparser import yandex as Y

TODAY = date(2026, 10, 8)


def card(sku, title, price, old=None, cross=True, d_from="2026-10-18", d_to="2026-10-21", rating=None, stock=None,
         specs=(("Процессор", "Intel N100"), ("Оперативная память", "16 ГБ"))):
    """Карточка выдачи market.yandex.uz: JSON в data-zone-data и разметка с характеристиками."""
    z = {"type": "offer", "marketSku": str(sku), "wareId": f"w{sku}", "title": title, "price": old or price,
         "additionalPrices": [{"priceType": "withDiscount", "priceValue": str(price)}] if old else [],
         "isCrossBorder": "true" if cross else "false", "isExpress": False, "shopId": "42",
         "availableDelivery": [{"type": "PICKUP", "dateFrom": d_from, "dateTo": d_to},
                               {"type": "ON_DEMAND", "dateFrom": d_from, "dateTo": d_to}],
         "featureBadges": [{"badgeTitle": "Скидка 10%"}] if old else []}
    if rating:
        z["rating"] = {"rating": str(rating), "gradesCount": "320", "purchaseCount": "945"}
    sp = "".join(f'<div><span>{k}:</span> <span>{v}</span></div>' for k, v in specs)
    st = f"<span>Осталось {stock} шт</span>" if stock else ""
    return (f'<div class="x" data-zone-name="productSnippet" data-zone-data="{html.escape(json.dumps(z, ensure_ascii=False))}">'
            f'<a href="/card/slug-{sku}/{sku}?cpc=AAA&amp;x=1"><span>{title}</span></a>{sp}{st}'
            f'<span>Цена {price} сум</span></div>')


def page(cards, total):
    return f'<html><noframes data-apiary="patch">{{"collections":{{"x":{{"total":{total}}}}}}}</noframes>' + "".join(cards) + "</html>"


class FakeSite:
    """Страница 1 — 8 карточек, дальше по 16; captcha_on — номер страницы с капчей."""

    def __init__(self, total=40, captcha_on=None, local_every=5):
        self.total, self.captcha_on, self.local_every, self.urls = total, captcha_on, local_every, []

    def __call__(self, url):
        self.urls.append(url)
        n = int(dict(p.split("=") for p in url.split("?", 1)[1].split("&")).get("page", 1))
        if n == self.captcha_on:
            return 200, "https://market.yandex.uz/showcaptcha?retpath=x", "<html>captcha</html>"
        start = 0 if n == 1 else 8 + (n - 2) * 16
        ids = range(start, min(self.total, start + (8 if n == 1 else 16)))
        cards = [card(1000 + i, f"Мини-ПК {i}", 1_000_000 + i * 10_000, cross=(i % self.local_every != 0),
                      d_from="2026-10-09" if i % self.local_every == 0 else "2026-10-18") for i in ids]
        return 200, url, page(cards, self.total)


class ParseTest(unittest.TestCase):
    def test_parse_card(self):
        items, total = Y.parse_page(page([card(5, "Мини ПК Chuwi", 11_473_210, old=12_473_209, rating=4.8, stock=3),
                                          card(6, "Кофе", 135_000, cross=False, d_from="2026-10-09", d_to="2026-10-09")],
                                         228), today=TODAY)
        self.assertEqual(total, 228)
        a, b = items
        self.assertEqual(a["url"], "https://market.yandex.uz/card/slug-5/5")
        self.assertEqual((a["price"], a["price_full"], a["discount"]), (11_473_210, 12_473_209, 8))
        self.assertEqual((a["cross_border"], a["origin"], a["delivery_days"], a["delivery"]),
                         (True, "из-за рубежа", 10, "2026-10-18 — 2026-10-21"))
        self.assertEqual((a["rating"], a["reviews"], a["bought"], a["stock_left"]), (4.8, 320, 945, 3))
        self.assertEqual(a["specs"], {"Процессор": "Intel N100", "Оперативная память": "16 ГБ"})
        self.assertEqual(a["delivery_types"], ["ON_DEMAND", "PICKUP"])
        self.assertEqual((b["cross_border"], b["origin"], b["delivery_days"], b["delivery"], b["price_full"]),
                         (False, "Узбекистан", 1, "2026-10-09", None))

    def test_build_url(self):
        self.assertEqual(Y.build_url("mini pc"), "https://market.yandex.uz/search?text=mini+pc")
        self.assertEqual(Y.build_url("кофе", price_from=1e5, price_to=5e5, sort="cheap"),
                         "https://market.yandex.uz/search?text=%D0%BA%D0%BE%D1%84%D0%B5&pricefrom=100000&priceto=500000&how=aprice")
        self.assertEqual(Y.build_url("market.yandex.uz/search?text=x&page=3&rs=abc"),
                         "https://market.yandex.uz/search?text=x")
        for bad in ("", "https://market.yandex.uz/card/x/1", "https://uzum.uz/ru/search?q=x"):
            with self.assertRaises(Y.YandexError):
                Y.build_url(bad)
        with self.assertRaises(Y.YandexError):
            Y.build_url("x", sort="random")

    def test_fetch_paginates_dedupes_stops(self):
        site = FakeSite(total=40)
        items, total = Y.fetch(Y.build_url("mini pc"), 100, get=site, sleep=lambda s: None)
        self.assertEqual((len(items), total, len(site.urls)), (40, 40, 3))  # 8 + 16 + 16
        self.assertEqual(len({i["id"] for i in items}), 40)
        items, _ = Y.fetch(Y.build_url("mini pc"), 10, get=FakeSite(), sleep=lambda s: None)
        self.assertEqual(len(items), 10)

    def test_captcha(self):
        with self.assertRaises(Y.YandexError) as e:
            Y.fetch(Y.build_url("x"), 50, get=FakeSite(captcha_on=1), sleep=lambda s: None)
        self.assertIn("капчу", str(e.exception))
        items, _ = Y.fetch(Y.build_url("x"), 50, get=FakeSite(captcha_on=2), sleep=lambda s: None)
        self.assertEqual(len(items), 8)  # что успели до капчи

    def test_report_splits_local_and_cross_border(self):
        items, total = Y.fetch(Y.build_url("x"), 100, get=FakeSite(total=20), sleep=lambda s: None)
        s = Y.build_report(items, Y.build_url("x"), total)["stats"]
        self.assertEqual((s["items"], s["local"]["items"], s["cross_border"]["items"]), (20, 4, 16))
        self.assertEqual(s["local"]["price_uzs"]["min"], 1_000_000)
        self.assertEqual(Y._slug(Y.build_url("мини пк")), "yandex_mini_pk")


class ToolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["UZPARSER_DATA"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("UZPARSER_DATA", None)
        self.tmp.cleanup()

    def call(self, srv, name, args):
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
        return r["result"]

    def test_search_local_only_and_paging(self):
        srv = S.Server(None, lambda m: None, sleep=lambda s: None)  # токен сервера парсеров не нужен
        srv.yandex_get = FakeSite(total=120)
        r = self.call(srv, "yandex_search", {"query": "mini pc", "limit": 120})
        self.assertNotIn("isError", r)
        out = json.loads(r["content"][0]["text"])
        self.assertEqual((out["items"], out["local"]["items"], out["cross_border"]["items"]), (120, 24, 96))
        self.assertEqual(len(out["items_list"]), 50)
        first, local = out["items_list"][1], out["items_list"][0]
        self.assertTrue(first["cross_border"])
        self.assertEqual(first["origin"], "из-за рубежа")
        self.assertNotIn("cross_border", local)
        self.assertTrue(local["delivery"].startswith("через "))

        r = self.call(srv, "yandex_get_items", {"dump_id": out["dump_id"], "local_only": True, "full": True})
        part = json.loads(r["content"][0]["text"])
        self.assertEqual(part["total"], 24)
        self.assertTrue(all("cross_border" not in i for i in part["items_list"]))
        self.assertIsInstance(part["items_list"][0]["specs"], dict)

        srv.yandex_get = FakeSite(total=60)
        out = json.loads(self.call(srv, "yandex_search", {"query": "mini pc", "local_only": True})["content"][0]["text"])
        self.assertEqual(len(out["items_list"]), 12)
        r = self.call(srv, "yandex_list_dumps", {})
        self.assertEqual(len(json.loads(r["content"][0]["text"])), 2)

    def test_errors(self):
        srv = S.Server(None, lambda m: None, sleep=lambda s: None)
        srv.yandex_get = FakeSite(captcha_on=1)
        self.assertTrue(self.call(srv, "yandex_search", {"query": "x"})["isError"])
        self.assertTrue(self.call(srv, "yandex_get_items", {"dump_id": "../../etc"})["isError"])


if __name__ == "__main__":
    unittest.main()
