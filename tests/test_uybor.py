import json
import os
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from uzparser import server as S
from uzparser import uybor as U


def listing(i, district="Яшнабадский район", price=95000, currency="usd", room="2", square=60, ptype="all", sale=True):
    usd = price if currency == "usd" else round(price / 12500, 2)
    return {"id": 1000 + i, "operationType": "sale" if sale else "rent", "categoryId": 7,
            "category": {"id": 7, "name": {"ru": "Квартира"}}, "price": price, "priceCurrency": currency,
            "priceType": ptype, "isPriceAuction": i % 2 == 0, "room": room, "square": square, "floor": 3,
            "floorTotal": 9, "isNewBuilding": i % 3 == 0, "repair": "evro", "foundation": "kirpich",
            "region": {"id": 13, "name": {"ru": "город Ташкент"}},
            "district": {"id": 200 + i, "name": {"ru": district}},  # у сайта десятки ID на один район
            "zone": {"id": 1, "name": {"ru": "махаллинский сход граждан Туйтепа"}},
            "street": {"id": 2, "name": {"ru": "улица Садыка Азимова"}}, "metro": None, "address": "ул. Азимова",
            "pricePeriodUnit": None if sale else "month", "description": "Квартира " * 40, "media": [{}, {}],
            "views": 10 + i, "createdAt": "2026-10-09T10:00:00Z", "upAt": "2026-10-10T10:00:00Z",
            "lat": 41.3, "lng": 69.3, "prices": {"usd": usd, "uzs": usd * 12500}}


class FakeApi:
    """API uybor: total объявлений, на каждой странице чередуются районы."""

    def __init__(self, total=250, districts=("Яшнабадский район", "Яшнободский район", "Чиланзарский район")):
        self.total, self.districts, self.urls = total, districts, []

    def __call__(self, url):
        self.urls.append(url)
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        page, size = int(q["page"]), int(q["limit"])
        ids = range((page - 1) * size, min(self.total, page * size))
        return {"total": self.total, "results": [listing(i, self.districts[i % len(self.districts)]) for i in ids]}


class ParamsTest(unittest.TestCase):
    def test_build_params(self):
        p = U.build_params("rent", "private_house", "2, 3,6", 300, 800, "usd", 50, None, False, "evro", "Ташкент", "cheap")
        self.assertEqual(p, {"operationType__eq": "rent", "category__eq": 8, "subCategory__eq": 28, "room__in": "2,3,6+",
                             "priceCurrency__eq": "usd", "price__gte": 300, "price__lte": 800, "square__gte": 50,
                             "isNewBuilding__eq": "false", "repair__eq": "evro", "region__eq": 13,
                             "order": "-priceEquivalent"})
        self.assertEqual(U.build_params()["category__eq"], 7)
        self.assertEqual(U.build_params(rooms="студия")["room__in"], "studio")
        for bad in (dict(operation="buy"), dict(category="castle"), dict(rooms="10x"), dict(sort="random"),
                    dict(region="Марс"), dict(repair="lux"), dict(price_to=1, currency="eur")):
            with self.assertRaises(U.UyborError):
                U.build_params(**bad)

    def test_regions(self):
        self.assertEqual(U.region_id("Ташкент"), 13)
        self.assertEqual(U.region_id("Ташкентская область"), 12)
        self.assertEqual(U.region_id("самаркандская"), 9)

    def test_place_matching_handles_spellings(self):
        item = U.normalize(listing(1, "Яшнободский район"))
        self.assertTrue(U.place_matches(item, "Яшнабадский район"))
        self.assertTrue(U.place_matches(item, "Туйтепа"))
        self.assertFalse(U.place_matches(item, "Чиланзар"))
        self.assertTrue(U.place_matches(U.normalize(listing(1, "Олмазорский район")), "Алмазарский"))

    def test_normalize(self):
        i = U.normalize(listing(4, price=1_250_000_000, currency="uzs", square=50))
        self.assertEqual((i["url"], i["price_usd"], i["usd_per_m2"], i["repair"], i["foundation"], i["bargain"]),
                         ("https://uybor.uz/ru/listings/1004", 100000, 2000, "евроремонт", "кирпич", True))
        self.assertIsNone(U.normalize(listing(1, ptype="sot"))["usd_per_m2"])  # цена за сотку — не за объект
        self.assertEqual(U.normalize(listing(1, sale=False))["rent_period"], "в месяц")


class FetchTest(unittest.TestCase):
    def test_paging_and_limit(self):
        api = FakeApi(total=250)
        items, total, scanned = U.fetch(U.build_params(), 150, get=api, sleep=lambda s: None)
        self.assertEqual((len(items), total, scanned, len(api.urls)), (150, 250, 200, 2))
        self.assertIn("embed=", api.urls[0])

    def test_district_scans_further(self):
        api = FakeApi(total=450)
        items, total, scanned = U.fetch(U.build_params(), 1000, "Яшнабадский", get=api, sleep=lambda s: None)
        self.assertEqual((len(items), scanned), (300, 450))  # оба написания района, Чиланзар отсеян
        self.assertTrue(all("Яшна" in i["district"] or "Яшно" in i["district"] for i in items))

    def test_report(self):
        items, total, scanned = U.fetch(U.build_params(), 30, get=FakeApi(total=30), sleep=lambda s: None)
        s = U.build_report(items, U.build_params(), None, total, scanned)["stats"]
        self.assertEqual((s["items"], s["price_usd"]["median"], s["usd_per_m2_median"], s["rooms"]), (30, 95000, 1583, {"2": 30}))


class ToolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["UZPARSER_DATA"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("UZPARSER_DATA", None)
        self.tmp.cleanup()

    def call(self, srv, name, args):
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})["result"]
        return r, (json.loads(r["content"][0]["text"]) if not r.get("isError") else r["content"][0]["text"])

    def test_search_get_list(self):
        srv = S.Server(None, lambda m: None, sleep=lambda s: None)  # токен не нужен
        srv.uybor_get = FakeApi(total=120)
        r, out = self.call(srv, "uybor_search", {"rooms": "2", "district": "Яшнабадский"})
        self.assertNotIn("isError", r)
        self.assertEqual((out["items"], out["scanned"], len(out["items_list"])), (80, 120, 50))
        first = out["items_list"][0]
        self.assertEqual((first["price"], first["usd_per_m2"], first["area"], first["floor"]),
                         ("95 000 у.е.", 1583, "60 м²", "3/9"))
        self.assertTrue(first["place"].startswith("Яшна"))
        self.assertTrue(first["description"].endswith("…"))
        r, part = self.call(srv, "uybor_get_items", {"dump_id": out["dump_id"], "offset": 50, "full": True})
        self.assertEqual((part["shown"], part["items_list"][0]["coords"]), ("50–80", "41.3,69.3"))
        r, dumps = self.call(srv, "uybor_list_dumps", {})
        self.assertEqual(dumps[0]["dump_id"], out["dump_id"])

    def test_errors(self):
        srv = S.Server(None, lambda m: None, sleep=lambda s: None)
        self.assertTrue(self.call(srv, "uybor_search", {"category": "castle"})[0]["isError"])
        self.assertTrue(self.call(srv, "uybor_get_items", {"dump_id": "../../etc"})[0]["isError"])


if __name__ == "__main__":
    unittest.main()
