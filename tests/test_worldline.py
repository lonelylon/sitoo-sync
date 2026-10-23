"""Tester för inläsning av Worldlines rapporter och korsreferens mot Sitoo (syntetiska filer)."""
from __future__ import annotations

import io
import unittest
import zipfile
from datetime import date
from decimal import Decimal
from xml.sax.saxutils import escape

from sitoosync.reports import ZRow, build_verification, parse_payment_accounts
from sitoosync.worldline import (
    WorldlineError, compare_days, group_months, parse_report, sitoo_card_by_day,
)

DETAIL_HEAD = ["UTBETALNINGSDATUM", "TRANSAKTIONSDATUM", "TID", "KUNDNUMMER", "UNDERHANDLARE", "MCC", "BATCH ID",
               "SÄLJSTÄLLE", "UTBETALNINGSREFERENS", "TRANSAKTIONSREFERENS", "BETALSÄTT", "KORTTYP", "KORTNR",
               "UTFÄRDANDE LAND", "TRANSAKTIONENS DOMÄN", "TRANSAKTIONENS LAND", "TYP", "TRANSAKTIONSBELOPP ",
               "VALUTA", "BELOPP", "VALUTA", "VÄXLINGSKURS", "TRANSAKTIONSAVGIFT", "VALUTA", "SLUTBELOPP", "VALUTA",
               "TILLÄGGSREF. 1", "TILLÄGGSREF. 2", "TILLÄGGSREF. 3", "TERMINAL-ID", "DCC-BELOPP", "DCC-VALUTA"]


def detail(payout_day, tx_day, ref, amount, fee, payout_ref, typ="Sale", merchant="Butiken"):
    net = Decimal(str(amount)) + Decimal(str(fee))
    return [payout_day, tx_day, "12:00:00", "111", None, 5712, 1, merchant, payout_ref, ref, "Visa", "Debit",
            "4111xx", "Sverige", "Domestic", "Sverige", typ, amount, "SEK", amount, "SEK", 1, fee, "SEK",
            float(net), "SEK", "00" + ref, ref, None, "T1", None, None]


def xlsx(sheets: dict[str, list[list]]) -> bytes:
    """Minimal xlsx med inline-text och tal, som Worldlines exporter."""
    buf = io.BytesIO()
    z = zipfile.ZipFile(buf, "w")
    wb = ['<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>']
    rels = ['<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    for i, (name, rows) in enumerate(sheets.items(), 1):
        wb.append(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>')
        rels.append(f'<Relationship Id="rId{i}" Type="worksheet" Target="worksheets/sheet{i}.xml"/>')
        xml = ['<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
        for r, row in enumerate(rows, 1):
            xml.append(f'<row r="{r}">')
            for c, v in enumerate(row):
                col = ""
                n = c + 1
                while n:
                    n, rem = divmod(n - 1, 26)
                    col = chr(65 + rem) + col
                if v is None:
                    continue
                if isinstance(v, (int, float, Decimal)):
                    xml.append(f'<c r="{col}{r}"><v>{v}</v></c>')
                else:
                    xml.append(f'<c r="{col}{r}" t="inlineStr"><is><t>{escape(str(v))}</t></is></c>')
            xml.append("</row>")
        xml.append("</sheetData></worksheet>")
        z.writestr(f"xl/worksheets/sheet{i}.xml", "".join(xml))
    wb.append("</sheets></workbook>")
    rels.append("</Relationships>")
    z.writestr("xl/workbook.xml", "".join(wb))
    z.writestr("xl/_rels/workbook.xml.rels", "".join(rels))
    z.close()
    return buf.getvalue()


def payout_file():
    return xlsx({
        "Överblick": [[None], [None, "Utbetalningsrapport"], [None, "Rapport", "B1"], [None, "Period", "2026-09-02"]],
        "Utbetalningar Detaljerat": [DETAIL_HEAD,
                                     detail("2026-09-02", "2026-09-01", "1001", 100, -1.0, "B1"),
                                     detail("2026-09-02", "2026-09-01", "1002", 250.5, -2.5, "B1")],
        "Reklamationer": [["DATUM", "KUNDNUMMER", "UTBETALNINGSREFERENS", "CASE ID", "BELOPP"]],
        "Justeringar": [["DATUM", "UTBETALNINGSREFERENS", "KUNDNUMMER", "DETALJER", "BELOPP", "VALUTA"],
                        ["2026-09-02", "B1", 111, "Transaktionsavgifter", -3.5, "SEK"],
                        ["2026-09-02", "B1", 111, "Terminalhyra", -99, "SEK"]],
    })


def monthly_file():
    sales_head = ["TRANSAKTIONSDATUM", "TID", "KUNDNUMMER", "TRANSAKTIONSREF", "BETALSÄTT", "KORTNUMMER", "KORTTYP",
                  "TYP", "STATUS", "VALUTA", "FÖRSÄLJNINGSBELOPP", "DRICKS", "TERMINAL-ID", "KASSÖR-ID", "SÄLJSTÄLLE"]
    return xlsx({
        "Överblick": [[None], [None, "Månadsrapport"], [None, "Rapport", "September"], [None, "Period", "20260901-20260930"]],
        "Försäljning": [sales_head,
                        ["2026-09-01", "11:00:00", "222", "1001", "Visa", "x", "Debit", "Sale", "Cleared", "SEK", 100, 0, "T1", None, "Butiken"],
                        ["2026-09-30", "15:00:00", "222", "1003", "Visa", "x", "Debit", "Sale", "Cleared", "SEK", 80, 0, "T1", None, "Butiken"],
                        ["2026-09-30", "15:05:00", "222", "1004", "Visa", "x", "Debit", "Refund", "Cleared", "SEK", -20, 0, "T1", None, "Butiken"]],
        "Utbetalningar": [["DATUM", "REFERENS", "MOTTAGARENS KONTO", "KUNDNUMMER", "SÄLJSTÄLLE", "TRANSAKTIONER", "VALUTA",
                           "OMSÄTTNING", "AVGIFTER", "JUSTERINGAR", "REKLAMATIONER", "BELOPP"],
                          ["2026-09-02", "B1", "SE00", 111, "Butiken", 2, "SEK", 350.5, -3.5, -99, 0, 248]],
        "Utbetalningar Detaljerat": [DETAIL_HEAD,
                                     detail("2026-09-02", "2026-09-01", "1001", 100, -1.0, "B1"),
                                     detail("2026-09-02", "2026-09-01", "1002", 250.5, -2.5, "B1")],
        "Justeringar": [["DATUM", "UTBETALNINGSREFERENS", "KUNDNUMMER", "DETALJER", "BELOPP", "VALUTA"],
                        ["2026-09-02", "B1", 111, "Transaktionsavgifter", -3.5, "SEK"]],
    })


class ParseTests(unittest.TestCase):
    def test_payout_report(self):
        rep = parse_report(payout_file())
        self.assertEqual((rep.kind, rep.reference, rep.period), ("utbetalning", "B1", "2026-09-02"))
        self.assertEqual(len(rep.transactions), 2)
        t = rep.transactions[1]
        self.assertEqual((t["ref"], t["date"], t["amount"], t["fee"], t["net"], t["payoutref"]),
                         ("1002", date(2026, 9, 1), Decimal("250.50"), Decimal("-2.50"), Decimal("248.00"), "B1"))
        # Utbetalningen räknas fram: avgiftsjusteringen räknas inte två gånger, terminalhyran dras
        p = rep.payouts[0]
        self.assertEqual((p["turnover"], p["fees"], p["adjustments"], p["amount"]),
                         (Decimal("350.50"), Decimal("-3.50"), Decimal("-99.00"), Decimal("248.00")))

    def test_monthly_report_merges_sales_and_details(self):
        rep = parse_report(monthly_file())
        self.assertEqual(rep.kind, "manad")
        by_ref = {t["ref"]: t for t in rep.transactions}
        self.assertEqual(set(by_ref), {"1001", "1002", "1003", "1004"})
        self.assertEqual(by_ref["1001"]["fee"], Decimal("-1.00"))       # från detaljfliken
        self.assertEqual(by_ref["1001"]["status"], "Cleared")          # från försäljningsfliken
        self.assertNotIn("fee", by_ref["1003"])                         # inte utbetald än
        self.assertEqual(by_ref["1004"]["amount"], Decimal("-20.00"))
        self.assertEqual(rep.payouts[0]["amount"], Decimal("248.00"))   # från fliken Utbetalningar
        self.assertEqual(rep.dates, (date(2026, 9, 1), date(2026, 9, 30)))
        rep.as_json_dict()                                              # går att serialisera

    def test_not_a_report(self):
        with self.assertRaises(WorldlineError):
            parse_report(b"not a zip")
        with self.assertRaises(WorldlineError):
            parse_report(xlsx({"Blad1": [["A", "B"], [1, 2]]}))


def zrow(day, payments):
    return ZRow("R1", 1, 1, day, day, False, None, Decimal(0), Decimal(0), Decimal(0), Decimal(0), 0,
                payments={k: Decimal(v) for k, v in payments.items()})


class CrossRefTests(unittest.TestCase):
    PA = parse_payment_accounts("Kort=1937; Swish=1936; Kontant=KONTANT")

    def test_day_status_and_months(self):
        sitoo = sitoo_card_by_day([zrow(date(2026, 9, 1), {"Kort": "350.50", "Swish": "50"}),
                                   zrow(date(2026, 9, 2), {"Kort": "100", "Kontant": "10"}),
                                   zrow(date(2026, 9, 3), {"Kort": "70"})], self.PA, "1937", "1911")
        self.assertEqual(sitoo, {date(2026, 9, 1): Decimal("350.50"), date(2026, 9, 2): Decimal("100"),
                                 date(2026, 9, 3): Decimal("70")})
        wl = {date(2026, 9, 1): {"Belopp": "350.50", "Antal": 2, "Avgift": "-3.50", "UtanAvgift": 0,
                                 "Utbetalningar": "B1,B1"},
              date(2026, 9, 3): {"Belopp": "60.00", "Antal": 1, "Avgift": "0", "UtanAvgift": 1, "Utbetalningar": None}}
        days = compare_days(date(2026, 9, 1), date(2026, 9, 4), sitoo, wl)
        self.assertEqual([d.status for d in days], ["ok", "saknas", "avvikelse", "ingen"])
        self.assertEqual(days[0].payouts, ["B1"])
        self.assertEqual(days[2].diff, Decimal("-10.00"))
        m = group_months(days)[0]
        self.assertEqual((m.month, m.status, len(m.missing), m.unpaid, m.fee),
                         ("2026-09", "ofullstandig", 1, 1, Decimal("-3.50")))

    def test_fee_moves_from_card_account_to_fee_account(self):
        z = ZRow("R1", 1, 1, date(2026, 9, 1), date(2026, 9, 1), False, None, Decimal("125.00"), Decimal(0),
                 Decimal(0), Decimal("125.00"), 1, vat_net={25: Decimal("100.00")}, vat_amount={25: Decimal("25.00")},
                 payments={"Kort": Decimal("125.00")})
        v = build_verification([z], Decimal(0), {}, self.PA, {}, card_fees=Decimal("1.25"))
        lines = {l.account: l for l in v.lines}
        self.assertEqual(lines["1937"].debit, Decimal("123.75"))
        self.assertIn("efter avgift", lines["1937"].note)
        self.assertEqual(lines["6570"].debit, Decimal("1.25"))
        self.assertEqual(v.diff, Decimal("0.00"))


if __name__ == "__main__":
    unittest.main()


class ExplainTests(unittest.TestCase):
    def test_explanations_and_table(self):
        from sitoosync.reports import Verification
        from sitoosync.web_reports import card_check_html, explain_day
        wl = {date(2026, 9, 1): {"Belopp": "100", "Antal": 1}, date(2026, 9, 2): {"Belopp": "289", "Antal": 3},
              date(2026, 9, 3): {"Belopp": "111", "Antal": 1}, date(2026, 9, 5): {"Belopp": "50", "Antal": 1}}
        sitoo = {date(2026, 9, 1): Decimal("100"), date(2026, 9, 2): Decimal("200"), date(2026, 9, 3): Decimal("200"),
                 date(2026, 9, 4): Decimal("70"), date(2026, 9, 5): Decimal("60")}
        days = compare_days(date(2026, 9, 1), date(2026, 9, 5), sitoo, wl)
        by = {d.day: d for d in days}
        self.assertIn("tar ut 89,00 kr mot tor 2026-09-03", explain_day(by[date(2026, 9, 2)], by))
        self.assertIn("Ingen Worldline-data", explain_day(by[date(2026, 9, 4)], by))
        self.assertIn("Sitoo har 10,00 kr mer", explain_day(by[date(2026, 9, 5)], by))
        v = Verification(lines=[], zrows=[], deposits=Decimal(0), warnings=[], card_days=days)
        html = card_check_html(v)
        self.assertIn("1 dag stämmer", html)
        self.assertIn("3 dagar avviker", html)
        self.assertIn("1 dag saknar Worldline-data", html)
        self.assertNotIn("tis 2026-09-01", html)          # dagar som stämmer listas inte
