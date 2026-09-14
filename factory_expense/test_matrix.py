"""
factory_expense/test_matrix.py

The company × bucket matrix, and the two splits that make it different from
running the wall board once per company.

The tests that matter most here are the ones that would pass if the matrix were
naively three boards added together — a shared meter billed twice, a
factory-wide salary blanket billed three times — so each has an explicit
assertion on the shared row rather than only on the total.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase

from accounts.models import Department
from company.models import Company
from cost_master.models import CostRate, CostType
from labour_gate.models import LabourGateEntry
from maintenance.models import DailyElectricityReading, ElectricityMeter
from person_gatein.models import Contractor

from .constants import LABOUR_COST_TYPE_CODE, SALARY_COST_TYPE_CODE
from .matrix import SHARED_ROW_KEY, build_matrix
from .services import build_board, get_settings

DAY = date(2026, 9, 10)


class MatrixFixture(TestCase):
    """Three companies, the two cost types, and a gate contractor."""

    def setUp(self):
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.mart = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        self.bev = Company.objects.create(name="Jivo Beverages", code="JIVO_BEVERAGES")
        self.companies = [self.bev, self.mart, self.oil]

        self.labour_type = CostType.objects.create(
            code=LABOUR_COST_TYPE_CODE,
            name="Factory — Contract Labour",
            default_basis="PER_PERSON_DAY",
        )
        self.salary_type = CostType.objects.create(
            code=SALARY_COST_TYPE_CODE,
            name="Factory — Salary",
            default_basis="PER_MONTH",
        )
        self.contractor = Contractor.objects.create(contractor_name="Sharma Labour")
        self.packing = Department.objects.create(name="Packing")

    # -- builders ---------------------------------------------------------

    def meter(self, name, *companies, rate="7.00"):
        row = ElectricityMeter.objects.create(name=name, rate_per_unit=Decimal(rate))
        if companies:
            row.companies.set(companies)
        return row

    def reading(self, meter, units, *, on=DAY):
        return DailyElectricityReading.objects.create(
            meter=meter,
            date=on,
            opening_reading=Decimal("0"),
            closing_reading=Decimal(units),
            multiplying_factor=Decimal("1"),
            units_consumed=Decimal(units),
            rate_per_unit=meter.rate_per_unit,
            total_cost=Decimal(units) * meter.rate_per_unit,
        )

    def gate(self, company, heads, *, on=DAY, department=None, contractor=None):
        """One register row.

        ``department=None`` is a gate row — the contractor turned up with N
        people. A department makes it an HOD's allocation OF those same people,
        which is why the tests below always create the gate row first.
        """
        return LabourGateEntry.objects.create(
            company=company,
            contractor=contractor or self.contractor,
            work_date=on,
            count_in=heads,
            department=department,
        )

    def dept(self, name):
        return Department.objects.create(name=name)

    def rate(self, cost_type, amount, *, scope="COMPANY", company=None, basis="PER_PERSON_DAY"):
        return CostRate.objects.create(
            cost_type=cost_type,
            scope=scope,
            company=company,
            basis=basis,
            rate=Decimal(amount),
            effective_from=date(2026, 1, 1),
        )

    # -- helpers ----------------------------------------------------------

    def cell(self, matrix, row_key, column):
        row = next(item for item in matrix["rows"] if item["key"] == row_key)
        return row["cells"][column]

    def amount(self, matrix, row_key, column):
        return Decimal(self.cell(matrix, row_key, column)["amount"])


class MeterMappingTests(MatrixFixture):
    """A meter belongs to whichever company the MAPPING says, not its tagging.

    ``ElectricityMeter.companies`` says six campus meters feed two companies at
    once, which is true of the supply and useless for costing — it leaves 69% of
    the bill unattributable. ``ELECTRICITY_METER_COMPANY`` is the factory's own
    answer, and it wins.
    """

    def test_the_mapping_beats_the_meters_own_tagging(self):
        # Tagged to Beverages, mapped to Oil. The mapping must win.
        self.reading(self.meter("Production Floor OIL", self.bev), 100)

        matrix = build_matrix(self.companies, DAY)

        self.assertEqual(self.amount(matrix, "JIVO_OIL", "ELECTRICITY"), Decimal("700.00"))
        self.assertEqual(self.amount(matrix, "JIVO_BEVERAGES", "ELECTRICITY"), Decimal("0.00"))

    def test_a_meter_with_no_tagging_at_all_still_maps(self):
        self.reading(self.meter("Terrace"), 200)

        matrix = build_matrix(self.companies, DAY)

        self.assertEqual(self.amount(matrix, "JIVO_BEVERAGES", "ELECTRICITY"), Decimal("1400.00"))

    def test_the_main_incomers_go_to_the_shared_row(self):
        """User's choice (2026-09-14): the mains are counted, in Shared.

        They belong to no single plant, and they are the supply the sub-meters
        are a breakdown OF — so the column knowingly counts the same electricity
        about three times. The warning below is what keeps that from passing as
        a bill.
        """
        self.reading(self.meter("Production Floor OIL", self.oil), 100)
        self.reading(self.meter("KWH", self.oil, self.bev), 900)
        self.reading(self.meter("KVAH", self.oil, self.bev), 940)
        self.reading(self.meter("LP-196", self.oil, self.bev), 60)

        matrix = build_matrix(self.companies, DAY)

        self.assertEqual(self.amount(matrix, "JIVO_OIL", "ELECTRICITY"), Decimal("700.00"))
        self.assertEqual(
            self.amount(matrix, SHARED_ROW_KEY, "ELECTRICITY"), Decimal("13300.00")
        )
        self.assertEqual(
            Decimal(matrix["total"]["cells"]["ELECTRICITY"]["amount"]), Decimal("14000.00")
        )

    def test_the_overlap_is_stated_rather_than_left_to_be_noticed(self):
        self.reading(self.meter("Production Floor OIL", self.oil), 100)
        self.reading(self.meter("KWH", self.oil, self.bev), 900)

        matrix = build_matrix(self.companies, DAY)

        self.assertTrue(
            any("three times the metered bill" in text for text in matrix["warnings"]),
            matrix["warnings"],
        )

    def test_the_mains_are_named_on_the_shared_row(self):
        self.reading(self.meter("KWH", self.oil), 900)
        self.reading(self.meter("KVAH", self.oil), 940)

        matrix = build_matrix(self.companies, DAY)

        note = self.cell(matrix, SHARED_ROW_KEY, "ELECTRICITY")["note"] or ""
        self.assertIn("KWH", note)
        self.assertIn("KVAH", note)

    def test_a_campus_meter_goes_to_the_shared_row(self):
        self.reading(self.meter("STP", self.oil, self.bev), 100)
        self.reading(self.meter("Lab", self.oil, self.bev), 50)

        matrix = build_matrix(self.companies, DAY)

        self.assertEqual(self.amount(matrix, SHARED_ROW_KEY, "ELECTRICITY"), Decimal("1050.00"))
        self.assertEqual(self.amount(matrix, "JIVO_OIL", "ELECTRICITY"), Decimal("0.00"))

    def test_an_unmapped_meter_is_shared_and_named(self):
        self.reading(self.meter("Spare Feeder", self.oil), 50)

        matrix = build_matrix(self.companies, DAY)

        self.assertEqual(self.amount(matrix, SHARED_ROW_KEY, "ELECTRICITY"), Decimal("350.00"))
        self.assertTrue(
            any("Spare Feeder" in text for text in matrix["warnings"]), matrix["warnings"]
        )

    def test_a_campus_meter_is_not_reported_as_unmapped(self):
        """STP is a decision, not an oversight, so it must not be warned about."""
        self.reading(self.meter("STP", self.oil), 10)

        matrix = build_matrix(self.companies, DAY)

        self.assertFalse(any("STP" in text for text in matrix["warnings"]), matrix["warnings"])

    def test_meters_the_mapping_is_still_waiting_for_are_named(self):
        matrix = build_matrix(self.companies, DAY)

        waiting = [text for text in matrix["warnings"] if "does not exist" in text or "do not exist" in text]
        self.assertTrue(waiting, matrix["warnings"])
        for expected in ("Basement", "LB", "Admin", "TR 60"):
            self.assertIn(expected, waiting[0])

    def test_ro_meter_and_tr_60_are_different_meters(self):
        """Both map to Beverages, but one exists and the other is awaited."""
        self.reading(self.meter("Ro meter", self.bev), 100)

        matrix = build_matrix(self.companies, DAY)

        self.assertEqual(self.amount(matrix, "JIVO_BEVERAGES", "ELECTRICITY"), Decimal("700.00"))
        self.assertTrue(any("TR 60" in text for text in matrix["warnings"]))


class ReconciliationTests(MatrixFixture):
    """The sum of the parts, against the meter the bill is struck on."""

    def test_the_sub_meters_are_reconciled_to_the_incomer(self):
        self.reading(self.meter("Production Floor OIL", self.oil), 500)
        self.reading(self.meter("Terrace", self.bev), 500)
        self.reading(self.meter("KWH", self.oil, self.bev), 1000)

        matrix = build_matrix(self.companies, DAY)
        check = matrix["electricity_reconciliation"]

        self.assertEqual(check["meter"], "KWH")
        self.assertEqual(Decimal(check["cost"]), Decimal("7000.00"))
        self.assertEqual(check["drift_pct"], 0.0)

    def test_the_check_measures_sub_meters_not_the_column_total(self):
        """With the mains inside the column, comparing the column to the incomer
        would compare a number to a part of itself — always +100% drift."""
        self.reading(self.meter("Production Floor OIL", self.oil), 1000)
        self.reading(self.meter("KWH", self.oil, self.bev), 1000)

        matrix = build_matrix(self.companies, DAY)
        check = matrix["electricity_reconciliation"]

        # Column is 14,000 (sub-meters + mains); the check sees only the 7,000.
        self.assertEqual(
            Decimal(matrix["total"]["cells"]["ELECTRICITY"]["amount"]), Decimal("14000.00")
        )
        self.assertEqual(Decimal(check["sub_meter_cost"]), Decimal("7000.00"))
        self.assertEqual(check["drift_pct"], 0.0)

    def test_drift_is_reported_when_the_parts_do_not_add_up(self):
        self.reading(self.meter("Production Floor OIL", self.oil), 800)
        self.reading(self.meter("KWH", self.oil, self.bev), 1000)
        # 800 sub-metered units against a 1,000-unit incomer.

        matrix = build_matrix(self.companies, DAY)

        # 800 of 1000 units accounted for: a fifth of the supply is unread.
        self.assertEqual(matrix["electricity_reconciliation"]["drift_pct"], -20.0)

    def test_no_incomer_reading_is_not_reported_as_no_drift(self):
        self.reading(self.meter("Production Floor OIL", self.oil), 100)

        matrix = build_matrix(self.companies, DAY)

        self.assertIsNone(matrix["electricity_reconciliation"])

    def test_the_excluded_meters_are_named(self):
        self.reading(self.meter("KWH", self.oil), 100)
        self.reading(self.meter("KVAH", self.oil), 104)

        matrix = build_matrix(self.companies, DAY)

        self.assertEqual(
            matrix["electricity_reconciliation"]["excluded_meters"], ["KVAH", "KWH"]
        )


class ElectricityWarningTests(MatrixFixture):
    """An empty square says which desk can fill it."""

    def test_company_with_no_meter_says_so(self):
        matrix = build_matrix(self.companies, DAY)

        self.assertEqual(
            self.cell(matrix, "JIVO_MART", "ELECTRICITY")["warning"],
            "No meter is mapped to this company",
        )

    def test_company_with_meters_but_no_reading_says_so_differently(self):
        self.meter("Production Floor OIL", self.oil)
        self.meter("HP-196", self.oil)

        matrix = build_matrix(self.companies, DAY)

        self.assertEqual(
            self.cell(matrix, "JIVO_OIL", "ELECTRICITY")["warning"],
            "No reading entered for its 2 meters",
        )

    def test_a_main_meter_does_not_count_as_the_companys_meter(self):
        """KWH exists and is tagged to Oil, but Oil still has nothing to show."""
        self.meter("KWH", self.oil)

        matrix = build_matrix(self.companies, DAY)

        self.assertEqual(
            self.cell(matrix, "JIVO_OIL", "ELECTRICITY")["warning"],
            "No meter is mapped to this company",
        )

    def test_a_read_meter_carries_no_warning(self):
        self.reading(self.meter("Production Floor OIL", self.oil), 100)

        matrix = build_matrix(self.companies, DAY)

        self.assertIsNone(self.cell(matrix, "JIVO_OIL", "ELECTRICITY")["warning"])
        self.assertEqual(self.cell(matrix, "JIVO_OIL", "ELECTRICITY")["unit"], "100.00")


class SalaryOwnershipTests(MatrixFixture):
    """A rate that names no company is the factory's, not everybody's."""

    def test_factory_wide_blanket_is_counted_once(self):
        # 30 days in September, so a factory-wide ₹3,00,000/month accrues
        # ₹10,000 on any one day — once, not once per company.
        self.rate(self.salary_type, "300000", scope="FACTORY", basis="PER_MONTH")

        matrix = build_matrix(self.companies, DAY)

        self.assertEqual(self.amount(matrix, "JIVO_OIL", "SALARY"), Decimal("0.00"))
        self.assertEqual(self.amount(matrix, SHARED_ROW_KEY, "SALARY"), Decimal("10000.00"))
        self.assertEqual(
            Decimal(matrix["total"]["cells"]["SALARY"]["amount"]), Decimal("10000.00")
        )

    def test_company_rate_lands_on_that_company(self):
        self.rate(self.salary_type, "300000", company=self.oil, basis="PER_MONTH")

        matrix = build_matrix(self.companies, DAY)

        self.assertEqual(self.amount(matrix, "JIVO_OIL", "SALARY"), Decimal("10000.00"))
        self.assertEqual(self.amount(matrix, "JIVO_BEVERAGES", "SALARY"), Decimal("0.00"))
        self.assertEqual(self.amount(matrix, SHARED_ROW_KEY, "SALARY"), Decimal("0.00"))

    def test_no_rate_at_all_warns_rather_than_reading_zero(self):
        matrix = build_matrix(self.companies, DAY)

        self.assertEqual(self.cell(matrix, "JIVO_OIL", "SALARY")["warning"], "No rate set")
        self.assertTrue(
            any("factory-salary" in text for text in matrix["warnings"]),
            matrix["warnings"],
        )

    def test_a_span_accrues_day_by_day(self):
        self.rate(self.salary_type, "300000", company=self.oil, basis="PER_MONTH")

        matrix = build_matrix(self.companies, DAY - timedelta(days=4), DAY)

        self.assertEqual(matrix["days"], 5)
        self.assertEqual(self.amount(matrix, "JIVO_OIL", "SALARY"), Decimal("50000.00"))


class LabourTests(MatrixFixture):
    """Labour is owned by its DEPARTMENT, never by the entry's company field.

    Two traps, both silent. The register keeps the gate's head count and an
    HOD's department split of those same people in rows of identical shape, so
    summing it reports the allocated people twice. And every department row on
    the live database is tagged ``JIVO_OIL`` regardless of which company the
    department serves, so the company foreign key cannot attribute anything.
    """

    def test_the_gate_row_and_its_allocation_are_not_added_together(self):
        self.rate(self.labour_type, "600", company=self.oil)
        self.gate(self.oil, 10)                                           # 10 walked in
        self.gate(self.oil, 10, department=self.dept("production(oil)"))  # the same 10

        matrix = build_matrix(self.companies, DAY)

        # 10 people, not 20. Adding the rows would bill Rs 12,000 for Rs 6,000
        # of labour — the live register read 1,308 man-days against 827 real.
        self.assertEqual(self.cell(matrix, "JIVO_OIL", "LABOUR")["unit"], 10)
        self.assertEqual(self.amount(matrix, "JIVO_OIL", "LABOUR"), Decimal("6000.00"))
        self.assertEqual(self.amount(matrix, SHARED_ROW_KEY, "LABOUR"), Decimal("0.00"))

    def test_a_department_decides_the_company_not_the_entry(self):
        """The whole point: every row here is tagged Oil, and one is not Oil's."""
        self.rate(self.labour_type, "600", company=self.oil)
        self.rate(self.labour_type, "400", company=self.bev)
        self.gate(self.oil, 30)
        self.gate(self.oil, 10, department=self.dept("production(oil)"))
        self.gate(self.oil, 10, department=self.dept("Warehouse Beverage"))

        matrix = build_matrix(self.companies, DAY)

        self.assertEqual(self.amount(matrix, "JIVO_OIL", "LABOUR"), Decimal("6000.00"))
        self.assertEqual(self.amount(matrix, "JIVO_BEVERAGES", "LABOUR"), Decimal("4000.00"))

    def test_the_gupta_godown_labour_is_oils_not_marts(self):
        """The stock in the Gupta godown is Mart's; the labour working it is not.

        The user corrected this on 2026-09-14, and it is the kind of thing that
        looks like a bug later: the department name says Gupta, the warehouse is
        Mart's, and the cost still belongs to Oil.
        """
        self.rate(self.labour_type, "600", company=self.oil)
        self.rate(self.labour_type, "500", company=self.mart)
        self.gate(self.oil, 10)
        self.gate(self.oil, 10, department=self.dept("Warehouse Gupta"))

        matrix = build_matrix(self.companies, DAY)

        self.assertEqual(self.amount(matrix, "JIVO_OIL", "LABOUR"), Decimal("6000.00"))
        self.assertEqual(self.amount(matrix, "JIVO_MART", "LABOUR"), Decimal("0.00"))

    def test_mart_has_no_labour_department_at_all(self):
        """Every mapped department belongs to Oil or Beverages.

        Asserted rather than assumed: if somebody later maps a department to
        Mart, this test says so, and Mart's row stops being structurally empty.
        """
        from .constants import LABOUR_DEPARTMENT_COMPANY

        self.assertNotIn("JIVO_MART", set(LABOUR_DEPARTMENT_COMPANY.values()))

    def test_department_names_match_whatever_the_case_and_spacing(self):
        self.rate(self.labour_type, "600", company=self.oil)
        self.gate(self.oil, 8)
        self.gate(self.oil, 8, department=self.dept("  PRODUCTION (Oil)  "))

        matrix = build_matrix(self.companies, DAY)

        self.assertEqual(self.amount(matrix, "JIVO_OIL", "LABOUR"), Decimal("4800.00"))

    def test_dock_labour_is_oils(self):
        """Reassigned from shared to Oil outright by the user (2026-09-14)."""
        self.rate(self.labour_type, "600", company=self.oil)
        self.gate(self.oil, 12)
        self.gate(self.oil, 12, department=self.dept("Dock"))

        matrix = build_matrix(self.companies, DAY)

        self.assertEqual(self.amount(matrix, "JIVO_OIL", "LABOUR"), Decimal("7200.00"))
        self.assertEqual(self.amount(matrix, SHARED_ROW_KEY, "LABOUR"), Decimal("0.00"))

    def test_a_campus_department_goes_to_the_shared_row(self):
        """Mess feeds whoever is on site, so it belongs in no company column."""
        self.rate(self.labour_type, "600", scope="FACTORY")
        self.gate(self.oil, 12)
        self.gate(self.oil, 12, department=self.dept("Mess"))

        matrix = build_matrix(self.companies, DAY)

        self.assertEqual(self.amount(matrix, "JIVO_OIL", "LABOUR"), Decimal("0.00"))
        self.assertEqual(self.amount(matrix, SHARED_ROW_KEY, "LABOUR"), Decimal("7200.00"))
        self.assertIn("Mess", self.cell(matrix, SHARED_ROW_KEY, "LABOUR")["note"])

    def test_an_unmapped_department_goes_to_the_shared_row_and_is_named(self):
        self.rate(self.labour_type, "600", scope="FACTORY")
        self.gate(self.oil, 5)
        self.gate(self.oil, 5, department=self.dept("store RM-PM"))

        matrix = build_matrix(self.companies, DAY)

        self.assertEqual(self.amount(matrix, SHARED_ROW_KEY, "LABOUR"), Decimal("3000.00"))
        self.assertIn("store RM-PM", self.cell(matrix, SHARED_ROW_KEY, "LABOUR")["note"])

    def test_labour_no_department_claimed_goes_to_the_shared_row(self):
        """346 of September's 827 man-days are in exactly this state."""
        self.rate(self.labour_type, "600", scope="FACTORY")
        self.gate(self.oil, 20)
        self.gate(self.oil, 8, department=self.dept("production(oil)"))

        matrix = build_matrix(self.companies, DAY)

        self.assertEqual(self.cell(matrix, "JIVO_OIL", "LABOUR")["unit"], 8)
        # The other 12 walked in and nobody has claimed them.
        self.assertEqual(self.cell(matrix, SHARED_ROW_KEY, "LABOUR")["unit"], 12)
        self.assertIn("unallocated", self.cell(matrix, SHARED_ROW_KEY, "LABOUR")["note"])

    def test_shared_labour_needs_a_rate_set_for_no_particular_company(self):
        """A company rate cannot price labour that belongs to no company."""
        self.rate(self.labour_type, "600", company=self.oil)
        self.gate(self.oil, 9)

        matrix = build_matrix(self.companies, DAY)

        cell = self.cell(matrix, SHARED_ROW_KEY, "LABOUR")
        self.assertEqual(cell["unit"], 9)
        self.assertEqual(cell["warning"], "No factory-wide labour rate set")

    def test_over_allocation_is_scaled_back_onto_the_gate_count(self):
        """An HOD splitting more people than walked in is an error, not labour."""
        self.rate(self.labour_type, "600", company=self.oil)
        self.gate(self.oil, 10)
        self.gate(self.oil, 20, department=self.dept("production(oil)"))

        matrix = build_matrix(self.companies, DAY)

        self.assertEqual(self.cell(matrix, "JIVO_OIL", "LABOUR")["unit"], 10)
        self.assertTrue(
            any("more labour to departments" in text for text in matrix["warnings"]),
            matrix["warnings"],
        )

    def test_each_contractor_is_reconciled_against_their_own_gate_count(self):
        other = Contractor.objects.create(contractor_name="Imran")
        self.rate(self.labour_type, "600", company=self.oil)
        self.gate(self.oil, 10)
        self.gate(self.oil, 6, contractor=other)
        self.gate(self.oil, 10, department=self.dept("production(oil)"))

        matrix = build_matrix(self.companies, DAY)

        # The first contractor is fully allocated; the second's 6 are not.
        self.assertEqual(self.cell(matrix, "JIVO_OIL", "LABOUR")["unit"], 10)
        self.assertEqual(self.cell(matrix, SHARED_ROW_KEY, "LABOUR")["unit"], 6)

    def test_unpriced_heads_are_counted_but_warned_about(self):
        self.gate(self.oil, 7)
        self.gate(self.oil, 7, department=self.dept("production(oil)"))

        matrix = build_matrix(self.companies, DAY)

        self.assertEqual(self.amount(matrix, "JIVO_OIL", "LABOUR"), Decimal("0.00"))
        self.assertEqual(self.cell(matrix, "JIVO_OIL", "LABOUR")["unit"], 7)
        self.assertTrue(any("7 man-days" in text for text in matrix["warnings"]))


class RowLabelTests(MatrixFixture):
    """The board names its own rows, without renaming a company."""

    def test_the_mart_row_reads_as_water(self):
        matrix = build_matrix(self.companies, DAY)

        row = next(item for item in matrix["rows"] if item["key"] == "JIVO_MART")
        self.assertEqual(row["label"], "Water")

    def test_the_row_still_keys_on_the_company_code(self):
        """The key is what everything else joins on, so it must not move.

        Ordering on the page, the permission scope and any future water meter
        mapping all address this row as JIVO_MART; only the words on screen say
        Water.
        """
        matrix = build_matrix(self.companies, DAY)

        keys = [item["key"] for item in matrix["rows"]]
        self.assertIn("JIVO_MART", keys)
        self.assertNotIn("WATER", keys)

    def test_the_company_record_is_untouched(self):
        """Renaming the Company would move the name across the whole product."""
        build_matrix(self.companies, DAY)

        self.mart.refresh_from_db()
        self.assertEqual(self.mart.name, "Jivo Mart")

    def test_a_company_with_no_override_keeps_its_own_name(self):
        matrix = build_matrix(self.companies, DAY)

        labels = {item["key"]: item["label"] for item in matrix["rows"]}
        self.assertEqual(labels["JIVO_OIL"], "Jivo Oil")
        self.assertEqual(labels["JIVO_BEVERAGES"], "Jivo Beverages")


class ShapeTests(MatrixFixture):
    """The grid adds up, and every company keeps its row."""

    def test_a_company_with_nothing_still_has_a_row(self):
        matrix = build_matrix(self.companies, DAY)

        keys = [row["key"] for row in matrix["rows"]]
        self.assertEqual(keys, ["JIVO_BEVERAGES", "JIVO_MART", "JIVO_OIL", SHARED_ROW_KEY])

    def test_row_totals_and_column_totals_agree(self):
        self.rate(self.labour_type, "600", company=self.oil)
        self.gate(self.oil, 10)
        self.gate(self.oil, 10, department=self.dept("production(oil)"))
        self.reading(self.meter("Production Floor OIL", self.oil), 100)
        self.reading(self.meter("STP", self.oil, self.bev), 1000)

        matrix = build_matrix(self.companies, DAY)

        down_the_rows = sum(
            (Decimal(row["total"]) for row in matrix["rows"]), Decimal("0")
        )
        across_the_columns = sum(
            (Decimal(cell["amount"]) for cell in matrix["total"]["cells"].values()),
            Decimal("0"),
        )
        self.assertEqual(down_the_rows, across_the_columns)
        self.assertEqual(Decimal(matrix["total"]["total"]), down_the_rows)

    def test_electricity_matches_the_wall_board_for_the_same_span(self):
        """The two screens must never disagree about the power bill.

        They compute it in different shapes — the board sums every reading once,
        the matrix splits them by ownership first.

        Labour is deliberately NOT compared. The wall board adds the gate count
        and the HOD's allocation of those same people, so it reports the
        allocated ones twice; the matrix does not. The two are expected to
        differ by exactly that double-count until the board is fixed.
        """
        self.rate(self.labour_type, "600", company=self.oil)
        self.gate(self.oil, 10)
        self.gate(self.oil, 10, department=self.dept("production(oil)"))
        self.reading(self.meter("Production Floor OIL", self.oil), 100)
        self.reading(self.meter("Boiler", self.bev), 60)
        self.reading(self.meter("KWH", self.oil, self.bev), 1000)

        matrix = build_matrix(self.companies, DAY)
        board = build_board(self.companies, DAY)

        # With the mains counted in the shared row, both screens read the same
        # set of meters again, so the power bill ties exactly. This is the
        # assertion that catches a split which loses or duplicates a meter.
        self.assertEqual(
            Decimal(matrix["total"]["cells"]["ELECTRICITY"]["amount"]),
            Decimal(board["buckets"]["ELECTRICITY"]["today"]),
        )
        # And the labour gap is exactly the double-count, not a lost row.
        self.assertEqual(
            Decimal(board["buckets"]["LABOUR"]["today"])
            - Decimal(matrix["total"]["cells"]["LABOUR"]["amount"]),
            Decimal("6000.00"),
        )

    def test_a_backwards_range_is_swapped_rather_than_rejected(self):
        matrix = build_matrix(self.companies, DAY, DAY - timedelta(days=3))

        self.assertEqual(matrix["date_from"], DAY - timedelta(days=3))
        self.assertEqual(matrix["date_to"], DAY)
        self.assertEqual(matrix["days"], 4)

    def test_no_companies_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            build_matrix([], DAY)
