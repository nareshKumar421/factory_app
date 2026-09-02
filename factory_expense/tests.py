"""
factory_expense/tests.py

The arithmetic the wall board depends on, and the rules that decide which Cost
Master row prices a head count.

Rates live in ``cost_master``; this module only reads them. The tests below
therefore build ``CostRate`` rows directly, which is also the closest thing to
what an admin does in Admin › Cost Master.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase

from accounts.models import Department
from company.models import Company
from cost_master.models import CostRate, CostType
from labour_gate.models import LabourGateEntry
from person_gatein.models import Contractor

from .constants import LABOUR_COST_TYPE_CODE, SALARY_COST_TYPE_CODE
from .models import MonthlyBudget, month_start
from .rates import load_rates, monthly_amounts_by_department, resolve
from .services import build_board, labour_costs, salary_costs


class CostMasterFixture(TestCase):
    """Shared setup: the two cost types plus a company and two departments."""

    def setUp(self):
        self.company = Company.objects.create(name="Oil", code="JIVO_OIL")
        self.other = Company.objects.create(name="Bev", code="JIVO_BEVERAGES")
        self.packing = Department.objects.create(name="Packing")
        self.refinery = Department.objects.create(name="Refinery")
        self.labour_type = CostType.objects.create(
            code=LABOUR_COST_TYPE_CODE, name="Factory — Contract Labour",
            default_basis="PER_PERSON_DAY",
        )
        self.salary_type = CostType.objects.create(
            code=SALARY_COST_TYPE_CODE, name="Factory — Salary",
            default_basis="PER_MONTH",
        )

    def rate(self, cost_type, amount, *, scope="FACTORY", company=None,
             department=None, basis="PER_PERSON_DAY", effective_from="2026-01-01"):
        return CostRate.objects.create(
            cost_type=cost_type,
            scope=scope,
            company=company,
            department=department,
            basis=basis,
            rate=Decimal(amount),
            effective_from=date.fromisoformat(effective_from),
        )


class RateResolutionTests(CostMasterFixture):
    """Most specific Cost Master row wins, and history is never repriced."""

    def _resolve(self, department_id, on_date=date(2026, 6, 1)):
        return resolve(load_rates(LABOUR_COST_TYPE_CODE, self.company, on_date),
                       department_id, on_date)

    def test_factory_rate_applies_when_nothing_more_specific(self):
        self.rate(self.labour_type, "500")
        self.assertEqual(self._resolve(self.packing.id).rate, Decimal("500.0000"))

    def test_company_rate_beats_factory_wide(self):
        self.rate(self.labour_type, "500")
        self.rate(self.labour_type, "560", scope="COMPANY", company=self.company)
        self.assertEqual(self._resolve(self.packing.id).rate, Decimal("560.0000"))

    def test_department_rate_beats_company(self):
        self.rate(self.labour_type, "500")
        self.rate(self.labour_type, "560", scope="COMPANY", company=self.company)
        self.rate(self.labour_type, "650", scope="DEPARTMENT", department=self.packing)
        self.assertEqual(self._resolve(self.packing.id).rate, Decimal("650.0000"))
        # A department with no rate of its own still falls back.
        self.assertEqual(self._resolve(self.refinery.id).rate, Decimal("560.0000"))

    def test_company_specific_department_rate_beats_company_agnostic_one(self):
        self.rate(self.labour_type, "600", scope="DEPARTMENT", department=self.packing)
        self.rate(self.labour_type, "700", scope="DEPARTMENT",
                  department=self.packing, company=self.company)
        self.assertEqual(self._resolve(self.packing.id).rate, Decimal("700.0000"))

    def test_another_companys_rate_is_never_used(self):
        self.rate(self.labour_type, "900", scope="COMPANY", company=self.other)
        self.assertIsNone(self._resolve(self.packing.id))

    def test_a_later_rate_does_not_reprice_earlier_days(self):
        self.rate(self.labour_type, "500", effective_from="2026-01-01")
        self.rate(self.labour_type, "700", effective_from="2026-06-01")
        self.assertEqual(
            self._resolve(None, date(2026, 5, 31)).rate, Decimal("500.0000")
        )
        self.assertEqual(
            self._resolve(None, date(2026, 6, 1)).rate, Decimal("700.0000")
        )

    def test_value_scoped_rows_are_ignored(self):
        """VALUE rows are keyed to something this board cannot identify."""
        CostRate.objects.create(
            cost_type=self.labour_type, scope="VALUE", value_key="machine:BM-01",
            basis="PER_PERSON_DAY", rate=Decimal("999"),
            effective_from=date(2026, 1, 1),
        )
        self.assertIsNone(self._resolve(self.packing.id))

    def test_no_cost_type_at_all_resolves_to_none(self):
        CostType.objects.filter(code=LABOUR_COST_TYPE_CODE).delete()
        self.assertEqual(load_rates(LABOUR_COST_TYPE_CODE, self.company, date(2026, 6, 1)), [])


class LabourCostTests(CostMasterFixture):
    """Gate headcount times the Cost Master rate."""

    def setUp(self):
        super().setUp()
        self.contractor = Contractor.objects.create(contractor_name="Sharma Labour")
        self.day = date(2026, 6, 15)

    def _entry(self, count, *, department=None, shift="DAY"):
        return LabourGateEntry.objects.create(
            company=self.company, department=department, contractor=self.contractor,
            work_date=self.day, shift=shift, count_in=count,
        )

    def test_headcount_is_priced_at_the_cost_master_rate(self):
        self.rate(self.labour_type, "550")
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
        self.rate(self.labour_type, "500")
        entry = self._entry(10)
        entry.is_active = False
        entry.save(update_fields=["is_active"])
        per_date, _, _, _ = labour_costs(self.company, [self.day])
        self.assertEqual(per_date[self.day]["headcount"], 0)

    def test_departments_are_priced_by_their_own_rate(self):
        self.rate(self.labour_type, "500")
        self.rate(self.labour_type, "800", scope="DEPARTMENT", department=self.packing)
        self._entry(10, department=self.packing)
        self._entry(10, department=self.refinery)
        per_date, _, _, _ = labour_costs(self.company, [self.day])
        # 10 x 800 + 10 x 500
        self.assertEqual(per_date[self.day]["cost"], Decimal("13000.00"))

    def test_a_retired_cost_rate_stops_pricing(self):
        rate = self.rate(self.labour_type, "500")
        self._entry(10)
        rate.is_active = False
        rate.save(update_fields=["is_active"])
        per_date, _, _, unpriced = labour_costs(self.company, [self.day])
        self.assertEqual(per_date[self.day]["cost"], Decimal("0.00"))
        self.assertEqual(unpriced, 10)


class SalaryTests(CostMasterFixture):
    """PER_MONTH Cost Master rates, spread across the month's days."""

    def _salary(self, amount, **kwargs):
        kwargs.setdefault("basis", "PER_MONTH")
        return self.rate(self.salary_type, amount, **kwargs)

    def test_department_rates_accrue_daily(self):
        self._salary("600000", scope="DEPARTMENT", department=self.packing)
        result = salary_costs(self.company, date(2026, 6, 10))
        self.assertEqual(result["days_in_month"], 30)
        self.assertEqual(result["daily"], Decimal("20000.00"))
        self.assertEqual(result["mtd"], Decimal("200000.00"))
        self.assertEqual(result["departments"][0]["department"], "Packing")

    def test_several_departments_sum(self):
        self._salary("300000", scope="DEPARTMENT", department=self.packing)
        self._salary("300000", scope="DEPARTMENT", department=self.refinery)
        result = salary_costs(self.company, date(2026, 6, 10))
        self.assertEqual(result["monthly"], Decimal("600000.00"))
        self.assertEqual(len(result["departments"]), 2)

    def test_a_company_blanket_is_not_added_on_top_of_department_rows(self):
        """Otherwise the same salary bill would be counted twice."""
        self._salary("900000", scope="COMPANY", company=self.company)
        self._salary("300000", scope="DEPARTMENT", department=self.packing)
        result = salary_costs(self.company, date(2026, 6, 10))
        self.assertEqual(result["monthly"], Decimal("300000.00"))
        self.assertEqual(len(result["departments"]), 1)

    def test_a_company_blanket_is_used_when_no_department_rows_exist(self):
        self._salary("900000", scope="COMPANY", company=self.company)
        result = salary_costs(self.company, date(2026, 6, 10))
        self.assertEqual(result["monthly"], Decimal("900000.00"))
        self.assertEqual(result["departments"][0]["department"], "All departments")

    def test_no_rate_reports_itself(self):
        result = salary_costs(self.company, date(2026, 6, 10))
        self.assertFalse(result["configured"])
        self.assertEqual(result["monthly"], Decimal("0.00"))

    def test_a_later_salary_rate_does_not_reprice_last_month(self):
        self._salary("300000", scope="DEPARTMENT", department=self.packing,
                     effective_from="2026-01-01")
        self._salary("400000", scope="DEPARTMENT", department=self.packing,
                     effective_from="2026-06-01")
        may = salary_costs(self.company, date(2026, 5, 20))
        june = salary_costs(self.company, date(2026, 6, 20))
        self.assertEqual(may["monthly"], Decimal("300000.00"))
        self.assertEqual(june["monthly"], Decimal("400000.00"))


class MonthlyAmountHelperTests(CostMasterFixture):
    def test_department_rows_sort_by_name(self):
        self.rate(self.salary_type, "1", scope="DEPARTMENT",
                  department=self.refinery, basis="PER_MONTH")
        self.rate(self.salary_type, "2", scope="DEPARTMENT",
                  department=self.packing, basis="PER_MONTH")
        rows = monthly_amounts_by_department(
            load_rates(SALARY_COST_TYPE_CODE, self.company, date(2026, 6, 1)),
            date(2026, 6, 1),
        )
        self.assertEqual([row[1] for row in rows], ["Packing", "Refinery"])


class BoardTests(CostMasterFixture):
    """The assembled payload."""

    def setUp(self):
        super().setUp()
        self.contractor = Contractor.objects.create(contractor_name="Sharma Labour")
        self.day = date(2026, 6, 15)
        self.rate(self.labour_type, "500")
        self.rate(self.salary_type, "300000", scope="DEPARTMENT",
                  department=self.packing, basis="PER_MONTH")

    def test_board_totals_the_four_buckets(self):
        LabourGateEntry.objects.create(
            company=self.company, department=self.packing, contractor=self.contractor,
            work_date=self.day, shift="DAY", count_in=30,
        )
        board = build_board(self.company, self.day)
        self.assertEqual(board["buckets"]["LABOUR"]["today"], Decimal("15000.00"))
        self.assertEqual(board["buckets"]["SALARY"]["today"], Decimal("10000.00"))
        self.assertEqual(board["total"]["today"], Decimal("25000.00"))
        self.assertEqual(board["buckets"]["LABOUR"]["unit"], 30)

    def test_trend_covers_a_fortnight_and_ends_on_the_chosen_day(self):
        board = build_board(self.company, self.day)
        self.assertEqual(len(board["trend"]), 14)
        self.assertTrue(board["trend"][-1]["is_today"])
        self.assertEqual(board["trend"][-1]["date"], self.day.isoformat())

    def test_a_single_day_is_the_default_and_says_so(self):
        board = build_board(self.company, self.day)
        self.assertEqual(board["date_from"], self.day.isoformat())
        self.assertEqual(board["date_to"], self.day.isoformat())
        self.assertEqual(board["days"], 1)
        self.assertTrue(board["is_single_day"])

    def test_month_to_date_reaches_back_past_the_trend_window(self):
        """On the 28th the month is wider than the fortnight — MTD must be whole."""
        late = date(2026, 6, 28)
        for offset in range(28):
            LabourGateEntry.objects.create(
                company=self.company, department=self.packing, contractor=self.contractor,
                work_date=date(2026, 6, 1) + timedelta(days=offset),
                shift="DAY", count_in=10,
            )
        board = build_board(self.company, late)
        # 28 days x 10 labourers x Rs 500
        self.assertEqual(board["buckets"]["LABOUR"]["mtd"], Decimal("140000.00"))

    def test_budget_drives_the_variance_chip(self):
        MonthlyBudget.objects.create(
            company=self.company, bucket="SALARY",
            month=date(2026, 6, 1), amount=Decimal("300000"),
        )
        board = build_board(self.company, self.day)
        self.assertEqual(board["buckets"]["SALARY"]["mtd"], Decimal("150000.00"))
        self.assertEqual(board["buckets"]["SALARY"]["budget_used_pct"], 50.0)

    def test_a_bucket_with_no_budget_reports_no_percentage(self):
        board = build_board(self.company, self.day)
        self.assertIsNone(board["buckets"]["LABOUR"]["budget_used_pct"])

    def test_missing_rate_surfaces_a_warning_naming_the_cost_type(self):
        LabourGateEntry.objects.create(
            company=self.company, contractor=self.contractor,
            work_date=self.day, shift="DAY", count_in=5,
        )
        CostRate.objects.filter(cost_type=self.labour_type).update(is_active=False)
        board = build_board(self.company, self.day)
        self.assertTrue(
            any(LABOUR_COST_TYPE_CODE in text for text in board["warnings"]),
            board["warnings"],
        )

    def test_settings_row_is_created_on_first_read(self):
        board = build_board(self.company, self.day)
        self.assertEqual(board["settings"]["refresh_seconds"], 60)

    def test_salary_tile_reports_department_count_not_employees(self):
        board = build_board(self.company, self.day)
        self.assertEqual(board["buckets"]["SALARY"]["unit"], 1)
        self.assertEqual(board["buckets"]["SALARY"]["unit_label"], "departments")


class MonthHelperTests(TestCase):
    def test_month_start_normalises(self):
        self.assertEqual(month_start(date(2026, 6, 17)), date(2026, 6, 1))


class DateRangeTests(CostMasterFixture):
    """The from/to filter. A single day is just a range of one."""

    def setUp(self):
        super().setUp()
        self.contractor = Contractor.objects.create(contractor_name="Sharma Labour")
        self.rate(self.labour_type, "500")
        # 10 labourers a day for the first ten days of June.
        for offset in range(10):
            LabourGateEntry.objects.create(
                company=self.company, department=self.packing, contractor=self.contractor,
                work_date=date(2026, 6, 1) + timedelta(days=offset),
                shift="DAY", count_in=10,
            )

    def test_a_range_totals_every_day_in_it(self):
        board = build_board(self.company, date(2026, 6, 1), date(2026, 6, 5))
        self.assertEqual(board["days"], 5)
        self.assertFalse(board["is_single_day"])
        # 5 days x 10 labourers x Rs 500
        self.assertEqual(board["buckets"]["LABOUR"]["today"], Decimal("25000.00"))
        self.assertEqual(board["buckets"]["LABOUR"]["unit"], 50)

    def test_a_one_day_range_matches_the_single_day_call(self):
        ranged = build_board(self.company, date(2026, 6, 3), date(2026, 6, 3))
        single = build_board(self.company, date(2026, 6, 3))
        self.assertEqual(ranged["buckets"]["LABOUR"], single["buckets"]["LABOUR"])
        self.assertEqual(ranged["total"]["today"], single["total"]["today"])

    def test_a_backwards_range_is_swapped_rather_than_rejected(self):
        forwards = build_board(self.company, date(2026, 6, 1), date(2026, 6, 5))
        backwards = build_board(self.company, date(2026, 6, 5), date(2026, 6, 1))
        self.assertEqual(backwards["date_from"], "2026-06-01")
        self.assertEqual(backwards["date_to"], "2026-06-05")
        self.assertEqual(backwards["total"]["today"], forwards["total"]["today"])

    def test_per_day_average_divides_by_the_span(self):
        board = build_board(self.company, date(2026, 6, 1), date(2026, 6, 5))
        self.assertEqual(board["total"]["per_day"], Decimal("5000.00"))

    def test_a_short_range_still_gets_a_fortnight_of_trend_context(self):
        board = build_board(self.company, date(2026, 6, 3), date(2026, 6, 5))
        self.assertEqual(len(board["trend"]), 14)
        in_range = [point for point in board["trend"] if point["in_range"]]
        self.assertEqual(len(in_range), 3)

    def test_a_long_range_draws_itself(self):
        board = build_board(self.company, date(2026, 5, 1), date(2026, 6, 10))
        self.assertEqual(len(board["trend"]), 41)
        self.assertTrue(all(point["in_range"] for point in board["trend"]))

    def test_the_trend_strip_is_capped_on_a_very_long_range(self):
        board = build_board(self.company, date(2025, 6, 1), date(2026, 6, 10))
        self.assertEqual(len(board["trend"]), 92)
        # The headline figures still cover the whole range, not just the strip.
        self.assertEqual(board["days"], 375)

    def test_breakdowns_cover_the_whole_range_not_just_the_last_day(self):
        board = build_board(self.company, date(2026, 6, 1), date(2026, 6, 5))
        packing = next(
            row for row in board["labour_departments"] if row["department"] == "Packing"
        )
        self.assertEqual(packing["headcount"], 50)
        self.assertEqual(packing["cost"], Decimal("25000.00"))

    def test_month_to_date_still_follows_the_range_end(self):
        board = build_board(self.company, date(2026, 6, 1), date(2026, 6, 5))
        # MTD is the whole of June so far: 5 days of gate entries.
        self.assertEqual(board["buckets"]["LABOUR"]["mtd"], Decimal("25000.00"))
        self.assertEqual(board["month"], "2026-06-01")

    def test_salary_accrues_once_per_day_of_the_range(self):
        self.rate(self.salary_type, "300000", scope="DEPARTMENT",
                  department=self.packing, basis="PER_MONTH")
        board = build_board(self.company, date(2026, 6, 1), date(2026, 6, 5))
        # Rs 300,000 / 30 days = Rs 10,000/day, five days of it.
        self.assertEqual(board["buckets"]["SALARY"]["today"], Decimal("50000.00"))
