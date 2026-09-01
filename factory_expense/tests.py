"""
factory_expense/tests.py

The arithmetic the wall board depends on, and the rules that decide which rate
a headcount is priced at.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase

from accounts.models import Department
from company.models import Company
from labour_gate.models import LabourGateEntry
from person_gatein.models import Contractor

from .constants import RateShift
from .models import (
    DepartmentSalaryConfig,
    LabourRateConfig,
    MonthlyBudget,
    month_start,
)
from .services import build_board, labour_costs, resolve_rate, salary_costs


class RateResolutionTests(TestCase):
    """A headcount is priced by the most specific rate in force on its date."""

    def setUp(self):
        self.company = Company.objects.create(name="Oil", code="JIVO_OIL")
        self.packing = Department.objects.create(name="Packing")
        self.refinery = Department.objects.create(name="Refinery")

    def _rate(self, amount, *, department=None, shift=RateShift.ANY, effective_from="2026-01-01"):
        return LabourRateConfig.objects.create(
            company=self.company,
            department=department,
            shift=shift,
            rate_per_person_per_day=Decimal(amount),
            effective_from=date.fromisoformat(effective_from),
        )

    def test_company_wide_rate_applies_when_nothing_more_specific(self):
        self._rate("500")
        rates = list(LabourRateConfig.objects.all())
        resolved = resolve_rate(rates, self.packing.id, "DAY", date(2026, 6, 1))
        self.assertEqual(resolved.rate_per_person_per_day, Decimal("500"))

    def test_department_rate_beats_company_wide(self):
        self._rate("500")
        self._rate("650", department=self.packing)
        rates = list(LabourRateConfig.objects.all())
        self.assertEqual(
            resolve_rate(rates, self.packing.id, "DAY", date(2026, 6, 1)).rate_per_person_per_day,
            Decimal("650"),
        )
        # A different department still falls back to the company-wide row.
        self.assertEqual(
            resolve_rate(rates, self.refinery.id, "DAY", date(2026, 6, 1)).rate_per_person_per_day,
            Decimal("500"),
        )

    def test_shift_rate_beats_any_shift(self):
        self._rate("500")
        self._rate("600", shift=RateShift.NIGHT)
        rates = list(LabourRateConfig.objects.all())
        self.assertEqual(
            resolve_rate(rates, None, "NIGHT", date(2026, 6, 1)).rate_per_person_per_day,
            Decimal("600"),
        )
        self.assertEqual(
            resolve_rate(rates, None, "DAY", date(2026, 6, 1)).rate_per_person_per_day,
            Decimal("500"),
        )

    def test_a_later_rate_does_not_reprice_earlier_days(self):
        self._rate("500", effective_from="2026-01-01")
        self._rate("700", effective_from="2026-06-01")
        rates = list(LabourRateConfig.objects.all())
        self.assertEqual(
            resolve_rate(rates, None, "DAY", date(2026, 5, 31)).rate_per_person_per_day,
            Decimal("500"),
        )
        self.assertEqual(
            resolve_rate(rates, None, "DAY", date(2026, 6, 1)).rate_per_person_per_day,
            Decimal("700"),
        )

    def test_no_rate_configured_returns_none_rather_than_zero(self):
        self.assertIsNone(resolve_rate([], None, "DAY", date(2026, 6, 1)))


class LabourCostTests(TestCase):
    """Gate headcount times rate, and the warning when a rate is missing."""

    def setUp(self):
        self.company = Company.objects.create(name="Oil", code="JIVO_OIL")
        self.packing = Department.objects.create(name="Packing")
        self.contractor = Contractor.objects.create(contractor_name="Sharma Labour")
        self.day = date(2026, 6, 15)

    def _entry(self, count, *, department=None, shift="DAY"):
        return LabourGateEntry.objects.create(
            company=self.company,
            department=department,
            contractor=self.contractor,
            work_date=self.day,
            shift=shift,
            count_in=count,
        )

    def test_headcount_is_priced_at_the_configured_rate(self):
        LabourRateConfig.objects.create(
            company=self.company,
            rate_per_person_per_day=Decimal("550"),
            effective_from=date(2026, 1, 1),
        )
        self._entry(40, department=self.packing)
        per_date, departments, contractors, unpriced = labour_costs(self.company, [self.day])

        self.assertEqual(per_date[self.day]["headcount"], 40)
        self.assertEqual(per_date[self.day]["cost"], Decimal("22000.00"))
        self.assertEqual(unpriced, 0)
        self.assertEqual(departments["Packing"]["headcount"], 40)
        self.assertEqual(contractors["Sharma Labour"]["cost"], Decimal("22000.00"))

    def test_unpriced_headcount_is_counted_not_silently_zeroed(self):
        self._entry(25)
        per_date, _, _, unpriced = labour_costs(self.company, [self.day])
        self.assertEqual(unpriced, 25)
        self.assertEqual(per_date[self.day]["headcount"], 25)
        self.assertEqual(per_date[self.day]["cost"], Decimal("0.00"))

    def test_a_soft_deleted_entry_is_not_charged(self):
        LabourRateConfig.objects.create(
            company=self.company,
            rate_per_person_per_day=Decimal("500"),
            effective_from=date(2026, 1, 1),
        )
        entry = self._entry(10)
        entry.is_active = False
        entry.save(update_fields=["is_active"])
        per_date, _, _, _ = labour_costs(self.company, [self.day])
        self.assertEqual(per_date[self.day]["headcount"], 0)

    def test_day_and_night_are_priced_separately(self):
        LabourRateConfig.objects.create(
            company=self.company,
            shift=RateShift.DAY,
            rate_per_person_per_day=Decimal("500"),
            effective_from=date(2026, 1, 1),
        )
        LabourRateConfig.objects.create(
            company=self.company,
            shift=RateShift.NIGHT,
            rate_per_person_per_day=Decimal("700"),
            effective_from=date(2026, 1, 1),
        )
        self._entry(10, shift="DAY")
        self._entry(10, shift="NIGHT")
        per_date, _, _, _ = labour_costs(self.company, [self.day])
        self.assertEqual(per_date[self.day]["cost"], Decimal("12000.00"))


class SalaryTests(TestCase):
    """A monthly figure spread evenly across the month's days."""

    def setUp(self):
        self.company = Company.objects.create(name="Oil", code="JIVO_OIL")
        self.packing = Department.objects.create(name="Packing")
        self.admin = Department.objects.create(name="Admin")

    def test_monthly_amount_accrues_daily(self):
        DepartmentSalaryConfig.objects.create(
            company=self.company,
            department=self.packing,
            month=date(2026, 6, 1),
            employee_count=20,
            monthly_amount=Decimal("600000"),
        )
        result = salary_costs(self.company, date(2026, 6, 10))
        self.assertEqual(result["days_in_month"], 30)
        self.assertEqual(result["daily"], Decimal("20000.00"))
        self.assertEqual(result["mtd"], Decimal("200000.00"))
        self.assertEqual(result["departments"][0]["per_employee"], Decimal("30000.00"))

    def test_month_is_normalised_to_the_first(self):
        row = DepartmentSalaryConfig.objects.create(
            company=self.company,
            department=self.admin,
            month=date(2026, 6, 17),
            monthly_amount=Decimal("100000"),
        )
        row.refresh_from_db()
        self.assertEqual(row.month, date(2026, 6, 1))

    def test_no_configuration_reports_itself(self):
        result = salary_costs(self.company, date(2026, 6, 10))
        self.assertFalse(result["configured"])
        self.assertEqual(result["monthly"], Decimal("0.00"))

    def test_zero_headcount_hides_cost_per_employee(self):
        DepartmentSalaryConfig.objects.create(
            company=self.company,
            department=self.admin,
            month=date(2026, 6, 1),
            employee_count=0,
            monthly_amount=Decimal("50000"),
        )
        result = salary_costs(self.company, date(2026, 6, 10))
        self.assertIsNone(result["departments"][0]["per_employee"])


class BoardTests(TestCase):
    """The assembled payload."""

    def setUp(self):
        self.company = Company.objects.create(name="Oil", code="JIVO_OIL")
        self.packing = Department.objects.create(name="Packing")
        self.contractor = Contractor.objects.create(contractor_name="Sharma Labour")
        self.day = date(2026, 6, 15)
        LabourRateConfig.objects.create(
            company=self.company,
            rate_per_person_per_day=Decimal("500"),
            effective_from=date(2026, 1, 1),
        )
        DepartmentSalaryConfig.objects.create(
            company=self.company,
            department=self.packing,
            month=date(2026, 6, 1),
            employee_count=10,
            monthly_amount=Decimal("300000"),
        )

    def test_board_totals_the_four_buckets(self):
        LabourGateEntry.objects.create(
            company=self.company,
            department=self.packing,
            contractor=self.contractor,
            work_date=self.day,
            shift="DAY",
            count_in=30,
        )
        board = build_board(self.company, self.day)

        self.assertEqual(board["buckets"]["LABOUR"]["today"], Decimal("15000.00"))
        self.assertEqual(board["buckets"]["SALARY"]["today"], Decimal("10000.00"))
        self.assertEqual(board["total"]["today"], Decimal("25000.00"))
        self.assertEqual(board["buckets"]["LABOUR"]["unit"], 30)

    def test_trend_covers_a_fortnight_and_ends_today(self):
        board = build_board(self.company, self.day)
        self.assertEqual(len(board["trend"]), 14)
        self.assertTrue(board["trend"][-1]["is_today"])
        self.assertEqual(board["trend"][-1]["date"], self.day.isoformat())

    def test_month_to_date_reaches_back_past_the_trend_window(self):
        """On the 28th the month is wider than the fortnight — MTD must still be whole."""
        late = date(2026, 6, 28)
        for offset in range(28):
            LabourGateEntry.objects.create(
                company=self.company,
                department=self.packing,
                contractor=self.contractor,
                work_date=date(2026, 6, 1) + timedelta(days=offset),
                shift="DAY",
                count_in=10,
            )
        board = build_board(self.company, late)
        # 28 days x 10 labourers x Rs 500 = Rs 140,000
        self.assertEqual(board["buckets"]["LABOUR"]["mtd"], Decimal("140000.00"))

    def test_budget_drives_the_variance_chip(self):
        MonthlyBudget.objects.create(
            company=self.company,
            bucket="SALARY",
            month=date(2026, 6, 1),
            amount=Decimal("300000"),
        )
        board = build_board(self.company, self.day)
        # 15 days of a Rs 300,000 month = Rs 150,000, half the budget.
        self.assertEqual(board["buckets"]["SALARY"]["mtd"], Decimal("150000.00"))
        self.assertEqual(board["buckets"]["SALARY"]["budget_used_pct"], 50.0)

    def test_a_bucket_with_no_budget_reports_no_percentage(self):
        board = build_board(self.company, self.day)
        self.assertIsNone(board["buckets"]["LABOUR"]["budget_used_pct"])

    def test_missing_configuration_surfaces_as_a_warning(self):
        LabourGateEntry.objects.create(
            company=self.company,
            contractor=self.contractor,
            work_date=self.day,
            shift="DAY",
            count_in=5,
        )
        LabourRateConfig.objects.all().update(is_active=False)
        board = build_board(self.company, self.day)
        self.assertTrue(any("no rate configured" in text for text in board["warnings"]))

    def test_settings_row_is_created_on_first_read(self):
        board = build_board(self.company, self.day)
        self.assertEqual(board["settings"]["refresh_seconds"], 60)


class MonthHelperTests(TestCase):
    def test_month_start_normalises(self):
        self.assertEqual(month_start(date(2026, 6, 17)), date(2026, 6, 1))
