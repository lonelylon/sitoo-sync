"""Tester för inställningar, bakgrundsworker och webbgränssnitt."""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone

import test_sync as T

from sitoosync.settings import SettingsStore
from sitoosync.web import WebApp
from sitoosync.worker import LogBuffer, SyncWorker

COMPLETE = {
    "sitoo_base_url": "https://api.mysitoo.com/v2", "sitoo_account_id": "1500", "sitoo_api_id": "apiid",
    "sitoo_api_password": "secret", "site_ids": "1", "mssql_server": "192.168.1.10", "mssql_port": "1433",
    "mssql_database": "SitooSync", "mssql_user": "sitoosync", "mssql_password": "dbpass",
    "mssql_create_database": "1",
}


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SettingsStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_until_filled(self):
        self.assertIn("Konto-id", self.store.missing())
        self.assertEqual(self.store.update(COMPLETE), [])
        self.assertEqual(self.store.missing(), [])
        cfg = self.store.config()
        self.assertEqual(cfg.site_ids, [1])
        self.assertEqual(cfg.db_port, 1433)
        self.assertEqual(cfg.intervals.orders, 60)
        self.assertEqual(cfg.intervals.products, 900)

    def test_persisted_and_secret_kept_when_blank(self):
        self.store.update(COMPLETE)
        again = SettingsStore(self.tmp.name)
        self.assertEqual(again.values()["sitoo_api_password"], "secret")
        again.update({**COMPLETE, "sitoo_api_password": "", "site_ids": "1, 2"})
        v = again.values()
        self.assertEqual(v["sitoo_api_password"], "secret")
        self.assertEqual(v["site_ids"], "1,2")

    def test_validation_errors_save_nothing(self):
        self.store.update(COMPLETE)
        errors = self.store.update({**COMPLETE, "mssql_port": "abc", "orders_from_date": "2024-13-40"})
        self.assertEqual(len(errors), 2)
        self.assertEqual(self.store.values()["mssql_port"], 1433)

    def test_checkbox_unchecked_means_false(self):
        self.store.update(COMPLETE)
        form = dict(COMPLETE)
        del form["mssql_create_database"]
        self.store.update(form)
        self.assertFalse(self.store.values()["mssql_create_database"])

    def test_password(self):
        self.assertFalse(self.store.has_password())
        self.store.set_password("hemligt123")
        self.assertTrue(self.store.check_password("hemligt123"))
        self.assertFalse(self.store.check_password("fel"))

    def test_env_used_as_default(self):
        import os
        os.environ["SITOO_ACCOUNT_ID"] = "777"
        try:
            store = SettingsStore(tempfile.mkdtemp())
            self.assertEqual(store.values()["sitoo_account_id"], "777")
        finally:
            del os.environ["SITOO_ACCOUNT_ID"]

    def test_paused(self):
        v0 = self.store.version
        self.store.set_paused(True)
        self.assertTrue(SettingsStore(self.tmp.name).paused)
        self.assertGreater(self.store.version, v0)


class WebFakeDb(T.FakeDb):
    def __init__(self):
        super().__init__()
        self.resets = []

    def ensure_database(self): pass
    def apply_schema(self): pass

    def job_overview(self):
        return {"orders": {"attempt": datetime.now(timezone.utc), "success": None, "error": "SitooError: HTTP 401"},
                "products": {"attempt": None, "success": datetime.now(timezone.utc), "error": None, "rows": 250}}

    def table_counts(self):
        return {"Products": 250, "Orders": 1200}

    def last_runs(self, limit=100):
        now = datetime.now(timezone.utc)
        return [{"job": "products", "started": now, "finished": now, "status": "ok", "rows": 250,
                 "message": "3 API-anrop, 1 s"}]

    def reset_entity(self, pattern):
        self.resets.append(pattern)
        return 1


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SettingsStore(self.tmp.name)
        self.fake = T.FakeSitoo()
        self.server = T.start_server(self.fake)
        url = f"http://127.0.0.1:{self.server.server_address[1]}/v2"
        self.store.update({**COMPLETE, "sitoo_account_id": T.ACCOUNT, "sitoo_base_url": url})
        self.db = WebFakeDb()
        self.worker = SyncWorker(self.store, db_factory=lambda cfg: self.db)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_tick_runs_all_jobs_then_waits(self):
        self.worker._tick()
        self.assertEqual(self.worker.status.state, "väntar")
        self.assertEqual(len(self.db.tables["Products"]), 250)
        self.assertEqual(len(self.db.tables["Orders"]), 120)
        n = len(self.fake.requests)
        self.worker._tick()                     # inget är förfallet direkt efter
        self.assertEqual(len(self.fake.requests), n)

    def test_paused_skips_but_run_now_works(self):
        self.store.set_paused(True)
        self.worker._tick()
        self.assertEqual(self.worker.status.state, "pausad")
        self.assertNotIn("Products", self.db.tables)
        self.worker.request_run("users")
        self.worker._tick()
        self.assertEqual(len(self.db.tables["Users"]), 45)
        self.assertNotIn("Products", self.db.tables)
        self.assertEqual(self.worker.requested(), set())

    def test_orders_run_between_pages_of_long_job(self):
        self.worker._tick()                      # allt körs en gång
        # Ordrar är på tur igen och en ny order finns
        earlier = datetime.now(timezone.utc) - timedelta(minutes=5)
        self.db.state["job:orders"]["attempt"] = earlier
        self.db.state["job:orders"]["success"] = earlier
        self.fake.orders.append({"orderid": 700, "eshopid": 1, "orderdate": 1_800_000_000})
        self.fake.requests.clear()
        products = next(j for j in self.worker.status.jobs if j.name == "products")
        self.db.state.pop("products:1", None)    # tvinga full hämtning: 3 sidor
        self.worker.request_run("products")
        self.assertTrue(self.worker._run_if_due(products, self.worker._ctx))
        paths = [r[0].rsplit("/", 1)[-1] for r in self.fake.requests]
        first_products = paths.index("products.json")
        self.assertIn("orders", paths[first_products:])
        self.assertEqual(paths.count("orders"), 1)
        self.assertEqual(paths[-1], "products.json")   # produktjobbet fortsatte efteråt
        self.assertIn(700, self.db.tables["Orders"])
        self.assertIsNone(self.worker.status.current_job)

    def test_orders_first_in_job_order(self):
        self.worker._tick()
        self.assertEqual(self.worker.status.jobs[0].name, "orders")

    def test_not_configured(self):
        store = SettingsStore(tempfile.mkdtemp())
        w = SyncWorker(store, db_factory=lambda cfg: self.db)
        w._tick()
        self.assertEqual(w.status.state, "ej konfigurerad")

    def test_settings_change_reloads(self):
        self.worker._tick()
        self.store.update({**self.store.values(), "interval_users": "0", "mssql_create_database": "1"})
        self.worker._tick()
        self.assertNotIn("users", [j.name for j in self.worker.status.jobs])

    def test_thread_starts_and_stops(self):
        self.worker.tick_seconds = 0.05
        self.worker.start()
        self.worker.stop()
        self.worker.join(timeout=10)
        self.assertFalse(self.worker.is_alive())


class Client:
    """Minimal webbläsare med cookies."""

    def __init__(self, port):
        self.port = port
        self.cookie = None

    def req(self, method, path, form=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if self.cookie:
            headers["Cookie"] = self.cookie
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8")
        set_cookie = resp.getheader("Set-Cookie")
        if set_cookie:
            self.cookie = set_cookie.split(";", 1)[0]
        conn.close()
        return resp.status, resp.getheader("Location"), data

    def csrf(self, path="/"):
        _, _, html = self.req("GET", path)
        marker = 'name="csrf" value="'
        i = html.index(marker) + len(marker)
        return html[i:html.index('"', i)]


class WebBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SettingsStore(self.tmp.name)
        self.db = WebFakeDb()
        self.worker = SyncWorker(self.store, db_factory=lambda cfg: self.db)
        self.logbuf = LogBuffer()
        self.logbuf.lines.append("2026-10-20 23:00:00 INFO    Startar products <test>")
        self.sitoo_calls = []
        app = WebApp(self.store, self.worker, self.logbuf,
                     db_factory=lambda cfg, login_timeout=5: self.db,
                     sitoo_factory=self._fake_sitoo)
        self.app = app
        self.server = app.make_server("127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.c = Client(self.server.server_address[1])

    def _fake_sitoo(self, values):
        test = self

        class S:
            def get(self, path, params=None):
                test.sitoo_calls.append((values["sitoo_api_password"], path))
                if values["sitoo_api_password"] == "fel":
                    from sitoosync.sitoo import SitooError
                    raise SitooError("Sitoo svarade HTTP 401", status=401)
                return {"totalcount": 3}
        return S()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def login(self):
        self.store.set_password("hemligt123")
        status, loc, _ = self.c.req("POST", "/loggain", {"password": "hemligt123"})
        self.assertEqual((status, loc), (303, "/"))

class WebTests(WebBase):
    def test_health_without_login(self):
        self.assertEqual(self.c.req("GET", "/health")[0], 200)

    def test_first_start_flow(self):
        status, loc, _ = self.c.req("GET", "/")
        self.assertEqual((status, loc), (303, "/forsta-start"))
        status, _, html = self.c.req("POST", "/forsta-start", {"password": "kort", "password2": "kort"})
        self.assertIn("minst 8", html)
        status, loc, _ = self.c.req("POST", "/forsta-start", {"password": "hemligt123", "password2": "hemligt123"})
        self.assertEqual((status, loc), (303, "/installningar"))
        self.assertTrue(self.store.check_password("hemligt123"))
        status, _, html = self.c.req("GET", "/installningar")
        self.assertEqual(status, 200)
        self.assertIn("Spara inställningar", html)
        # Andra gången går det inte att sätta lösenord via första start
        other = Client(self.c.port)
        status, loc, _ = other.req("POST", "/forsta-start", {"password": "annat12345", "password2": "annat12345"})
        self.assertEqual(loc, "/loggain")
        self.assertTrue(self.store.check_password("hemligt123"))

    def test_requires_login(self):
        self.store.set_password("hemligt123")
        for path in ["/", "/installningar", "/logg"]:
            status, loc, _ = self.c.req("GET", path)
            self.assertEqual((status, loc), (303, "/loggain"), path)
        status, _, html = self.c.req("POST", "/loggain", {"password": "fel"})
        self.assertEqual(status, 401)
        self.assertIn("Fel lösenord", html)

    def test_csrf_required(self):
        self.login()
        status, _, _ = self.c.req("POST", "/paus", {"csrf": "fel"})
        self.assertEqual(status, 403)
        self.assertFalse(self.store.paused)
        status, loc, _ = self.c.req("POST", "/paus", {"csrf": self.c.csrf()})
        self.assertEqual((status, loc), (303, "/"))
        self.assertTrue(self.store.paused)

    def test_save_settings_and_secret_not_rendered(self):
        self.login()
        form = {**COMPLETE, "csrf": self.c.csrf("/installningar")}
        status, _, html = self.c.req("POST", "/installningar", form)
        self.assertEqual(status, 200)
        self.assertIn("Inställningarna är sparade", html)
        self.assertEqual(self.store.values()["mssql_password"], "dbpass")
        self.assertNotIn("dbpass", html)
        self.assertNotIn('value="secret"', html)
        self.assertIn("Sparat – lämna tomt för att behålla", html)

    def test_save_settings_validation_error(self):
        self.login()
        form = {**COMPLETE, "mssql_port": "abc", "csrf": self.c.csrf("/installningar")}
        _, _, html = self.c.req("POST", "/installningar", form)
        self.assertIn("Port: måste vara ett heltal", html)
        self.assertIn('value="abc"', html)   # det användaren skrev ligger kvar

    def test_overview_shows_jobs_errors_and_counts(self):
        self.login()
        self.store.update(COMPLETE)
        from sitoosync.jobs import build_jobs
        self.worker.status.jobs = build_jobs(self.store.config())
        self.worker.status.state = "väntar"
        _, _, html = self.c.req("GET", "/")
        self.assertIn("Nya ordrar", html)
        self.assertIn("SitooError: HTTP 401", html)
        self.assertIn("250", html)
        self.assertIn("1 200", html)
        self.assertIn("Kör nu", html)

    def test_overview_unconfigured(self):
        self.login()
        _, _, html = self.c.req("GET", "/")
        self.assertIn("Ej konfigurerad", html)
        self.assertIn("inställningarna", html)

    def test_run_now_and_reset(self):
        self.login()
        self.store.update(COMPLETE)
        csrf = self.c.csrf()
        self.c.req("POST", "/kor/zreports", {"csrf": csrf})
        self.assertIn("zreports", self.worker.requested())
        self.c.req("POST", "/kor/finnsinte", {"csrf": csrf})
        self.assertNotIn("finnsinte", self.worker.requested())
        self.c.req("POST", "/hamta-om/stock", {"csrf": csrf})
        self.assertEqual(self.db.resets, ["stock:%"])
        self.assertIn("stock", self.worker.requested())

    def test_sitoo_test_uses_unsaved_form_and_stored_secret(self):
        self.login()
        self.store.update(COMPLETE)
        csrf = self.c.csrf("/installningar")
        form = {**COMPLETE, "sitoo_api_password": "", "site_ids": "1,2", "csrf": csrf}
        _, _, body = self.c.req("POST", "/api/test/sitoo", form)
        d = json.loads(body)
        self.assertTrue(d["ok"], d)
        self.assertIn("Site 2: OK, 3 butiker", d["message"])
        self.assertEqual(self.sitoo_calls[0][0], "secret")
        self.assertEqual(self.store.values()["site_ids"], "1")   # testet sparar inget
        form["sitoo_api_password"] = "fel"
        d = json.loads(self.c.req("POST", "/api/test/sitoo", form)[2])
        self.assertFalse(d["ok"])
        self.assertIn("Fel API-id eller lösenord", d["message"])

    def test_db_test_reports_missing_fields(self):
        self.login()
        csrf = self.c.csrf("/installningar")
        d = json.loads(self.c.req("POST", "/api/test/db", {"csrf": csrf, "mssql_server": "x"})[2])
        self.assertFalse(d["ok"])
        self.assertIn("Fyll i alla fält", d["message"])

    def test_log_page_escapes(self):
        self.login()
        self.store.update(COMPLETE)
        _, _, html = self.c.req("GET", "/logg")
        self.assertIn("&lt;test&gt;", html)
        self.assertIn("3 API-anrop", html)

    def test_change_password(self):
        self.login()
        csrf = self.c.csrf("/installningar")
        _, _, html = self.c.req("POST", "/losenord", {"csrf": csrf, "current": "fel", "new": "nyttlosen1", "new2": "nyttlosen1"})
        self.assertIn("stämmer inte", html)
        _, _, html = self.c.req("POST", "/losenord", {"csrf": csrf, "current": "hemligt123", "new": "nyttlosen1", "new2": "nyttlosen1"})
        self.assertIn("Lösenordet är bytt", html)
        self.assertTrue(self.store.check_password("nyttlosen1"))

    def test_logout(self):
        self.login()
        self.c.req("POST", "/loggaut", {"csrf": self.c.csrf()})
        status, loc, _ = self.c.req("GET", "/")
        self.assertEqual(loc, "/loggain")


if __name__ == "__main__":
    unittest.main()


# ------------------------------------------------------------------ rapportsidor
from decimal import Decimal as _Dec  # noqa: E402
from datetime import date as _date  # noqa: E402

import sitoosync.web_reports as WR  # noqa: E402
from sitoosync.reports import Stamp as _Stamp, assemble_zrows as _assemble  # noqa: E402


class FakeReports:
    calls: list = []
    stamps_data: list = []
    zdata: tuple = ([], [], [])
    deposits_data: list = []

    def __init__(self, db):
        self.db = db

    def stamps(self, start, end, sku):
        FakeReports.calls.append(("stamps", start, end, sku))
        return list(FakeReports.stamps_data)

    def staff_names(self):
        return ["Anna Andersson", "Bo Berg"]

    def removed_stamps(self):
        return [{"OrderId": 77, "Kommentar": "Dubbel", "OrderDateLocal": datetime(2026, 9, 1, 10, 0), "Namn": "Bo Berg"}]

    def add_stamp(self, name, at, comment):
        FakeReports.calls.append(("add", name, at, comment))

    def delete_manual_stamp(self, i):
        FakeReports.calls.append(("delete_manual", i))

    def manual_stamps(self, start, end):
        return [{"Id": 5, "Namn": "Anna Andersson", "Stampling": datetime(2026, 8, 22, 10, 0), "Kommentar": "Glömt"}]

    def all_manual_stamps(self):
        FakeReports.calls.append(("all_manual",))
        return self.manual_stamps(None, None) + [
            {"Id": 1, "Namn": "Bo Berg", "Stampling": datetime(2021, 9, 4, 15, 45), "Kommentar": "Från Excel"}]

    def update_manual_stamp(self, i, name, at, comment):
        FakeReports.calls.append(("update_manual", i, name, at, comment))

    def remove_order_stamp(self, o, c):
        FakeReports.calls.append(("remove", o))

    def restore_order_stamp(self, o):
        FakeReports.calls.append(("restore", o))

    def zrows(self, start, end):
        FakeReports.calls.append(("zrows", start, end))
        return _assemble(*FakeReports.zdata)

    def deposits(self, start, end):
        return list(FakeReports.deposits_data)

    def add_deposit(self, d, a, c):
        FakeReports.calls.append(("deposit", d, a, c))

    def delete_deposit(self, i):
        FakeReports.calls.append(("delete_deposit", i))

    def set_zreport_date(self, rid, zid, d, c):
        FakeReports.calls.append(("zdate", rid, zid, d))


class ReportBase(WebBase):
    def setUp(self):
        super().setUp()
        self._orig = WR.Reports
        WR.Reports = FakeReports
        FakeReports.calls = []
        FakeReports.stamps_data = [
            _Stamp("Sitoo", 1001, "Anna Andersson", datetime(2026, 8, 21, 9, 59)),
            _Stamp("Sitoo", 1002, "Anna Andersson", datetime(2026, 8, 21, 18, 0)),
            _Stamp("Manuell", 5, "Anna Andersson", datetime(2026, 8, 22, 10, 0)),
        ]
        FakeReports.zdata = (
            [{"RegisterId": "R1", "ZReportId": 1845, "RegisterNumber": 1, "Dag": _date(2026, 9, 1),
              "OriginalDag": _date(2026, 9, 1), "NyttDatum": None, "Stangd": None, "MoneySalesTotal": "19997.50",
              "MoneyRefundTotal": "0", "MoneyRoundOff": "0", "MoneySummaryTotal": "19997.50", "NumReceipts": 50,
              "CashToBank": "-1000.00"}],
            [{"RegisterId": "R1", "ZReportId": 1845, "Kind": "sales", "VatValue": 25, "MoneyTotal": "19264.50",
              "MoneyTotalNet": "15411.60", "MoneyTotalVat": "3852.90"},
             {"RegisterId": "R1", "ZReportId": 1845, "Kind": "sales", "VatValue": 6, "MoneyTotal": "733.00",
              "MoneyTotalNet": "691.50", "MoneyTotalVat": "41.50"}],
            [{"RegisterId": "R1", "ZReportId": 1845, "Kind": "sales", "Name": "Kort", "MoneyTotal": "19962.50"},
             {"RegisterId": "R1", "ZReportId": 1845, "Kind": "sales", "Name": "Swish", "MoneyTotal": "35.00"}])
        FakeReports.deposits_data = []
        self.login()
        self.store.update(COMPLETE)

    def tearDown(self):
        WR.Reports = self._orig
        super().tearDown()

class ReportPageTests(ReportBase):
    def test_time_page(self):
        status, _, html = self.c.req("GET", "/tid?period=2026-09")
        self.assertEqual(status, 200)
        self.assertIn("Anna Andersson", html)
        self.assertIn("2026-08-21 – 2026-09-20", html)
        self.assertIn("8,02", html)                 # 09:59–18:00
        self.assertIn("Bara en stämpling", html)
        self.assertIn("Sitoo order 1001", html)
        self.assertIn("Order 77", html)              # borttagen
        self.assertEqual(FakeReports.calls[0], ("stamps", _date(2026, 8, 21), _date(2026, 9, 20), "91000"))

    def test_time_person_filter_and_csv(self):
        _, _, html = self.c.req("GET", "/tid?period=2026-09&person=Bo+Berg")
        self.assertIn("Inga stämplingar i perioden", html)
        status, _, body = self.c.req("GET", "/tid.csv?period=2026-09")
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith("﻿Namn;Datum"))
        self.assertIn("Anna Andersson;2026-08-21;fre;09:59;18:00;2;8,02;", body)

    def test_time_actions(self):
        csrf = self.c.csrf("/tid?period=2026-09")
        back = "/tid?period=2026-09"
        _, loc, _ = self.c.req("POST", "/tid/lagg-till", {"csrf": csrf, "back": back, "namn": "Bo Berg",
                                                          "datum": "2026-09-01", "tid": "17:30", "kommentar": "Glömt"})
        self.assertEqual(loc, back)
        self.assertIn(("add", "Bo Berg", datetime(2026, 9, 1, 17, 30), "Glömt"), FakeReports.calls)
        self.c.req("POST", "/tid/ta-bort", {"csrf": csrf, "back": back, "order_id": "1001"})
        self.c.req("POST", "/tid/aterstall", {"csrf": csrf, "back": back, "order_id": "77"})
        self.c.req("POST", "/tid/radera-manuell", {"csrf": csrf, "back": back, "id": "5"})
        self.assertIn(("remove", 1001), FakeReports.calls)
        self.assertIn(("restore", 77), FakeReports.calls)
        self.assertIn(("delete_manual", 5), FakeReports.calls)
        # Elak back-adress ignoreras
        _, loc, _ = self.c.req("POST", "/tid/ta-bort", {"csrf": csrf, "back": "//evil.example", "order_id": "1"})
        self.assertEqual(loc, "/tid")

    def test_accounting_page(self):
        status, _, html = self.c.req("GET", "/bokforing?manad=2026-09")
        self.assertEqual(status, 200)
        self.assertIn("Balanserar", html)
        self.assertIn("19 962,50", html)            # kort på 1937
        self.assertIn("15 411,60", html)            # 25 % netto
        self.assertIn("Kassa 1", html)
        self.assertIn("−1 000,00", html)            # uttag till bank
        self.assertEqual(FakeReports.calls[0], ("zrows", _date(2026, 9, 1), _date(2026, 9, 30)))

    def test_accounting_custom_period_and_csv(self):
        self.c.req("GET", "/bokforing?fran=2026-09-01&till=2026-09-15")
        self.assertEqual(FakeReports.calls[-1], ("zrows", _date(2026, 9, 1), _date(2026, 9, 15)))
        _, _, body = self.c.req("GET", "/bokforing.csv?manad=2026-09")
        self.assertIn("1937;Försäljning – Kort;19962,50;0,00", body)
        self.assertIn("Kassa 1;1845;2026-09-01;19997,50", body)

    def test_accounting_actions(self):
        csrf = self.c.csrf("/bokforing?manad=2026-09")
        back = "/bokforing?manad=2026-09"
        self.c.req("POST", "/bokforing/bankning", {"csrf": csrf, "back": back, "datum": "2026-09-30",
                                                   "belopp": "12 500,50", "kommentar": ""})
        self.assertIn(("deposit", _date(2026, 9, 30), _Dec("12500.50"), ""), FakeReports.calls)
        self.c.req("POST", "/bokforing/datum", {"csrf": csrf, "back": back, "register_id": "R1",
                                                "zreport_id": "1845", "datum": "2026-08-31"})
        self.assertIn(("zdate", "R1", 1845, _date(2026, 8, 31)), FakeReports.calls)
        self.c.req("POST", "/bokforing/datum", {"csrf": csrf, "back": back, "register_id": "R1",
                                                "zreport_id": "1845", "datum": "2026-08-31", "atgard": "aterstall"})
        self.assertIn(("zdate", "R1", 1845, None), FakeReports.calls)

    def test_accounting_unbalanced_warns(self):
        FakeReports.zdata[2].pop()      # Swish saknas -> 35 kr diff
        _, _, html = self.c.req("GET", "/bokforing?manad=2026-09")
        self.assertIn("balanserar inte", html)


class ManualAndPrintTests(ReportBase):
    def test_manual_list_and_toggle(self):
        _, _, html = self.c.req("GET", "/tid?period=2026-09")
        self.assertIn("Manuella stämplingar", html)
        self.assertIn('value="Glömt"', html)
        self.assertIn('value="2026-08-22"', html)
        self.assertIn("Visa alla manuella stämplingar", html)
        _, _, html = self.c.req("GET", "/tid?period=2026-09&alla_manuella=1")
        self.assertIn(("all_manual",), FakeReports.calls)
        self.assertIn("Från Excel", html)
        self.assertIn("Visa bara perioden", html)

    def test_manual_update_and_delete(self):
        csrf = self.c.csrf("/tid?period=2026-09")
        back = "/tid?period=2026-09"
        self.c.req("POST", "/tid/manuell", {"csrf": csrf, "back": back, "id": "5", "namn": "Anna Andersson",
                                            "datum": "2026-08-22", "tid": "10:15", "kommentar": "Rättad",
                                            "atgard": "spara"})
        self.assertIn(("update_manual", 5, "Anna Andersson", datetime(2026, 8, 22, 10, 15), "Rättad"),
                      FakeReports.calls)
        self.c.req("POST", "/tid/manuell", {"csrf": csrf, "back": back, "id": "5", "atgard": "radera"})
        self.assertIn(("delete_manual", 5), FakeReports.calls)

    def test_print_pages(self):
        status, _, html = self.c.req("GET", "/tid/utskrift?period=2026-09")
        self.assertEqual(status, 200)
        self.assertIn("@page{size:A4 portrait;margin:12mm}", html)
        self.assertIn("Tidrapport – löneperiod 2026-09", html)
        self.assertIn("Attesterad av", html)
        self.assertNotIn("<header>", html)
        status, _, html = self.c.req("GET", "/bokforing/utskrift?manad=2026-09")
        self.assertEqual(status, 200)
        self.assertIn("@page{size:A4 landscape;margin:10mm}", html)
        self.assertIn("Bokföringsunderlag 2026-09", html)
        self.assertIn("Balanserar", html)
        self.assertIn("19 962,50", html)

    def test_pages_link_to_print(self):
        _, _, html = self.c.req("GET", "/tid?period=2026-09")
        self.assertIn('href="/tid/utskrift?period=2026-09&amp;person="', html)
        _, _, html = self.c.req("GET", "/bokforing?manad=2026-09")
        self.assertIn('href="/bokforing/utskrift?manad=2026-09"', html)
