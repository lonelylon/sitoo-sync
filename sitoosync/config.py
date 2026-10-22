"""Inställningar.

Inställningarna görs i webbgränssnittet och sparas i /data/settings.json.
Miljövariabler (t.ex. från en .env-fil) används som startvärden om filen
saknar ett värde, så den gamla installationsvägen fungerar fortfarande.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Field:
    key: str
    env: str
    label: str
    kind: str            # str, int, bool, secret, date
    default: object
    section: str         # sitoo, db, schedule, other
    required: bool = False
    help: str = ""


FIELDS: list[Field] = [
    # Sitoo
    Field("sitoo_base_url", "SITOO_BASE_URL", "Bas-URL", "str", "https://api.mysitoo.com/v2", "sitoo", True,
          "Allt fram till och med /v2, t.ex. https://api.mysitoo.com/v2"),
    Field("sitoo_account_id", "SITOO_ACCOUNT_ID", "Konto-id", "str", "", "sitoo", True,
          "Siffran efter /accounts/ i bas-URL:en från Sitoo Backoffice"),
    Field("sitoo_api_id", "SITOO_API_ID", "API-id", "str", "", "sitoo", True),
    Field("sitoo_api_password", "SITOO_API_PASSWORD", "API-lösenord", "secret", "", "sitoo", True),
    Field("site_ids", "SITOO_SITE_IDS", "Site-id", "str", "1", "sitoo", True,
          "Ett eller flera, kommaseparerade (eshopid)"),
    # Databas
    Field("mssql_server", "MSSQL_SERVER", "Server", "str", "", "db", True,
          "QNAP:ens IP-adress, eller SQL Server-containerns namn om de delar nätverk"),
    Field("mssql_port", "MSSQL_PORT", "Port", "int", 1433, "db", True),
    Field("mssql_database", "MSSQL_DATABASE", "Databas", "str", "SitooSync", "db", True),
    Field("mssql_user", "MSSQL_USER", "Användare", "str", "", "db", True),
    Field("mssql_password", "MSSQL_PASSWORD", "Lösenord", "secret", "", "db", True),
    Field("mssql_create_database", "MSSQL_CREATE_DATABASE", "Skapa databasen om den saknas", "bool", True, "db",
          help="Kräver att användaren får skapa databaser"),
    # Schema
    Field("interval_stores", "INTERVAL_STORES_MIN", "Butiker och lager", "int", 60, "schedule"),
    Field("interval_products", "INTERVAL_PRODUCTS_MIN", "Produkter (ändrade)", "int", 15, "schedule"),
    Field("interval_products_deleted", "INTERVAL_PRODUCTS_DELETED_MIN", "Kontroll av borttagna produkter", "int",
          1440, "schedule"),
    Field("interval_stock", "INTERVAL_STOCK_MIN", "Lagersaldo", "int", 15, "schedule"),
    Field("interval_orders_sec", "INTERVAL_ORDERS_SEC", "Nya ordrar (sekunder)", "int", 60, "schedule",
          help="Hur ofta nya ordrar hämtas. Minst 15 sekunder"),
    Field("interval_orders_refresh", "INTERVAL_ORDERS_REFRESH_MIN", "Uppdatera senaste ordrarna", "int", 360,
          "schedule"),
    Field("interval_zreports", "INTERVAL_ZREPORTS_MIN", "Z-rapporter", "int", 60, "schedule"),
    Field("interval_users", "INTERVAL_USERS_MIN", "Personer", "int", 360, "schedule"),
    # Övrigt
    Field("orders_from_date", "ORDERS_FROM_DATE", "Hämta ordrar från datum", "date", "", "other",
          help="Gäller första hämtningen. Tomt = all historik. Format ÅÅÅÅ-MM-DD"),
    Field("orders_refresh_days", "ORDERS_REFRESH_DAYS", "Dagar bakåt som ordrar uppdateras", "int", 3, "other"),
    Field("enrich_orders", "ENRICH_ORDERS", "Komplettera vid nya ordrar", "bool", True, "other",
          help="Uppdaterar lagersaldo för sålda artiklar och hämtar produkter, personal och lager som saknas"),
    Field("page_size", "PAGE_SIZE", "Poster per API-anrop", "int", 100, "other"),
    Field("log_level", "LOG_LEVEL", "Loggnivå", "str", "INFO", "other", help="INFO eller DEBUG"),
    # Rapporter: tidrapport
    Field("report_stamp_sku", "REPORT_STAMP_SKU", "Artikelnummer för stämpling", "str", "91000", "time",
          help="Artikeln som scannas i kassan vid in- och utstämpling"),
    Field("report_payroll_start", "REPORT_PAYROLL_START", "Löneperioden börjar dag", "int", 21, "time",
          help="21 ger perioden 21:a till 20:e. 1 ger hela kalendermånader"),
    # Rapporter: bokföring
    # Konton ställs in på sidan Bokföring › Konton
    Field("acc_payments", "ACC_PAYMENTS", "Konton för betalsätt", "str",
          "Kort=1937; Swish=1936; Kontant=KONTANT", "accounting",
          help="Betalsätt=konto. KONTANT delar beloppet på bankningar och kassa"),
    Field("acc_chart", "ACC_CHART", "Kontoplan", "str",
          "1911=Kassa; 1936=Swish; 1937=Kort; 1938=Bank; "
          "2610=Utgående moms 25 %; 2621=Utgående moms 12 %; 2622=Utgående moms 6 %; "
          "3009=Försäljning 6 % moms; 3010=Försäljning 12 % moms; 3011=Försäljning 25 % moms; "
          "3015=Försäljning momsfri; 3740=Öres- och kronutjämning", "accounting",
          help="Konto=namn"),
    Field("acc_net_25", "ACC_NET_25", "Försäljning 25 %", "str", "3011", "accounting"),
    Field("acc_vat_25", "ACC_VAT_25", "Utgående moms 25 %", "str", "2610", "accounting"),
    Field("acc_net_12", "ACC_NET_12", "Försäljning 12 %", "str", "3010", "accounting"),
    Field("acc_vat_12", "ACC_VAT_12", "Utgående moms 12 %", "str", "2621", "accounting"),
    Field("acc_net_6", "ACC_NET_6", "Försäljning 6 %", "str", "3009", "accounting"),
    Field("acc_vat_6", "ACC_VAT_6", "Utgående moms 6 %", "str", "2622", "accounting"),
    Field("acc_net_0", "ACC_NET_0", "Försäljning momsfri", "str", "3015", "accounting"),
    Field("acc_rounding", "ACC_ROUNDING", "Öresavrundning", "str", "3740", "accounting"),
    Field("acc_bank", "ACC_BANK", "Bankningar (kontant till bank)", "str", "1938", "accounting"),
    Field("acc_cash", "ACC_CASH", "Kassa (kontanter kvar)", "str", "1911", "accounting"),
]
FIELD_BY_KEY = {f.key: f for f in FIELDS}


def parse_value(f: Field, raw) -> object:
    """Tolkar ett värde från formulär, fil eller miljö. Kastar ConfigError vid fel."""
    if f.kind == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "ja", "on")
    text = "" if raw is None else str(raw).strip()
    if f.kind == "int":
        if text == "":
            return f.default
        try:
            value = int(text)
        except ValueError:
            raise ConfigError(f"{f.label}: måste vara ett heltal") from None
        if value < 0:
            raise ConfigError(f"{f.label}: får inte vara negativt")
        return value
    if f.kind == "date" and text:
        try:
            datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            raise ConfigError(f"{f.label}: använd formatet ÅÅÅÅ-MM-DD") from None
    if f.key == "site_ids" and text:
        try:
            ids = [int(s) for s in text.replace(";", ",").split(",") if s.strip()]
        except ValueError:
            raise ConfigError(f"{f.label}: ange heltal separerade med komma") from None
        text = ",".join(str(i) for i in ids)
    if f.key == "log_level":
        text = (text or "INFO").upper()
    return text


def env_defaults() -> dict:
    """Värden från miljövariabler (används när inställningsfilen saknar dem)."""
    values = {}
    for f in FIELDS:
        raw = os.environ.get(f.env)
        if raw is not None and raw.strip() != "":
            try:
                values[f.key] = parse_value(f, raw)
            except ConfigError:
                pass
    return values


def missing_required(values: dict) -> list[str]:
    return [f.label for f in FIELDS if f.required and (values.get(f.key) in (None, ""))]


MIN_ORDER_SECONDS = 15


def _orders_seconds(value: int) -> int:
    """0 stänger av; annars minst MIN_ORDER_SECONDS för att inte belasta Sitoo i onödan."""
    return 0 if value == 0 else max(MIN_ORDER_SECONDS, value)


@dataclass
class Intervals:
    """Sekunder mellan körningar per jobb. 0 = jobbet är avstängt."""
    stores_warehouses: int = 3600
    products: int = 900
    products_deleted: int = 86400
    stock: int = 900
    orders: int = 60
    orders_refresh: int = 21600
    zreports: int = 3600
    users: int = 21600


@dataclass
class Config:
    sitoo_base_url: str
    sitoo_account_id: str
    sitoo_api_id: str
    sitoo_api_password: str
    site_ids: list[int]

    db_server: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    db_create_database: bool = True

    page_size: int = 100
    request_timeout: int = 300
    orders_refresh_days: int = 3
    orders_from_date: str | None = None
    overlap_seconds: int = 300
    retry_after_error_minutes: int = 10
    log_level: str = "INFO"
    enrich_orders: bool = True
    intervals: Intervals = field(default_factory=Intervals)

    @staticmethod
    def from_values(values: dict) -> "Config":
        missing = missing_required(values)
        if missing:
            raise ConfigError("Saknas: " + ", ".join(missing))
        v = {f.key: values.get(f.key, f.default) for f in FIELDS}
        site_ids = [int(s) for s in str(v["site_ids"]).split(",") if s.strip()]
        if not site_ids:
            raise ConfigError("Site-id: ange minst ett")
        return Config(
            sitoo_base_url=str(v["sitoo_base_url"]),
            sitoo_account_id=str(v["sitoo_account_id"]),
            sitoo_api_id=str(v["sitoo_api_id"]),
            sitoo_api_password=str(v["sitoo_api_password"]),
            site_ids=site_ids,
            db_server=str(v["mssql_server"]),
            db_port=int(v["mssql_port"]),
            db_name=str(v["mssql_database"]),
            db_user=str(v["mssql_user"]),
            db_password=str(v["mssql_password"]),
            db_create_database=bool(v["mssql_create_database"]),
            page_size=max(1, int(v["page_size"])),
            orders_refresh_days=int(v["orders_refresh_days"]),
            orders_from_date=str(v["orders_from_date"]) or None,
            log_level=str(v["log_level"]),
            enrich_orders=bool(v["enrich_orders"]),
            intervals=Intervals(
                stores_warehouses=int(v["interval_stores"]) * 60,
                products=int(v["interval_products"]) * 60,
                products_deleted=int(v["interval_products_deleted"]) * 60,
                stock=int(v["interval_stock"]) * 60,
                orders=_orders_seconds(int(v["interval_orders_sec"])),
                orders_refresh=int(v["interval_orders_refresh"]) * 60,
                zreports=int(v["interval_zreports"]) * 60,
                users=int(v["interval_users"]) * 60,
            ),
        )
