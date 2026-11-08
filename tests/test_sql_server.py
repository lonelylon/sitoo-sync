"""Integrationstester mot en riktig SQL Server.

Körs bara när SQLTEST_SERVER är satt, till exempel i GitHub Actions där SQL Server 2019 startas
som tjänst. Testar schemat och de SQL-frågor som rapportsidorna använder, med syntetisk data.
"""
from __future__ import annotations

import os
import unittest
from datetime import date
from decimal import Decimal

SERVER = os.environ.get("SQLTEST_SERVER")


@unittest.skipUnless(SERVER, "SQLTEST_SERVER är inte satt")
class SqlServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sitoosync.db import Database
        cls.db = Database(SERVER, int(os.environ.get("SQLTEST_PORT", "1433")), "SitooSyncTest",
                          os.environ.get("SQLTEST_USER", "sa"), os.environ["SQLTEST_PASSWORD"])
        cls.db.wait_until_available(attempts=30, delay=5)
        cls.db.ensure_database()
        cls.db.apply_schema()
        cls.db.apply_schema()          # schemat ska gå att köra flera gånger

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def reports(self):
        from sitoosync.reports import Reports
        return Reports(self.db)

    def test_worldline_upload_both_report_types(self):
        from sitoosync.worldline import file_hash, parse_report
        from test_worldline import monthly_file, payout_file
        r = self.reports()
        for i, (name, data) in enumerate((("payout_report_20260902.xlsx", payout_file()),
                                          ("monthly_report_202609.xlsx", monthly_file()),
                                          ("payout_report_20260902.xlsx", payout_file()))):  # samma igen: inga dubbletter
            rep = parse_report(data)
            digest = file_hash(data + bytes([i]))
            new, total = r.wl_save(name, rep, digest)
            self.assertEqual(total, len(rep.transactions))
            self.assertEqual(r.wl_file_by_hash(digest)["FileName"], name)
        rows = self.db.query("SELECT TransactionRef, Typ, Amount, Fee, PayoutRef, Status FROM sitoo.WorldlineTransactions "
                             "ORDER BY TransactionRef")
        by_ref = {x["TransactionRef"]: x for x in rows}
        self.assertEqual(set(by_ref), {"1001", "1002", "1003", "1004"})
        self.assertEqual(by_ref["1001"]["Fee"], Decimal("-1.00"))
        self.assertEqual(by_ref["1001"]["Status"], "Cleared")
        self.assertIsNone(by_ref["1003"]["Fee"])
        self.assertEqual(by_ref["1004"]["Amount"], Decimal("-20.00"))
        files = r.wl_files()
        self.assertEqual(len(files), 3)
        self.assertEqual(files[-1]["NewRows"], 2)            # första filen: två nya
        payouts = r.wl_payouts(date(2026, 9, 1), date(2026, 9, 30), [])
        self.assertEqual([(p["PayoutRef"], p["Amount"]) for p in payouts], [("B1", Decimal("248.00"))])
        daily = r.wl_daily(date(2026, 9, 1), date(2026, 9, 30), ["Butiken"])
        self.assertEqual(daily[date(2026, 9, 1)]["Belopp"], Decimal("350.50"))
        self.assertEqual(daily[date(2026, 9, 30)]["UtanAvgift"], 2)
        self.assertEqual(r.wl_daily(date(2026, 9, 1), date(2026, 9, 30), ["Annan"]), {})
        self.assertEqual(r.wl_other_costs(date(2026, 9, 1), date(2026, 9, 30)), Decimal("99.00"))
        self.assertEqual(r.wl_merchants(), ["Butiken"])
        self.assertEqual(r.first_data_year(), 2026)

    def test_report_queries_run(self):
        """Frågorna bakom tidrapport och bokföring ska fungera även på en tom databas."""
        r = self.reports()
        start, end = date(2026, 9, 1), date(2026, 9, 30)
        r.zrows(start, end)
        r.deposits(start, end)
        r.stamps(start, end, "91000")
        r.manual_stamps(start, end)
        r.all_manual_stamps()
        r.removed_stamps()
        r.staff_names()
        r.payment_names()


if __name__ == "__main__":
    unittest.main()
