import json
import os
import tempfile
import unittest

from uzparser import avtoelon as A
from uzparser import server as S


def card(aid, title, price, year, desc, city="Ташкент", bargain=False, badge=None, brand="Chevrolet", model="Cobalt",
         usd=None):
    """Карточка в разметке avtoelon: HTML, затем listing.items.push с JSON."""
    b = '<span class="payment-package-corner__badge-text">%s</span>' % badge if badge else ""
    bg = ('<div class="badge badge--theme-orange-filled badge--urgent"><span class="badge__text">Торг есть</span></div>'
          if bargain else "")
    return f"""
<div
     data-id="{aid}"
     id="advert-{aid}"
          class="row list-item a-elem"
>{b}
  <a class="js__advert-link" href="/a/show/{aid}">{title}</a>
  <picture><img alt="x" src="https://kluz-photos.kcdn.online/webp/aa/{aid}/1-160x120.webp"></picture>
            <span class="price">
                <span>Цена: </span>{price}
            </span>
        </div>
        <div class="a-info-mid">
            <div class="desc">{bg}
                <span class="year">{year}&nbsp;г.,</span>
                {desc}
            </div>
        </div>
        <div class="a-info-bot">
            <div class="a-info-text">
                <a class="a-info-text__region" href="/avto/x/">{city}</a>
                <span class="date">5 октября</span>
                <span class="nb-views">12 просмотров</span>
            </div>
        </div>
    </div>
</div>
<script type="text/javascript">
    listing.items.push({json.dumps({"attributes": {"model": model, "brand": brand}, "lastUpdate": "2026-10-05T22:50:22+05:00",
                                     "unitPrice": usd, "photos": 7, "url": f"https://avtoelon.uz/a/show/{aid}"})});
</script>"""


def page(cards, total):
    return ('<html><script type="application/ld+json">{"offers":{"@type":"AggregateOffer","offerCount":"%d"}}</script>' % total
            + "".join(cards) + "</html>")


VIP = card(1, "Chevrolet Cobalt, 4 позиция", "12&nbsp;000&nbsp;y.e.", 2024,
           "1.5 л, Бензин, 15 000 км, Белый, Седан, КПП Автомат, Литые диски", badge="VIP", usd=12000)


class FakeSite:
    """Три страницы по 20; VIP-объявление 1 повторяется на каждой, дальше — 301."""

    def __init__(self, total=50):
        self.total, self.urls = total, []

    def __call__(self, url):
        self.urls.append(url)
        n = int(url.split("page=")[1].split("&")[0]) if "page=" in url else 1
        ids = list(range(2 + (n - 1) * 19, 2 + n * 19))
        ids = [i for i in ids if i <= self.total]
        if not ids:
            return 301, ""
        cards = [VIP] + [card(i, "Chevrolet Cobalt", f"~{i}&nbsp;000&nbsp;y.e.", 2020, f"Газ-бензин, {i} 000 км",
                              bargain=i % 2 == 0, usd=i * 1000) for i in ids]
        return 200, page(cards, self.total)


class ParseTest(unittest.TestCase):
    def test_parse_card(self):
        items, total = A.parse_page(page([VIP, card(7, "Chevrolet Cobalt", "~2&nbsp;379&nbsp;y.e.", 2002,
                                                    "Газ-бензин, Седан", bargain=True, city="Самарканд", usd=2379)], 29400))
        self.assertEqual(total, 29400)
        v, c = items
        self.assertEqual((v["id"], v["brand"], v["model"], v["price_usd"], v["year"], v["engine_l"], v["fuel"],
                          v["mileage_km"], v["body"], v["gearbox"], v["badges"], v["bargain"]),
                         (1, "Chevrolet", "Cobalt", 12000, 2024, 1.5, "Бензин", 15000, "Седан", "Автомат", ["VIP"], False))
        self.assertEqual(v["photo"], "https://kluz-photos.kcdn.online/webp/aa/1/1-full.webp")
        self.assertEqual((v["city"], v["date"], v["views"], v["photos"]), ("Ташкент", "5 октября", 12, 7))
        self.assertEqual((c["price"], c["price_converted"], c["bargain"], c["fuel"], c["body"], c["mileage_km"], c["year"]),
                         ("~2 379 y.e.", True, True, "Газ-бензин", "Седан", None, 2002))
        self.assertEqual(c["description"], "Газ-бензин, Седан")
        self.assertIsNone(c["rent_usd_month"])
        r, = A.parse_page(page([card(9, "Chevrolet Gentra", "3&nbsp;800&nbsp;y.e.", 2022,
                                     "1.6 л, Бензин, 72 000 км, Седан… Аренда 320 y.e./мес", usd=3800)], 1))[0]
        self.assertEqual(r["rent_usd_month"], 320)

    def test_build_url(self):
        self.assertEqual(A.build_url("Chevrolet Cobalt", price_to=12000, year_from=2020, sort="cheap"),
                         "https://avtoelon.uz/avto/chevrolet/cobalt/?price%5Bto%5D=12000&year%5Bfrom%5D=2020&sort_by=price-asc")
        self.assertEqual(A.build_url("byd song plus"), "https://avtoelon.uz/avto/byd/song-plus/")
        self.assertEqual(A.build_url("https://avtoelon.uz/avto/kia/k5/?year%5Bfrom%5D=2021&page=3"),
                         "https://avtoelon.uz/avto/kia/k5/?year%5Bfrom%5D=2021")
        for bad in ("", "кобальт", "https://avtoelon.uz/a/show/123", "https://olx.uz/x/y"):
            with self.assertRaises(A.AvtoelonError):
                A.build_url(bad)
        with self.assertRaises(A.AvtoelonError):
            A.build_url("kia k5", sort="random")

    def test_fetch_paginates_dedupes_stops(self):
        site = FakeSite(total=50)
        items, total = A.fetch("https://avtoelon.uz/avto/chevrolet/cobalt/", 1000, get=site, sleep=lambda s: None)
        self.assertEqual(total, 50)
        self.assertEqual([i["id"] for i in items], list(range(1, 51)))  # VIP 1 только один раз
        self.assertEqual(len(site.urls), 3)  # 3 страницы, на 3-й набрали total
        items, _ = A.fetch("https://avtoelon.uz/avto/chevrolet/cobalt/", 25, get=FakeSite(), sleep=lambda s: None)
        self.assertEqual(len(items), 25)

    def test_geo_404(self):
        with self.assertRaisesRegex(A.AvtoelonError, "Узбекистан"):
            A.fetch("https://avtoelon.uz/avto/", get=lambda u: (404, ""), sleep=lambda s: None)


class ToolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["UZPARSER_DATA"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("UZPARSER_DATA", None)
        self.tmp.cleanup()

    def test_search_without_token_and_paging(self):
        sent = []
        srv = S.Server(None, sent.append, sleep=lambda s: None)  # токен сервера парсеров не нужен
        srv.avtoelon_get = FakeSite(total=70)
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "avtoelon_search", "arguments": {"query": "chevrolet cobalt", "limit": 70, "sort": "cheap"},
            "_meta": {"progressToken": "p"}}})["result"]
        self.assertNotIn("isError", r)
        out = json.loads(r["content"][0]["text"])
        self.assertEqual((out["items"], out["total_on_site"], len(out["items_list"])), (70, 70, 50))
        self.assertIn("sort_by=price-asc", out["source_url"])
        first = out["items_list"][0]
        self.assertEqual((first["price"], first["mileage"], first["specs"], first["badges"]),
                         ("12 000 y.e.", "15 000 км", "1.5 л, Бензин, Автомат, Седан", "VIP"))
        self.assertTrue(any(m.get("method") == "notifications/progress" for m in sent))

        r = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "avtoelon_get_items", "arguments": {"dump_id": out["dump_id"], "offset": 50, "full": True}}})["result"]
        part = json.loads(r["content"][0]["text"])
        self.assertEqual((part["shown"], len(part["items_list"])), ("50–70", 20))
        r = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "avtoelon_list_dumps", "arguments": {}}})["result"]
        self.assertEqual(json.loads(r["content"][0]["text"])[0]["dump_id"], out["dump_id"])

    def test_errors(self):
        srv = S.Server(None, lambda m: None, sleep=lambda s: None)
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "avtoelon_search", "arguments": {"query": "кобальт"}}})["result"]
        self.assertTrue(r["isError"])
        r = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "avtoelon_get_items", "arguments": {"dump_id": "../../etc"}}})["result"]
        self.assertTrue(r["isError"])


if __name__ == "__main__":
    unittest.main()
