"""Tidrapport och bokföringsunderlag (tidigare Excel-filerna Tidrapport_SQL och Sitoo_Rapport).

Hämtningen görs med SQL mot databasen. Beräkningarna görs i rena Python-funktioner
(summarize_time, build_verification) så att de går att testa utan databas.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

log = logging.getLogger("sitoosync.reports")
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
    minus borttagna, plus manuella stämplingar. Parametrar: sku, från, till (exkl.), från, till.

    Frågan utgår från orderraderna med stämplingsartikeln (index på Sku) och slår upp
    ordern via primärnyckeln, så den behöver inte läsa periodens alla ordrar."""
    return f"""
SELECT 'Sitoo' AS Kalla, o.OrderId AS Ref,
       COALESCE(NULLIF(LTRIM(RTRIM(CONCAT(u.NameFirst, ' ', u.NameLast))), ''), CONCAT('Okänd ', o.UserIdStaff)) AS Namn,
       o.OrderDateLocal AS Tid
FROM (SELECT DISTINCT i.OrderId FROM sitoo.OrderItems i WHERE i.Sku = %s) s
JOIN sitoo.Orders o ON o.OrderId = s.OrderId
LEFT JOIN sitoo.Users u ON u.UserId = o.UserIdStaff
WHERE o.OrderDateLocal >= %s AND o.OrderDateLocal < %s
  AND NOT EXISTS (SELECT 1 FROM sitoo.TimeStampRemove r WHERE r.OrderId = o.OrderId)
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
    "card": "1937",
    "card_fee": "6570",
}
DEFAULT_PAYMENT_ACCOUNTS = "Kort=1937; Swish=1936; Kontant=KONTANT"
CASH = "KONTANT"   # betalsätt som delas på bankningar och kassa
CASH_NAMES = ("kontant", "kontanter", "cash")

def _pairs(text: str) -> "OrderedDict[str, str]":
    out: "OrderedDict[str, str]" = OrderedDict()
    for part in (text or "").replace("\n", ";").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


def _clean(text: str) -> str:
    """Tar bort tecknen som används som avgränsare i inställningstexten."""
    return " ".join(str(text or "").replace(";", ",").replace("=", "-").split())


def parse_payment_map(text: str) -> "OrderedDict[str, str]":
    """'Kort=1937; Swish QR=1936' -> {'Kort': '1937', 'Swish QR': '1936'} (namnen som de står)."""
    return _pairs(text)


def format_payment_map(mapping: dict[str, str]) -> str:
    return "; ".join(f"{_clean(k)}={_clean(v)}" for k, v in mapping.items() if _clean(k) and _clean(v))


def parse_payment_accounts(text: str) -> dict[str, str]:
    """Som parse_payment_map men med namnen i gemener, för uppslag oavsett skiftläge."""
    return {k.casefold(): v for k, v in _pairs(text).items()}


def parse_chart(text: str) -> "OrderedDict[str, str]":
    """Kontoplanen: '1936=Swish; 1937=Kort' -> {'1936': 'Swish', '1937': 'Kort'}, sorterad på konto."""
    return OrderedDict(sorted(_pairs(text).items(), key=lambda kv: kv[0]))


def format_chart(chart: dict[str, str]) -> str:
    return "; ".join(f"{_clean(k)}={_clean(v)}" for k, v in sorted(chart.items()) if _clean(k) and _clean(v))


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
    note: str = ""        # vilka betalsätt raden summerar, när det inte framgår av texten


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


def is_cash_payment(name: str, payment_accounts: dict[str, str], cash_account: str) -> bool:
    """Kontanter delas på bankningar och kassa. payment_accounts har namnen i gemener."""
    mapped = payment_accounts.get(name.casefold())
    if mapped and mapped.upper() == CASH:
        return True
    # Äldre inställning, t.ex. 'Kontant=1911': kontanter delas fortfarande på bank och kassa
    return name.casefold() in CASH_NAMES and mapped in (None, "", cash_account)


def payment_columns(names: list[str], payment_accounts: dict[str, str], chart: dict[str, str],
                    cash_account: str) -> "OrderedDict[str, list[str]]":
    """Betalsätten grupperade som i verifikationen: rubrik -> betalsätt.
    Betalsätt med samma konto hamnar i samma kolumn, kontanter i en egen."""
    payment_accounts = {k.casefold(): v for k, v in payment_accounts.items()}
    groups: "OrderedDict[str, list[str]]" = OrderedDict()
    labels: dict[str, str] = {}
    for n in names:
        if is_cash_payment(n, payment_accounts, cash_account):
            key = CASH
        else:
            key = payment_accounts.get(n.casefold()) or f"?{n}"
        groups.setdefault(key, []).append(n)
    out: "OrderedDict[str, list[str]]" = OrderedDict()
    for key, members in groups.items():
        if key == CASH:
            label = "Kontanter"
        elif key.startswith("?"):
            label = members[0]
        else:
            label = chart.get(key) or ", ".join(members)
        labels[key] = label
        out[label if label not in out else f"{label} ({key})"] = members
    # Kontanter sist, som i verifikationen
    if "Kontanter" in out:
        out.move_to_end("Kontanter")
    return out


def build_verification(zrows: list[ZRow], deposits: Decimal, accounts: dict[str, str],
                       payment_accounts: dict[str, str], chart: dict[str, str] | None = None,
                       card_fees: Decimal = ZERO) -> Verification:
    """Konteringsförslag som i Excel-bladet 'Försäljning'.

    Kredit: netto per momssats och moms (räknad som netto x sats, som i Excel).
    Skillnaden mellan räknad och rapporterad moms läggs tillsammans med
    öresavrundningen på öresavrundningskontot.
    Debet: betalsätten, en rad per konto. Betalsätt med samma konto summeras.
    Kontanter delas på bank (bankningar) och kassa (resten).
    card_fees (positivt belopp) är Worldlines avgifter: de debiteras avgiftskontot och
    dras från kortkontot, så att kortkontot visar det som betalas ut.
    """
    chart = chart or {}
    payment_accounts = {k.casefold(): v for k, v in payment_accounts.items()}
    a = {**DEFAULT_ACCOUNTS, **{k: v for k, v in accounts.items() if v}}
    warnings: list[str] = []
    lines: list[Line] = []

    # ---- debet: betalsätt
    pay_totals: "OrderedDict[str, Decimal]" = OrderedDict()
    for z in zrows:
        for name, amount in z.payments.items():
            pay_totals[name] = pay_totals.get(name, ZERO) + amount
    cash_names = {n for n in pay_totals if is_cash_payment(n, payment_accounts, a["cash"])}
    cash_total = sum((pay_totals[n] for n in cash_names), ZERO)
    by_account: "OrderedDict[str, list]" = OrderedDict()   # konto -> [summa, [betalsätt]]
    for name, amount in pay_totals.items():
        if name in cash_names:
            continue
        account = payment_accounts.get(name.casefold())
        if not account:
            if amount == 0:
                continue
            warnings.append(f"Betalsättet '{name}' saknar konto. Välj konto under Bokföring › Konton.")
            account = "????"
        entry = by_account.setdefault(account, [ZERO, []])
        entry[0] += amount
        entry[1].append(name)
    for account, (amount, names) in by_account.items():
        if amount == 0:
            continue
        label = chart.get(account) or ", ".join(names)
        note = ", ".join(names) if ", ".join(names) != label else ""
        lines.append(Line(account, f"Försäljning – {label}", debit=amount if amount > 0 else ZERO,
                          credit=-amount if amount < 0 else ZERO, note=note))
    if card_fees:
        card_line = next((l for l in lines if l.account == a["card"] and l.debit >= card_fees), None)
        if card_line:
            card_line.debit -= card_fees
            card_line.note = ", ".join(x for x in (card_line.note, "efter avgift") if x)
        else:
            lines.append(Line(a["card"], "Kortavgifter Worldline – avdrag", credit=card_fees))
        lines.append(Line(a["card_fee"], "Kortavgifter Worldline", debit=card_fees))
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

    def _q(self, sql: str, params: tuple, label: str) -> list[dict]:
        """Kör en fråga och loggar den om den tar mer än en sekund (för felsökning av långsamma sidor)."""
        started = time.perf_counter()
        rows = self.db.query(sql, params)
        seconds = time.perf_counter() - started
        if seconds > 1:
            log.warning("Långsam rapportfråga '%s': %.1f s, %s rader", label, seconds, len(rows))
        return rows

    # ------------------------------------------------------------ tid
    def stamps(self, start: date, end: date, sku: str) -> list[Stamp]:
        frm = datetime.combine(start, datetime.min.time())
        to = datetime.combine(end + timedelta(days=1), datetime.min.time())
        rows = self._q(stamps_sql(), (sku, frm, to, frm, to), "stämplingar")
        return [Stamp(r["Kalla"], r["Ref"], r["Namn"], r["Tid"]) for r in rows]

    def manual_stamps(self, start: date, end: date) -> list[dict]:
        frm = datetime.combine(start, datetime.min.time())
        to = datetime.combine(end + timedelta(days=1), datetime.min.time())
        return self.db.query("SELECT Id, Namn, Stampling, Kommentar FROM sitoo.TimeStampAdd "
                             "WHERE Stampling >= %s AND Stampling < %s ORDER BY Stampling", (frm, to))

    def removed_stamps(self) -> list[dict]:
        return self._q("SELECT r.OrderId, r.Kommentar, o.OrderDateLocal, "
                             "LTRIM(RTRIM(CONCAT(u.NameFirst, ' ', u.NameLast))) AS Namn "
                             "FROM sitoo.TimeStampRemove r LEFT JOIN sitoo.Orders o ON o.OrderId = r.OrderId "
                             "LEFT JOIN sitoo.Users u ON u.UserId = o.UserIdStaff ORDER BY o.OrderDateLocal DESC",
                       (), "borttagna stämplingar")

    def staff_names(self) -> list[str]:
        """Personal (användare med roll) och namn från manuella stämplingar. Personer som
        stämplat i perioden läggs till av sidan själv, så ordertabellen behöver inte läsas här."""
        rows = self._q("SELECT LTRIM(RTRIM(CONCAT(NameFirst, ' ', NameLast))) AS Namn "
                       "FROM sitoo.Users WHERE Role IS NOT NULL "
                       "UNION SELECT Namn FROM sitoo.TimeStampAdd", (), "personal")
        return sorted({r["Namn"] for r in rows if r["Namn"]}, key=str.casefold)

    def add_stamp(self, name: str, at: datetime, comment: str | None) -> None:
        self.db.execute("INSERT sitoo.TimeStampAdd (Namn, Stampling, Kommentar) VALUES (%s, %s, %s)",
                        (name, at, comment or None))

    def all_manual_stamps(self) -> list[dict]:
        return self.db.query("SELECT Id, Namn, Stampling, Kommentar FROM sitoo.TimeStampAdd ORDER BY Stampling DESC")

    def update_manual_stamp(self, stamp_id: int, name: str, at: datetime, comment: str | None) -> None:
        self.db.execute("UPDATE sitoo.TimeStampAdd SET Namn = %s, Stampling = %s, Kommentar = %s WHERE Id = %s",
                        (name, at, comment or None, stamp_id))

    def delete_manual_stamp(self, stamp_id: int) -> None:
        self.db.execute("DELETE FROM sitoo.TimeStampAdd WHERE Id = %s", (stamp_id,))

    def remove_order_stamp(self, order_id: int, comment: str | None) -> None:
        self.db.execute("IF NOT EXISTS (SELECT 1 FROM sitoo.TimeStampRemove WHERE OrderId = %s) "
                        "INSERT sitoo.TimeStampRemove (OrderId, Kommentar) VALUES (%s, %s)",
                        (order_id, order_id, comment or None))

    def restore_order_stamp(self, order_id: int) -> None:
        """Återställer en borttagen Sitoo-stämpling. Var den flyttad till en annan person
        tas den flyttade kopian bort, så stämplingen inte räknas två gånger."""
        self.db.execute("DELETE FROM sitoo.TimeStampAdd WHERE FranOrderId = %s; "
                        "DELETE FROM sitoo.TimeStampRemove WHERE OrderId = %s", (order_id, order_id))

    def move_order_stamp(self, order_id: int, from_name: str, to_name: str) -> None:
        """Flyttar en Sitoo-stämpling: ordern tas bort ur tidrapporten och en manuell
        stämpling med samma tid skapas på den nya personen, i en och samma transaktion."""
        self.db.execute(
            "SET XACT_ABORT ON; BEGIN TRAN; "
            "IF NOT EXISTS (SELECT 1 FROM sitoo.TimeStampRemove WHERE OrderId = %s) "
            "INSERT sitoo.TimeStampRemove (OrderId, Kommentar) VALUES (%s, %s); "
            "INSERT sitoo.TimeStampAdd (Namn, Stampling, Kommentar, FranOrderId) "
            "SELECT %s, OrderDateLocal, %s, OrderId FROM sitoo.Orders WHERE OrderId = %s; "
            "COMMIT",
            (order_id, order_id, f"Flyttad till {to_name}"[:500],
             to_name, f"Flyttad från {from_name} (order {order_id})"[:500], order_id))

    def move_manual_stamp(self, stamp_id: int, to_name: str) -> None:
        self.db.execute("UPDATE sitoo.TimeStampAdd SET Namn = %s WHERE Id = %s", (to_name, stamp_id))

    # ------------------------------------------------------------ bokföring
    # ------------------------------------------------------------ Worldline
    @staticmethod
    def _merchant_filter(merchants: list[str], col: str = "Merchant") -> tuple[str, tuple]:
        if not merchants:
            return "", ()
        return f" AND {col} IN ({', '.join(['%s'] * len(merchants))})", tuple(merchants)

    def wl_file_by_hash(self, digest: str) -> dict | None:
        rows = self.db.query("SELECT TOP 1 * FROM sitoo.WorldlineFiles WHERE FileHash = %s ORDER BY Id DESC", (digest,))
        return rows[0] if rows else None

    def wl_save(self, filename: str, report, digest: str) -> tuple[int, int]:
        """Sparar rapporten. Returnerar (nya transaktioner, transaktioner i filen)."""
        import json as _json
        rows = self.db.query(
            "INSERT sitoo.WorldlineFiles (FileName, ReportType, Period, Reference, FileHash, Transactions, NewRows) "
            "OUTPUT INSERTED.Id VALUES (%s, %s, %s, %s, %s, %s, 0)",
            (filename[:260], report.kind, report.period, report.reference, digest, len(report.transactions)))
        file_id = int(rows[0]["Id"])
        try:
            res = self.db.query("EXEC sitoo.MergeWorldline %s, %s",
                                (file_id, _json.dumps(report.as_json_dict(), ensure_ascii=False)))
        except Exception:
            # Ta bort filraden så att samma fil kan laddas upp igen
            self.db.execute("DELETE FROM sitoo.WorldlineFiles WHERE Id = %s", (file_id,))
            raise
        new = int(res[0]["NewRows"]) if res else 0
        return new, len(report.transactions)

    def wl_files(self, limit: int = 100) -> list[dict]:
        return self._q(f"SELECT TOP {int(limit)} Id, FileName, ReportType, Period, Reference, Transactions, NewRows, "
                       "UploadedUtc FROM sitoo.WorldlineFiles ORDER BY Id DESC", (), "worldline-filer")

    def wl_merchants(self) -> list[str]:
        rows = self._q("SELECT DISTINCT Merchant FROM sitoo.WorldlineTransactions WHERE Merchant IS NOT NULL", (),
                       "worldline-säljställen")
        return sorted((r["Merchant"] for r in rows), key=str.casefold)

    def wl_daily(self, start: date, end: date, merchants: list[str]) -> dict[date, dict]:
        where, params = self._merchant_filter(merchants)
        rows = self._q(
            "SELECT TransactionDate AS Dag, SUM(Amount) AS Belopp, COUNT(*) AS Antal, SUM(COALESCE(Fee, 0)) AS Avgift, "
            "SUM(CASE WHEN Fee IS NULL THEN 1 ELSE 0 END) AS UtanAvgift, "
            "STRING_AGG(CAST(PayoutRef AS NVARCHAR(MAX)), ',') AS Utbetalningar "
            "FROM sitoo.WorldlineTransactions WHERE TransactionDate >= %s AND TransactionDate <= %s "
            f"AND (Status IS NULL OR Status = 'Cleared'){where} GROUP BY TransactionDate",
            (start, end) + params, "worldline per dag")
        return {r["Dag"]: r for r in rows}

    def wl_other_costs(self, start: date, end: date) -> Decimal:
        """Justeringar utöver transaktionsavgifterna (t.ex. månadsavgifter), som positiv kostnad."""
        rows = self._q("SELECT SUM(Belopp) AS Belopp FROM sitoo.WorldlineAdjustments "
                       "WHERE Datum >= %s AND Datum <= %s AND LOWER(Detaljer) <> 'transaktionsavgifter'",
                       (start, end), "worldline justeringar")
        total = Decimal(rows[0]["Belopp"] or 0) if rows else Decimal(0)
        return (-total).quantize(Decimal("0.01"))

    def wl_payouts(self, start: date, end: date, merchants: list[str]) -> list[dict]:
        where, params = self._merchant_filter(merchants)
        return self._q("SELECT * FROM sitoo.WorldlinePayouts WHERE PayoutDate >= %s AND PayoutDate <= %s"
                       f"{where} ORDER BY PayoutDate", (start, end) + params, "worldline utbetalningar")

    def wl_range(self) -> tuple[date | None, date | None]:
        rows = self._q("SELECT MIN(TransactionDate) AS Fran, MAX(TransactionDate) AS Till FROM sitoo.WorldlineTransactions",
                       (), "worldline period")
        return (rows[0]["Fran"], rows[0]["Till"]) if rows else (None, None)

    def payment_names(self) -> list[str]:
        """Alla betalsätt som förekommer i z-rapporterna."""
        rows = self._q("SELECT DISTINCT LTRIM(RTRIM(Name)) AS Name FROM sitoo.ZReportPayments "
                       "WHERE Name IS NOT NULL AND LTRIM(RTRIM(Name)) <> ''", (), "betalsätt")
        return sorted({r["Name"] for r in rows}, key=str.casefold)

    def zrows(self, start: date, end: date) -> list[ZRow]:
        zs = self._q(zreports_sql(), (start, end), "z-rapporter")
        vats = self._q(zchildren_sql("ZReportVatGroups",
                                     "c.VatValue, c.MoneyTotal, c.MoneyTotalNet, c.MoneyTotalVat"),
                       (start, end), "momsgrupper")
        pays = self._q(zchildren_sql("ZReportPayments", "c.Name, c.MoneyTotal"), (start, end), "betalningar")
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
