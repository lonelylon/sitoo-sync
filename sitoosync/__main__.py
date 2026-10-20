"""Startpunkt: python -m sitoosync

Utan argument startar webbgränssnittet (port 8080) och synken i bakgrunden.
  python -m sitoosync --once            kör alla jobb en gång och avslutar
  python -m sitoosync --once orders     kör bara angivna jobb en gång
  python -m sitoosync --check           testar anslutning till Sitoo och databasen
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading

from .config import ConfigError
from .db import Database
from .jobs import Context, build_jobs
from .settings import SettingsStore
from .sitoo import SitooClient
from .web import WebApp
from .worker import LogBuffer, SyncWorker, is_due, run_job  # noqa: F401  (is_due används av tester)

log = logging.getLogger("sitoosync")


def setup_logging(logbuf: LogBuffer | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.handlers = [console] + ([logbuf] if logbuf else [])


def run_cli(store: SettingsStore, once: list[str] | None, check: bool) -> int:
    try:
        cfg = store.config()
    except ConfigError as err:
        log.error("Inställningarna är inte kompletta: %s", err)
        return 2
    api = SitooClient(cfg.sitoo_base_url, cfg.sitoo_account_id, cfg.sitoo_api_id, cfg.sitoo_api_password,
                      page_size=cfg.page_size, timeout=cfg.request_timeout)
    db = Database.from_config(cfg)
    db.wait_until_available()
    db.ensure_database()
    db.apply_schema()
    if check:
        site = cfg.site_ids[0]
        stores = api.get(f"/sites/{site}/stores.json", {"num": 1, "fields": "storeid"}) or {}
        log.info("Sitoo OK: site %s har %s butiker. Databasen OK.", site, stores.get("totalcount"))
        return 0
    ctx = Context(cfg=cfg, api=api, db=db)
    jobs = build_jobs(cfg)
    wanted = set(once or [])
    unknown = wanted - {j.name for j in jobs}
    if unknown:
        log.error("Okända jobb: %s. Tillgängliga: %s", ", ".join(sorted(unknown)), ", ".join(j.name for j in jobs))
        return 2
    ok = all([run_job(j, ctx) for j in jobs if not wanted or j.name in wanted])
    db.close()
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sitoosync")
    parser.add_argument("--once", nargs="*", metavar="JOBB", help="kör jobben en gång och avsluta")
    parser.add_argument("--check", action="store_true", help="testa anslutningarna och avsluta")
    args = parser.parse_args(argv)

    data_dir = os.environ.get("DATA_DIR", "/data" if os.path.isdir("/data") else "./data")
    store = SettingsStore(data_dir)

    if args.once is not None or args.check:
        setup_logging()
        return run_cli(store, args.once, args.check)

    logbuf = LogBuffer()
    setup_logging(logbuf)
    port = int(os.environ.get("WEB_PORT", "8080"))

    worker = SyncWorker(store)
    app = WebApp(store, worker, logbuf)
    server = app.make_server("0.0.0.0", port)

    stopping = threading.Event()

    def stop(signum, _frame):
        if stopping.is_set():
            return
        stopping.set()
        log.info("Signal %s mottagen, avslutar", signum)
        worker.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    log.info("Sitoo Sync startar. Webbgränssnitt på port %s, inställningar i %s", port, data_dir)
    worker.start()
    server.serve_forever()
    worker.join(timeout=30)
    log.info("Avslutad")
    return 0


if __name__ == "__main__":
    sys.exit(main())
