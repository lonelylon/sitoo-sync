"""Sidorna Tidrapport och Bokföring (ersätter Excel-filerna)."""
from __future__ import annotations

import csv
import html
import io
import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlencode, urlparse

from .reports import (
    DEFAULT_ACCOUNTS, Reports, build_verification, month_period, parse_payment_accounts,
    payroll_period, payroll_period_for, recent_months, summarize_time,
)

log = logging.getLogger("sitoosync.web")

WEEKDAYS = ["mån", "tis", "ons", "tor", "fre", "lör", "sön"]


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def money(value, blank_zero: bool = False) -> str:
    if value is None:
        return ""
    d = Decimal(value)
    if blank_zero and d == 0:
        return ""
    sign = "−" if d < 0 else ""
    whole, frac = f"{abs(d):.2f}".split(".")
    groups = []
    while whole:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    return f"{sign}{' '.join(groups)},{frac}"


def hours_fmt(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


def day_fmt(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d:%Y-%m-%d}"


def parse_date(text: str | None) -> date | None:
    try:
        return datetime.strptime((text or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def csv_response(rows: list[list]) -> str:
    """Semikolon och decimalkomma, så att filen öppnas rätt i svenskt Excel."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    for row in rows:
        out = []
        for v in row:
            if isinstance(v, Decimal):
                out.append(f"{v:.2f}".replace(".", ","))
            elif isinstance(v, float):
                out.append(f"{v:.2f}".replace(".", ","))
            elif isinstance(v, datetime):
                out.append(v.strftime("%Y-%m-%d %H:%M"))
            elif isinstance(v, date):
                out.append(v.strftime("%Y-%m-%d"))
            else:
                out.append("" if v is None else str(v))
        w.writerow(out)
    return "﻿" + buf.getvalue()


REPORT_CSS = """
.filters{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end}
.filters>div{min-width:150px}
select,input[type=date],input[type=time]{font:inherit;padding:7px 10px;border-radius:8px;border:1px solid var(--line);
background:var(--bg);color:var(--text)}
table.compact th,table.compact td{padding:6px 8px;white-space:nowrap}
table.compact td.small{white-space:normal}
tr.total td{font-weight:650;border-top:2px solid var(--line)}
tr.warn td{background:var(--warn-bg)}
.pill{display:inline-block;font-size:12px;padding:1px 7px;border-radius:999px;background:var(--bg);color:var(--muted)}
.pill.warn{background:var(--warn-bg);color:var(--warn)}
.pill.ok{background:var(--ok-bg);color:var(--ok)}
details>summary{cursor:pointer;color:var(--accent)}
details[open]>summary{margin-bottom:8px}
.inline-form{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.person h2{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.person h2 .muted{font-weight:400;font-size:14px;margin-left:10px}
@media print{header,.no-print,.filters,form,details>summary{display:none!important}
.card{border:0;padding:0;margin-bottom:14px;break-inside:avoid}body{background:#fff;color:#000}
details{display:block}details>*{display:block}}
"""


class ReportPagesMixin:
    """Blandas in i RequestHandler i web.py."""

    # ------------------------------------------------------------ hjälp
    def _q(self) -> dict:
        return {k: v[-1] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _reports(self) -> Reports | None:
        db = self.app._db()
        return Reports(db) if db is not None else None

    def _back(self, form: dict, default: str) -> str:
        back = form.get("back") or default
        return back if back.startswith("/") and not back.startswith("//") else default

    def _not_configured(self, title: str, active: str) -> None:
        self._page(title, f"<h1>{esc(title)}</h1><div class='alert a-warn'>Databasen är inte inställd ännu. "
                          "Fyll i <a href='/installningar'>inställningarna</a>.</div>", active=active)

    def _settings(self) -> dict:
        return self.app.store.values()

    def _db_error(self, title: str, active: str, err: Exception) -> None:
        log.exception("Fel i rapport %s", title)
        self._page(title, f"<h1>{esc(title)}</h1><div class='alert a-err'>Kunde inte hämta data: {esc(err)}</div>",
                   active=active)

    # ============================================================ TIDRAPPORT
    def _time_params(self) -> tuple[str, date, date, str, int]:
        settings = self._settings()
        start_day = int(settings.get("report_payroll_start") or 21)
        q = self._q()
        period = q.get("period") or payroll_period_for(date.today(), start_day)
        try:
            start, end = payroll_period(period, start_day)
        except (ValueError, TypeError):
            period = payroll_period_for(date.today(), start_day)
            start, end = payroll_period(period, start_day)
        return period, start, end, q.get("person", ""), start_day

    def page_time(self) -> None:
        reports = self._reports()
        if reports is None:
            return self._not_configured("Tidrapport", "time")
        period, start, end, person, start_day = self._time_params()
        sku = self._settings().get("report_stamp_sku") or "91000"
        try:
            stamps = reports.stamps(start, end, sku)
            names = reports.staff_names()
            removed = reports.removed_stamps()
        except Exception as err:  # noqa: BLE001
            return self._db_error("Tidrapport", "time", err)
        finally:
            reports.db.close()

        people = summarize_time(stamps)
        all_names = sorted({p.name for p in people} | set(names), key=str.casefold)
        if person:
            people = [p for p in people if p.name == person]
        back = self.path
        csrf = self._csrf_field()

        periods = recent_months(date.today().replace(day=1), 36)
        nxt = payroll_period_for(date.today(), start_day)
        if nxt not in periods:
            periods.insert(0, nxt)
        if period not in periods:
            periods.insert(0, period)
        period_opts = "".join(f'<option value="{p}" {"selected" if p == period else ""}>{p}</option>'
                              for p in periods)
        person_opts = '<option value="">Alla</option>' + "".join(
            f'<option value="{esc(n)}" {"selected" if n == person else ""}>{esc(n)}</option>' for n in all_names)
        qs = urlencode({"period": period, "person": person})

        # ---- sammanställning
        def warn_pill(n: int) -> str:
            return f'<span class="pill warn">{n} att kontrollera</span>' if n else ""
        sum_rows = "".join(
            f'<tr><td><a href="#p-{i}">{esc(p.name)}</a></td><td class="num">{len(p.days)}</td>'
            f'<td class="num">{p.stamp_count}</td><td class="num">{hours_fmt(p.hours)}</td>'
            f'<td>{warn_pill(p.warnings)}</td></tr>'
            for i, p in enumerate(people))
        total_h = sum(p.hours for p in people)
        summary = ("<p class='muted'>Inga stämplingar i perioden.</p>" if not people else
                   '<div class="scroll"><table class="compact"><thead><tr><th>Person</th><th class="num">Dagar (AD)</th>'
                   '<th class="num">Stämplingar (AS)</th><th class="num">Timmar</th><th></th></tr></thead>'
                   f'<tbody>{sum_rows}<tr class="total"><td>Totalt</td><td class="num">{sum(len(p.days) for p in people)}</td>'
                   f'<td class="num">{sum(p.stamp_count for p in people)}</td><td class="num">{hours_fmt(total_h)}</td>'
                   '<td></td></tr></tbody></table></div>')

        # ---- per person
        person_cards = []
        for i, p in enumerate(people):
            rows = []
            for d in p.days:
                stamp_items = []
                for s in d.stamps:
                    if s.source == "Sitoo":
                        action = (f'<form class="inline" method="post" action="/tid/ta-bort">{csrf}'
                                  f'<input type="hidden" name="order_id" value="{s.ref}">'
                                  f'<input type="hidden" name="back" value="{esc(back)}">'
                                  f'<button class="sm danger" onclick="return confirm(\'Ta bort stämplingen {s.at:%H:%M}?\')">'
                                  'Ta bort</button></form>')
                        src = f"Sitoo order {s.ref}"
                    else:
                        action = (f'<form class="inline" method="post" action="/tid/radera-manuell">{csrf}'
                                  f'<input type="hidden" name="id" value="{s.ref}">'
                                  f'<input type="hidden" name="back" value="{esc(back)}">'
                                  f'<button class="sm danger" onclick="return confirm(\'Radera den manuella stämplingen?\')">'
                                  'Radera</button></form>')
                        src = "Manuell"
                    stamp_items.append(f'<tr><td>{s.at:%H:%M}</td><td class="muted small">{esc(src)}</td><td>{action}</td></tr>')
                details = (f'<details class="no-print"><summary>{d.count} st</summary>'
                           f'<table class="compact"><tbody>{"".join(stamp_items)}</tbody></table></details>')
                warn = f'<span class="pill warn">{esc(d.warning)}</span>' if d.warning else ""
                rows.append(f'<tr class="{"warn" if d.warning else ""}"><td>{day_fmt(d.day)}</td>'
                            f'<td>{d.time_in:%H:%M}</td><td>{d.time_out:%H:%M}</td>'
                            f'<td class="num">{hours_fmt(d.hours)}</td><td>{details}</td><td>{warn}</td></tr>')
            person_cards.append(
                f'<div class="card person" id="p-{i}"><h2>{esc(p.name)} <span class="muted">{len(p.days)} dagar · '
                f'{hours_fmt(p.hours)} timmar</span></h2><div class="scroll"><table class="compact"><thead><tr>'
                '<th>Datum</th><th>In</th><th>Ut</th><th class="num">Timmar</th><th>Stämplingar</th><th></th>'
                f'</tr></thead><tbody>{"".join(rows)}<tr class="total"><td>Totalt</td><td></td><td></td>'
                f'<td class="num">{hours_fmt(p.hours)}</td><td>{p.stamp_count} st</td><td></td></tr></tbody></table></div></div>')

        name_list = "".join(f'<option value="{esc(n)}">' for n in all_names)
        add_form = f"""<div class="card no-print"><h2>Lägg till stämpling</h2>
<p class="lead small">För glömda in- eller utstämplingar. Sparas i databasen och räknas som en vanlig stämpling.</p>
<form method="post" action="/tid/lagg-till" class="filters">{csrf}
<input type="hidden" name="back" value="{esc(back)}">
<div><label for="a_namn">Namn</label><input id="a_namn" type="text" name="namn" list="namnlista" required></div>
<datalist id="namnlista">{name_list}</datalist>
<div><label for="a_datum">Datum</label><input id="a_datum" type="date" name="datum" required></div>
<div><label for="a_tid">Tid</label><input id="a_tid" type="time" name="tid" required></div>
<div style="flex:1;min-width:200px"><label for="a_kom">Kommentar</label><input id="a_kom" type="text" name="kommentar"></div>
<div><button class="primary" type="submit">Lägg till</button></div></form></div>"""

        removed_rows = "".join(
            f'<tr><td>{esc(r.get("Namn"))}</td><td>{r["OrderDateLocal"]:%Y-%m-%d %H:%M}</td>'
            f'<td class="muted small">Order {r["OrderId"]}</td><td class="small">{esc(r.get("Kommentar"))}</td>'
            f'<td><form class="inline" method="post" action="/tid/aterstall">{csrf}'
            f'<input type="hidden" name="order_id" value="{r["OrderId"]}">'
            f'<input type="hidden" name="back" value="{esc(back)}"><button class="sm">Återställ</button></form></td></tr>'
            if r.get("OrderDateLocal") else
            f'<tr><td>–</td><td>–</td><td class="muted small">Order {r["OrderId"]} (finns inte i databasen)</td>'
            f'<td class="small">{esc(r.get("Kommentar"))}</td><td></td></tr>'
            for r in removed)
        removed_card = (f'<div class="card no-print"><details><summary>Borttagna stämplingar ({len(removed)})</summary>'
                        '<div class="scroll"><table class="compact"><tbody>'
                        f'{removed_rows or "<tr><td class=muted>Inga</td></tr>"}</tbody></table></div></details></div>')

        body = f"""<h1>Tidrapport</h1>
<div class="card no-print"><form method="get" action="/tid" class="filters">
<div><label for="f_period">Löneperiod</label><select id="f_period" name="period">{period_opts}</select></div>
<div><label for="f_person">Person</label><select id="f_person" name="person">{person_opts}</select></div>
<div><button class="primary" type="submit">Visa</button></div>
<div><a class="btn" href="/tid.csv?{esc(qs)}">Exportera (CSV)</a></div>
<div><button type="button" onclick="window.print()">Skriv ut</button></div>
</form></div>
<div class="card"><h2>Löneperiod {esc(period)} <span class="muted small">{start:%Y-%m-%d} – {end:%Y-%m-%d}</span></h2>
<p class="lead small">In är dagens första stämpling och Ut den sista. Timmar = Ut − In. Dagar med bara en stämpling ger 0 timmar och markeras.</p>
{summary}</div>
{''.join(person_cards)}
{add_form}
{removed_card}
<style>{REPORT_CSS}</style>"""
        self._page("Tidrapport", body, active="time")

    def csv_time(self) -> None:
        reports = self._reports()
        if reports is None:
            return self._send(400, "Databasen är inte inställd", "text/plain; charset=utf-8")
        period, start, end, person, _ = self._time_params()
        sku = self._settings().get("report_stamp_sku") or "91000"
        try:
            people = summarize_time(reports.stamps(start, end, sku))
        finally:
            reports.db.close()
        rows = [["Namn", "Datum", "Veckodag", "In", "Ut", "Stämplingar", "Timmar", "Kontrollera"]]
        for p in people:
            if person and p.name != person:
                continue
            for d in p.days:
                rows.append([p.name, d.day, WEEKDAYS[d.day.weekday()],
                             d.time_in.strftime("%H:%M"), d.time_out.strftime("%H:%M"), d.count,
                             round(d.hours, 2), d.warning or ""])
            rows.append([f"{p.name} totalt", "", "", "", "", p.stamp_count, round(p.hours, 2), ""])
        self._send(200, csv_response(rows), "text/csv; charset=utf-8",
                   {"Content-Disposition": f"attachment; filename=\"tidrapport-{period}.csv\""})

    def post_time(self, path: str, form: dict) -> None:
        back = self._back(form, "/tid")
        reports = self._reports()
        if reports is None:
            return self._redirect("/tid")
        try:
            if path == "/tid/lagg-till":
                day = parse_date(form.get("datum"))
                try:
                    t = datetime.strptime(form.get("tid", ""), "%H:%M").time()
                except ValueError:
                    t = None
                name = (form.get("namn") or "").strip()
                if day and t and name:
                    reports.add_stamp(name, datetime.combine(day, t), form.get("kommentar"))
                    log.info("Manuell stämpling tillagd: %s %s %s", name, day, t)
            elif path == "/tid/radera-manuell":
                reports.delete_manual_stamp(int(form["id"]))
            elif path == "/tid/ta-bort":
                reports.remove_order_stamp(int(form["order_id"]), form.get("kommentar"))
                log.info("Stämpling (order %s) borttagen ur tidrapporten", form["order_id"])
            elif path == "/tid/aterstall":
                reports.restore_order_stamp(int(form["order_id"]))
        finally:
            reports.db.close()
        self._redirect(back)

    # ============================================================ BOKFÖRING
    def _accounting_params(self) -> tuple[str, date, date, str]:
        q = self._q()
        frm, to = parse_date(q.get("fran")), parse_date(q.get("till"))
        if frm and to and frm <= to:
            return "", frm, to, f"{frm:%Y-%m-%d} – {to:%Y-%m-%d}"
        month = q.get("manad") or f"{date.today():%Y-%m}"
        try:
            start, end = month_period(month)
        except (ValueError, TypeError):
            month = f"{date.today():%Y-%m}"
            start, end = month_period(month)
        return month, start, end, month

    def _accounts(self) -> tuple[dict, dict]:
        s = self._settings()
        accounts = {k: s.get(f"acc_{k}") or v for k, v in DEFAULT_ACCOUNTS.items()}
        return accounts, parse_payment_accounts(s.get("acc_payments") or "")

    def _verification(self, reports: Reports, start: date, end: date):
        zrows = reports.zrows(start, end)
        deposits = reports.deposits(start, end)
        total_dep = sum((Decimal(d["Belopp"]) for d in deposits), Decimal("0.00"))
        accounts, pay_accounts = self._accounts()
        return build_verification(zrows, total_dep, accounts, pay_accounts), deposits

    def page_accounting(self) -> None:
        reports = self._reports()
        if reports is None:
            return self._not_configured("Bokföring", "accounting")
        month, start, end, label = self._accounting_params()
        try:
            v, deposits = self._verification(reports, start, end)
        except Exception as err:  # noqa: BLE001
            return self._db_error("Bokföring", "accounting", err)
        finally:
            reports.db.close()
        back = self.path
        csrf = self._csrf_field()

        months = recent_months(date.today(), 36)
        month_opts = "".join(f'<option value="{m}" {"selected" if m == month else ""}>{m}</option>' for m in months)
        qs = urlencode({"manad": month} if month else {"fran": f"{start:%Y-%m-%d}", "till": f"{end:%Y-%m-%d}"})

        warn_html = "".join(f'<div class="alert a-warn">{esc(w)}</div>' for w in v.warnings)
        balanced = ('<span class="pill ok">Balanserar</span>' if v.diff == 0 and v.lines else
                    f'<span class="pill warn">Diff {money(v.diff)}</span>' if v.lines else "")

        # ---- verifikation
        ver_rows = "".join(
            f'<tr><td>{esc(l.account)}</td><td>{esc(l.text)}</td><td class="num">{money(l.debit, True)}</td>'
            f'<td class="num">{money(l.credit, True)}</td></tr>' for l in v.lines)
        ver = ("<p class='muted'>Inga z-rapporter i perioden.</p>" if not v.zrows else
               '<table class="compact"><thead><tr><th>Konto</th><th>Text</th><th class="num">Debet</th>'
               f'<th class="num">Kredit</th></tr></thead><tbody>{ver_rows}<tr class="total"><td></td><td>Summa</td>'
               f'<td class="num">{money(v.debit)}</td><td class="num">{money(v.credit)}</td></tr></tbody></table>')

        # ---- z-rapporter
        rates = sorted({r for z in v.zrows for r in z.vat_net}, reverse=True)
        pay_names = sorted({n for z in v.zrows for n in z.payments}, key=str.casefold)
        head = ("<th>Kassa</th><th>Z-nr</th><th>Datum</th><th class='num'>Försäljning</th><th class='num'>Retur</th>"
                + "".join(f"<th class='num'>{r} % netto</th>" + (f"<th class='num'>Moms {r} %</th>" if r else "")
                          for r in rates)
                + "<th class='num'>Öresavr.</th>"
                + "".join(f"<th class='num'>{esc(n)}</th>" for n in pay_names)
                + "<th class='num'>Uttag till bank</th><th class='no-print'></th>")
        zrows_html = []
        for z in v.zrows:
            fixed = (f' <span class="pill warn" title="Ursprungligt datum {z.original_day:%Y-%m-%d}">rättat</span>'
                     if z.fixed else "")
            cells = "".join(f"<td class='num'>{money(z.vat_net.get(r), True)}</td>"
                            + (f"<td class='num'>{money(z.vat_amount.get(r), True)}</td>" if r else "")
                            for r in rates)
            pays = "".join(f"<td class='num'>{money(z.payments.get(n), True)}</td>" for n in pay_names)
            fix_form = (f'<details><summary>Ändra datum</summary><form method="post" action="/bokforing/datum" '
                        f'class="inline-form">{csrf}<input type="hidden" name="back" value="{esc(back)}">'
                        f'<input type="hidden" name="register_id" value="{esc(z.register_id)}">'
                        f'<input type="hidden" name="zreport_id" value="{z.zreport_id}">'
                        f'<input type="date" name="datum" value="{z.day:%Y-%m-%d}">'
                        '<button class="sm">Spara</button>'
                        + ('<button class="sm" name="atgard" value="aterstall">Återställ</button>' if z.fixed else "")
                        + '</form></details>')
            zrows_html.append(
                f"<tr><td>{esc(z.kassa)}</td><td>{z.zreport_id}</td><td>{day_fmt(z.day)}{fixed}</td>"
                f"<td class='num'>{money(z.sales)}</td><td class='num'>{money(z.refunds, True)}</td>{cells}"
                f"<td class='num'>{money(z.rounding, True)}</td>{pays}"
                f"<td class='num'>{money(z.cash_to_bank, True)}</td><td class='no-print'>{fix_form}</td></tr>")
        sums = ("<tr class='total'><td>Summa</td><td></td><td></td>"
                f"<td class='num'>{money(sum((z.sales for z in v.zrows), Decimal(0)))}</td>"
                f"<td class='num'>{money(sum((z.refunds for z in v.zrows), Decimal(0)), True)}</td>"
                + "".join(f"<td class='num'>{money(sum((z.vat_net.get(r, Decimal(0)) for z in v.zrows), Decimal(0)))}</td>"
                          + (f"<td class='num'>{money(sum((z.vat_amount.get(r, Decimal(0)) for z in v.zrows), Decimal(0)))}</td>"
                             if r else "") for r in rates)
                + f"<td class='num'>{money(sum((z.rounding for z in v.zrows), Decimal(0)), True)}</td>"
                + "".join(f"<td class='num'>{money(sum((z.payments.get(n, Decimal(0)) for z in v.zrows), Decimal(0)))}</td>"
                          for n in pay_names)
                + f"<td class='num'>{money(sum((z.cash_to_bank for z in v.zrows), Decimal(0)), True)}</td><td class='no-print'></td></tr>")
        ztable = ("<p class='muted'>Inga z-rapporter i perioden.</p>" if not v.zrows else
                  f'<div class="scroll"><table class="compact"><thead><tr>{head}</tr></thead>'
                  f'<tbody>{"".join(zrows_html)}{sums}</tbody></table></div>')

        # ---- bankningar
        dep_rows = "".join(
            f'<tr><td>{d["Datum"]:%Y-%m-%d}</td><td class="num">{money(d["Belopp"])}</td>'
            f'<td class="small">{esc(d.get("Kommentar"))}</td><td class="no-print"><form class="inline" method="post" '
            f'action="/bokforing/radera-bankning">{csrf}<input type="hidden" name="id" value="{d["Id"]}">'
            f'<input type="hidden" name="back" value="{esc(back)}"><button class="sm danger" '
            f'onclick="return confirm(\'Radera bankningen?\')">Radera</button></form></td></tr>' for d in deposits)
        dep_card = f"""<div class="card"><h2>Bankningar av kontanter</h2>
<p class="lead small">Kontanter som satts in på banken under perioden. Debiteras {esc(self._accounts()[0]['bank'])}; resten av kontantförsäljningen hamnar på {esc(self._accounts()[0]['cash'])}.</p>
<table class="compact"><tbody>{dep_rows or '<tr><td class="muted">Inga bankningar i perioden</td></tr>'}</tbody></table>
<form method="post" action="/bokforing/bankning" class="filters no-print" style="margin-top:14px">{csrf}
<input type="hidden" name="back" value="{esc(back)}">
<div><label for="b_datum">Datum</label><input id="b_datum" type="date" name="datum" required></div>
<div><label for="b_belopp">Belopp</label><input id="b_belopp" type="text" name="belopp" inputmode="decimal" required></div>
<div style="flex:1;min-width:200px"><label for="b_kom">Kommentar</label><input id="b_kom" type="text" name="kommentar"></div>
<div><button class="primary" type="submit">Lägg till</button></div></form></div>"""

        body = f"""<h1>Bokföring</h1>
<div class="card no-print"><form method="get" action="/bokforing" class="filters">
<div><label for="f_manad">Månad</label><select id="f_manad" name="manad">{month_opts}</select></div>
<div><button class="primary" type="submit">Visa</button></div>
</form>
<details style="margin-top:12px"><summary>Annan period</summary>
<form method="get" action="/bokforing" class="filters">
<div><label for="f_fran">Från</label><input id="f_fran" type="date" name="fran" value="{start:%Y-%m-%d}"></div>
<div><label for="f_till">Till</label><input id="f_till" type="date" name="till" value="{end:%Y-%m-%d}"></div>
<div><button type="submit">Visa</button></div></form></details>
<div class="row" style="margin-top:12px"><a class="btn" href="/bokforing.csv?{esc(qs)}">Exportera (CSV)</a>
<button type="button" onclick="window.print()">Skriv ut</button>
<a class="btn" href="/installningar#accounting">Konton</a></div></div>
{warn_html}
<div class="card"><h2>Verifikation {esc(label)} {balanced}</h2>
<p class="lead small">Försäljning enligt z-rapporterna {start:%Y-%m-%d} – {end:%Y-%m-%d}. Moms räknas som netto × momssats; skillnaden mot z-rapportens moms läggs på öresavrundning.</p>
{ver}</div>
<div class="card"><h2>Z-rapporter ({len(v.zrows)} st)</h2>
<p class="lead small">Datum är dagen kassan öppnades. Använd Ändra datum om en z-rapport hör till en annan dag.</p>{ztable}</div>
{dep_card}
<style>{REPORT_CSS}</style>"""
        self._page("Bokföring", body, active="accounting")

    def csv_accounting(self) -> None:
        reports = self._reports()
        if reports is None:
            return self._send(400, "Databasen är inte inställd", "text/plain; charset=utf-8")
        month, start, end, label = self._accounting_params()
        try:
            v, _ = self._verification(reports, start, end)
        finally:
            reports.db.close()
        rows = [[f"Verifikation {label}", f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}"], [],
                ["Konto", "Text", "Debet", "Kredit"]]
        rows += [[l.account, l.text, l.debit, l.credit] for l in v.lines]
        rows.append(["", "Summa", v.debit, v.credit])
        rows += [[], ["Kassa", "Z-nr", "Datum", "Försäljning", "Retur", "25 % netto", "Moms 25 %", "12 % netto",
                      "Moms 12 %", "6 % netto", "Moms 6 %", "0 % netto", "Öresavrundning", "Betalningar",
                      "Uttag till bank"]]
        for z in v.zrows:
            rows.append([z.kassa, z.zreport_id, z.day, z.sales, z.refunds,
                         z.vat_net.get(25), z.vat_amount.get(25), z.vat_net.get(12), z.vat_amount.get(12),
                         z.vat_net.get(6), z.vat_amount.get(6), z.vat_net.get(0), z.rounding,
                         ", ".join(f"{n} {a:.2f}".replace(".", ",") for n, a in z.payments.items()),
                         z.cash_to_bank])
        name = month or f"{start:%Y%m%d}-{end:%Y%m%d}"
        self._send(200, csv_response(rows), "text/csv; charset=utf-8",
                   {"Content-Disposition": f"attachment; filename=\"bokforing-{name}.csv\""})

    def post_accounting(self, path: str, form: dict) -> None:
        back = self._back(form, "/bokforing")
        reports = self._reports()
        if reports is None:
            return self._redirect("/bokforing")
        try:
            if path == "/bokforing/bankning":
                day = parse_date(form.get("datum"))
                try:
                    amount = Decimal((form.get("belopp") or "").replace(" ", "").replace(",", "."))
                except InvalidOperation:
                    amount = None
                if day and amount is not None:
                    reports.add_deposit(day, amount, form.get("kommentar"))
            elif path == "/bokforing/radera-bankning":
                reports.delete_deposit(int(form["id"]))
            elif path == "/bokforing/datum":
                new_day = None if form.get("atgard") == "aterstall" else parse_date(form.get("datum"))
                reports.set_zreport_date(form["register_id"], int(form["zreport_id"]), new_day, form.get("kommentar"))
                log.info("Z-rapport %s datum satt till %s", form["zreport_id"], new_day or "original")
        finally:
            reports.db.close()
        self._redirect(back)
