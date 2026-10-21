"""Tidrapport och bokföringsunderlag (tidigare Excel-filerna Tidrapport_SQL och Sitoo_Rapport).

Hämtningen görs med SQL mot databasen. Beräkningarna görs i rena Python-funktioner
(summarize_time, build_verification) så att de går att testa utan databas.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

ZERO = Decimal("0.00")
TZ = "'W. Europe Standard Time'"


def D(value) -> Decimal:
    if value is None or value == "":
        return ZERO
    return Decimal(str(value))


def round2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# =========================================================================== perioder
def payroll_period(name: str, start_day: int = 21) -> tuple[date, date]:
    """'2026-09' -> (2026-08-21, 2026-09-20) med startdag 21."""
    year, month = (int(x) for x in name.split("-"))
    end = date(year, month, start_day) - timedelta(days=1) if start_day > 1 else _month_end(year, month)
    first_month = (year, month - 1) if month > 1 else (year - 1, 12)
    start = date(first_month[0], first_month[1], start_day) if start_day > 1 else date(year, month, 1)
    return start, end


def _month_end(year: int, month: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def payroll_period_for(day: date, start_day: int = 21) -> str:
    """Vilken löneperiod en dag hör till: dagar från startdagen räknas till nästa månad."""
    if start_day > 1 and day.day >= start_day:
        y, m = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    else:
        y, m = day.year, day.month
    return f"{y:04d}-{m:02d}"


def month_period(name: str) -> tuple[date, date]:
    year, month = (int(x) for x in name.split("-"))
    return date(year, month, 1), _month_end(year, month)


def recent_months(today: date, count: int = 24) -> list[str]:
    out = []
    y, m = today.year, today.month
    for _ in range(count):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out


# =========================================================================== tidrapport
@dataclass
class Stamp:
    source: str           # "Sitoo" eller "Manuell"
    ref: int | None       # OrderId (Sitoo) eller Id (manuell)
    name: str
    at: datetime          # svensk tid


@dataclass
class WorkDay:
    name: str
    day: date
    stamps: list[Stamp]

    @property
    def time_in(self) -> datetime:
        return min(s.at for s in self.stamps)

    @property
    def time_out(self) -> datetime:
        return max(s.at for s in self.stamps)

    @property
    def count(self) -> int:
        return len(self.stamps)

    @property
    def hours(self) -> float:
        return (self.time_out - self.time_in).total_seconds() / 3600

    @property
    def warning(self) -> str | None:
        if self.count == 1:
            return "Bara en stämpling"
        if self.count % 2 == 1:
            return f"Udda antal stämplingar ({self.count})"
        return None


@dataclass
class PersonSummary:
    name: str
    days: list[WorkDay] = field(default_factory=list)

    @property
    def hours(self) -> float:
        return sum(d.hours for d in self.days)

    @property
    def stamp_count(self) -> int:
        return sum(d.count for d in self.days)

    @property
    def warnings(self) -> int:
        return sum(1 for d in self.days if d.warning)


def summarize_time(stamps: list[Stamp]) -> list[PersonSummary]:
    """In = första och Ut = sista stämplingen per person och dag (som i Excel-rapporten)."""
    by_person: dict[str, dict[date, list[Stamp]]] = {}
    for s in stamps:
        name = (s.name or "").strip() or "(okänd)"
        by_person.setdefault(name, {}).setdefault(s.at.date(), []).append(s)
    result = []
    for name in sorted(by_person, key=str.casefold):
        days = [WorkDay(name, d, sorted(v, key=lambda x: x.at)) for d, v in sorted(by_person[name].items())]
        result.append(PersonSummary(name, days))
    return result


def stamps_sql() -> str:
    """Stämplingar i ett datumintervall (svensk tid): Sitoo-ordrar med stämplingsartikeln
    minus borttagna, plus manuella stämplingar. Parametrar: sku, från, till (exkl.), från, till."""
    return f"""
SELECT 'Sitoo' AS Kalla, o.OrderId AS Ref,
       COALESCE(NULLIF(LTRIM(RTRIM(CONCAT(u.NameFirst, ' ', u.NameLast))), ''), CONCAT('Okänd ', o.UserIdStaff)) AS Namn,
       COALESCE(o.OrderDateLocal,
                CAST(o.OrderDateUtc AT TIME ZONE 'UTC' AT TIME ZONE {TZ} AS DATETIME2(0))) AS Tid
FROM sitoo.Orders o
LEFT JOIN sitoo.Users u ON u.UserId = o.UserIdStaff
WHERE EXISTS (SELECT 1 FROM sitoo.OrderItems i WHERE i.OrderId = o.OrderId AND i.Sku = %s)
  AND NOT EXISTS (SELECT 1 FROM sitoo.TimeStampRemove r WHERE r.OrderId = o.OrderId)
  AND o.OrderDateLocal >= %s AND o.OrderDateLocal < %s
UNION ALL
SELECT 'Manuell', a.Id, a.Namn, a.Stampling
FROM sitoo.TimeStampAdd a
WHERE a.Stampling >= %s AND a.Stampling < %s
"""


# =========================================================================== bokföring
VAT_RATES = (25, 12, 6, 0)

DEFAULT_ACCOUNTS = {
    "net_25": "3011", "vat_25": "2610",
    "net_12": "3010", "vat_12": "2621",
    "net_6": "3009", "vat_6": "2622",
    "net_0": "3015",
    "rounding": "3740",
    "bank": "1938",
    "cash": "1911",
}
DEFAULT_PAYMENT_ACCOUNTS = "Kort=1937; Swish=1936; Kontant=1911"


def parse_payment_accounts(text: str) -> dict[str, str]:
    """'Kort=1937; Swish=1936' -> {'kort': '1937', 'swish': '1936'} (namn utan skiftlägeskänslighet)."""
    out = {}
    for part in (text or "").replace("\n", ";").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() and v.strip():
                out[k.strip().casefold()] = v.strip()
    return out


@dataclass
class ZRow:
    register_id: str
    zreport_id: int
    register_number: int | None
    day: date
    original_day: date
    fixed: bool
    closed: datetime | None
    sales: Decimal
    refunds: Decimal
    rounding: Decimal
    total: Decimal
    receipts: int | None
    cash_to_bank: Decimal = ZERO
    vat_net: dict[int, Decimal] = field(default_factory=dict)
    vat_amount: dict[int, Decimal] = field(default_factory=dict)
    payments: dict[str, Decimal] = field(default_factory=dict)

    @property
    def kassa(self) -> str:
        return f"Kassa {self.register_number}" if self.register_number is not None else self.register_id


@dataclass
class Line:
    account: str
    text: str
    debit: Decimal = ZERO
    credit: Decimal = ZERO


@dataclass
class Verification:
    lines: list[Line]
    zrows: list[ZRow]
    deposits: Decimal
    warnings: list[str]

    @property
    def debit(self) -> Decimal:
        return sum((l.debit for l in self.lines), ZERO)

    @property
    def credit(self) -> Decimal:
        return sum((l.credit for l in self.lines), ZERO)

    @property
    def diff(self) -> Decimal:
        return self.debit - self.credit


def _net_of_refund(sales: Decimal, refund: Decimal) -> Decimal:
    """Retur dras av oavsett om Sitoo redovisar den med plus- eller minustecken."""
    return sales - abs(refund)


def assemble_zrows(zreports: list[dict], vatgroups: list[dict], payments: list[dict]) -> list[ZRow]:
    rows: "OrderedDict[tuple, ZRow]" = OrderedDict()
    for z in zreports:
        key = (z["RegisterId"], int(z["ZReportId"]))
        rows[key] = ZRow(
            register_id=z["RegisterId"], zreport_id=int(z["ZReportId"]),
            register_number=z.get("RegisterNumber"), day=z["Dag"], original_day=z.get("OriginalDag") or z["Dag"],
            fixed=z.get("NyttDatum") is not None, closed=z.get("Stangd"),
            sales=D(z.get("MoneySalesTotal")), refunds=D(z.get("MoneyRefundTotal")),
            rounding=D(z.get("MoneyRoundOff")), total=D(z.get("MoneySummaryTotal")),
            receipts=z.get("NumReceipts"), cash_to_bank=D(z.get("CashToBank")),
        )
    sales_vat: dict[tuple, dict[int, list[Decimal]]] = {}
    for v in vatgroups:
        key = (v["RegisterId"], int(v["ZReportId"]))
        if key not in rows or v["Kind"] not in ("sales", "refund"):
            continue
        rate = int(round(float(v.get("VatValue") or 0)))
        acc = sales_vat.setdefault(key, {}).setdefault(rate, [ZERO, ZERO, ZERO, ZERO])  # net s, vat s, net r, vat r
        if v["Kind"] == "sales":
            acc[0] += D(v.get("MoneyTotalNet")); acc[1] += D(v.get("MoneyTotalVat"))
        else:
            acc[2] += D(v.get("MoneyTotalNet")); acc[3] += D(v.get("MoneyTotalVat"))
    for key, rates in sales_vat.items():
        for rate, (ns, vs, nr, vr) in rates.items():
            rows[key].vat_net[rate] = _net_of_refund(ns, nr)
            rows[key].vat_amount[rate] = _net_of_refund(vs, vr)
    pay: dict[tuple, dict[str, list[Decimal]]] = {}
    for p in payments:
        key = (p["RegisterId"], int(p["ZReportId"]))
        if key not in rows:
            continue
        name = (p.get("Name") or "Okänt").strip()
        acc = pay.setdefault(key, {}).setdefault(name, [ZERO, ZERO])
        acc[0 if p["Kind"] == "sales" else 1] += D(p.get("MoneyTotal"))
    for key, names in pay.items():
        for name, (s, r) in names.items():
            rows[key].payments[name] = _net_of_refund(s, r)
    return sorted(rows.values(), key=lambda r: (r.register_number or 0, r.day, r.zreport_id))


def build_verification(zrows: list[ZRow], deposits: Decimal, accounts: dict[str, str],
                       payment_accounts: dict[str, str]) -> Verification:
    """Konteringsförslag som i Excel-bladet 'Försäljning'.

    Kredit: netto per momssats och moms (räknad som netto x sats, som i Excel).
    Skillnaden mellan räknad och rapporterad moms läggs tillsammans med
    öresavrundningen på öresavrundningskontot.
    Debet: betalsätten. Kontanter delas på bank (bankningar) och kassa (resten).
    """
    a = {**DEFAULT_ACCOUNTS, **{k: v for k, v in accounts.items() if v}}
    warnings: list[str] = []
    lines: list[Line] = []

    # ---- debet: betalsätt
    pay_totals: "OrderedDict[str, Decimal]" = OrderedDict()
    for z in zrows:
        for name, amount in z.payments.items():
            pay_totals[name] = pay_totals.get(name, ZERO) + amount
    cash_names = {n for n in pay_totals if n.casefold() in ("kontant", "kontanter", "cash")}
    cash_total = sum((pay_totals[n] for n in cash_names), ZERO)
    for name, amount in pay_totals.items():
        if name in cash_names or amount == 0:
            continue
        account = payment_accounts.get(name.casefold())
        if not account:
            warnings.append(f"Betalsättet '{name}' saknar konto i inställningarna.")
            account = "????"
        lines.append(Line(account, f"Försäljning – {name}", debit=amount if amount > 0 else ZERO,
                          credit=-amount if amount < 0 else ZERO))
    if deposits:
        lines.append(Line(a["bank"], "Bankningar – kontant", debit=deposits))
    rest = cash_total - deposits
    if rest:
        lines.append(Line(a["cash"], "Kontant försäljning minus bankningar",
                          debit=rest if rest > 0 else ZERO, credit=-rest if rest < 0 else ZERO))

    # ---- kredit: momsgrupper
    net = {r: ZERO for r in VAT_RATES}
    vat = {r: ZERO for r in VAT_RATES}
    rounding = ZERO
    for z in zrows:
        rounding += z.rounding
        for rate, amount in z.vat_net.items():
            if rate not in net:
                warnings.append(f"Okänd momssats {rate} % i {z.kassa} z-rapport {z.zreport_id}.")
                net[rate] = ZERO
                vat[rate] = ZERO
            net[rate] += amount
            vat[rate] += z.vat_amount.get(rate, ZERO)
    vat_diff = ZERO
    for rate in sorted(net, reverse=True):
        if net[rate] == 0 and vat[rate] == 0:
            continue
        lines.append(Line(a.get(f"net_{rate}", "????"), f"Försäljning {rate} % moms", credit=net[rate]))
        if rate:
            computed = round2(net[rate] * Decimal(rate) / 100)
            lines.append(Line(a.get(f"vat_{rate}", "????"), f"Utgående moms {rate} %", credit=computed))
            vat_diff += vat[rate] - computed
    if rounding or vat_diff:
        lines.append(Line(a["rounding"], "Öresavrundning", credit=rounding + vat_diff))

    v = Verification(lines=lines, zrows=zrows, deposits=deposits, warnings=warnings)
    if v.diff != 0:
        warnings.append(f"Verifikationen balanserar inte: debet − kredit = {v.diff:.2f} kr.")
    return v


def zreports_sql() -> str:
    return """
SELECT v.RegisterId, v.ZReportId, v.RegisterNumber, v.Dag, v.OriginalDag, v.NyttDatum, v.Stangd,
       v.MoneySalesTotal, v.MoneyRefundTotal, v.MoneyRoundOff, v.MoneySummaryTotal, v.NumReceipts,
       TRY_CONVERT(DECIMAL(19,2), JSON_VALUE(sitoo.Json(z.RawJson), '$.moneycash_bank')) AS CashToBank
FROM sitoo.vZRapporter v
JOIN sitoo.ZReports z ON z.RegisterId = v.RegisterId AND z.ZReportId = v.ZReportId
WHERE v.Dag >= %s AND v.Dag <= %s
ORDER BY v.RegisterNumber, v.Dag, v.ZReportId
"""


def zchildren_sql(table: str, columns: str) -> str:
    return f"""
SELECT c.RegisterId, c.ZReportId, c.Kind, {columns}
FROM sitoo.{table} c
JOIN sitoo.vZRapporter v ON v.RegisterId = c.RegisterId AND v.ZReportId = c.ZReportId
WHERE v.Dag >= %s AND v.Dag <= %s
"""


class Reports:
    """Hämtar data till rapporterna. Tar en Database (eller en testersättning med query/execute)."""

    def __init__(self, db):
        self.db = db

    # ------------------------------------------------------------ tid
    def stamps(self, start: date, end: date, sku: str) -> list[Stamp]:
        frm = datetime.combine(start, datetime.min.time())
        to = datetime.combine(end + timedelta(days=1), datetime.min.time())
        rows = self.db.query(stamps_sql(), (sku, frm, to, frm, to))
        return [Stamp(r["Kalla"], r["Ref"], r["Namn"], r["Tid"]) for r in rows]

    def manual_stamps(self, start: date, end: date) -> list[dict]:
        frm = datetime.combine(start, datetime.min.time())
        to = datetime.combine(end + timedelta(days=1), datetime.min.time())
        return self.db.query("SELECT Id, Namn, Stampling, Kommentar FROM sitoo.TimeStampAdd "
                             "WHERE Stampling >= %s AND Stampling < %s ORDER BY Stampling", (frm, to))

    def removed_stamps(self) -> list[dict]:
        return self.db.query("SELECT r.OrderId, r.Kommentar, o.OrderDateLocal, "
                             "LTRIM(RTRIM(CONCAT(u.NameFirst, ' ', u.NameLast))) AS Namn "
                             "FROM sitoo.TimeStampRemove r LEFT JOIN sitoo.Orders o ON o.OrderId = r.OrderId "
                             "LEFT JOIN sitoo.Users u ON u.UserId = o.UserIdStaff ORDER BY o.OrderDateLocal DESC")

    def staff_names(self) -> list[str]:
        rows = self.db.query("SELECT DISTINCT LTRIM(RTRIM(CONCAT(NameFirst, ' ', NameLast))) AS Namn "
                             "FROM sitoo.Users WHERE Role IS NOT NULL OR UserId IN "
                             "(SELECT DISTINCT UserIdStaff FROM sitoo.Orders WHERE UserIdStaff IS NOT NULL) "
                             "ORDER BY 1")
        return [r["Namn"] for r in rows if r["Namn"]]

    def add_stamp(self, name: str, at: datetime, comment: str | None) -> None:
        self.db.execute("INSERT sitoo.TimeStampAdd (Namn, Stampling, Kommentar) VALUES (%s, %s, %s)",
                        (name, at, comment or None))

    def delete_manual_stamp(self, stamp_id: int) -> None:
        self.db.execute("DELETE FROM sitoo.TimeStampAdd WHERE Id = %s", (stamp_id,))

    def remove_order_stamp(self, order_id: int, comment: str | None) -> None:
        self.db.execute("IF NOT EXISTS (SELECT 1 FROM sitoo.TimeStampRemove WHERE OrderId = %s) "
                        "INSERT sitoo.TimeStampRemove (OrderId, Kommentar) VALUES (%s, %s)",
                        (order_id, order_id, comment or None))

    def restore_order_stamp(self, order_id: int) -> None:
        self.db.execute("DELETE FROM sitoo.TimeStampRemove WHERE OrderId = %s", (order_id,))

    # ------------------------------------------------------------ bokföring
    def zrows(self, start: date, end: date) -> list[ZRow]:
        zs = self.db.query(zreports_sql(), (start, end))
        vats = self.db.query(zchildren_sql("ZReportVatGroups",
                                           "c.VatValue, c.MoneyTotal, c.MoneyTotalNet, c.MoneyTotalVat"),
                             (start, end))
        pays = self.db.query(zchildren_sql("ZReportPayments", "c.Name, c.MoneyTotal"), (start, end))
        return assemble_zrows(zs, vats, pays)

    def deposits(self, start: date, end: date) -> list[dict]:
        return self.db.query("SELECT Id, Datum, Belopp, Kommentar FROM sitoo.BankDeposit "
                             "WHERE Datum >= %s AND Datum <= %s ORDER BY Datum", (start, end))

    def add_deposit(self, day: date, amount: Decimal, comment: str | None) -> None:
        self.db.execute("INSERT sitoo.BankDeposit (Datum, Belopp, Kommentar) VALUES (%s, %s, %s)",
                        (day, amount, comment or None))

    def delete_deposit(self, deposit_id: int) -> None:
        self.db.execute("DELETE FROM sitoo.BankDeposit WHERE Id = %s", (deposit_id,))

    def set_zreport_date(self, register_id: str, zreport_id: int, day: date | None, comment: str | None) -> None:
        self.db.execute("DELETE FROM sitoo.ZReportDateFix WHERE RegisterId = %s AND ZReportId = %s",
                        (register_id, zreport_id))
        if day:
            self.db.execute("INSERT sitoo.ZReportDateFix (RegisterId, ZReportId, NyttDatum, Kommentar) "
                            "VALUES (%s, %s, %s, %s)", (register_id, zreport_id, day, comment or None))
