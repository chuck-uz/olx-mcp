import io
import json
import unittest
import urllib.error

from uzparser import server as S


def offer(i, uzs=5_000_000):
    return {"id": i, "url": f"https://www.olx.uz/d/obyavlenie/x-ID{i}.html", "title": f"iPhone {i}",
            "price": {"value": 450, "currency": "USD", "uzs": uzs, "label": "450 у.е.", "negotiable": True},
            "condition": "Б/у", "params": {"Марка": "Apple"}, "location": "Ташкент, Юнусабадский район",
            "created": "2026-10-03T10:00:00+05:00", "seller": {"name": "FOCUS", "business": True, "since": "2025-01-02"},
            "promoted": False, "text": "x" * 500}


REPORT = {"url": "https://www.olx.uz/list/q-iphone/", "title": None, "fetched_at": "2026-10-04T10:00:00+00:00",
          "filters": {"query": "iphone", "params": {"query": "iphone"}},
          "stats": {"offers": 120, "total_on_site": 1000, "with_price": 120, "currencies": {"USD": 120},
                    "price_uzs": {"min": 1e6, "median": 5e6, "max": 9e6}, "business": 3, "from": "2026-09-01",
                    "to": "2026-10-04"},
          "offers": [offer(i) for i in range(120)]}


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


UZUM_REPORT = {"url": "https://uzum.uz/ru/search?query=mini%20pc", "title": "«mini pc»",
               "fetched_at": "2026-10-05T10:00:00+00:00",
               "filters": {"query": "mini pc", "sort": "BY_PRICE_ASC"},
               "stats": {"items": 60, "total_on_site": 184, "with_price": 60,
                         "price_uzs": {"min": 36000, "median": 2313655, "max": 13685740},
                         "with_discount": 50, "avg_rating": 4.49},
               "items": [{"id": i, "title": f"Mini PC {i}", "price": 177210, "price_card": 162890 if i == 0 else None,
                          "price_full": 179000, "discount": 1, "rating": 4.6, "reviews": 206,
                          "installment": "12 679 сум/мес", "delivery": "Завтра", "labels": ["Стало дешевле"],
                          "url": f"https://uzum.uz/ru/product/{i}"} for i in range(60)]}


class FakeHttp:
    """Имитирует API сервера парсеров: задание сначала running, потом done."""

    def uzum(self, path):
        if path == "/api/uzum/jobs":
            return {"id": "u1", "status": "running", "fetched": 0, "total": None}
        if path == "/api/uzum/jobs/u1":
            return {"id": "u1", "status": "done", "fetched": 60, "dump_id": "uzum_q_mini_pc-20261005-100000-abcdef"}
        if path == "/api/uzum/dumps":
            return [{"id": "uzum_q_mini_pc-20261005-100000-abcdef", **{k: v for k, v in UZUM_REPORT.items() if k != "items"}}]
        return UZUM_REPORT

    def __init__(self, fail=None, cached=False):
        self.calls, self.polls, self.fail, self.cached = [], 0, fail, cached

    def __call__(self, req, timeout):
        body = json.loads(req.data) if req.data else None
        self.calls.append((req.get_method(), req.full_url, body, req.headers.get("Authorization")))
        if self.fail:
            raise urllib.error.HTTPError(req.full_url, self.fail, "x", {}, io.BytesIO(b'{"detail": "boom"}'))
        path = req.full_url.split("tools.test", 1)[1]
        if path.startswith("/api/uzum/"):
            return FakeResp(json.dumps(self.uzum(path)).encode())
        if path == "/api/olx/jobs":
            data = ({"id": "j1", "status": "done", "fetched": 120, "dump_id": "olx_q_iphone-20261004-100000-abcdef",
                     "cached": True} if self.cached else {"id": "j1", "status": "queued", "fetched": 0})
        elif path == "/api/olx/jobs/j1":
            self.polls += 1
            data = ({"id": "j1", "status": "running", "fetched": 50, "total": 1000} if self.polls == 1
                    else {"id": "j1", "status": "done", "fetched": 120, "dump_id": "olx_q_iphone-20261004-100000-abcdef"})
        elif path == "/api/olx/dumps":
            data = [{"id": "olx_q_iphone-20261004-100000-abcdef", **{k: v for k, v in REPORT.items() if k != "offers"}}]
        elif path.startswith("/api/olx/dumps/"):
            data = REPORT
        else:
            raise AssertionError(path)
        return FakeResp(json.dumps(data).encode())


def make(http=None, token=True):
    sent = []
    api = S.Api("https://tools.test/", "tok", opener=http or FakeHttp()) if token else None
    return S.Server(api, sent.append, sleep=lambda s: None), sent


def call(srv, name, args, token=None):
    params = {"name": name, "arguments": args}
    if token is not None:
        params["_meta"] = {"progressToken": token}
    r = srv.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": params})
    res = r["result"]
    return res, (json.loads(res["content"][0]["text"]) if not res.get("isError") else res["content"][0]["text"])


class ProtocolTest(unittest.TestCase):
    def test_initialize_and_list(self):
        srv, _ = make()
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-03-26", "capabilities": {}}})
        self.assertEqual(r["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("OLX", r["result"]["instructions"])
        r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}})
        self.assertEqual(r["result"]["protocolVersion"], S.PROTOCOL_VERSIONS[0])
        self.assertIsNone(srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        names = [t["name"] for t in srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
        self.assertEqual(names, ["olx_search", "olx_get_offers", "uzum_search", "uzum_get_items", "uzum_list_dumps",
                                 "avtoelon_search", "avtoelon_get_items", "avtoelon_list_dumps", "yandex_search", "yandex_get_items",
                                 "yandex_list_dumps", "shops_search", "shops_get_items", "shops_list_dumps", "uybor_search",
                                 "uybor_get_items", "uybor_list_dumps",
                                 "olx_list_dumps"])
        self.assertEqual(srv.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})["error"]["code"], -32601)

    def test_main_loop_over_stdio(self):
        import sys
        lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}), "", "not json"]
        old_in, old_out = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = io.StringIO("\n".join(lines) + "\n"), io.StringIO()
        try:
            S.main()
            out = [json.loads(l) for l in sys.stdout.getvalue().splitlines()]
        finally:
            sys.stdin, sys.stdout = old_in, old_out
        self.assertEqual(out[0], {"jsonrpc": "2.0", "id": 1, "result": {}})
        self.assertEqual(out[1]["error"]["code"], -32700)


class ToolsTest(unittest.TestCase):
    def test_search_flow_with_progress(self):
        http = FakeHttp()
        srv, sent = make(http)
        res, out = call(srv, "olx_search", {"query": "iphone 15", "limit": 120}, token="p1")
        self.assertNotIn("isError", res)
        self.assertEqual(http.calls[0][:3], ("POST", "https://tools.test/api/olx/jobs",
                                             {"url": "iphone 15", "limit": 120, "photos": False}))
        self.assertEqual(http.calls[0][3], "Bearer tok")
        self.assertEqual(out["dump_id"], "olx_q_iphone-20261004-100000-abcdef")
        self.assertEqual(out["price_uzs"]["median"], "5 000 000")
        self.assertEqual(len(out["offers_list"]), 50)
        self.assertIn("offset=50", out["more"])
        o = out["offers_list"][0]
        self.assertEqual(o["price"], "450 USD (≈5 000 000 сум), торг")
        self.assertEqual(o["seller"], "магазин FOCUS, на OLX с 2025-01")
        self.assertTrue(o["text"].endswith("…") and len(o["text"]) <= S.TEXT_CUT + 1)
        self.assertNotIn("promoted", o)
        prog = [m["params"] for m in sent if m["method"] == "notifications/progress"]
        self.assertEqual(prog[0]["progressToken"], "p1")
        self.assertEqual(prog[0]["total"], 120)

    def test_get_offers_paging_and_full(self):
        srv, _ = make()
        _, out = call(srv, "olx_get_offers", {"dump_id": "olx_q_iphone-20261004-100000-abcdef", "offset": 100,
                                              "count": 50, "full": True})
        self.assertEqual(out["shown"], "100–120")
        self.assertNotIn("more", out)
        self.assertEqual(len(out["offers_list"][0]["text"]), 500)
        res, err = call(srv, "olx_get_offers", {"dump_id": "../../etc"})
        self.assertTrue(res["isError"])

    def test_list_dumps(self):
        srv, _ = make()
        _, out = call(srv, "olx_list_dumps", {})
        self.assertEqual(out[0]["median_uzs"], "5 000 000")
        self.assertEqual(out[0]["query"], "iphone")

    def test_errors_are_tool_results(self):
        srv, _ = make(token=False)
        res, msg = call(srv, "olx_search", {"query": "x"})
        self.assertTrue(res["isError"]) and self.assertIn("UZPARSER_TOKEN", msg)
        srv, _ = make(FakeHttp(fail=401))
        res, msg = call(srv, "olx_list_dumps", {})
        self.assertIn("токен", msg)
        srv, _ = make(FakeHttp(fail=400))
        res, msg = call(srv, "olx_search", {"query": "x"})
        self.assertEqual(msg, "Сервер парсеров ответил 400: boom")
        res, msg = call(srv, "olx_search", {})
        self.assertIn("Неверные аргументы", msg)
        res, msg = call(srv, "nope", {})
        self.assertTrue(res["isError"])
        srv, _ = make(FakeHttp(fail=429))  # OLX забанил сервер: просим не долбить повторами
        res, msg = call(srv, "olx_search", {"query": "x"})
        self.assertTrue(res["isError"])
        self.assertTrue(msg.startswith("boom.") and "Не повторяй поиск OLX" in msg)

    def test_cached_search_is_marked(self):
        http = FakeHttp(cached=True)
        srv, _ = make(http)
        _, out = call(srv, "olx_search", {"query": "iphone"})
        self.assertIn("10:00 UTC", out["cached"])
        self.assertEqual(http.polls, 0)  # готовое задание — без опроса
        _, plain = call(make()[0], "olx_search", {"query": "iphone"})
        self.assertNotIn("cached", plain)


class UzumToolsTest(unittest.TestCase):
    def test_search(self):
        http = FakeHttp()
        srv, sent = make(http)
        res, out = call(srv, "uzum_search", {"query": " mini pc ", "limit": 60, "sort": "cheap"}, token="p")
        self.assertNotIn("isError", res)
        self.assertEqual(http.calls[0][:3], ("POST", "https://tools.test/api/uzum/jobs",
                                             {"url": "mini pc", "limit": 60, "sort": "cheap", "photos": False}))
        self.assertEqual(out["sort"], "BY_PRICE_ASC")
        self.assertEqual(out["price_uzs"]["median"], "2 313 655")
        self.assertEqual(len(out["items_list"]), 50)
        self.assertIn("uzum_get_items", out["more"])
        i = out["items_list"][0]
        self.assertEqual((i["price"], i["price_card"], i["discount"], i["rating"]),
                         ("177 210 сум", "162 890", "−1% от 179 000", "4.6 (206 отз.)"))
        self.assertNotIn("price_card", out["items_list"][1])

    def test_get_items_and_list(self):
        srv, _ = make()
        _, out = call(srv, "uzum_get_items", {"dump_id": "uzum_q_mini_pc-20261005-100000-abcdef", "offset": 50})
        self.assertEqual(out["shown"], "50–60")
        self.assertNotIn("more", out)
        _, out = call(srv, "uzum_list_dumps", {})
        self.assertEqual(out[0]["items"], 60)
        self.assertEqual(out[0]["median_uzs"], "2 313 655")

    def test_bad_sort(self):
        srv, _ = make()
        res, msg = call(srv, "uzum_search", {"query": "x", "sort": "random"})
        self.assertTrue(res["isError"])
        self.assertIn("popular", msg)


class BuildQueryTest(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(S.build_query("  iphone 15 "), "iphone 15")

    def test_filters_on_query(self):
        self.assertEqual(S.build_query("iphone 15", price_to=5e6, sort="cheap"),
                         "https://www.olx.uz/list/q-iphone-15/?search%5Bfilter_float_price%3Ato%5D=5000000"
                         "&search%5Border%5D=filter_float_price%3Aasc")

    def test_filters_on_url(self):
        self.assertEqual(S.build_query("https://www.olx.uz/transport/?a=1", price_from=1000),
                         "https://www.olx.uz/transport/?a=1&search%5Bfilter_float_price%3Afrom%5D=1000")

    def test_bad_sort(self):
        with self.assertRaises(S.ToolError):
            S.build_query("x", sort="random")


class MultiTest(unittest.TestCase):
    def test_multi_offer_keeps_long_text_and_note(self):
        plain = S.compact_offer(offer(1))
        self.assertNotIn("multi", plain)
        self.assertEqual(len(plain["text"]), S.TEXT_CUT + 1)  # обрезано + «…»
        multi = S.compact_offer({**offer(2), "multi": True})
        self.assertEqual(multi["multi"], S.MULTI_NOTE)
        self.assertEqual(len(multi["text"]), 500)  # целиком: цены позиций в описании

    def test_summary_counts_multi(self):
        self.assertIsNone(S.summary(REPORT, "d")["multi_item_offers"])
        rep = {**REPORT, "stats": {**REPORT["stats"], "multi": 7}}
        self.assertTrue(S.summary(rep, "d")["multi_item_offers"].startswith("7 "))


class ProgressFileTest(unittest.TestCase):
    def test_search_writes_progress_and_cleans_up(self):
        import os, tempfile
        d = tempfile.mkdtemp()
        seen = []
        http = FakeHttp()
        srv = S.Server(S.Api("https://tools.test/", "tok", opener=http), lambda m: None,
                       sleep=lambda s: seen.extend(json.load(open(os.path.join(d, f))) for f in os.listdir(d)),
                       progress_dir=d)
        res = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "olx_search", "arguments": {"query": "iphone 15", "limit": 120}}})
        self.assertNotIn("isError", res["result"])
        self.assertTrue(seen)
        self.assertEqual(seen[0]["tool"], "olx_search")
        self.assertEqual(seen[0]["query"], "iphone 15")
        self.assertEqual(os.listdir(d), [])  # по завершении файл удалён

    def test_non_search_tools_and_no_dir_write_nothing(self):
        import os, tempfile
        d = tempfile.mkdtemp()
        srv = S.Server(S.Api("https://tools.test/", "tok", opener=FakeHttp()), lambda m: None,
                       sleep=lambda s: None, progress_dir=d)
        srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "olx_list_dumps", "arguments": {}}})
        self.assertEqual(os.listdir(d), [])
        f = S.ProgressFile(None, "olx_search", {"query": "x"}, 1)
        f.update(5, 10, "Собрано 5 из 10")
        f.remove()
        self.assertIsNone(f.path)


if __name__ == "__main__":
    unittest.main()
