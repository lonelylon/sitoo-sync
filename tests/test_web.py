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
        self.assertEqual(cfg.intervals.orders, 15)

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


class WebTests(unittest.TestCase):
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
