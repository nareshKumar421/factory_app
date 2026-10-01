"""``?month=YYYY-MM`` on a monthly board: none or the current month reads now,
an ended month is read as of its last day, and anything else is refused."""

from datetime import date

from django.test import SimpleTestCase

from .months import as_of_for_month

TODAY = date(2026, 10, 1)


class AsOfForMonthTests(SimpleTestCase):
    def test_no_month_reads_now(self):
        self.assertIsNone(as_of_for_month(None, TODAY))
        self.assertIsNone(as_of_for_month("", TODAY))

    def test_the_current_month_reads_now(self):
        self.assertIsNone(as_of_for_month("2026-10", TODAY))

    def test_an_ended_month_is_read_as_of_its_last_day(self):
        self.assertEqual(as_of_for_month("2026-09", TODAY), date(2026, 9, 30))
        self.assertEqual(as_of_for_month("2025-12", TODAY), date(2025, 12, 31))
        self.assertEqual(as_of_for_month("2024-02", TODAY), date(2024, 2, 29))

    def test_a_month_that_has_not_started_is_refused(self):
        with self.assertRaises(ValueError):
            as_of_for_month("2026-11", TODAY)

    def test_a_malformed_month_is_refused(self):
        for raw in ("2026-9", "2026-13", "Sep 2026", "2026-09-01"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                as_of_for_month(raw, TODAY)
