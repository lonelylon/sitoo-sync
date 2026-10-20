"""Synkjobb, ett per entitet."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .config import Config
from .sitoo import SitooClient

log = logging.getLogger(__name__)

# Fält som hämtas från Sitoo. Allt som hämtas sparas även i kolumnen RawJson,
# så fler fält kan läggas till här utan att ändra databasen.
PRODUCT_FIELDS = (
    "productid,sku,title,variantparentid,variant,barcode,barcodealiases,active,activepos,"
    "vatid,vatvalue,manufacturerid,defaultcategoryid,categories,moneyprice,moneypriceorg,"
    "moneypricein,moneyfinalprice,stockcountenable,stockallowbackorder,unitlabel,"
    "skumanufacturer,descriptionshort,custom1,custom2,custom3,custom4,custom5,"
    "customattributes,datecreated,datemodified"
)
STOCK_FIELDS = (
    "warehouseitemid,sku,binlocation,decimalthreshold,decimaltotal,decimalreserved,"
    "decimalavailable,moneytotal,datelastmodified,datelaststocktaking"
)
ORDER_FIELDS = (
    "orderid,eshopid,externalid,creditorderid,cartid,email,namefirst,namelast,personalid,"
    "company,phone,customerref,comment,orderdate,orderstateid,paymentstateid,ordertypeid,"
    "userid_staff,registerid,warehouseid,currencycode,moneytotal_net,moneytotal_vat,"
    "moneyfinal_net,moneyfinal_vat,moneyfinal_salestax,moneytotal_gross_roundoff,"
    "moneytotal_gross_all,additionaldata,orderitems,payments"
)
USER_FIELDS = (
    "userid,email,namefirst,namelast,personalid,externalid,customernumber,company,department,"
    "companyid,address,address2,zip,city,state,countryid,phone,mobile,notes,role,warehouses,"
    "active,datecreated,datemodified"
)

ZREPORT_MAX_PER_CALL = 500  # Sitoo returnerar högst 500 z-rapporter per anrop


def now_ts() -> int:
    return int(time.time())


def dumps(items) -> str:
    return json.dumps(items, ensure_ascii=False, separators=(",", ":"))


@dataclass
class Context:
    cfg: Config
    api: SitooClient
    db: object  # Database (eller testens FakeDb)


@dataclass
class Job:
    name: str
    interval_min: int
    run: Callable[[Context], int]


# --------------------------------------------------------------------- jobs
def sync_stores_and_warehouses(ctx: Context) -> int:
    rows = 0
    for site in ctx.cfg.site_ids:
        stores = ctx.api.all_items(f"/sites/{site}/stores.json")
        if stores:
            rows += ctx.db.exec_proc("MergeStores", SiteId=site, Json=dumps(stores), FullSync=1)
        warehouses = ctx.api.all_items(f"/sites/{site}/warehouses.json")
        if warehouses:
            rows += ctx.db.exec_proc("MergeWarehouses", SiteId=site, Json=dumps(warehouses), FullSync=1)
        log.info("Site %s: %s butiker, %s lager", site, len(stores), len(warehouses))
    return rows


def sync_products(ctx: Context) -> int:
    """Hämtar produkter som ändrats sedan förra körningen (första gången: alla)."""
    rows = 0
    for site in ctx.cfg.site_ids:
        key = f"products:{site}"
        last, _ = ctx.db.get_state(key)
        params = {"includeinactive": "true", "fields": PRODUCT_FIELDS}
        if last:
            params["datelastmodified"] = max(0, last - ctx.cfg.overlap_seconds)
        started = now_ts()
        newest = last or 0
        count = 0
        for page in ctx.api.pages(f"/sites/{site}/products.json", params):
            rows += ctx.db.exec_proc("MergeProducts", SiteId=site, Json=dumps(page))
            count += len(page)
            newest = max([newest] + [int(p.get("datemodified") or 0) for p in page])
        # Spara högsta ändringstid vi sett, men aldrig senare än när körningen startade.
        ctx.db.set_value(key, min(newest, started) if newest else started)
        log.info("Site %s: %s produkter %s", site, count, "ändrade" if last else "(full hämtning)")
    return rows


def sync_deleted_products(ctx: Context) -> int:
    """Hämtar alla produkt-id och markerar produkter som inte längre finns som borttagna."""
    rows = 0
    for site in ctx.cfg.site_ids:
        ids: list[int] = []
        expected = None
        path = f"/sites/{site}/products.json"
        params = {"includeinactive": "true", "fields": "productid"}
        for page in ctx.api.pages(path, params):
            ids.extend(int(p["productid"]) for p in page if p.get("productid") is not None)
        # Säkerhetskontroll: jämför med totalcount så att ett avbrutet svar aldrig raderar data.
        probe = ctx.api.get(path, {**params, "start": 0, "num": 1}) or {}
        expected = int(probe.get("totalcount") or 0)
        if not ids or len(set(ids)) < expected:
            log.warning("Site %s: fick %s av %s produkt-id, hoppar över borttagningskontrollen",
                        site, len(set(ids)), expected)
            continue
        marked = ctx.db.exec_proc("MarkDeletedProducts", SiteId=site, IdsJson=dumps(sorted(set(ids))))
        rows += marked
        log.info("Site %s: %s produkter finns, %s markerade som borttagna", site, len(set(ids)), marked)
    return rows


def sync_stock(ctx: Context) -> int:
    """Lagersaldo per lager. Inkrementellt på datelastmodified."""
    rows = 0
    for site in ctx.cfg.site_ids:
        warehouses = ctx.api.all_items(f"/sites/{site}/warehouses.json", {"fields": "warehouseid,name"})
        for wh in warehouses:
            wid = int(wh["warehouseid"])
            key = f"stock:{site}:{wid}"
            last, _ = ctx.db.get_state(key)
            params = {"fields": STOCK_FIELDS, "sort": "warehouseitemid"}
            if last:
                params["datelastmodifiedfrom"] = max(0, last - ctx.cfg.overlap_seconds)
            started = now_ts()
            newest = last or 0
            count = 0
            for page in ctx.api.pages(f"/sites/{site}/warehouses/{wid}/warehouseitems.json", params):
                rows += ctx.db.exec_proc("MergeStockItems", SiteId=site, WarehouseId=wid, Json=dumps(page))
                count += len(page)
                newest = max([newest] + [int(i.get("datelastmodified") or 0) for i in page])
            ctx.db.set_value(key, min(newest, started) if newest else started)
            if count:
                log.info("Site %s, lager %s (%s): %s artiklar", site, wid, wh.get("name"), count)
    return rows


def _orders_from_date_ts(cfg: Config) -> int | None:
    if not cfg.orders_from_date:
        return None
    d = datetime.strptime(cfg.orders_from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(d.timestamp())


def sync_orders(ctx: Context) -> int:
    """Nya ordrar: hämtar allt med orderid högre än det senast sparade."""
    rows = 0
    for site in ctx.cfg.site_ids:
        key = f"orders:{site}"
        last, _ = ctx.db.get_state(key)
        params = {"eshopid": site, "sort": "orderid", "fields": ORDER_FIELDS}
        if last:
            params["orderidfrom"] = last + 1
        else:
            params["orderdatefrom"] = _orders_from_date_ts(ctx.cfg)
        highest = last or 0
        count = 0
        for page in ctx.api.pages("/orders", params):
            rows += ctx.db.exec_proc("MergeOrders", Json=dumps(page))
            count += len(page)
            highest = max([highest] + [int(o.get("orderid") or 0) for o in page])
            ctx.db.set_value(key, highest)   # spara efter varje sida, så en stor första hämtning kan återupptas
        log.info("Site %s: %s nya ordrar", site, count)
    return rows


def refresh_recent_orders(ctx: Context) -> int:
    """Hämtar om senaste dagarnas ordrar så att status- och betalningsändringar kommer med."""
    rows = 0
    since = now_ts() - ctx.cfg.orders_refresh_days * 86400
    for site in ctx.cfg.site_ids:
        params = {"eshopid": site, "sort": "orderid", "orderdatefrom": since, "fields": ORDER_FIELDS}
        count = 0
        for page in ctx.api.pages("/orders", params):
            rows += ctx.db.exec_proc("MergeOrders", Json=dumps(page))
            count += len(page)
        log.info("Site %s: %s ordrar från senaste %s dagarna uppdaterade", site, count, ctx.cfg.orders_refresh_days)
    return rows


def sync_zreports(ctx: Context) -> int:
    """Z-rapporter per kassa. Bläddras med dateadded (max 500 per anrop)."""
    key = "zreports"
    last, _ = ctx.db.get_state(key)
    sites = ",".join(str(s) for s in ctx.cfg.site_ids)
    date_from = last or 0
    rows = 0
    count = 0
    while True:
        data = ctx.api.get("/pos-z-reports", {"eshopid": sites, "dateaddedfrom": date_from}) or []
        if isinstance(data, dict):  # om Sitoo någon gång lindar in svaret
            data = data.get("items") or []
        if not data:
            break
        rows += ctx.db.exec_proc("MergeZReports", Json=dumps(data))
        count += len(data)
        newest = max(int(z.get("dateadded") or 0) for z in data)
        ctx.db.set_value(key, newest)
        if len(data) < ZREPORT_MAX_PER_CALL:
            break
        # dateaddedfrom är inklusive. Om alla 500 har samma tid måste vi ändå komma framåt.
        date_from = newest if newest > date_from else date_from + 1
    log.info("%s z-rapporter hämtade", count)
    return rows


def sync_users(ctx: Context) -> int:
    """Personer (användare) delas mellan alla siter, så de hämtas via första site-id."""
    site = ctx.cfg.site_ids[0]
    rows = 0
    count = 0
    for page in ctx.api.pages(f"/sites/{site}/users.json", {"fields": USER_FIELDS}):
        rows += ctx.db.exec_proc("MergeUsers", Json=dumps(page))
        count += len(page)
    log.info("%s personer synkade", count)
    return rows


def build_jobs(cfg: Config) -> list[Job]:
    i = cfg.intervals
    jobs = [
        Job("stores_warehouses", i.stores_warehouses, sync_stores_and_warehouses),
        Job("products", i.products, sync_products),
        Job("products_deleted", i.products_deleted, sync_deleted_products),
        Job("stock", i.stock, sync_stock),
        Job("orders", i.orders, sync_orders),
        Job("orders_refresh", i.orders_refresh, refresh_recent_orders),
        Job("zreports", i.zreports, sync_zreports),
        Job("users", i.users, sync_users),
    ]
    return [j for j in jobs if j.interval_min > 0]
