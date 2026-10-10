import json
import os
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from uzparser import server as S
from uzparser import shops as SH


def idea(n, title, price, old=None, stock=2):
    return {"id": n, "title_name": title, "current_price": price, "old_price": old or price, "in_stock": stock,
            "min_price_per_month": price // 6, "min_price_per_month_duration": 6, "rating": 0, "rating_count": 0,
            "brand_name": "Apple", "category_name": "Компьютеры", "url": f"https://idea.uz/product/{n}"}


def ax_card(pid, title, price, old=None, stock=True, preorder=False, reviews=0):
    """Карточка выдачи asaxiy.uz в их разметке."""
    old_html = f'<span class="product__item-old--price">{old:,} сум</span>'.replace(",", " ") if old else ""
    flag = "<span>Нет в наличии</span>" if not stock else ("<span>Предзаказ</span>" if preorder else "")
    stars = '<span><i class="fas fa-star"></i></span>' * 4 + '<span><i class="far fa-star"></i></span>'
    return f"""<div class="col-6"><div class="product__item d-flex flex-column" data-actual-price="{price}">
      <button class="product__item-heart" data-product-id="{pid}"></button>
      <a href="/ru/product/item-{pid}"><div class="product__item-info position-relative">
        <p class="title__link m-0"><span class="product__item__info-title product-name-id-{pid}">
          {title}  </span></p>
        <div class="product__item-rating--stars">{stars}</div>
        <div class="product__item-info--comments"><span>{reviews} отзывов</span></div>
        {old_html}<span class="product__item-price product-main-price-id-{pid}">{price:,} сум</span>
        <div class="installment__price  p-1 mt-2">{price // 12:,} сум x 12 мес</div>{flag}
      </div></a></div></div>""".replace(",", " ")


ASAXIY_PAGE_HTML = (
    '<div class="product__item d-flex">реклама в шапке без id</div>'
    '<div class="row custom-gutter loading-more-product-list  mb-40">'
    + ax_card(95688, "Настольный компьютер Apple Mac Mini 2024 M4, 16GB/256GB", 10_289_000, reviews=3)
    + ax_card(82838, "Беспроводной микрофон DJI Mic Mini", 1_239_000)
    + ax_card(70001, "Настольный компьютер Apple Mac Mini 2023 M2", 10_319_000, old=11_000_000, stock=False)
    + ax_card(70002, "Apple Mac mini M4 Pro", 19_000_000, preorder=True)
    + '</div><script>var dataJson = \'{"size":24,"totalCount":31}\';</script>')


class FakeShops:
    """Пять API магазинов. pages — сколько страниц у каждого; fail — магазин, который отвечает ошибкой."""

    def __init__(self, fail=None, pages=2):
        self.fail, self.pages, self.calls = fail, pages, []

    def __call__(self, url, body, raw=False):
        host = urlsplit(url).netloc
        self.calls.append((host, body))
        qs = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        if self.fail and self.fail in host:
            raise SH.ShopsError("ответил 500")
        if "idea" in host:
            page = int(qs["page"])
            data = ([idea(1, "Зарядка Ugreen Mini 20W", 99_000), idea(2, "Apple Mac mini M4 16/256", 9_500_000, 10_000_000)]
                    if page == 1 else [idea(3, "Apple Mac mini M4 16/512", 12_000_000, stock=0)])
            return {"data": data, "meta": {"total": 3, "last_page": self.pages}}
        if "alifshop" in host:
            page = body["page"]
            items = [{"id": f"a{page}", "slug": f"mac-mini-{page}", "price": 857_000_000, "old_price": 899_500_000,
                      "quantity": 3, "condition": {"duration": 24}, "partner": {"slug": "kuiko-warehouse"},
                      "has_guarantee": True, "brand_name": "Apple",
                      "product": {"name": "Системный блок Apple Mac mini [Apple M4]" if page == 1 else "MacBook Air M2"}}]
            return {"items": items, "n_items": 40, "n_pages": self.pages}
        if "texnomart" in host:
            return {"data": {"products": [{"id": 7, "name": "Яндекс Станция Мини 3", "sale_price": 1_599_000, "old_price": 0,
                                           "availability": "openToCart", "axiom_monthly_price": "от 106 600 сум / 24 мес.",
                                           "main_characters": [{"name": "Мощность", "value": "12 Вт"}]}],
                             "pagination": {"total_count": 1, "total_page": 1}}}
        if "mediapark" in host:
            return {"products": [{"id": "m1", "name": "Apple Mac mini M4 24/512", "price": "15000000", "oldPrice": "0",
                                  "available": False, "linkUrl": "/ru/products/view/mac-mini-1",
                                  "meta": {"installmentPrices": ["1500000"]}, "categories": [{"name": "Неттопы"}]}],
                    "totalHits": "1", "totalPages": 1}
        if "olcha" in host:
            return {"data": {"products": [{"id": 9, "name_ru": "Apple Mac mini M4", "alias": "apple-mac-mini-m4",
                                           "total_price": "12480600", "discount_price": 11000000, "discount": 1,
                                           "in_stock": True, "out_of_stock": False, "quantity": 10,
                                           "has_installment": 1, "monthly_repayment": 1614000,
                                           "plan": {"max_period": "12"}, "comments_rating": "4.50", "comments_count": 2,
                                           "warranty_month": 12, "store_id": 27482, "category": {"name_ru": "Компьютеры"}}],
                             "paginator": {"total": 1, "last_page": 1}}}
        if "asaxiy" in host:
            assert raw
            if "page=" in url:  # за последней страницей asaxiy отдаёт первую
                return ASAXIY_PAGE_HTML
            return ASAXIY_PAGE_HTML
        raise AssertionError(url)


class ParseTest(unittest.TestCase):
    def search(self, **kw):
        return SH.search("mac mini", get=kw.pop("get", FakeShops()), sleep=lambda s: None, **kw)

    def test_all_shops_parsed_and_filtered(self):
        rep = self.search()
        by = {}
        for i in rep["items"]:
            by.setdefault(i["shop"], []).append(i)
        self.assertEqual(sorted(by), ["alifshop", "asaxiy", "idea", "mediapark", "olcha"])  # у texnomart совпадений нет
        ax = {i["id"]: i for i in by["asaxiy"]}
        self.assertEqual(sorted(ax), ["70001", "70002", "95688"])  # микрофон «Mic Mini» отсеян
        m4 = ax["95688"]
        self.assertEqual((m4["price"], m4["in_stock"], m4["rating"], m4["reviews"], m4["installment"], m4["url"]),
                         (10_289_000, True, 4.0, 3, "от 857 416 сум × 12 мес", "https://asaxiy.uz/ru/product/item-95688"))
        self.assertEqual((ax["70001"]["in_stock"], ax["70001"]["price_old"], ax["70001"]["discount"]), (False, 11_000_000, 6))
        self.assertEqual((ax["70002"]["in_stock"], ax["70002"]["specs"], ax["70002"]["rating"]),
                         (False, {"Статус": "предзаказ"}, None))  # без отзывов звёзды-заглушки не считаем
        self.assertEqual([i["title"] for i in by["idea"]], ["Apple Mac mini M4 16/256", "Apple Mac mini M4 16/512"])
        a = by["alifshop"][0]
        self.assertEqual((a["price"], a["price_old"], a["discount"], a["seller"], a["warranty"], a["installment"]),
                         (8_570_000, 8_995_000, 5, "kuiko-warehouse", "есть", "рассрочка Alif до 24 мес"))
        self.assertEqual(a["url"], "https://alifshop.uz/ru/moderated-offer/mac-mini-1")
        o = by["olcha"][0]
        self.assertEqual((o["price"], o["price_old"], o["warranty"], o["rating"], o["url"]),
                         (11_000_000, 12_480_600, "12 мес", 4.5, "https://olcha.uz/ru/product/view/apple-mac-mini-m4"))
        self.assertIn("1 614 000 сум/мес, до 12 мес", o["installment"])
        m = by["mediapark"][0]
        self.assertEqual((m["in_stock"], m["url"], m["installment"]),
                         (False, "https://mediapark.uz/ru/products/view/mac-mini-1", "от 1 500 000 сум/мес"))
        self.assertEqual(by["idea"][0]["installment"], "от 1 583 333 сум × 6 мес")
        # сначала то, что в наличии, по цене; потом нет в наличии
        flags = [i["in_stock"] for i in rep["items"]]
        self.assertEqual(flags, sorted(flags, reverse=True))
        stocked = [i["price"] for i in rep["items"] if i["in_stock"]]
        self.assertEqual(stocked, sorted(stocked))
        tm = next(s for s in rep["stats"]["shops"] if s["shop"] == "texnomart.uz")
        self.assertEqual((tm["matched"], tm["unmatched_examples"]), (0, ["Яндекс Станция Мини 3"]))

    def test_strict_off_price_filter_and_pages(self):
        rep = self.search(strict=False)
        self.assertTrue(any(i["title"].startswith("Зарядка") for i in rep["items"]))
        rep = self.search(price_from=9_000_000, price_to=12_000_000)
        self.assertTrue(all(9_000_000 <= i["price"] <= 12_000_000 for i in rep["items"]))
        fake = FakeShops(pages=10)
        self.search(get=fake, max_pages=3)
        self.assertEqual(sum(1 for h, _ in fake.calls if "alifshop" in h), 3)  # не дальше max_pages

    def test_one_shop_failing_keeps_others(self):
        rep = self.search(get=FakeShops(fail="olcha"))
        ol = next(s for s in rep["stats"]["shops"] if s["shop"] == "olcha.uz")
        self.assertIn("olcha.uz ответил 500", ol["error"])
        self.assertTrue(any(i["shop"] == "idea" for i in rep["items"]))

    def test_matching_and_errors(self):
        self.assertTrue(SH.matches("Мини-ПК Beelink", SH.query_words("мини пк")))
        self.assertTrue(SH.matches("iPhone 15 Pro Max", SH.query_words("iphone 15")))
        self.assertFalse(SH.matches("iPhone 150", ["iphone", "15"]) and False)
        self.assertFalse(SH.matches("Яндекс Станция Мини", SH.query_words("mac mini")))
        self.assertTrue(SH.matches("Ёлка", ["елка"]))
        with self.assertRaises(SH.ShopsError):
            self.search(shops=["wildberries"])
        with self.assertRaises(SH.ShopsError):
            SH.search("  ", get=FakeShops())


class AsaxiyTest(unittest.TestCase):
    def test_pages_stop_when_site_repeats_first_page(self):
        calls = []

        def get(url, body, raw=False):
            calls.append(url)
            return ASAXIY_PAGE_HTML
        res = SH.search_shop("asaxiy", "mac mini", 50, get, max_pages=5, sleep=lambda s: None)
        self.assertEqual(calls, ["https://asaxiy.uz/ru/product?key=mac+mini"])  # <24 новых — последняя страница
        self.assertEqual(len(res["items"]), 3)

    def test_cloudflare_message(self):
        import io
        import urllib.error
        from unittest import mock
        err = urllib.error.HTTPError("u", 403, "x", {}, io.BytesIO(b"<title>Just a moment...</title>"))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(SH.ShopsError) as e:
                SH.http_json("https://asaxiy.uz/ru/product?key=x", raw=True)
        self.assertIn("Cloudflare", str(e.exception))


class ToolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["UZPARSER_DATA"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("UZPARSER_DATA", None)
        self.tmp.cleanup()

    def call(self, srv, name, args):
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": name, "arguments": args, "_meta": {"progressToken": "p"}}})["result"]
        return r, (json.loads(r["content"][0]["text"]) if not r.get("isError") else r["content"][0]["text"])

    def test_search_get_list(self):
        sent = []
        srv = S.Server(None, sent.append, sleep=lambda s: None)  # токен не нужен
        srv.shops_get = FakeShops()
        r, out = self.call(srv, "shops_search", {"query": "mac mini"})
        self.assertNotIn("isError", r)
        self.assertEqual(out["items"], 8)  # idea 2, alifshop 1, mediapark 1, olcha 1, asaxiy 3
        self.assertEqual(out["items_list"][0]["shop"], "alifshop.uz")
        self.assertEqual(out["items_list"][0]["price"], "8 570 000 сум")
        self.assertEqual(len(out["shops"]), 6)
        self.assertTrue(any(m.get("method") == "notifications/progress" for m in sent))
        r, part = self.call(srv, "shops_get_items", {"dump_id": out["dump_id"], "shop": "idea", "full": True})
        self.assertEqual(part["total"], 2)
        self.assertEqual(part["items_list"][0]["category"], "Компьютеры")
        r, dumps = self.call(srv, "shops_list_dumps", {})
        self.assertEqual(dumps[0]["dump_id"], out["dump_id"])

    def test_errors(self):
        srv = S.Server(None, lambda m: None, sleep=lambda s: None)
        srv.shops_get = FakeShops()
        self.assertTrue(self.call(srv, "shops_search", {"query": "x", "shops": ["ozon"]})[0]["isError"])
        self.assertTrue(self.call(srv, "shops_get_items", {"dump_id": "../../etc"})[0]["isError"])


if __name__ == "__main__":
    unittest.main()
