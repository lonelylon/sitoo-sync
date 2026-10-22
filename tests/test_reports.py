"""Tester för tidrapport och bokföringsunderlag (ren logik, ingen databas)."""
from __future__ import annotations

import unittest
from datetime import date, datetime
from decimal import Decimal

from sitoosync.reports import (
    D, Stamp, assemble_zrows, build_verification, format_chart, format_payment_map, parse_chart,
    parse_payment_accounts, parse_payment_map, payroll_period,
    payroll_period_for, summarize_time,
)


class PeriodTests(unittest.TestCase):
    def test_payroll_period(self):
        self.assertEqual(payroll_period("2026-09"), (date(2026, 8, 21), date(2026, 9, 20)))
        self.assertEqual(payroll_period("2026-01"), (date(2025, 12, 21), date(2026, 1, 20)))
        self.assertEqual(payroll_period("2026-02", start_day=1), (date(2026, 2, 1), date(2026, 2, 28)))

    def test_period_for_day(self):
        self.assertEqual(payroll_period_for(date(2026, 8, 21)), "2026-09")
        self.assertEqual(payroll_period_for(date(2026, 9, 20)), "2026-09")
        self.assertEqual(payroll_period_for(date(2026, 12, 25)), "2027-01")


class TimeTests(unittest.TestCase):
    def test_in_out_hours_like_excel(self):
        # Exempel från Excel: Edvin Ulrichs 2026-08-21, in 09:59, ut 18:00 = 8,0167 h, AD 1, AS 2
        stamps = [
            Stamp("Sitoo", 1, "Edvin Ulrichs", datetime(2026, 8, 21, 18, 0)),
            Stamp("Sitoo", 2, "Edvin Ulrichs ", datetime(2026, 8, 21, 9, 59)),
            Stamp("Sitoo", 3, "Felix Lundkvist", datetime(2026, 8, 22, 13, 55)),
            Stamp("Manuell", 9, "Felix Lundkvist", datetime(2026, 8, 22, 20, 25)),
            Stamp("Sitoo", 4, "Edvin Ulrichs", datetime(2026, 8, 23, 14, 1)),
        ]
        people = summarize_time(stamps)
        self.assertEqual([p.name for p in people], ["Edvin Ulrichs", "Felix Lundkvist"])
        edvin = people[0]
        first = edvin.days[0]
        self.assertEqual((first.time_in, first.time_out), (datetime(2026, 8, 21, 9, 59), datetime(2026, 8, 21, 18, 0)))
        self.assertAlmostEqual(first.hours, 8.016666, places=4)
        self.assertEqual(first.count, 2)
        self.assertIsNone(first.warning)
        self.assertEqual(edvin.days[1].warning, "Bara en stämpling")
        self.assertEqual(edvin.days[1].hours, 0)
        self.assertEqual(edvin.warnings, 1)
        felix = people[1]
        self.assertAlmostEqual(felix.hours, 6.5, places=4)

    def test_odd_count_flagged(self):
        stamps = [Stamp("Sitoo", i, "A", datetime(2026, 1, 1, 8 + i)) for i in range(3)]
        self.assertEqual(summarize_time(stamps)[0].days[0].warning, "Udda antal stämplingar (3)")


def z(rid, zid, num, day, sales, refund="0.00", rounding="0.00", bank=None):
    return {"RegisterId": rid, "ZReportId": zid, "RegisterNumber": num, "Dag": day, "OriginalDag": day,
            "NyttDatum": None, "Stangd": None, "MoneySalesTotal": sales, "MoneyRefundTotal": refund,
            "MoneyRoundOff": rounding, "MoneySummaryTotal": sales, "NumReceipts": 10, "CashToBank": bank}


def vg(rid, zid, kind, rate, net, vat):
    return {"RegisterId": rid, "ZReportId": zid, "Kind": kind, "VatValue": rate,
            "MoneyTotal": str(D(net) + D(vat)), "MoneyTotalNet": net, "MoneyTotalVat": vat}


def pay(rid, zid, kind, name, amount):
    return {"RegisterId": rid, "ZReportId": zid, "Kind": kind, "Name": name, "MoneyTotal": amount}


class VerificationTests(unittest.TestCase):
    ACC = parse_payment_accounts("Kort=1937; Swish=1936; Kontant=1911")

    def test_parse_payment_accounts(self):
        self.assertEqual(self.ACC, {"kort": "1937", "swish": "1936", "kontant": "1911"})

    def test_excel_example_balances(self):
        # Kassa 1, z-rapport 1845, 2026-09-01: brutto 19 997,50; 25 %: 15 411,60 + 3 852,90; 6 %: 691,50 + 41,50
        zs = [z("R1", 1845, 1, date(2026, 9, 1), "19997.50")]
        vats = [vg("R1", 1845, "sales", 25, "15411.60", "3852.90"),
                vg("R1", 1845, "sales", 6, "691.50", "41.50")]
        pays = [pay("R1", 1845, "sales", "Kort", "19962.50"), pay("R1", 1845, "sales", "Swish", "35.00")]
        rows = assemble_zrows(zs, vats, pays)
        self.assertEqual(rows[0].vat_net, {25: Decimal("15411.60"), 6: Decimal("691.50")})
        v = build_verification(rows, Decimal("0"), {}, self.ACC)
        lines = {(l.account, l.text): (l.debit, l.credit) for l in v.lines}
        self.assertEqual(lines[("1937", "Försäljning – Kort")], (Decimal("19962.50"), Decimal("0.00")))
        self.assertEqual(lines[("1936", "Försäljning – Swish")][0], Decimal("35.00"))
        self.assertEqual(lines[("3011", "Försäljning 25 % moms")][1], Decimal("15411.60"))
        self.assertEqual(lines[("2610", "Utgående moms 25 %")][1], Decimal("3852.90"))
        self.assertEqual(lines[("3009", "Försäljning 6 % moms")][1], Decimal("691.50"))
        # 691,50 x 6 % = 41,49; rapporterad moms 41,50 -> 0,01 till öresavrundning (som i Excel)
        self.assertEqual(lines[("2622", "Utgående moms 6 %")][1], Decimal("41.49"))
        self.assertEqual(lines[("3740", "Öresavrundning")][1], Decimal("0.01"))
        self.assertEqual(v.diff, Decimal("0.00"))
        self.assertEqual(v.warnings, [])

    def test_refunds_subtracted_whatever_sign(self):
        for refund_net, refund_vat, refund_pay in (("80.00", "20.00", "100.00"), ("-80.00", "-20.00", "-100.00")):
            zs = [z("R1", 1, 1, date(2026, 9, 1), "500.00", refund="100.00")]
            vats = [vg("R1", 1, "sales", 25, "400.00", "100.00"), vg("R1", 1, "refund", 25, refund_net, refund_vat)]
            pays = [pay("R1", 1, "sales", "Kort", "500.00"), pay("R1", 1, "refund", "Kort", refund_pay)]
            v = build_verification(assemble_zrows(zs, vats, pays), Decimal("0"), {}, self.ACC)
            credit = {l.account: l.credit for l in v.lines}
            self.assertEqual(credit["3011"], Decimal("320.00"))
            self.assertEqual(credit["2610"], Decimal("80.00"))
            self.assertEqual(v.diff, Decimal("0.00"))

    def test_cash_split_between_bank_and_register(self):
        zs = [z("R1", 1, 1, date(2026, 9, 1), "1250.00")]
        vats = [vg("R1", 1, "sales", 25, "1000.00", "250.00")]
        pays = [pay("R1", 1, "sales", "Kontant", "1250.00")]
        v = build_verification(assemble_zrows(zs, vats, pays), Decimal("1000.00"), {}, self.ACC)
        debit = {l.account: l.debit for l in v.lines}
        self.assertEqual(debit["1938"], Decimal("1000.00"))
        self.assertEqual(debit["1911"], Decimal("250.00"))
        self.assertEqual(v.diff, Decimal("0.00"))
        # Mer bankat än kontant försäljning -> kassan krediteras
        v = build_verification(assemble_zrows(zs, vats, pays), Decimal("2000.00"), {}, self.ACC)
        cash = [l for l in v.lines if l.account == "1911"][0]
        self.assertEqual((cash.debit, cash.credit), (Decimal("0.00"), Decimal("750.00")))

    def test_payments_on_same_account_summed_on_one_line(self):
        zs = [z("R1", 1, 1, date(2026, 9, 1), "1250.00")]
        vats = [vg("R1", 1, "sales", 25, "1000.00", "250.00")]
        pays = [pay("R1", 1, "sales", "Swish QR", "1000.00"), pay("R1", 1, "sales", "Swish nr", "200.00"),
                pay("R1", 1, "sales", "Kort", "50.00")]
        acc = parse_payment_accounts("Kort=1937; Swish QR=1936; Swish nr=1936")
        v = build_verification(assemble_zrows(zs, vats, pays), Decimal("0"), {}, acc,
                               parse_chart("1936=Swish; 1937=Kort"))
        swish = [l for l in v.lines if l.account == "1936"]
        self.assertEqual(len(swish), 1)
        self.assertEqual(swish[0].debit, Decimal("1200.00"))
        self.assertEqual(swish[0].text, "Försäljning – Swish")
        self.assertEqual(swish[0].note, "Swish QR, Swish nr")
        kort = [l for l in v.lines if l.account == "1937"][0]
        self.assertEqual((kort.text, kort.note), ("Försäljning – Kort", ""))
        self.assertEqual(v.diff, Decimal("0.00"))
        # Utan kontoplan blir texten betalsättens namn
        v = build_verification(assemble_zrows(zs, vats, pays), Decimal("0"), {}, acc)
        self.assertIn("Försäljning – Swish QR, Swish nr", [l.text for l in v.lines])

    def test_cash_token_and_legacy_cash_account(self):
        zs = [z("R1", 1, 1, date(2026, 9, 1), "1250.00")]
        vats = [vg("R1", 1, "sales", 25, "1000.00", "250.00")]
        pays = [pay("R1", 1, "sales", "Kontanter SEK", "1250.00")]
        v = build_verification(assemble_zrows(zs, vats, pays), Decimal("1000.00"), {},
                               parse_payment_accounts("Kontanter SEK=KONTANT"))
        debit = {l.account: l.debit for l in v.lines}
        self.assertEqual((debit["1938"], debit["1911"]), (Decimal("1000.00"), Decimal("250.00")))
        # 'Kontant' mappat mot ett vanligt konto som inte är kassan bokförs bara där
        pays = [pay("R1", 1, "sales", "Kontant", "1250.00")]
        v = build_verification(assemble_zrows(zs, vats, pays), Decimal("0"), {},
                               parse_payment_accounts("Kontant=1930"))
        self.assertEqual([(l.account, l.debit) for l in v.lines if l.debit], [("1930", Decimal("1250.00"))])
        # Äldre inställning 'Kontant=1911' delas fortfarande på bank och kassa
        v = build_verification(assemble_zrows(zs, vats, pays), Decimal("1000.00"), {},
                               parse_payment_accounts("Kontant=1911"))
        debit = {l.account: l.debit for l in v.lines}
        self.assertEqual((debit["1938"], debit["1911"]), (Decimal("1000.00"), Decimal("250.00")))

    def test_parse_and_format_settings_text(self):
        m = parse_payment_map("Swish QR=1936; Kort=1937")
        self.assertEqual(list(m.items()), [("Swish QR", "1936"), ("Kort", "1937")])
        self.assertEqual(format_payment_map({"A;b": "1=2"}), "A,b=1-2")
        self.assertEqual(list(parse_chart("3011=Försäljning; 1936=Swish")), ["1936", "3011"])
        self.assertEqual(format_chart({"1937": "Kort", "1936": "Swish"}), "1936=Swish; 1937=Kort")

    def test_unknown_payment_warns(self):
        zs = [z("R1", 1, 1, date(2026, 9, 1), "125.00")]
        vats = [vg("R1", 1, "sales", 25, "100.00", "25.00")]
        pays = [pay("R1", 1, "sales", "Presentkort", "125.00")]
        v = build_verification(assemble_zrows(zs, vats, pays), Decimal("0"), {}, self.ACC)
        self.assertTrue(any("Presentkort" in w for w in v.warnings))

    def test_custom_accounts_and_rounding(self):
        zs = [z("R1", 1, 2, date(2026, 9, 4), "1236.00", rounding="-0.40")]
        vats = [vg("R1", 1, "sales", 25, "893.60", "223.40"), vg("R1", 1, "sales", 6, "112.26", "6.74")]
        pays = [pay("R1", 1, "sales", "Kort", "1235.60")]
        v = build_verification(assemble_zrows(zs, vats, pays), Decimal("0"), {"net_25": "3001"}, self.ACC)
        accounts = {l.account for l in v.lines}
        self.assertIn("3001", accounts)
        self.assertNotIn("3011", accounts)
        self.assertEqual(v.diff, Decimal("0.00"))

    def test_multiple_zreports_sorted_by_register_and_day(self):
        zs = [z("R2", 500, 2, date(2026, 9, 4), "0"), z("R1", 1846, 1, date(2026, 9, 2), "0"),
              z("R1", 1845, 1, date(2026, 9, 1), "0")]
        rows = assemble_zrows(zs, [], [])
        self.assertEqual([(r.kassa, r.zreport_id) for r in rows],
                         [("Kassa 1", 1845), ("Kassa 1", 1846), ("Kassa 2", 500)])


if __name__ == "__main__":
    unittest.main()
