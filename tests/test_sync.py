"""Tester mot en låtsas-Sitoo (lokal HTTP-server) och en låtsas-databas.

Kör: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import base64
import json
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from sitoosync import jobs as J
from sitoosync.__main__ import is_due, run_job
from sitoosync.config import Config, Intervals
from sitoosync.db import SCHEMA_FILE, split_batches
from sitoosync.sitoo import SitooClient, SitooError

ACCOUNT = "1500"
AUTH = "Basic " + base64.b64encode(b"apiid:secret").decode()


# --------------------------------------------------------------- fake Sitoo
class FakeSitoo:
    def __init__(self):
        self.products = [{"productid": i, "sku": f"SKU{i}", "title": f"Produkt {i}",
                          "datemodified": 1_700_000_000 + i, "moneyprice": "10.00"} for i in range(1, 251)]
        self.stores = [{"storeid": 1, "name": "Butik A"}, {"storeid": 2, "name": "Butik B"}]
        self.warehouses = [{"warehouseid": 10, "name": "Lager A", "storeid": 1},
                           {"warehouseid": 11, "name": "Lager B", "storeid": 2}]
        self.stock = {10: [{"warehouseitemid": n, "sku": f"SKU{n}", "decimaltotal": "5.000",
                            "datelastmodified": 1_700_000_000 + n} for n in range(1, 31)],
                      11: []}
        self.orders = [{"orderid": i, "eshopid": 1, "orderdate": 1_700_000_000 + i * 60,
                        "orderitems": [{"sku": "SKU1", "quantity": 1}],
                        "payments": [{"name": "Kort", "moneyamount": "10.00"}]} for i in range(1, 121)]
        self.zreports = [{"registerid": "R1", "zreportid": i, "dateadded": 1_700_000_000 + i}
                         for i in range(1, 1201)]
        self.users = [{"userid": f"{{U{i}}}", "email": f"u{i}@ex.se"} for i in range(1, 46)]
        self.requests: list[tuple[str, dict]] = []
        self.rate_limit_next = 0
        self.max_num = 100

    def handle(self, path: str, q: dict) -> tuple[int, dict, object]:
        self.requests.append((path, q))
        if self.rate_limit_next > 0:
            self.rate_limit_next -= 1
            return 429, {"X-Rate-Limit-Reset": "0"}, {"statuscode": 429, "errortext": "Too many"}

        prefix = f"/v2/accounts/{ACCOUNT}"
        assert path.startswith(prefix), path
        p = path[len(prefix):]
        start = int(q.get("start", 0))
        num = min(int(q.get("num", 10)), self.max_num)

        def env(items):
            return 200, {}, {"totalcount": len(items), "items": items[start:start + num]}

        if p == "/sites/1/products.json":
            items = self.products
            if "datelastmodified" in q:
                items = [x for x in items if x["datemodified"] > int(q["datelastmodified"])]
            if "productid" in q:
                wanted = {int(x) for x in q["productid"].split(",")}
                items = [x for x in items if x["productid"] in wanted]
            if q.get("fields") == "productid":
                items = [{"productid": x["productid"]} for x in items]
            return env(items)
        if p == "/sites/1/stores.json":
            return env(self.stores)
        if p == "/sites/1/warehouses.json":
            return env(self.warehouses)
        if p.startswith("/sites/1/warehouses/") and p.endswith("/warehouseitems.json"):
            wid = int(p.split("/")[4])
            if wid not in self.stock:
                return 404, {}, {"statuscode": 404, "errortext": "warehouse not found"}
            items = self.stock[wid]
            if "datelastmodifiedfrom" in q:
                items = [x for x in items if x["datelastmodified"] >= int(q["datelastmodifiedfrom"])]
            if "sku" in q:
                wanted = set(q["sku"].split(","))
                items = [x for x in items if x["sku"] in wanted]
            return env(items)
        if p == "/orders":
            items = sorted(self.orders, key=lambda o: o["orderid"])
            if "orderidfrom" in q:
                items = [o for o in items if o["orderid"] >= int(q["orderidfrom"])]
            if "orderdatefrom" in q:
                items = [o for o in items if o["orderdate"] >= int(q["orderdatefrom"])]
            return env(items)
        if p == "/pos-z-reports":
            items = [z for z in self.zreports if z["dateadded"] >= int(q.get("dateaddedfrom", 0))]
            return 200, {}, items[:500]
        if p == "/sites/1/users.json":
            items = self.users
            if "userid" in q:
                wanted = set(q["userid"].split(","))
                items = [x for x in items if x["userid"] in wanted]
            return env(items)
        return 404, {}, {"statuscode": 404, "errortext": "not found"}


def start_server(fake: FakeSitoo):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.headers.get("Authorization") != AUTH:
                self.send_response(401)
                self.end_headers()
                return
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            status, headers, body = fake.handle(u.path, q)
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Sitoo-Request-ID", "test-req-id")
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ------------------------------------------------------------------ fake DB
class FakeDb:
    def __init__(self):
        self.state: dict[str, dict] = {}
        self.tables: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.logs: list[tuple] = []

    def exec_proc(self, proc, **params):
        self.calls.append((proc, params))
        if proc == "MarkDeletedProducts":
            ids = set(json.loads(params["IdsJson"]))
            t = self.tables.setdefault("Products", {})
            n = 0
            for key, row in t.items():
                if key[1] not in ids and not row.get("_deleted"):
                    row["_deleted"] = True
                    n += 1
            return n
        items = json.loads(params["Json"])
        keyfn = {
            "MergeStores": lambda r: (params["SiteId"], r["storeid"]),
            "MergeWarehouses": lambda r: (params["SiteId"], r["warehouseid"]),
            "MergeProducts": lambda r: (params["SiteId"], r["productid"]),
            "MergeStockItems": lambda r: (params["SiteId"], params["WarehouseId"], r["sku"]),
            "MergeOrders": lambda r: r["orderid"],
            "MergeZReports": lambda r: (r["registerid"], r["zreportid"]),
            "MergeUsers": lambda r: r["userid"],
        }[proc]
        t = self.tables.setdefault(proc.replace("Merge", ""), {})
        for r in items:
            t[keyfn(r)] = dict(r)
        return len(items)

    def _s(self, e):
        return self.state.setdefault(e, {"value": None, "success": None, "attempt": None, "error": None})

    def get_state(self, e):
        s = self.state.get(e)
        return (s["value"], s["success"]) if s else (None, None)

    def existing_ids(self, kind, ids, site=None):
        t = self.tables.get({"products": "Products", "users": "Users", "warehouses": "Warehouses"}[kind], {})
        if kind == "users":
            return {i for i in ids if i in t}
        return {i for i in ids if (site, i) in t}

    def get_last_attempt(self, e):
        s = self.state.get(e)
        return s["attempt"] if s else None

    def set_value(self, e, v):
        self._s(e)["value"] = v

    def mark_attempt(self, e):
        self._s(e)["attempt"] = datetime.now(timezone.utc)

    def mark_success(self, e):
        s = self._s(e)
        s["success"] = datetime.now(timezone.utc)
        s["error"] = None

    def mark_error(self, e, m):
        self._s(e)["error"] = m

    def log_run(self, *a):
        self.logs.append(a)

    def close(self):
        pass


def make_cfg(**kw) -> Config:
    base = dict(
        sitoo_base_url="", sitoo_account_id=ACCOUNT, sitoo_api_id="apiid", sitoo_api_password="secret",
        site_ids=[1], db_server="x", db_port=1433, db_name="SitooSync", db_user="u", db_password="p",
        intervals=Intervals(),
    )
    base.update(kw)
    return Config(**base)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSitoo()
        self.server = start_server(self.fake)
        url = f"http://127.0.0.1:{self.server.server_address[1]}/v2"
        self.cfg = make_cfg(sitoo_base_url=url)
        self.sleeps: list[float] = []
        self.api = SitooClient(url, ACCOUNT, "apiid", "secret", page_size=100, timeout=10,
                               sleep=self.sleeps.append)
        self.db = FakeDb()
        self.ctx = J.Context(cfg=self.cfg, api=self.api, db=self.db)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    # --- pagination & full syncs
    def test_products_full_then_incremental(self):
        J.sync_products(self.ctx)
        self.assertEqual(len(self.db.tables["Products"]), 250)
        product_calls = [r for r in self.fake.requests if r[0].endswith("products.json")]
        self.assertEqual(len(product_calls), 3)  # 100 + 100 + 50
        self.assertNotIn("datelastmodified", product_calls[0][1])
        self.assertEqual(product_calls[0][1]["includeinactive"], "true")
        self.assertEqual(self.db.get_state("products:1")[0], 1_700_000_250)

        # En produkt ändras -> nästa körning hämtar bara den (plus överlappet)
        self.fake.products[0]["datemodified"] = 1_700_000_999
        self.fake.products[0]["title"] = "Ny titel"
        self.fake.requests.clear()
        J.sync_products(self.ctx)
        call = self.fake.requests[0][1]
        self.assertEqual(int(call["datelastmodified"]), 1_700_000_250 - self.cfg.overlap_seconds)
        self.assertEqual(self.db.tables["Products"][(1, 1)]["title"], "Ny titel")
        self.assertEqual(self.db.get_state("products:1")[0], 1_700_000_999)

    def test_deleted_products_marked(self):
        J.sync_products(self.ctx)
        del self.fake.products[5]  # productid 6
        J.sync_deleted_products(self.ctx)
        deleted = [k for k, r in self.db.tables["Products"].items() if r.get("_deleted")]
        self.assertEqual(deleted, [(1, 6)])

    def test_deleted_products_skipped_when_incomplete(self):
        J.sync_products(self.ctx)
        # Simulera ett API som slutar leverera mitt i (totalcount stämmer inte med antal id)
        orig = self.api.pages

        def short_pages(path, params=None):
            for i, page in enumerate(orig(path, params)):
                if i == 0:
                    yield page
        self.api.pages = short_pages
        J.sync_deleted_products(self.ctx)
        self.assertFalse(any(c[0] == "MarkDeletedProducts" for c in self.db.calls))

    def test_stores_and_warehouses(self):
        J.sync_stores_and_warehouses(self.ctx)
        self.assertEqual(len(self.db.tables["Stores"]), 2)
        self.assertEqual(len(self.db.tables["Warehouses"]), 2)
        merge = [c for c in self.db.calls if c[0] == "MergeStores"][0]
        self.assertEqual(merge[1]["FullSync"], 1)

    def test_stock_per_warehouse_incremental(self):
        J.sync_stock(self.ctx)
        self.assertEqual(len(self.db.tables["StockItems"]), 30)
        self.assertEqual(self.db.get_state("stock:1:10")[0], 1_700_000_030)
        self.assertIsNotNone(self.db.get_state("stock:1:11")[0])  # tomt lager får också en markör
        self.fake.requests.clear()
        J.sync_stock(self.ctx)
        wh_calls = [r for r in self.fake.requests if "warehouseitems" in r[0]]
        self.assertTrue(all("datelastmodifiedfrom" in r[1] for r in wh_calls))

    def test_orders_incremental_by_orderid(self):
        J.sync_orders(self.ctx)
        self.assertEqual(len(self.db.tables["Orders"]), 120)
        self.assertEqual(self.db.get_state("orders:1")[0], 120)
        self.fake.orders.append({"orderid": 121, "eshopid": 1, "orderdate": 1_800_000_000})
        self.fake.requests.clear()
        J.sync_orders(self.ctx)
        q = self.fake.requests[0][1]
        self.assertEqual(q["orderidfrom"], "121")
        self.assertEqual(q["sort"], "orderid")
        self.assertEqual(q["eshopid"], "1")
        self.assertIn("orderitems", q["fields"])
        self.assertEqual(self.db.get_state("orders:1")[0], 121)

    def test_orders_from_date_limits_first_load(self):
        self.ctx.cfg.orders_from_date = "2023-11-14"
        J.sync_orders(self.ctx)
        q = self.fake.requests[0][1]
        self.assertEqual(int(q["orderdatefrom"]), 1_699_920_000)

    def test_orders_refresh_window(self):
        now = int(time.time())
        self.fake.orders = [{"orderid": 1, "orderdate": now - 10 * 86400},
                            {"orderid": 2, "orderdate": now - 3600}]
        J.refresh_recent_orders(self.ctx)
        self.assertEqual(set(self.db.tables["Orders"]), {2})

    def test_zreports_paging_over_500_cap(self):
        J.sync_zreports(self.ctx)
        self.assertEqual(len(self.db.tables["ZReports"]), 1200)
        self.assertEqual(self.db.get_state("zreports")[0], 1_700_001_200)
        calls = [r for r in self.fake.requests if r[0].endswith("/pos-z-reports")]
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][1]["eshopid"], "1")

    def test_zreports_same_timestamp_does_not_loop(self):
        self.fake.zreports = [{"registerid": "R1", "zreportid": i, "dateadded": 5} for i in range(600)]
        J.sync_zreports(self.ctx)  # skulle hänga om bläddringen inte kom framåt
        self.assertEqual(len(self.db.tables["ZReports"]), 500)

    def test_users(self):
        J.sync_users(self.ctx)
        self.assertEqual(len(self.db.tables["Users"]), 45)

    # --- robustness
    def test_rate_limit_429_is_retried(self):
        self.fake.rate_limit_next = 2
        J.sync_users(self.ctx)
        self.assertEqual(len(self.db.tables["Users"]), 45)
        self.assertEqual(self.sleeps, [2, 2])  # X-Rate-Limit-Reset 0 + 2 s marginal

    def test_4xx_is_not_retried(self):
        with self.assertRaises(SitooError) as cm:
            self.api.get("/finns-inte")
        self.assertEqual(cm.exception.status, 404)
        self.assertEqual(cm.exception.request_id, "test-req-id")
        self.assertEqual(self.sleeps, [])

    def test_wrong_password_gives_401(self):
        bad = SitooClient(self.api.root.rsplit("/accounts", 1)[0], ACCOUNT, "apiid", "fel", sleep=self.sleeps.append)
        with self.assertRaises(SitooError) as cm:
            bad.get("/sites/1/stores.json")
        self.assertEqual(cm.exception.status, 401)

    def test_unicode_survives(self):
        self.fake.users = [{"userid": "{Å}", "email": "åsa@exempel.se", "namefirst": "Åsa Örn"}]
        J.sync_users(self.ctx)
        sent = [c for c in self.db.calls if c[0] == "MergeUsers"][0][1]["Json"]
        self.assertIn("Åsa Örn", sent)

    def test_run_job_records_error_and_retries_later(self):
        def boom(ctx):
            raise RuntimeError("testfel")
        job = J.Job("boom", 60, boom)
        self.assertFalse(run_job(job, self.ctx))
        self.assertEqual(self.db.state["job:boom"]["error"], "RuntimeError: testfel")
        now = datetime.now(timezone.utc)
        self.assertFalse(is_due(job, self.db, 10, now))
        self.assertTrue(is_due(job, self.db, 10, now + timedelta(minutes=11)))

    def test_scheduler_due_logic(self):
        job = J.Job("x", 15 * 60, lambda c: 0)
        now = datetime.now(timezone.utc)
        self.assertTrue(is_due(job, self.db, 10, now))
        self.assertTrue(run_job(job, self.ctx))
        self.assertFalse(is_due(job, self.db, 10, now + timedelta(minutes=14)))
        self.assertTrue(is_due(job, self.db, 10, now + timedelta(minutes=16)))

    def test_build_jobs_respects_disabled(self):
        cfg = make_cfg(intervals=Intervals(users=0, zreports=0))
        names = [j.name for j in J.build_jobs(cfg)]
        self.assertNotIn("users", names)
        self.assertNotIn("zreports", names)
        self.assertIn("orders", names)


class EnrichTests(unittest.TestCase):
    """Komplettering när nya ordrar kommer in."""

    setUp = SyncTests.setUp
    tearDown = SyncTests.tearDown

    def _first_load(self):
        J.sync_stores_and_warehouses(self.ctx)
        J.sync_products(self.ctx)
        J.sync_users(self.ctx)
        J.sync_orders(self.ctx)        # första hämtningen kompletteras inte
        self.fake.requests.clear()
        self.db.calls.clear()

    def _new_order(self, **kw):
        order = {"orderid": 500, "eshopid": 1, "orderdate": 1_800_000_000, "warehouseid": 10,
                 "userid_staff": "{U1}",
                 "orderitems": [{"productid": 3, "sku": "SKU3", "quantity": 1}]}
        order.update(kw)
        self.fake.orders.append(order)
        return order

    def test_first_load_is_not_enriched(self):
        J.sync_orders(self.ctx)
        paths = {r[0].rsplit("/", 1)[-1] for r in self.fake.requests}
        self.assertEqual(paths, {"orders"})

    def test_sold_stock_refreshed(self):
        self._first_load()
        self.fake.stock[10].append({"warehouseitemid": 99, "sku": "SKU3", "decimaltotal": "4.000",
                                    "datelastmodified": 1_800_000_000})
        self._new_order()
        J.sync_orders(self.ctx)
        stock_calls = [r for r in self.fake.requests if "warehouseitems" in r[0]]
        self.assertEqual(len(stock_calls), 1)
        self.assertEqual(stock_calls[0][1]["sku"], "SKU3")
        self.assertIn("/warehouses/10/", stock_calls[0][0])
        self.assertEqual(self.db.tables["StockItems"][(1, 10, "SKU3")]["decimaltotal"], "4.000")

    def test_existing_product_and_staff_not_refetched(self):
        self._first_load()
        self._new_order()
        J.sync_orders(self.ctx)
        self.assertFalse([r for r in self.fake.requests if r[0].endswith("products.json")])
        self.assertFalse([r for r in self.fake.requests if r[0].endswith("users.json")])

    def test_missing_product_and_staff_fetched(self):
        self._first_load()
        self.fake.products.append({"productid": 999, "sku": "NY1", "title": "Ny produkt", "datemodified": 1})
        self.fake.users.append({"userid": "{PERSONAL}", "email": "kassa@ex.se", "role": 40})
        self._new_order(userid_staff="{PERSONAL}",
                        orderitems=[{"productid": 999, "sku": "NY1"}, {"productid": 3, "sku": "SKU3"}])
        J.sync_orders(self.ctx)
        product_calls = [r for r in self.fake.requests if r[0].endswith("products.json")]
        self.assertEqual([c[1]["productid"] for c in product_calls], ["999"])
        self.assertIn((1, 999), self.db.tables["Products"])
        user_calls = [r for r in self.fake.requests if r[0].endswith("users.json")]
        self.assertEqual([c[1]["userid"] for c in user_calls], ["{PERSONAL}"])
        self.assertIn("{PERSONAL}", self.db.tables["Users"])

    def test_unknown_warehouse_refreshes_stores(self):
        self._first_load()
        self.fake.warehouses.append({"warehouseid": 12, "name": "Nytt lager"})
        self.fake.stock[12] = []
        self._new_order(warehouseid=12)
        J.sync_orders(self.ctx)
        self.assertIn((1, 12), self.db.tables["Warehouses"])

    def test_enrich_failure_does_not_fail_orders(self):
        self._first_load()
        self.fake.users.append({"userid": "{PERSONAL}", "email": "kassa@ex.se"})
        self._new_order(warehouseid=77, userid_staff="{PERSONAL}")
        J.sync_orders(self.ctx)          # får inte kasta
        self.assertIn(500, self.db.tables["Orders"])
        self.assertEqual(self.db.get_state("orders:1")[0], 500)
        self.assertIn("{PERSONAL}", self.db.tables["Users"])   # övriga steg körs ändå

    def test_enrich_can_be_disabled(self):
        self._first_load()
        self.ctx.cfg.enrich_orders = False
        self._new_order()
        J.sync_orders(self.ctx)
        self.assertEqual({r[0].rsplit("/", 1)[-1] for r in self.fake.requests}, {"orders"})

    def test_quiet_job_without_rows_not_logged(self):
        J.sync_orders(self.ctx)
        job = J.Job("orders", 60, J.sync_orders, quiet=True)
        self.db.logs.clear()
        self.assertTrue(run_job(job, self.ctx))
        self.assertEqual(self.db.logs, [])
        self._new_order()
        self.assertTrue(run_job(job, self.ctx))
        self.assertEqual(len(self.db.logs), 1)


class SchemaTests(unittest.TestCase):
    def test_schema_splits_and_has_all_procs(self):
        batches = split_batches(SCHEMA_FILE.read_text(encoding="utf-8"))
        text = "\n".join(batches)
        for proc in ["MergeStores", "MergeWarehouses", "MergeProducts", "MarkDeletedProducts",
                     "MergeStockItems", "MergeOrders", "MergeZReports", "MergeUsers"]:
            self.assertIn(f"CREATE OR ALTER PROCEDURE sitoo.{proc}", text)
        # Varje CREATE OR ALTER måste ligga först i sin egen batch
        for b in batches:
            if "CREATE OR ALTER" in b:
                code = "\n".join(l for l in b.splitlines() if not l.strip().startswith("--")).lstrip()
                self.assertTrue(code.startswith("CREATE OR ALTER"), b[:80])
                self.assertEqual(b.count("CREATE OR ALTER"), 1)

    def test_procs_called_with_existing_parameters(self):
        text = SCHEMA_FILE.read_text(encoding="utf-8")
        expected = {
            "MergeStores": ["@SiteId", "@Json", "@FullSync"],
            "MergeWarehouses": ["@SiteId", "@Json", "@FullSync"],
            "MergeProducts": ["@SiteId", "@Json"],
            "MarkDeletedProducts": ["@SiteId", "@IdsJson"],
            "MergeStockItems": ["@SiteId", "@WarehouseId", "@Json"],
            "MergeOrders": ["@Json"],
            "MergeZReports": ["@Json"],
            "MergeUsers": ["@Json"],
        }
        for proc, params in expected.items():
            head = text.split(f"CREATE OR ALTER PROCEDURE sitoo.{proc}", 1)[1].split("AS\nBEGIN", 1)[0]
            for p in params:
                self.assertIn(p, head, f"{proc} saknar {p}")

    def test_no_reserved_column_names(self):
        text = SCHEMA_FILE.read_text(encoding="utf-8").upper()
        for word in [" LINENO ", " KEY ", " ROWCOUNT "]:
            self.assertNotIn(word + "  ", text)


if __name__ == "__main__":
    unittest.main()
