"""Databaslager mot SQL Server (pymssql)."""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_FILE = Path(__file__).with_name("sql") / "schema.sql"
_GO = re.compile(r"^\s*GO\s*$", re.IGNORECASE | re.MULTILINE)


def split_batches(sql: str) -> list[str]:
    return [b.strip() for b in _GO.split(sql) if b.strip()]


class Database:
    def __init__(self, server: str, port: int, database: str, user: str, password: str,
                 create_database: bool = True, login_timeout: int = 30):
        self.server = server
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        self.create_database = create_database
        self.login_timeout = login_timeout
        self._conn = None

    @classmethod
    def from_config(cls, cfg, login_timeout: int = 30) -> "Database":
        return cls(cfg.db_server, cfg.db_port, cfg.db_name, cfg.db_user, cfg.db_password,
                   create_database=cfg.db_create_database, login_timeout=login_timeout)

    # ------------------------------------------------------------ connection
    def _connect(self, database: str, timeout: int = 900):
        import pymssql  # importeras här så att testerna kan köras utan drivrutinen

        return pymssql.connect(
            server=self.server,
            port=str(self.port),
            user=self.user,
            password=self.password,
            database=database,
            autocommit=True,
            login_timeout=self.login_timeout,
            timeout=timeout,
            charset="UTF-8",
            tds_version="7.4",
            appname="sitoo-sync",
        )

    @property
    def conn(self):
        if self._conn is None:
            self._conn = self._connect(self.database)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def wait_until_available(self, attempts: int = 30, delay: int = 10) -> None:
        """SQL Server-containern kan starta långsammare än sync-tjänsten."""
        last_error = None
        for i in range(1, attempts + 1):
            try:
                conn = self._connect("master")
                conn.close()
                return
            except Exception as err:  # noqa: BLE001
                last_error = err
                log.info("Väntar på SQL Server (%s/%s): %s", i, attempts, err)
                time.sleep(delay)
        raise RuntimeError(f"Kunde inte ansluta till SQL Server: {last_error}")

    def ensure_database(self) -> None:
        if not self.create_database:
            return
        conn = self._connect("master")
        try:
            cur = conn.cursor()
            cur.execute("SELECT DB_ID(%s)", (self.database,))
            if cur.fetchone()[0] is None:
                log.info("Skapar databasen %s", self.database)
                cur.execute("EXEC(N'CREATE DATABASE ' + QUOTENAME(%s))", (self.database,))
        finally:
            conn.close()

    def apply_schema(self) -> None:
        """Skapar/uppdaterar tabeller och procedurer. Egen anslutning utan tidsgräns,
        eftersom en migrering av stora tabeller kan ta flera minuter."""
        sql = SCHEMA_FILE.read_text(encoding="utf-8")
        conn = self._connect(self.database, timeout=0)
        try:
            cur = conn.cursor()
            for batch in split_batches(sql):
                if "sitoo.MigrateRawJson N'" in batch or "sitoo.DropRawJson N'" in batch:
                    log.debug("Kör %s", batch.strip())
                cur.execute(batch)
        finally:
            conn.close()
        log.info("Databasschemat är uppdaterat (%s)", self.database)

    # ------------------------------------------------------------ operations
    def exec_proc(self, proc: str, **params) -> int:
        """Kör en procedur i schemat sitoo och returnerar antal påverkade rader."""
        names = list(params)
        args = ", ".join(f"@{n}=%s" for n in names)
        sql = f"EXEC sitoo.{proc} {args}" if args else f"EXEC sitoo.{proc}"
        cur = self.conn.cursor()
        cur.execute(sql, tuple(params[n] for n in names))
        rows = 0
        try:
            if cur.description:
                row = cur.fetchone()
                if row and row[0] is not None:
                    rows = int(row[0])
        except Exception:  # noqa: BLE001
            pass
        return rows

    def get_state(self, entity: str) -> tuple[int | None, datetime | None]:
        cur = self.conn.cursor()
        cur.execute("SELECT LastValue, LastSuccessUtc FROM sitoo.SyncState WHERE Entity = %s", (entity,))
        row = cur.fetchone()
        if not row:
            return None, None
        last_success = row[1].replace(tzinfo=timezone.utc) if row[1] else None
        return row[0], last_success

    def get_last_attempt(self, entity: str) -> datetime | None:
        cur = self.conn.cursor()
        cur.execute("SELECT LastAttemptUtc FROM sitoo.SyncState WHERE Entity = %s", (entity,))
        row = cur.fetchone()
        return row[0].replace(tzinfo=timezone.utc) if row and row[0] else None

    def set_value(self, entity: str, value: int | None) -> None:
        self._upsert_state(entity, "LastValue = %s", (value,))

    def mark_attempt(self, entity: str) -> None:
        self._upsert_state(entity, "LastAttemptUtc = SYSUTCDATETIME()", ())

    def mark_success(self, entity: str) -> None:
        self._upsert_state(entity, "LastSuccessUtc = SYSUTCDATETIME(), LastError = NULL", ())

    def mark_error(self, entity: str, message: str) -> None:
        self._upsert_state(entity, "LastError = %s", (message[:4000],))

    def _upsert_state(self, entity: str, set_clause: str, values: tuple) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "IF NOT EXISTS (SELECT 1 FROM sitoo.SyncState WHERE Entity = %s) "
            "INSERT sitoo.SyncState (Entity) VALUES (%s); "
            f"UPDATE sitoo.SyncState SET {set_clause} WHERE Entity = %s;",
            (entity, entity, *values, entity),
        )

    def log_run(self, job: str, started: datetime, status: str, rows: int | None, message: str | None) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT sitoo.SyncLog (Job, StartedUtc, FinishedUtc, Status, RowsAffected, Message) "
            "VALUES (%s, %s, SYSUTCDATETIME(), %s, %s, %s)",
            (job, started.replace(tzinfo=None), status, rows, (message or "")[:4000] or None),
        )

    def test_connection(self) -> tuple[bool, str]:
        """Används av knappen Testa anslutning. Returnerar (ok, meddelande)."""
        conn = self._connect("master")
        try:
            cur = conn.cursor()
            cur.execute("SELECT CAST(SERVERPROPERTY('ProductVersion') AS NVARCHAR(50)), "
                        "CAST(SERVERPROPERTY('Edition') AS NVARCHAR(100)), DB_ID(%s)", (self.database,))
            version, edition, db_id = cur.fetchone()
        finally:
            conn.close()
        lines = [f"Ansluten till SQL Server {version} ({edition})."]
        if db_id is None:
            if self.create_database:
                lines.append(f"Databasen {self.database} finns inte ännu och skapas när synken startar.")
            else:
                return False, "\n".join(lines + [f"Databasen {self.database} finns inte. Skapa den, "
                                                 "eller kryssa i Skapa databasen om den saknas."])
        else:
            conn = self._connect(self.database)
            try:
                cur = conn.cursor()
                cur.execute("SELECT IS_MEMBER('db_owner'), IS_MEMBER('db_ddladmin')")
                owner, ddl = cur.fetchone()
            finally:
                conn.close()
            if not (owner or ddl):
                return False, "\n".join(lines + [f"Användaren saknar rätt att skapa tabeller i {self.database}. "
                                                 "Lägg till den i rollen db_owner."])
            lines.append(f"Databasen {self.database} finns och användaren har rätt behörighet.")
        return True, "\n".join(lines)

    _EXISTING_SQL = {
        "products": ("SELECT ProductId FROM sitoo.Products WHERE SiteId = %s AND ProductId IN "
                     "(SELECT TRY_CONVERT(INT, [value]) FROM OPENJSON(%s))", int),
        "warehouses": ("SELECT WarehouseId FROM sitoo.Warehouses WHERE SiteId = %s AND WarehouseId IN "
                       "(SELECT TRY_CONVERT(INT, [value]) FROM OPENJSON(%s))", int),
        "users": ("SELECT UserId FROM sitoo.Users WHERE UserId IN "
                  "(SELECT CAST([value] AS NVARCHAR(64)) FROM OPENJSON(%s))", str),
    }

    def existing_ids(self, kind: str, ids: list, site: int | None = None) -> set:
        """Vilka av ids som redan finns i databasen (products, warehouses eller users)."""
        if not ids:
            return set()
        sql, cast = self._EXISTING_SQL[kind]
        payload = json.dumps(list(ids), ensure_ascii=False)
        args = (payload,) if kind == "users" else (site, payload)
        cur = self.conn.cursor()
        cur.execute(sql, args)
        return {cast(r[0]) for r in cur.fetchall()}

    def oldest_order_ts(self) -> int | None:
        cur = self.conn.cursor()
        cur.execute("SELECT DATEDIFF_BIG(SECOND, '19700101', MIN(OrderDateUtc)) FROM sitoo.Orders")
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else None

    # ------------------------------------------------------------ webbgränssnitt
    def job_overview(self) -> dict[str, dict]:
        cur = self.conn.cursor()
        cur.execute("SELECT Entity, LastAttemptUtc, LastSuccessUtc, LastError FROM sitoo.SyncState "
                    "WHERE Entity LIKE 'job:%'")
        result = {}
        for entity, attempt, success, error in cur.fetchall():
            result[entity[4:]] = {
                "attempt": attempt.replace(tzinfo=timezone.utc) if attempt else None,
                "success": success.replace(tzinfo=timezone.utc) if success else None,
                "error": error,
            }
        cur.execute("SELECT l.Job, l.RowsAffected, l.Message FROM sitoo.SyncLog l "
                    "JOIN (SELECT Job, MAX(Id) AS Id FROM sitoo.SyncLog WHERE Status = 'ok' GROUP BY Job) m "
                    "ON m.Id = l.Id")
        for job, rows, message in cur.fetchall():
            result.setdefault(job, {"attempt": None, "success": None, "error": None})
            result[job]["rows"] = rows
            result[job]["message"] = message
        return result

    def last_runs(self, limit: int = 100) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute(f"SELECT TOP ({int(limit)}) Job, StartedUtc, FinishedUtc, Status, RowsAffected, Message "
                    "FROM sitoo.SyncLog ORDER BY Id DESC")
        return [
            {"job": r[0], "started": r[1].replace(tzinfo=timezone.utc), "finished": r[2].replace(tzinfo=timezone.utc),
             "status": r[3], "rows": r[4], "message": r[5]}
            for r in cur.fetchall()
        ]

    def table_counts(self) -> dict[str, int]:
        cur = self.conn.cursor()
        cur.execute("SELECT t.name, SUM(p.rows) FROM sys.tables t "
                    "JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1) "
                    "WHERE t.schema_id = SCHEMA_ID('sitoo') GROUP BY t.name")
        return {name: int(rows or 0) for name, rows in cur.fetchall()}

    def reset_entity(self, pattern: str) -> int:
        """Tar bort synkmarkörer så att nästa körning hämtar allt igen. pattern är ett LIKE-mönster."""
        cur = self.conn.cursor()
        cur.execute("SET NOCOUNT ON; DELETE FROM sitoo.SyncState WHERE Entity LIKE %s; SELECT @@ROWCOUNT;", (pattern,))
        row = cur.fetchone()
        return int(row[0]) if row else 0
