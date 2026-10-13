"""Voucher date rules and the bunch drift comparison (pure; no database).

The dates are shown only on the cash -> SAP screens; ``cash_book`` validation
is unchanged on this branch. Drift is what replaces the bunch lock the spec
wanted in ``cash_book``.
"""

from datetime import date

from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError

from cash_sap import drift
from cash_sap.voucher_dates import check_voucher_dates, voucher_date_warnings

TODAY = date(2026, 10, 9)


def codes(warnings):
    return [w["code"] for w in warnings]


class VoucherDateTests(SimpleTestCase):
    def test_future_dates_are_refused_with_their_field(self):
        with self.assertRaises(ValidationError) as caught:
            check_voucher_dates(date(2026, 10, 10), today=TODAY)
        self.assertIn("entry_date", caught.exception.detail)
        with self.assertRaises(ValidationError) as caught:
            check_voucher_dates(date(2026, 10, 1), date(2026, 10, 12), today=TODAY)
        self.assertIn("bill_date", caught.exception.detail)

    def test_a_clean_recent_voucher_has_no_warning(self):
        self.assertEqual(voucher_date_warnings(date(2026, 10, 1), today=TODAY), [])

    def test_old_dates_warn_only_with_age_on(self):
        self.assertEqual(codes(voucher_date_warnings(date(2026, 7, 20), today=TODAY)), ["DATE_OLD"])
        self.assertEqual(voucher_date_warnings(date(2026, 7, 20), today=TODAY, include_age=False), [])

    def test_bill_after_entry_and_other_month(self):
        got = codes(voucher_date_warnings(date(2026, 9, 30), date(2026, 10, 2), today=TODAY))
        self.assertIn("BILL_AFTER_ENTRY", got)
        self.assertIn("BILL_OTHER_MONTH", got)

    def test_looks_swapped(self):
        # 09/05 read as 9 May is months old; 5 September is recent.
        got = voucher_date_warnings(date(2026, 5, 9), today=TODAY, include_age=False)
        self.assertEqual(codes(got), ["LOOKS_SWAPPED"])
        self.assertIn("05 Sep 2026", got[0]["message"])


class DriftTests(SimpleTestCase):
    SNAP = {
        "1": {"amount": "350.00", "gl": "5630004", "branch": 1, "entry_date": "2026-09-03"},
        "2": {"amount": "150.00", "gl": "5680015", "branch": 1, "entry_date": "2026-09-04"},
    }

    def test_same_members_no_drift(self):
        self.assertEqual(drift.compare(self.SNAP, dict(self.SNAP)), {})

    def test_removed_added_and_changed(self):
        now = {
            "1": dict(self.SNAP["1"], amount="360.00"),
            "3": {"amount": "10.00", "gl": "5690002", "branch": 1, "entry_date": "2026-09-05"},
        }
        got = drift.compare(self.SNAP, now)
        self.assertEqual(got["removed"], [2])
        self.assertEqual(got["added"], [3])
        self.assertEqual(got["changed"], {"1": {"amount": ["350.00", "360.00"]}})
        self.assertIn("Bunch 17 changed", drift.describe(got, 17))
