"""Webbgränssnitt för inställningar, status och manuella körningar.

Byggt på Pythons inbyggda http.server, inga externa beroenden.
"""
from __future__ import annotations

import html
import json
import logging
import secrets
import threading
import time
from datetime import datetime, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlparse

from .config import FIELDS, ConfigError
from .db import Database
from .settings import SettingsStore
from .sitoo import SitooClient, SitooError
from .web_reports import ReportPagesMixin
from .worker import JOB_LABELS, RESET_PATTERNS, LogBuffer, SyncWorker, next_run, utcnow

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("Europe/Stockholm")
except Exception:  # noqa: BLE001
    LOCAL_TZ = timezone.utc

log = logging.getLogger("sitoosync.web")

SESSION_HOURS = 12
MIN_PASSWORD = 8

TABLE_LABELS = [
    ("Stores", "Butiker"),
    ("Warehouses", "Lager"),
    ("Products", "Produkter"),
    ("StockItems", "Lagersaldon"),
    ("Orders", "Ordrar"),
    ("OrderItems", "Orderrader"),
    ("OrderPayments", "Orderbetalningar"),
    ("ZReports", "Z-rapporter"),
    ("Users", "Personer"),
]

SECTIONS = [
    ("sitoo", "Sitoo", "Uppgifterna finns i Sitoo Backoffice under Inställningar › Sitoo REST API."),
    ("db", "SQL Server", "Databasen där datan sparas. Tabeller skapas automatiskt i schemat sitoo."),
    ("schedule", "Schema", "Minuter mellan körningar. 0 stänger av jobbet."),
    ("other", "Övrigt", ""),
    ("time", "Tidrapport", "Stämplingar görs genom att artikeln nedan scannas i kassan."),
    ("accounting", "Bokföring", "Konton i verifikationen på sidan Bokföring."),
]


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def fmt_time(dt: datetime | None) -> str:
    if not dt:
        return "–"
    local = dt.astimezone(LOCAL_TZ)
    today = datetime.now(LOCAL_TZ).date()
    if local.date() == today:
        return local.strftime("idag %H:%M")
    return local.strftime("%Y-%m-%d %H:%M")


def _ord(n: int) -> str:
    return f"{n}:a" if n % 10 in (1, 2) and n % 100 not in (11, 12) else f"{n}:e"


def fmt_interval(seconds: int) -> str:
    if seconds < 60:
        return f"var {seconds}:e sekund"
    if seconds % 60:
        return f"var {seconds} s"
    minutes = seconds // 60
    if minutes % 1440 == 0:
        days = minutes // 1440
        return "varje dygn" if days == 1 else f"var {_ord(days)} dygn"
    if minutes % 60 == 0:
        hours = minutes // 60
        return "varje timme" if hours == 1 else f"var {_ord(hours)} timme"
    return "varje minut" if minutes == 1 else f"var {minutes} min"


def fmt_relative(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return "nu"
    seconds = (dt - now).total_seconds()
    if seconds <= 2:
        return "nu"
    if seconds < 60:
        return f"om {int(seconds)} s"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"om {minutes} min"
    hours = minutes // 60
    if hours < 24:
        return f"om {hours} h {minutes % 60} min"
    return fmt_time(dt)


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2330;--muted:#5d6675;--line:#e3e6eb;--accent:#2457d6;--accent-ink:#fff;
--ok:#16794a;--ok-bg:#e5f5ec;--warn:#9a5b00;--warn-bg:#fff3dc;--err:#b42318;--err-bg:#fdecea;--run:#2457d6;--run-bg:#e8eefc;
--radius:10px;font-synthesis:none}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1a1f27;--text:#e7eaf0;--muted:#9aa3b2;--line:#2b323d;
--accent:#6b93ff;--accent-ink:#0b1020;--ok:#5fd394;--ok-bg:#15301f;--warn:#f0b65a;--warn-bg:#33270f;--err:#ff8a7d;--err-bg:#3a1714;
--run:#8fb0ff;--run-bg:#1b2747}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--accent)}
header{background:var(--card);border-bottom:1px solid var(--line)}
.wrap{max-width:1080px;margin:0 auto;padding:0 20px}
header .wrap{display:flex;align-items:center;gap:24px;height:58px}
.brand{font-weight:650;letter-spacing:-.01em;white-space:nowrap}
nav{display:flex;gap:4px;flex:1;overflow-x:auto}
nav a{padding:6px 12px;border-radius:8px;text-decoration:none;color:var(--muted);white-space:nowrap}
nav a.on,nav a:hover{background:var(--bg);color:var(--text)}
main{padding:28px 0 60px}
h1{font-size:22px;margin:0 0 18px;letter-spacing:-.01em}
h2{font-size:16px;margin:0 0 4px}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:20px;margin-bottom:20px}
.card>p.lead{margin:0 0 16px;color:var(--muted)}
.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.spacer{flex:1}
.muted{color:var(--muted)}
.small{font-size:13px}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);font-weight:600}
tr:last-child td{border-bottom:0}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.scroll{overflow-x:auto}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;font-size:13px;font-weight:600;white-space:nowrap}
.b-ok{background:var(--ok-bg);color:var(--ok)}.b-err{background:var(--err-bg);color:var(--err)}
.b-warn{background:var(--warn-bg);color:var(--warn)}.b-run{background:var(--run-bg);color:var(--run)}
.b-idle{background:var(--bg);color:var(--muted)}
.errtext{color:var(--err);font-size:13px;margin-top:4px;word-break:break-word}
button,.btn{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--text);padding:7px 14px;
border-radius:8px;cursor:pointer;text-decoration:none;display:inline-block;white-space:nowrap}
button:hover,.btn:hover{border-color:var(--muted)}
button.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink);font-weight:600}
button.danger{color:var(--err)}
button.sm{padding:4px 10px;font-size:13px}
button:disabled{opacity:.55;cursor:default}
form.inline{display:inline}
.grid{display:flex;flex-wrap:wrap;gap:16px 20px}.grid>div{flex:1 1 260px;min-width:0}
label{display:block;font-weight:550;font-size:14px;margin-bottom:4px}
input[type=text],input[type=password],input[type=number]{width:100%;font:inherit;padding:8px 10px;border-radius:8px;
border:1px solid var(--line);background:var(--bg);color:var(--text)}
input:focus{outline:2px solid var(--accent);outline-offset:1px;border-color:transparent}
.check{display:flex;gap:8px;align-items:center;font-weight:500}
.help{font-size:12.5px;color:var(--muted);margin-top:4px}
.alert{border-radius:var(--radius);padding:12px 16px;margin-bottom:20px;border:1px solid transparent}
.a-ok{background:var(--ok-bg);color:var(--ok)}.a-err{background:var(--err-bg);color:var(--err)}
.a-warn{background:var(--warn-bg);color:var(--warn)}
.result{font-size:14px;margin-top:10px;min-height:1.5em;white-space:pre-wrap}
pre.log{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:12px;font-size:12.5px;
line-height:1.45;max-height:560px;overflow:auto;white-space:pre-wrap;word-break:break-word;margin:0}
.auth{max-width:380px;margin:9vh auto}
.stats{display:flex;flex-wrap:wrap;gap:12px}
.stat{flex:1 1 140px;border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.stat b{display:block;font-size:20px;font-variant-numeric:tabular-nums}
.stat span{font-size:13px;color:var(--muted)}
@media (max-width:640px){.wrap{padding:0 16px}header .wrap{gap:12px}.brand{display:none}
.hide-sm{display:none}th,td{padding:9px 6px}}
"""

TEST_JS = """
async function runTest(btn, url){
  const form = btn.closest('form'); const out = document.getElementById(btn.dataset.out);
  btn.disabled = true; out.className='result muted'; out.textContent='Testar…';
  try{
    const r = await fetch(url,{method:'POST',body:new URLSearchParams(new FormData(form))});
    const d = await r.json(); out.className='result '+(d.ok?'a-ok':'a-err'); out.textContent=d.message;
  }catch(e){ out.className='result a-err'; out.textContent='Kunde inte testa: '+e; }
  btn.disabled = false;
}
"""


class WebApp:
    def __init__(self, store: SettingsStore, worker: SyncWorker, logbuf: LogBuffer,
                 db_factory: Callable = Database.from_config,
                 sitoo_factory: Callable | None = None):
        self.store = store
        self.worker = worker
        self.logbuf = logbuf
        self.db_factory = db_factory
        self.sitoo_factory = sitoo_factory or (lambda v: SitooClient(
            v["sitoo_base_url"], v["sitoo_account_id"], v["sitoo_api_id"], v["sitoo_api_password"],
            timeout=20, max_retries=0, max_error_retries=0))
        self.sessions: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ sessioner
    def new_session(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self.sessions[token] = {"csrf": secrets.token_urlsafe(24), "expires": time.time() + SESSION_HOURS * 3600}
        return token

    def get_session(self, token: str | None) -> dict | None:
        if not token:
            return None
        with self._lock:
            s = self.sessions.get(token)
            if not s:
                return None
            if s["expires"] < time.time():
                del self.sessions[token]
                return None
            s["expires"] = time.time() + SESSION_HOURS * 3600
            return s

    def end_session(self, token: str | None) -> None:
        with self._lock:
            self.sessions.pop(token or "", None)

    # ------------------------------------------------------------ databas
    def _db(self) -> Database | None:
        try:
            cfg = self.store.config()
        except ConfigError:
            return None
        return self.db_factory(cfg, login_timeout=5)

    def make_server(self, host: str, port: int) -> ThreadingHTTPServer:
        app = self

        class Handler(RequestHandler):
            pass

        Handler.app = app
        server = ThreadingHTTPServer((host, port), Handler)
        server.daemon_threads = True
        return server


class RequestHandler(ReportPagesMixin, BaseHTTPRequestHandler):
    app: WebApp
    server_version = "sitoo-sync"
    sys_version = ""

    # ------------------------------------------------------------ hjälp
    def log_message(self, fmt, *args):  # tyst: åtkomstlogg behövs inte
        pass

    def _token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = cookies.SimpleCookie()
        try:
            jar.load(raw)
        except cookies.CookieError:
            return None
        return jar["sid"].value if "sid" in jar else None

    def _form(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            return {}
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        return {k: v[-1] for k, v in parse_qs(body, keep_blank_values=True).items()}

    def _send(self, status: int, body: str, content_type: str = "text/html; charset=utf-8",
              headers: dict | None = None) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _redirect(self, location: str, headers: dict | None = None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _json(self, data: dict, status: int = 200) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8")

    def _page(self, title: str, body: str, active: str = "", nav: bool = True, refresh: int | None = None,
              status: int = 200) -> None:
        links = [("/", "Översikt", "home"), ("/tid", "Tidrapport", "time"), ("/bokforing", "Bokföring", "accounting"),
                 ("/installningar", "Inställningar", "settings"), ("/logg", "Logg", "log")]
        nav_html = ""
        if nav:
            items = "".join(f'<a href="{href}" class="{"on" if key == active else ""}">{label}</a>'
                            for href, label, key in links)
            csrf = self._session_csrf()
            nav_html = (f'<header><div class="wrap"><div class="brand">Sitoo Sync</div><nav>{items}</nav>'
                        f'<form class="inline" method="post" action="/loggaut">'
                        f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
                        f'<button class="sm" type="submit">Logga ut</button></form></div></header>')
        meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
        doc = (f'<!doctype html><html lang="sv"><head><meta charset="utf-8">'
               f'<meta name="viewport" content="width=device-width,initial-scale=1">{meta}'
               f'<title>{esc(title)} · Sitoo Sync</title><style>{CSS}</style></head>'
               f'<body>{nav_html}<main><div class="wrap">{body}</div></main></body></html>')
        self._send(status, doc)

    def _session_csrf(self) -> str:
        s = self.app.get_session(self._token())
        return s["csrf"] if s else ""

    def _csrf_field(self) -> str:
        return f'<input type="hidden" name="csrf" value="{esc(self._session_csrf())}">'

    # ------------------------------------------------------------ routing
    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            return self._send(200, "ok", "text/plain; charset=utf-8")
        if not self.app.store.has_password():
            return self._redirect("/forsta-start") if path != "/forsta-start" else self.page_setup()
        if path == "/loggain":
            return self.page_login()
        if not self.app.get_session(self._token()):
            return self._redirect("/loggain")
        routes = {"/": self.page_overview, "/installningar": self.page_settings, "/logg": self.page_log,
                  "/tid": self.page_time, "/tid.csv": self.csv_time,
                  "/bokforing": self.page_accounting, "/bokforing.csv": self.csv_accounting}
        handler = routes.get(path)
        if handler is None:
            return self._page("Hittades inte", "<h1>Sidan finns inte</h1>", status=404)
        return handler()

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        form = self._form()
        if path == "/forsta-start":
            return self.post_setup(form)
        if path == "/loggain":
            return self.post_login(form)
        session = self.app.get_session(self._token())
        if not session:
            if path.startswith("/api/"):
                return self._json({"ok": False, "message": "Du är utloggad, ladda om sidan"}, 401)
            return self._redirect("/loggain")
        if not secrets.compare_digest(form.get("csrf", ""), session["csrf"]):
            if path.startswith("/api/"):
                return self._json({"ok": False, "message": "Ogiltig begäran, ladda om sidan"}, 403)
            return self._page("Fel", '<div class="alert a-err">Ogiltig begäran. Ladda om sidan och försök igen.</div>',
                              status=403)

        if path == "/loggaut":
            self.app.end_session(self._token())
            return self._redirect("/loggain", {"Set-Cookie": "sid=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"})
        if path == "/installningar":
            return self.post_settings(form)
        if path == "/losenord":
            return self.post_password(form)
        if path == "/paus":
            self.app.store.set_paused(True)
            log.info("Synken pausad från webbgränssnittet")
            return self._redirect("/")
        if path == "/starta":
            self.app.store.set_paused(False)
            self.app.worker.wake()
            log.info("Synken startad från webbgränssnittet")
            return self._redirect("/")
        if path.startswith("/kor/"):
            name = path[len("/kor/"):]
            if name in JOB_LABELS:
                self.app.worker.request_run(name)
                log.info("Körning av %s begärd från webbgränssnittet", name)
            return self._redirect("/")
        if path.startswith("/tid/"):
            return self.post_time(path, form)
        if path.startswith("/bokforing/"):
            return self.post_accounting(path, form)
        if path.startswith("/hamta-om/"):
            return self.post_reset(path[len("/hamta-om/"):])
        if path == "/api/test/sitoo":
            return self.api_test_sitoo(form)
        if path == "/api/test/db":
            return self.api_test_db(form)
        return self._page("Hittades inte", "<h1>Sidan finns inte</h1>", status=404)

    # ------------------------------------------------------------ inloggning
    def page_setup(self, error: str = "") -> None:
        alert = f'<div class="alert a-err">{esc(error)}</div>' if error else ""
        body = f"""<div class="auth"><div class="card">
<h1>Välkommen</h1>
<p class="lead">Välj ett lösenord för webbgränssnittet. Det skyddar dina Sitoo- och databasuppgifter.</p>
{alert}
<form method="post" action="/forsta-start">
<p><label for="p1">Lösenord</label><input id="p1" type="password" name="password" autocomplete="new-password" required minlength="{MIN_PASSWORD}"></p>
<p><label for="p2">Upprepa lösenordet</label><input id="p2" type="password" name="password2" autocomplete="new-password" required></p>
<button class="primary" type="submit">Spara och fortsätt</button>
<p class="help">Minst {MIN_PASSWORD} tecken.</p>
</form></div></div>"""
        self._page("Första start", body, nav=False)

    def post_setup(self, form: dict) -> None:
        if self.app.store.has_password():
            return self._redirect("/loggain")
        pw, pw2 = form.get("password", ""), form.get("password2", "")
        if len(pw) < MIN_PASSWORD:
            return self.page_setup(f"Lösenordet måste vara minst {MIN_PASSWORD} tecken")
        if pw != pw2:
            return self.page_setup("Lösenorden är inte lika")
        self.app.store.set_password(pw)
        log.info("Lösenord för webbgränssnittet skapat")
        token = self.app.new_session()
        return self._redirect("/installningar", {"Set-Cookie": self._cookie(token)})

    @staticmethod
    def _cookie(token: str) -> str:
        return f"sid={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_HOURS * 3600}"

    def page_login(self, error: str = "") -> None:
        alert = f'<div class="alert a-err">{esc(error)}</div>' if error else ""
        body = f"""<div class="auth"><div class="card">
<h1>Sitoo Sync</h1>
{alert}
<form method="post" action="/loggain">
<p><label for="pw">Lösenord</label><input id="pw" type="password" name="password" autocomplete="current-password" autofocus required></p>
<button class="primary" type="submit">Logga in</button>
</form></div></div>"""
        self._page("Logga in", body, nav=False, status=401 if error else 200)

    def post_login(self, form: dict) -> None:
        if self.app.store.check_password(form.get("password", "")):
            token = self.app.new_session()
            return self._redirect("/", {"Set-Cookie": self._cookie(token)})
        time.sleep(1)  # bromsar gissningsförsök
        return self.page_login("Fel lösenord")

    def post_password(self, form: dict) -> None:
        if not self.app.store.check_password(form.get("current", "")):
            return self.page_settings(error="Nuvarande lösenord stämmer inte")
        new, new2 = form.get("new", ""), form.get("new2", "")
        if len(new) < MIN_PASSWORD:
            return self.page_settings(error=f"Nytt lösenord måste vara minst {MIN_PASSWORD} tecken")
        if new != new2:
            return self.page_settings(error="De nya lösenorden är inte lika")
        self.app.store.set_password(new)
        return self.page_settings(notice="Lösenordet är bytt")

    # ------------------------------------------------------------ översikt
    def page_overview(self) -> None:
        app = self.app
        st = app.worker.status
        now = utcnow()
        alerts = []
        overview, counts = {}, {}
        db = app._db()
        if db is not None:
            try:
                overview = db.job_overview()
                counts = db.table_counts()
            except Exception as err:  # noqa: BLE001
                alerts.append(f'<div class="alert a-err">Kunde inte läsa från databasen: {esc(err)}</div>')
            finally:
                db.close()

        missing = app.store.missing()
        if missing:
            alerts.append('<div class="alert a-warn">Synken är inte igång ännu. Fyll i '
                          '<a href="/installningar">inställningarna</a> för Sitoo och SQL Server.</div>')
        elif st.error:
            alerts.append(f'<div class="alert a-err">{esc(st.error)}</div>')

        paused = app.store.paused
        if st.current_job:
            badge = (f'<span class="badge b-run">Kör: {esc(JOB_LABELS.get(st.current_job, st.current_job))}</span>'
                     f' <span class="muted small">sedan {esc(fmt_time(st.current_started))}</span>')
        elif missing:
            badge = '<span class="badge b-warn">Ej konfigurerad</span>'
        elif paused:
            badge = '<span class="badge b-warn">Pausad</span>'
        elif st.state == "fel":
            badge = '<span class="badge b-err">Fel</span>'
        elif st.state == "uppdaterar databasen":
            badge = '<span class="badge b-run">Uppdaterar databasen…</span>'
        elif st.state == "startar":
            badge = '<span class="badge b-idle">Startar…</span>'
        else:
            badge = '<span class="badge b-ok">Igång</span>'

        if st.note:
            badge += f' <span class="badge b-warn">{esc(st.note)}</span>'
        csrf = self._csrf_field()
        if paused:
            toggle = f'<form class="inline" method="post" action="/starta">{csrf}<button class="primary">Starta synken</button></form>'
        else:
            toggle = f'<form class="inline" method="post" action="/paus">{csrf}<button>Pausa synken</button></form>'

        retry = 10
        requested = app.worker.requested()
        rows_html = []
        for job in st.jobs:
            info = overview.get(job.name, {})
            attempt, success, error = info.get("attempt"), info.get("success"), info.get("error")
            failed = attempt and (success is None or attempt > success) and error
            if st.current_job == job.name:
                state = '<span class="badge b-run">Kör</span>'
            elif job.name in requested:
                state = '<span class="badge b-run">I kö</span>'
            elif failed:
                state = '<span class="badge b-err">Fel</span>'
            elif success:
                state = '<span class="badge b-ok">OK</span>'
            else:
                state = '<span class="badge b-idle">Ej körd</span>'
            nxt = "pausad" if paused else fmt_relative(next_run(job, attempt, success, retry), now)
            err_html = f'<div class="errtext">{esc(error)}</div>' if failed else ""
            rows = info.get("rows")
            busy = "disabled" if st.current_job == job.name or job.name in requested else ""
            rows_html.append(
                f'<tr><td><b>{esc(JOB_LABELS.get(job.name, job.name))}</b>{err_html}</td>'
                f'<td>{state}</td>'
                f'<td class="hide-sm">{esc(fmt_interval(job.interval_sec))}</td>'
                f'<td>{esc(fmt_time(success))}</td>'
                f'<td>{esc(nxt)}</td>'
                f'<td class="num hide-sm">{"" if rows is None else f"{rows:,}".replace(",", " ")}</td>'
                f'<td class="num"><form class="inline" method="post" action="/kor/{esc(job.name)}">{csrf}'
                f'<button class="sm" {busy}>Kör nu</button></form></td></tr>')
        jobs_table = ("<p class='muted'>Jobben visas när inställningarna är ifyllda.</p>" if not rows_html else
                      '<div class="scroll"><table><thead><tr><th>Jobb</th><th>Status</th><th class="hide-sm">Intervall</th>'
                      '<th>Senast klar</th><th>Nästa</th><th class="num hide-sm">Rader</th><th></th></tr></thead>'
                      f'<tbody>{"".join(rows_html)}</tbody></table></div>')

        stats = "".join(
            f'<div class="stat"><b>{counts.get(t, 0):,}</b><span>{label}</span></div>'.replace(",", " ")
            for t, label in TABLE_LABELS) if counts else '<p class="muted">Ingen data ännu.</p>'

        resets = "".join(
            f'<form class="inline" method="post" action="/hamta-om/{key}" '
            f'onsubmit="return confirm(\'Hämta om alla {esc(label.lower())} från Sitoo? Det kan ta en stund.\')">'
            f'{csrf}<button class="sm">{esc(label)}</button></form> '
            for key, (label, _) in RESET_PATTERNS.items())

        body = f"""
<div class="row" style="margin-bottom:18px"><h1 style="margin:0">Översikt</h1>{badge}<div class="spacer"></div>{toggle}</div>
{''.join(alerts)}
<div class="card"><h2>Jobb</h2><p class="lead small">Sidan uppdateras automatiskt var 20:e sekund.</p>{jobs_table}</div>
<div class="card"><h2>Data i databasen</h2><p class="lead small">Antal rader per tabell i schemat sitoo.</p><div class="stats">{stats}</div></div>
<div class="card"><h2>Hämta om allt</h2>
<p class="lead small">Glömmer var synken senast slutade, så att nästa körning hämtar allt för den typen igen.
Befintliga rader skrivs över, inget tas bort. Ordrar hämtas från datumet i inställningarna, eller all historik om det är tomt.</p>
<div class="row">{resets}</div></div>"""
        self._page("Översikt", body, active="home", refresh=20)

    def post_reset(self, key: str) -> None:
        if key not in RESET_PATTERNS:
            return self._redirect("/")
        label, pattern = RESET_PATTERNS[key]
        db = self.app._db()
        if db is None:
            return self._redirect("/")
        try:
            db.reset_entity(pattern)
        finally:
            db.close()
        job = {"products": "products", "stock": "stock", "orders": "orders", "zreports": "zreports"}[key]
        self.app.worker.request_run(job)
        log.info("Hämta om allt: %s", label)
        return self._redirect("/")

    # ------------------------------------------------------------ inställningar
    def page_settings(self, error: str = "", notice: str = "", values: dict | None = None,
                      errors: list[str] | None = None) -> None:
        v = values or self.app.store.values()
        stored = self.app.store.values()
        parts = []
        if error:
            parts.append(f'<div class="alert a-err">{esc(error)}</div>')
        if errors:
            parts.append('<div class="alert a-err">' + "<br>".join(esc(e) for e in errors) + "</div>")
        if notice:
            parts.append(f'<div class="alert a-ok">{esc(notice)}</div>')

        csrf = self._csrf_field()
        sections = []
        for key, title, lead in SECTIONS:
            fields_html = []
            for f in [f for f in FIELDS if f.section == key]:
                fid = f"f_{f.key}"
                help_html = f'<div class="help">{esc(f.help)}</div>' if f.help else ""
                if f.kind == "bool":
                    checked = "checked" if v.get(f.key) else ""
                    fields_html.append(f'<div><label class="check"><input type="checkbox" name="{f.key}" value="1" '
                                       f'{checked}> {esc(f.label)}</label>{help_html}</div>')
                    continue
                if f.kind == "secret":
                    has = bool(stored.get(f.key))
                    ph = "Sparat – lämna tomt för att behålla" if has else ""
                    inp = (f'<input id="{fid}" type="password" name="{f.key}" value="" placeholder="{esc(ph)}" '
                           f'autocomplete="new-password">')
                else:
                    typ = "number" if f.kind == "int" else "text"
                    extra = ' min="0"' if f.kind == "int" else ""
                    ph = ' placeholder="ÅÅÅÅ-MM-DD"' if f.kind == "date" else ""
                    inp = f'<input id="{fid}" type="{typ}" name="{f.key}" value="{esc(v.get(f.key))}"{extra}{ph}>'
                req = ' <span class="muted">*</span>' if f.required else ""
                fields_html.append(f'<div><label for="{fid}">{esc(f.label)}{req}</label>{inp}{help_html}</div>')
            test = ""
            if key == "sitoo":
                test = ('<div class="row" style="margin-top:16px"><button type="button" data-out="t_sitoo" '
                        'onclick="runTest(this,\'/api/test/sitoo\')">Testa anslutning till Sitoo</button></div>'
                        '<div id="t_sitoo" class="result"></div>')
            if key == "db":
                test = ('<div class="row" style="margin-top:16px"><button type="button" data-out="t_db" '
                        'onclick="runTest(this,\'/api/test/db\')">Testa anslutning till databasen</button></div>'
                        '<div id="t_db" class="result"></div>')
            lead_html = f'<p class="lead small">{esc(lead)}</p>' if lead else ""
            sections.append(f'<div class="card" id="{key}"><h2>{esc(title)}</h2>{lead_html}<div class="grid">{"".join(fields_html)}</div>{test}</div>')

        body = f"""<h1>Inställningar</h1>{''.join(parts)}
<form method="post" action="/installningar">{csrf}
{''.join(sections)}
<div class="row"><button class="primary" type="submit">Spara inställningar</button>
<span class="muted small">Synken läser in de nya inställningarna direkt.</span></div>
</form>
<div class="card" style="margin-top:28px"><h2>Byt lösenord för webbgränssnittet</h2>
<form method="post" action="/losenord">{csrf}<div class="grid" style="margin-top:12px">
<div><label for="cur">Nuvarande lösenord</label><input id="cur" type="password" name="current" autocomplete="current-password"></div>
<div><label for="n1">Nytt lösenord</label><input id="n1" type="password" name="new" autocomplete="new-password"></div>
<div><label for="n2">Upprepa nytt lösenord</label><input id="n2" type="password" name="new2" autocomplete="new-password"></div>
</div><div class="row" style="margin-top:16px"><button type="submit">Byt lösenord</button></div></form></div>
<script>{TEST_JS}</script>"""
        self._page("Inställningar", body, active="settings")

    def post_settings(self, form: dict) -> None:
        errors = self.app.store.update(form)
        if errors:
            values, _ = self.app.store.preview(form)
            # Visa det användaren skrev, även felaktiga värden, så att det går att rätta
            for f in FIELDS:
                if f.kind not in ("secret", "bool") and f.key in form:
                    values[f.key] = form[f.key]
            return self.page_settings(errors=errors, values=values)
        self.app.worker.wake()
        log.info("Inställningarna sparade från webbgränssnittet")
        missing = self.app.store.missing()
        notice = "Inställningarna är sparade."
        if missing:
            notice += " Fyll också i: " + ", ".join(missing) + "."
        else:
            notice += " Synken startar strax, följ den under Översikt."
        return self.page_settings(notice=notice)

    def api_test_sitoo(self, form: dict) -> None:
        values, errors = self.app.store.preview(form)
        if errors:
            return self._json({"ok": False, "message": "\n".join(errors)})
        need = ["sitoo_base_url", "sitoo_account_id", "sitoo_api_id", "sitoo_api_password", "site_ids"]
        if any(not values.get(k) for k in need):
            return self._json({"ok": False, "message": "Fyll i alla fält för Sitoo först"})
        client = self.app.sitoo_factory(values)
        lines = []
        try:
            for site in [int(s) for s in str(values["site_ids"]).split(",") if s.strip()]:
                data = client.get(f"/sites/{site}/stores.json", {"num": 1, "fields": "storeid"}) or {}
                lines.append(f"Site {site}: OK, {data.get('totalcount', 0)} butiker")
        except SitooError as err:
            hint = {401: "Fel API-id eller lösenord.", 404: "Fel konto-id, site-id eller bas-URL.",
                    429: "Sitoo är upptaget just nu (kanske kör synken). Försök igen om en minut."}.get(err.status, "")
            return self._json({"ok": False, "message": f"{hint}\n{err}".strip()})
        except Exception as err:  # noqa: BLE001
            return self._json({"ok": False, "message": f"Kunde inte nå Sitoo: {err}"})
        return self._json({"ok": True, "message": "\n".join(lines)})

    def api_test_db(self, form: dict) -> None:
        values, errors = self.app.store.preview(form)
        if errors:
            return self._json({"ok": False, "message": "\n".join(errors)})
        need = ["mssql_server", "mssql_port", "mssql_database", "mssql_user", "mssql_password"]
        if any(values.get(k) in (None, "") for k in need):
            return self._json({"ok": False, "message": "Fyll i alla fält för SQL Server först"})
        db = Database(values["mssql_server"], int(values["mssql_port"]), values["mssql_database"],
                      values["mssql_user"], values["mssql_password"], login_timeout=5)
        try:
            ok, message = db.test_connection()
        except Exception as err:  # noqa: BLE001
            ok, message = False, f"Kunde inte ansluta: {err}"
        return self._json({"ok": ok, "message": message})

    # ------------------------------------------------------------ logg
    def page_log(self) -> None:
        runs_html = ""
        db = self.app._db()
        if db is not None:
            try:
                runs = db.last_runs(50)
                rows = []
                for r in runs:
                    badge = '<span class="badge b-ok">OK</span>' if r["status"] == "ok" else '<span class="badge b-err">Fel</span>'
                    rows.append(f'<tr><td>{esc(fmt_time(r["started"]))}</td>'
                                f'<td>{esc(JOB_LABELS.get(r["job"], r["job"]))}</td><td>{badge}</td>'
                                f'<td class="num">{"" if r["rows"] is None else r["rows"]}</td>'
                                f'<td class="small">{esc(r["message"])}</td></tr>')
                runs_html = ('<div class="scroll"><table><thead><tr><th>Start</th><th>Jobb</th><th>Status</th>'
                             '<th class="num">Rader</th><th>Info</th></tr></thead><tbody>'
                             + ("".join(rows) or '<tr><td colspan="5" class="muted">Inga körningar ännu</td></tr>')
                             + "</tbody></table></div>")
            except Exception as err:  # noqa: BLE001
                runs_html = f'<div class="alert a-err">Kunde inte läsa från databasen: {esc(err)}</div>'
            finally:
                db.close()
        else:
            runs_html = '<p class="muted">Databasen är inte inställd ännu.</p>'
        lines = self.app.logbuf.tail(400)
        log_text = esc("\n".join(reversed(lines))) or "Inga loggrader ännu."
        body = f"""<h1>Logg</h1>
<div class="card"><h2>Senaste körningarna</h2><p class="lead small">Sparas i tabellen sitoo.SyncLog.</p>{runs_html}</div>
<div class="card"><h2>Tjänstens logg</h2><p class="lead small">Senaste raderna först. Nollställs när containern startar om.</p>
<pre class="log">{log_text}</pre></div>"""
        self._page("Logg", body, active="log")
