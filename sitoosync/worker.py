"""Synken körs i en bakgrundstråd som styrs från webbgränssnittet."""
from __future__ import annotations

import collections
import logging
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from .config import ConfigError
from .db import Database
from .jobs import Context, Job, build_jobs
from .settings import SettingsStore
from .sitoo import SitooClient

log = logging.getLogger("sitoosync")

JOB_LABELS = {
    "stores_warehouses": "Butiker och lager",
    "products": "Produkter",
    "products_deleted": "Borttagna produkter",
    "stock": "Lagersaldo",
    "orders": "Nya ordrar",
    "orders_refresh": "Uppdatera senaste ordrarna",
    "zreports": "Z-rapporter",
    "users": "Personer",
}

# Vilka synkmarkörer som tas bort när man väljer "Hämta om allt"
RESET_PATTERNS = {
    "products": ("Produkter", "products:%"),
    "stock": ("Lagersaldo", "stock:%"),
    "orders": ("Ordrar", "orders:%"),
    "zreports": ("Z-rapporter", "zreports"),
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------ schemaläggning
def next_run(job: Job, last_attempt: datetime | None, last_success: datetime | None,
             retry_minutes: int) -> datetime | None:
    """När jobbet ska köras nästa gång. None = så snart som möjligt."""
    if last_attempt and (last_success is None or last_attempt > last_success):
        return last_attempt + timedelta(minutes=min(retry_minutes, job.interval_min))
    if last_success is None:
        return None
    return last_success + timedelta(minutes=job.interval_min)


def is_due(job: Job, db, retry_minutes: int, now: datetime) -> bool:
    entity = f"job:{job.name}"
    _, last_success = db.get_state(entity)
    last_attempt = db.get_last_attempt(entity)
    when = next_run(job, last_attempt, last_success, retry_minutes)
    return when is None or now >= when


def run_job(job: Job, ctx: Context) -> bool:
    entity = f"job:{job.name}"
    started = utcnow()
    calls_before = ctx.api.request_count
    ctx.db.mark_attempt(entity)
    log.info("Startar %s", job.name)
    try:
        rows = job.run(ctx)
    except Exception as err:  # noqa: BLE001
        message = f"{type(err).__name__}: {err}"
        log.error("%s misslyckades: %s", job.name, message)
        log.debug(traceback.format_exc())
        try:
            ctx.db.mark_error(entity, message)
            ctx.db.log_run(job.name, started, "error", None, message)
        except Exception:  # noqa: BLE001
            log.exception("Kunde inte logga felet i databasen")
            ctx.db.close()
        return False
    ctx.db.mark_success(entity)
    seconds = (utcnow() - started).total_seconds()
    calls = ctx.api.request_count - calls_before
    ctx.db.log_run(job.name, started, "ok", rows, f"{calls} API-anrop, {seconds:.0f} s")
    log.info("Klar: %s (%s rader, %s API-anrop, %.0f s)", job.name, rows, calls, seconds)
    return True


# ------------------------------------------------------------ loggbuffert
class LogBuffer(logging.Handler):
    """Sparar de senaste loggraderna så att webbgränssnittet kan visa dem."""

    def __init__(self, capacity: int = 1000):
        super().__init__()
        self.lines: collections.deque[str] = collections.deque(maxlen=capacity)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:  # noqa: BLE001
            pass

    def tail(self, n: int = 300) -> list[str]:
        return list(self.lines)[-n:]


# ------------------------------------------------------------ worker
@dataclass
class WorkerStatus:
    state: str = "startar"          # startar, ej konfigurerad, pausad, väntar, kör, fel
    current_job: str | None = None
    current_started: datetime | None = None
    error: str | None = None
    jobs: list[Job] = field(default_factory=list)


class SyncWorker(threading.Thread):
    def __init__(self, store: SettingsStore,
                 db_factory: Callable = Database.from_config,
                 api_factory: Callable | None = None,
                 tick_seconds: float = 30.0):
        super().__init__(name="sync-worker", daemon=True)
        self.store = store
        self.db_factory = db_factory
        self.api_factory = api_factory or (lambda cfg: SitooClient(
            cfg.sitoo_base_url, cfg.sitoo_account_id, cfg.sitoo_api_id, cfg.sitoo_api_password,
            page_size=cfg.page_size, timeout=cfg.request_timeout))
        self.tick_seconds = tick_seconds
        self.status = WorkerStatus()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._requested: set[str] = set()
        self._lock = threading.Lock()
        self._loaded_version = -1
        self._ctx: Context | None = None

    # -------------------------------------------------- styrning från webben
    def request_run(self, job_name: str) -> None:
        with self._lock:
            self._requested.add(job_name)
        self._wake.set()

    def requested(self) -> set[str]:
        with self._lock:
            return set(self._requested)

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # -------------------------------------------------- huvudloop
    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as err:  # noqa: BLE001
                log.exception("Oväntat fel i synken")
                self.status.state = "fel"
                self.status.error = f"{type(err).__name__}: {err}"
                if self._ctx:
                    self._ctx.db.close()
                self._wait(30)
                continue
            self._wait(self.tick_seconds)
        if self._ctx:
            self._ctx.db.close()
        log.info("Synken stoppad")

    def _wait(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    def _reload_if_needed(self) -> bool:
        """Läser om inställningarna när de ändrats. Returnerar True om synken är redo."""
        if self._loaded_version == self.store.version and self._ctx is not None:
            return True
        if self._ctx:
            self._ctx.db.close()
            self._ctx = None
        try:
            cfg = self.store.config()
        except ConfigError as err:
            self.status.state = "ej konfigurerad"
            self.status.error = str(err)
            self.status.jobs = []
            self._loaded_version = self.store.version
            return False
        logging.getLogger().setLevel(getattr(logging, cfg.log_level, logging.INFO))
        db = self.db_factory(cfg)
        log.info("Ansluter till %s på %s:%s", cfg.db_name, cfg.db_server, cfg.db_port)
        db.ensure_database()
        db.apply_schema()
        self._ctx = Context(cfg=cfg, api=self.api_factory(cfg), db=db)
        self.status.jobs = build_jobs(cfg)
        self.status.error = None
        self._loaded_version = self.store.version
        log.info("Synken är redo. Siter %s. Jobb: %s", cfg.site_ids,
                 ", ".join(f"{j.name} ({j.interval_min} min)" for j in self.status.jobs))
        return True

    def _tick(self) -> None:
        if self._loaded_version != self.store.version or self._ctx is None:
            if self.store.missing():
                self.status.state = "ej konfigurerad"
                self.status.error = "Fyll i inställningarna för Sitoo och databasen"
                self.status.jobs = []
                self._loaded_version = self.store.version
                return
            if not self._reload_if_needed():
                return
        ctx = self._ctx
        self.status.error = None
        for job in self.status.jobs:
            if self._stop.is_set() or self._loaded_version != self.store.version:
                return
            with self._lock:
                forced = job.name in self._requested
            if not forced and self.store.paused:
                continue
            if forced or is_due(job, ctx.db, ctx.cfg.retry_after_error_minutes, utcnow()):
                with self._lock:
                    self._requested.discard(job.name)
                self.status.state = "kör"
                self.status.current_job = job.name
                self.status.current_started = utcnow()
                try:
                    run_job(job, ctx)
                finally:
                    self.status.current_job = None
                    self.status.current_started = None
        self.status.state = "pausad" if self.store.paused else "väntar"
