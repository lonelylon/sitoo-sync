"""Inläsning av Worldlines (Bamboras) rapporter från Merchant Portal.

Två sorters Excel-filer stöds:
- Månadsrapport (monthly_report_ÅÅÅÅMM.xlsx): flikarna Försäljning (alla transaktioner per
  transaktionsdatum), Utbetalningar, Utbetalningar Detaljerat, Reklamationer och Justeringar.
- Utbetalningsrapport (payout_report_ÅÅÅÅMMDD.xlsx): en utbetalning med flikarna
  Utbetalningar Detaljerat, Reklamationer och Justeringar.

Filen läses med zipfile och XML, utan externa bibliotek. Kolumnerna hittas på rubrik, så
ordningen spelar ingen roll.
"""
from __future__ import annotations

import hashlib
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
MAX_UNCOMPRESSED = 200 * 1024 * 1024
FEE_ADJUSTMENT = "transaktionsavgifter"   # justeringsraden som redan finns som avgift per transaktion


class WorldlineError(Exception):
    """Filen kunde inte läsas som en Worldline-rapport."""


# =========================================================================== xlsx
def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_xlsx(data: bytes) -> dict[str, list[list]]:
    """Alla flikar som listor av rader. Tal blir Decimal, text str, tomt None."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as err:
        raise WorldlineError("Filen är inte en Excel-fil (.xlsx).") from err
    if sum(i.file_size for i in z.infolist()) > MAX_UNCOMPRESSED:
        raise WorldlineError("Filen är för stor.")
    names = set(z.namelist())
    if "xl/workbook.xml" not in names:
        raise WorldlineError("Filen är inte en Excel-fil (.xlsx).")
    shared: list[str] = []
    if "xl/sharedStrings.xml" in names:
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.findall("m:si", NS):
            shared.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))
    rels = {}
    if "xl/_rels/workbook.xml.rels" in names:
        for rel in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")).iter(f"{REL_NS}Relationship"):
            target = rel.get("Target", "")
            rels[rel.get("Id")] = target.lstrip("/") if target.startswith("/") else "xl/" + target
    sheets: dict[str, list[list]] = {}
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    for sh in wb.iter(f"{{{NS['m']}}}sheet"):
        path = rels.get(sh.get(f"{{{NS['r']}}}id"))
        if not path or path not in names:
            continue
        rows: list[list] = []
        for row in ET.fromstring(z.read(path)).iter(f"{{{NS['m']}}}row"):
            values: list = []
            for c in row.findall("m:c", NS):
                idx = _col_index(c.get("r", "A1"))
                while len(values) < idx:
                    values.append(None)
                t = c.get("t")
                v = c.find("m:v", NS)
                if t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter(f"{{{NS['m']}}}t"))
                elif t == "s" and v is not None:
                    val = shared[int(v.text)]
                elif t in ("str", "e"):
                    val = v.text if v is not None else None
                elif t == "b":
                    val = v is not None and v.text == "1"
                elif v is not None and v.text is not None:
                    try:
                        val = Decimal(v.text)
                    except InvalidOperation:
                        val = v.text
                else:
                    val = None
                if isinstance(val, str):
                    val = val.strip() or None
                values.append(val)
            rows.append(values)
        sheets[sh.get("name", "")] = rows
    return sheets


# =========================================================================== värden
def _text(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        v = format(v.normalize(), "f") if v == v.to_integral() else str(v)
    s = str(v).strip()
    return s or None


def _money(v) -> Decimal | None:
    if v is None or v == "":
        return None
    if isinstance(v, Decimal):
        return v.quantize(Decimal("0.01"))
    s = str(v).replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return Decimal(s).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _date(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, Decimal):                   # Excel-serienummer
        return date(1899, 12, 30) + timedelta(days=int(v))
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:19] if "%H" in fmt else s[:10], fmt).date()
        except ValueError:
            continue
    return None


def _time(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, Decimal):                   # andel av dygn
        secs = int(round(float(v % 1) * 86400))
        return f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}"
    s = str(v).strip()
    return s if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", s) else None


def _table(rows: list[list]) -> list[dict]:
    """Rader med rubriker -> dicts. Rubriker som förekommer flera gånger (VALUTA) får suffix _2, _3."""
    if not rows:
        return []
    header: list[str] = []
    seen: dict[str, int] = {}
    for h in rows[0]:
        key = (_text(h) or "").upper()
        seen[key] = seen.get(key, 0) + 1
        header.append(key if seen[key] == 1 else f"{key}_{seen[key]}")
    out = []
    for r in rows[1:]:
        if not any(x is not None for x in r):
            continue
        out.append({header[i]: (r[i] if i < len(r) else None) for i in range(len(header)) if header[i]})
    return out


def _get(row: dict, *keys):
    for k in keys:
        if k in row and row[k] is not None:
            return row[k]
    return None


# =========================================================================== rapport
@dataclass
class Report:
    kind: str                     # "manad" eller "utbetalning"
    period: str | None
    reference: str | None
    transactions: list[dict] = field(default_factory=list)
    payouts: list[dict] = field(default_factory=list)
    adjustments: list[dict] = field(default_factory=list)
    chargebacks: list[dict] = field(default_factory=list)

    @property
    def label(self) -> str:
        return "Månadsrapport" if self.kind == "manad" else "Utbetalningsrapport"

    def as_json_dict(self) -> dict:
        def conv(o):
            if isinstance(o, dict):
                return {k: conv(v) for k, v in o.items()}
            if isinstance(o, list):
                return [conv(v) for v in o]
            if isinstance(o, Decimal):
                return str(o)
            if isinstance(o, date):
                return o.isoformat()
            return o
        return conv({"transactions": self.transactions, "payouts": self.payouts,
                     "adjustments": self.adjustments, "chargebacks": self.chargebacks})

    @property
    def dates(self) -> tuple[date | None, date | None]:
        ds = [t["date"] for t in self.transactions if t.get("date")]
        return (min(ds), max(ds)) if ds else (None, None)


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sheet(sheets: dict, name: str) -> list[list]:
    for k, v in sheets.items():
        if k.strip().casefold() == name.casefold():
            return v
    return []


def _overview(rows: list[list]) -> dict[str, str]:
    """Överblick: rubriken (Månadsrapport/Utbetalningsrapport) och fälten Rapport och Period."""
    info: dict[str, str] = {}
    for r in rows:
        cells = [_text(x) for x in r if _text(x)]
        if not cells:
            continue
        if cells[0] in ("Månadsrapport", "Utbetalningsrapport") and "title" not in info:
            info["title"] = cells[0]
        elif cells[0] in ("Rapport", "Period") and len(cells) > 1:
            info.setdefault(cells[0].lower(), cells[1])
    return info


def parse_report(data: bytes) -> Report:
    sheets = read_xlsx(data)
    info = _overview(_sheet(sheets, "Överblick"))
    details = _table(_sheet(sheets, "Utbetalningar Detaljerat"))
    sales = _table(_sheet(sheets, "Försäljning"))
    if info.get("title") == "Månadsrapport" or sales:
        kind = "manad"
    elif info.get("title") == "Utbetalningsrapport" or details:
        kind = "utbetalning"
    else:
        raise WorldlineError("Hittar varken en månadsrapport eller en utbetalningsrapport från Worldline i filen.")
    rep = Report(kind=kind, period=info.get("period"), reference=info.get("rapport"))

    txs: dict[tuple, dict] = {}

    def put(t: dict) -> None:
        if not t.get("ref") or not t.get("typ") or not t.get("date") or t.get("amount") is None:
            return
        key = (t["ref"], t["typ"])
        if key in txs:            # samma transaktion i båda flikarna: slå ihop
            txs[key].update({k: v for k, v in t.items() if v is not None})
        else:
            txs[key] = t

    # Försäljning (månadsrapporten): alla transaktioner i månaden, även de som inte betalats ut än
    for r in sales:
        put({
            "ref": _text(_get(r, "TRANSAKTIONSREF", "TRANSAKTIONSREFERENS")),
            "typ": _text(_get(r, "TYP")),
            "date": _date(_get(r, "TRANSAKTIONSDATUM")),
            "time": _time(_get(r, "TID")),
            "customer": _text(_get(r, "KUNDNUMMER")),
            "merchant": _text(_get(r, "SÄLJSTÄLLE")),
            "method": _text(_get(r, "BETALSÄTT")),
            "cardtype": _text(_get(r, "KORTTYP")),
            "terminal": _text(_get(r, "TERMINAL-ID")),
            "status": _text(_get(r, "STATUS")),
            "currency": _text(_get(r, "VALUTA")),
            "amount": _money(_get(r, "FÖRSÄLJNINGSBELOPP", "BELOPP")),
            "tip": _money(_get(r, "DRICKS")),
        })
    # Utbetalningar Detaljerat: transaktioner med avgift och utbetalning
    for r in details:
        put({
            "ref": _text(_get(r, "TRANSAKTIONSREFERENS", "TRANSAKTIONSREF")),
            "typ": _text(_get(r, "TYP")),
            "date": _date(_get(r, "TRANSAKTIONSDATUM")),
            "time": _time(_get(r, "TID")),
            "merchant": _text(_get(r, "SÄLJSTÄLLE")),
            "method": _text(_get(r, "BETALSÄTT")),
            "cardtype": _text(_get(r, "KORTTYP")),
            "country": _text(_get(r, "UTFÄRDANDE LAND")),
            "domain": _text(_get(r, "TRANSAKTIONENS DOMÄN")),
            "terminal": _text(_get(r, "TERMINAL-ID")),
            "currency": _text(_get(r, "VALUTA_2", "VALUTA")),
            "amount": _money(_get(r, "BELOPP", "TRANSAKTIONSBELOPP")),
            "fee": _money(_get(r, "TRANSAKTIONSAVGIFT")) or Decimal("0.00"),
            "net": _money(_get(r, "SLUTBELOPP")),
            "payoutref": _text(_get(r, "UTBETALNINGSREFERENS")),
            "payoutdate": _date(_get(r, "UTBETALNINGSDATUM")),
        })
    rep.transactions = list(txs.values())

    for r in _table(_sheet(sheets, "Justeringar")):
        a = {"payoutref": _text(_get(r, "UTBETALNINGSREFERENS")), "details": _text(_get(r, "DETALJER")) or "Justering",
             "amount": _money(_get(r, "BELOPP")), "date": _date(_get(r, "DATUM")), "currency": _text(_get(r, "VALUTA"))}
        if a["payoutref"] and a["amount"] is not None and a["date"]:
            rep.adjustments.append(a)
    for r in _table(_sheet(sheets, "Reklamationer")):
        c = {"caseid": _text(_get(r, "CASE ID")), "payoutref": _text(_get(r, "UTBETALNINGSREFERENS")),
             "date": _date(_get(r, "DATUM")), "ref": _text(_get(r, "TRANSAKTIONSREFERENS")),
             "reason": _text(_get(r, "REASON")), "amount": _money(_get(r, "BELOPP")),
             "currency": _text(_get(r, "VALUTA"))}
        if c["caseid"] and c["amount"] is not None and all(x["caseid"] != c["caseid"] for x in rep.chargebacks):
            rep.chargebacks.append(c)

    # Utbetalningar: från månadsrapportens flik, annars räknade från transaktionerna
    payouts: dict[str, dict] = {}
    for r in _table(_sheet(sheets, "Utbetalningar")):
        ref = _text(_get(r, "REFERENS", "UTBETALNINGSREFERENS"))
        d = _date(_get(r, "DATUM"))
        if not ref or not d:
            continue
        payouts[ref] = {
            "ref": ref, "date": d, "customer": _text(_get(r, "KUNDNUMMER")), "merchant": _text(_get(r, "SÄLJSTÄLLE")),
            "account": _text(_get(r, "MOTTAGARENS KONTO")), "transactions": int(_get(r, "TRANSAKTIONER") or 0),
            "currency": _text(_get(r, "VALUTA")), "turnover": _money(_get(r, "OMSÄTTNING")) or Decimal("0.00"),
            "fees": _money(_get(r, "AVGIFTER")) or Decimal("0.00"),
            "adjustments": _money(_get(r, "JUSTERINGAR")) or Decimal("0.00"),
            "chargebacks": _money(_get(r, "REKLAMATIONER")) or Decimal("0.00"),
            "amount": _money(_get(r, "BELOPP")) or Decimal("0.00"),
        }
    zero = Decimal("0.00")
    for t in rep.transactions:
        ref = t.get("payoutref")
        if not ref or ref in payouts and "_calc" not in payouts[ref]:
            continue
        p = payouts.setdefault(ref, {"ref": ref, "date": t.get("payoutdate"), "customer": None,
                                     "merchant": t.get("merchant"), "account": None, "transactions": 0,
                                     "currency": t.get("currency"), "turnover": zero, "fees": zero,
                                     "adjustments": zero, "chargebacks": zero, "amount": zero, "_calc": True})
        p["transactions"] += 1
        p["turnover"] += t.get("amount") or zero
        p["fees"] += t.get("fee") or zero
    for p in payouts.values():
        if p.pop("_calc", False):
            p["adjustments"] = sum((a["amount"] for a in rep.adjustments if a["payoutref"] == p["ref"]
                                    and a["details"].casefold() != FEE_ADJUSTMENT), zero)
            p["chargebacks"] = sum((c["amount"] for c in rep.chargebacks if c["payoutref"] == p["ref"]), zero)
            p["amount"] = p["turnover"] + p["fees"] + p["adjustments"] + p["chargebacks"]
    rep.payouts = [p for p in payouts.values() if p.get("date")]
    if not rep.transactions and not rep.payouts:
        raise WorldlineError("Rapporten innehåller inga transaktioner.")
    return rep


# =========================================================================== korsreferens mot Sitoo
ZERO = Decimal("0.00")


@dataclass
class DayCheck:
    day: date
    sitoo: Decimal = ZERO            # kortbetalningar enligt z-rapporterna
    worldline: Decimal | None = None  # transaktioner hos Worldline (None = inga uppladdade)
    count: int = 0
    fee: Decimal = ZERO              # avgifter (negativa)
    unpaid: int = 0                  # transaktioner utan avgift, dvs. utbetalningen är inte uppladdad
    payouts: list[str] = field(default_factory=list)

    @property
    def diff(self) -> Decimal:
        return (self.worldline or ZERO) - self.sitoo

    @property
    def status(self) -> str:
        """ok, saknas (Sitoo har kortförsäljning men Worldline-data saknas), avvikelse eller ingen."""
        if self.worldline is None or (self.worldline == 0 and self.count == 0):
            return "saknas" if self.sitoo != 0 else "ingen"
        return "ok" if abs(self.diff) < Decimal("0.01") else "avvikelse"


def compare_days(start: date, end: date, sitoo: dict[date, Decimal], wl: dict[date, dict]) -> list[DayCheck]:
    out = []
    d = start
    while d <= end:
        w = wl.get(d)
        dc = DayCheck(day=d, sitoo=sitoo.get(d, ZERO))
        if w:
            dc.worldline = Decimal(w["Belopp"]).quantize(ZERO)
            dc.count = int(w.get("Antal") or 0)
            dc.fee = Decimal(w.get("Avgift") or 0).quantize(ZERO)
            dc.unpaid = int(w.get("UtanAvgift") or 0)
            dc.payouts = sorted({p for p in (w.get("Utbetalningar") or "").split(",") if p})
        out.append(dc)
        d += timedelta(days=1)
    return out


@dataclass
class MonthCheck:
    month: str
    days: list[DayCheck]

    @property
    def sitoo(self) -> Decimal:
        return sum((d.sitoo for d in self.days), ZERO)

    @property
    def worldline(self) -> Decimal:
        return sum((d.worldline or ZERO for d in self.days), ZERO)

    @property
    def diff(self) -> Decimal:
        return self.worldline - self.sitoo

    @property
    def fee(self) -> Decimal:
        return sum((d.fee for d in self.days), ZERO)

    @property
    def missing(self) -> list[DayCheck]:
        return [d for d in self.days if d.status == "saknas"]

    @property
    def deviations(self) -> list[DayCheck]:
        return [d for d in self.days if d.status == "avvikelse"]

    @property
    def unpaid(self) -> int:
        return sum(d.unpaid for d in self.days)

    @property
    def status(self) -> str:
        if not any(d.sitoo or d.worldline for d in self.days):
            return "ingen"
        if all(d.worldline is None for d in self.days):
            return "saknas"
        if self.missing:
            return "ofullstandig"
        if self.deviations:
            return "avvikelse"
        if self.unpaid:
            return "utan_avgift"
        return "ok"


def group_months(days: list[DayCheck]) -> list[MonthCheck]:
    months: dict[str, list[DayCheck]] = {}
    for d in days:
        months.setdefault(f"{d.day:%Y-%m}", []).append(d)
    return [MonthCheck(m, ds) for m, ds in sorted(months.items(), reverse=True)]


def sitoo_card_by_day(zrows, payment_accounts: dict[str, str], card_account: str, cash_account: str) -> dict[date, Decimal]:
    """Kortbetalningar per dag enligt z-rapporterna: betalsätt kopplade till kortkontot."""
    from .reports import is_cash_payment
    pa = {k.casefold(): v for k, v in payment_accounts.items()}
    out: dict[date, Decimal] = {}
    for z in zrows:
        for name, amount in z.payments.items():
            if is_cash_payment(name, pa, cash_account) or pa.get(name.casefold()) != card_account:
                continue
            out[z.day] = out.get(z.day, ZERO) + amount
    return out
