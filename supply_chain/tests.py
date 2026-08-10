"""Tests for the Smart Supply Chain module.

The module's job is steps 6 and 7 of the brief — turning requirements into DATED
orders and checking the plan can actually be run — so that is what these cover,
plus the reference-data import that feeds them and the policy that settles the
brief's open questions.

Everything runs against rows in Postgres/SQLite; nothing here touches SAP or HANA.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from sales_planning_requirement.models import SalesPlanningRequirementRow

from .models import (
    AlarmDispatch,
    AlarmState,
    AlarmSubscription,
    FloorBasis,
    FloorConvention,
    SalesTrend,
    MachineCapacity,
    MaterialLeadTime,
    MaterialMachineMap,
    ReferenceImport,
    SupplyChainPolicy,
)
from .services import SupplyChainError
from .services import alarms
from .services import planning
from .services import template_import

COMPANY = "JIVO_TEST"


def plan_row(item_code, **kwargs):
    """A SalesPlanningRequirementRow as the HANA procedure would leave it."""
    defaults = dict(
        company_code=COMPANY, source_schema="TEST", item_code=item_code,
        item_name=item_code, planned_qty=0, base_required_qty=0, min_stock=0,
        stock_in_hand=0, required_qty=0, open_po_qty=0, net_shortage_qty=0,
        forecast_start_date=date(2026, 9, 1), forecast_end_date=date(2026, 9, 30),
    )
    defaults.update(kwargs)
    return SalesPlanningRequirementRow.objects.create(**defaults)


class StockFloorPolicyTests(TestCase):
    """The brief's headline rule, and the ambiguity it left behind."""

    def test_the_two_readings_of_the_35_percent_floor_differ_by_three_times(self):
        """"35% of the three-month sales trend" has two readings. Both are supported
        and the default is stated, because the difference is 3x on every SKU."""
        monthly = SupplyChainPolicy(company_code=COMPANY, floor_basis=FloorBasis.MONTHLY_AVERAGE)
        total = SupplyChainPolicy(company_code=COMPANY, floor_basis=FloorBasis.THREE_MONTH_TOTAL)
        # 3 months of sales = 3000 units.
        self.assertEqual(monthly.stock_floor(3000), Decimal("350.000000"))   # 35% of 1000/month
        self.assertEqual(total.stock_floor(3000), Decimal("1050.000000"))    # 35% of 3000
        self.assertEqual(total.stock_floor(3000), monthly.stock_floor(3000) * 3)

    def test_floor_is_zero_without_sales_and_honours_a_changed_percentage(self):
        policy = SupplyChainPolicy(company_code=COMPANY)
        self.assertEqual(policy.stock_floor(0), Decimal("0"))
        self.assertEqual(policy.stock_floor(None), Decimal("0"))
        policy.floor_percent = Decimal("50")
        self.assertEqual(policy.stock_floor(3000), Decimal("500.000000"))

    def test_a_company_with_no_policy_row_still_reads_the_brief_defaults(self):
        """Every screen must work before an admin has configured anything."""
        policy = SupplyChainPolicy.for_company("NEVER_CONFIGURED")
        self.assertIsNone(policy.pk)
        self.assertEqual(policy.floor_percent, Decimal("35.00"))
        self.assertEqual(policy.floor_basis, FloorBasis.MONTHLY_AVERAGE)


class MachineCapacityModelTests(TestCase):
    def test_capacity_deducts_changeover_the_brief_forgets(self):
        machine = MachineCapacity(
            company_code=COMPANY, machine_id="M-01", output_per_hour=1000,
            shift_hours=8, shifts_per_day=2, working_days_per_month=25,
            changeover_minutes=60,
        )
        self.assertEqual(machine.available_hours, Decimal("400"))
        # No changeover: the template's own formula, 400h x 1000/h.
        self.assertEqual(machine.effective_capacity_units(0), Decimal("400000"))
        # Four SKUs = four hours lost, which is 4000 units the plan cannot have.
        self.assertEqual(machine.effective_capacity_units(4), Decimal("396000"))
        # Turning the policy off restores the template's number exactly.
        self.assertEqual(
            machine.effective_capacity_units(4, include_changeover=False), Decimal("400000")
        )

    def test_capacity_never_goes_negative(self):
        machine = MachineCapacity(
            company_code=COMPANY, machine_id="M-X", output_per_hour=100,
            shift_hours=1, shifts_per_day=1, working_days_per_month=1,
            changeover_minutes=600,
        )
        self.assertEqual(machine.effective_capacity_units(10), Decimal("0"))


class MoqRoundingTests(TestCase):
    def test_orders_round_up_to_whole_moq_lots(self):
        """The template collects MOQ and the brief never uses it — an order for 10
        against an MOQ of 500 cannot actually be placed."""
        self.assertEqual(planning._round_to_moq(Decimal("10"), Decimal("500")), Decimal("500"))
        self.assertEqual(planning._round_to_moq(Decimal("500"), Decimal("500")), Decimal("500"))
        self.assertEqual(planning._round_to_moq(Decimal("501"), Decimal("500")), Decimal("1000"))
        # No MOQ on file — leave the quantity alone rather than inventing one.
        self.assertEqual(planning._round_to_moq(Decimal("501"), Decimal("0")), Decimal("501"))


class ProcurementAlarmTests(TestCase):
    """Step 6 — the reason the brief asks for a system rather than a report."""

    def setUp(self):
        self.today = date(2026, 8, 10)
        # The plan period starts 1 Sep — 22 days out.
        MaterialLeadTime.objects.create(
            company_code=COMPANY, material_code="PM-LONG", material_name="Long lead",
            lead_time_days=45, moq=1000, unit="Pcs", supplier_name="Slow Co",
        )
        MaterialLeadTime.objects.create(
            company_code=COMPANY, material_code="PM-TIGHT", lead_time_days=20, moq=0,
        )
        MaterialLeadTime.objects.create(
            company_code=COMPANY, material_code="PM-SHORT", lead_time_days=3, moq=0,
        )

    def _alarms(self, **kwargs):
        return planning.material_alarms(COMPANY, today=self.today, **kwargs)

    def test_a_lead_time_longer_than_the_runway_is_already_overdue(self):
        """45 days of lead time against 22 days of runway — the order needed placing
        three weeks ago. Nothing in the system says this today."""
        plan_row("PM-LONG", required_qty=5000, net_shortage_qty=5000)
        row = self._alarms()["rows"][0]
        self.assertEqual(row["alarm"], AlarmState.OVERDUE)
        self.assertEqual(row["order_by"], "2026-07-18")     # 1 Sep − 45 days
        self.assertEqual(row["days_until_order_by"], -23)
        self.assertEqual(row["order_qty"], "5000")          # already a whole MOQ lot

    def test_an_order_inside_the_urgency_window_reads_as_order_now(self):
        plan_row("PM-TIGHT", required_qty=800, net_shortage_qty=800)
        row = self._alarms()["rows"][0]
        self.assertEqual(row["alarm"], AlarmState.ORDER_NOW)
        self.assertEqual(row["order_by"], "2026-08-12")     # 1 Sep − 20 days
        self.assertEqual(row["days_until_order_by"], 2)

    def test_a_short_lead_time_can_wait_with_a_stated_date(self):
        plan_row("PM-SHORT", required_qty=100, net_shortage_qty=100)
        row = self._alarms()["rows"][0]
        self.assertEqual(row["alarm"], AlarmState.SCHEDULED)
        self.assertEqual(row["order_by"], "2026-08-29")
        self.assertEqual(row["days_until_order_by"], 19)

    def test_a_material_with_no_lead_time_is_surfaced_not_buried(self):
        """The missing reference data IS the finding — the template exists to close
        this gap, so it must be visible rather than silently sorted last."""
        plan_row("PM-UNKNOWN", required_qty=400, net_shortage_qty=400)
        result = self._alarms()
        row = result["rows"][0]
        self.assertEqual(row["alarm"], AlarmState.NO_LEAD_TIME)
        self.assertIsNone(row["order_by"])
        self.assertEqual(result["totals"]["no_lead_time"], 1)

    def test_nothing_to_order_is_covered_not_an_alarm(self):
        plan_row("PM-SHORT", required_qty=100, net_shortage_qty=0)
        result = self._alarms()
        self.assertEqual(result["rows"][0]["alarm"], AlarmState.COVERED)
        self.assertEqual(result["totals"]["order_now"], 0)

    def test_open_purchase_orders_stop_the_same_material_being_ordered_again(self):
        """Without netting open POs, every cycle re-raises an order for material that
        is already on the water."""
        plan_row("PM-SHORT", required_qty=1000, open_po_qty=1000, net_shortage_qty=0)
        self.assertEqual(self._alarms()["rows"][0]["alarm"], AlarmState.COVERED)

        policy = SupplyChainPolicy(company_code=COMPANY, use_net_of_open_po=False)
        gross = self._alarms(policy=policy)["rows"][0]
        self.assertEqual(gross["alarm"], AlarmState.SCHEDULED)
        self.assertEqual(gross["shortage_qty"], "1000")

    def test_order_quantity_rounds_up_to_the_suppliers_moq(self):
        plan_row("PM-LONG", required_qty=1200, net_shortage_qty=1200)
        row = self._alarms()["rows"][0]
        self.assertEqual(row["shortage_qty"], "1200")
        self.assertEqual(row["order_qty"], "2000")   # 2 lots of 1000
        self.assertEqual(row["supplier_name"], "Slow Co")

    def test_finished_goods_are_produced_not_purchased(self):
        """An FG on the machine map must not appear on the procurement list."""
        MaterialMachineMap.objects.create(
            company_code=COMPANY, sku_code="FG0000030", primary_machine_id="M-01",
        )
        plan_row("FG0000030", required_qty=5000, net_shortage_qty=5000)
        plan_row("PM-SHORT", required_qty=100, net_shortage_qty=100)
        codes = [r["item_code"] for r in self._alarms()["rows"]]
        self.assertEqual(codes, ["PM-SHORT"])

    def test_the_most_urgent_material_is_first(self):
        plan_row("PM-SHORT", required_qty=100, net_shortage_qty=100)   # scheduled
        plan_row("PM-LONG", required_qty=100, net_shortage_qty=100)    # overdue
        plan_row("PM-TIGHT", required_qty=100, net_shortage_qty=100)   # order now
        plan_row("PM-UNKNOWN", required_qty=100, net_shortage_qty=100)  # no lead time
        result = self._alarms()
        self.assertEqual(
            [r["alarm"] for r in result["rows"]],
            [AlarmState.OVERDUE, AlarmState.ORDER_NOW,
             AlarmState.NO_LEAD_TIME, AlarmState.SCHEDULED],
        )
        self.assertEqual(result["totals"]["overdue"], 1)
        self.assertEqual(result["totals"]["materials"], 4)


class CapacityCheckTests(TestCase):
    """Step 7 — is the plan actually runnable?"""

    def setUp(self):
        # 8h x 2 shifts x 25 days = 400 hours a month.
        MachineCapacity.objects.create(
            company_code=COMPANY, machine_id="M-01", name="PET Line 1",
            output_per_hour=1000, shift_hours=8, shifts_per_day=2,
            working_days_per_month=25, changeover_minutes=60,
        )
        MaterialMachineMap.objects.create(
            company_code=COMPANY, sku_code="FG-A", sku_name="A",
            primary_machine_id="M-01", output_on_primary=1000, alternate_machine_ids="M-04",
        )

    def test_a_plan_that_fits_is_feasible(self):
        plan_row("FG-A", required_qty=100000, net_shortage_qty=100000)  # 100 hours
        result = planning.capacity_check(COMPANY)
        line = result["machines"][0]
        self.assertEqual(line["required_hours"], "100.00")
        self.assertEqual(line["usable_hours"], "399.00")   # one changeover
        self.assertTrue(line["feasible"])
        self.assertTrue(result["totals"]["feasible"])
        self.assertEqual(line["alternates_available"], ["M-04"])

    def test_a_plan_that_does_not_fit_reports_the_shortfall_in_hours(self):
        plan_row("FG-A", required_qty=500000, net_shortage_qty=500000)  # 500 hours
        result = planning.capacity_check(COMPANY)
        line = result["machines"][0]
        self.assertFalse(line["feasible"])
        self.assertEqual(line["required_hours"], "500.00")
        self.assertEqual(line["shortfall_hours"], "101.00")
        self.assertEqual(result["totals"]["over_capacity"], 1)
        self.assertFalse(result["totals"]["feasible"])

    def test_changeover_is_what_tips_a_marginal_plan_over(self):
        """399.5 hours of work fits in 400 raw hours but not once the line has to
        change over — exactly the case the brief's step 7 would have passed."""
        plan_row("FG-A", required_qty=399500, net_shortage_qty=399500)
        self.assertFalse(planning.capacity_check(COMPANY)["machines"][0]["feasible"])

        relaxed = SupplyChainPolicy(company_code=COMPANY, include_changeover_in_capacity=False)
        self.assertTrue(
            planning.capacity_check(COMPANY, policy=relaxed)["machines"][0]["feasible"]
        )

    def test_hours_are_summed_per_sku_rate_not_by_adding_units(self):
        """Two SKUs on one line at different rates: 50000 at 1000/h and 50000 at
        500/h is 150 hours, not 100000 'units' compared against anything."""
        MaterialMachineMap.objects.create(
            company_code=COMPANY, sku_code="FG-B", primary_machine_id="M-01",
            output_on_primary=500,
        )
        plan_row("FG-A", required_qty=50000, net_shortage_qty=50000)
        plan_row("FG-B", required_qty=50000, net_shortage_qty=50000)
        line = planning.capacity_check(COMPANY)["machines"][0]
        self.assertEqual(line["required_hours"], "150.00")
        self.assertEqual(line["sku_count"], 2)
        self.assertEqual(line["usable_hours"], "398.00")   # two changeovers

    def test_an_sku_with_no_machine_is_reported_not_dropped(self):
        MaterialMachineMap.objects.create(
            company_code=COMPANY, sku_code="FG-ORPHAN", primary_machine_id="M-99",
        )
        plan_row("FG-ORPHAN", required_qty=1000, net_shortage_qty=1000)
        result = planning.capacity_check(COMPANY)
        self.assertEqual(result["totals"]["unmapped_skus"], 1)
        self.assertEqual(result["unmapped_skus"][0]["reason"], "No machine on file")
        # An unrunnable SKU means the plan as a whole is not proven feasible.
        self.assertFalse(result["totals"]["feasible"])

    def test_an_sku_already_covered_by_stock_consumes_no_capacity(self):
        plan_row("FG-A", required_qty=100000, net_shortage_qty=0)
        self.assertEqual(planning.capacity_check(COMPANY)["machines"], [])


class DashboardTests(TestCase):
    def test_headline_answers_the_two_questions_a_hod_actually_asks(self):
        MachineCapacity.objects.create(
            company_code=COMPANY, machine_id="M-01", output_per_hour=1000,
            shift_hours=8, shifts_per_day=2, working_days_per_month=25,
        )
        MaterialMachineMap.objects.create(
            company_code=COMPANY, sku_code="FG-A", primary_machine_id="M-01",
            output_on_primary=1000,
        )
        MaterialLeadTime.objects.create(
            company_code=COMPANY, material_code="PM-LONG", lead_time_days=90,
        )
        plan_row("FG-A", required_qty=500000, net_shortage_qty=500000)   # over capacity
        plan_row("PM-LONG", required_qty=10, net_shortage_qty=10)        # overdue
        plan_row("PM-NOLEAD", required_qty=10, net_shortage_qty=10)      # unknown

        result = planning.dashboard(COMPANY, today=date(2026, 8, 10))
        self.assertEqual(result["headline"]["needs_ordering_today"], 1)
        self.assertEqual(result["headline"]["missing_lead_times"], 1)
        self.assertEqual(result["headline"]["lines_over_capacity"], 1)
        self.assertFalse(result["headline"]["plan_is_feasible"])
        # The policy in force travels with the numbers it produced.
        self.assertEqual(result["policy"]["floor_percent"], "35.00")
        self.assertEqual(result["company_code"], COMPANY)


class TemplateImportTests(TestCase):
    """The reference template — including the trap it sets."""

    LEAD_ROWS = [
        ["1.  LEAD TIMES  —  Procurement"],
        ["Fill one row per purchased material."],
        ["Material Code", "Material Name", "Material Type", "Category / Spec",
         "Supplier Name", "Lead Time (Days)", "MOQ", "Unit", "Remarks"],
        ["PM-CAP-26", "PET Cap 26 GM", "Packaging", "Cap",
         "e.g. ABC Caps Pvt Ltd", "21", "50000", "Pcs", "example"],
        ["PM-REAL", "Real Cap", "Packaging", "Cap",
         "Actual Supplier Ltd", "14", "1000", "Pcs", ""],
        ["RM-OIL-MUS", "Mustard Oil", "Oil / Raw Material", "Bulk Oil",
         "Farm Co-op Ltd", "45", "20", "Tons", ""],
    ]
    MACHINE_ROWS = [
        ["2.  MACHINE CAPACITIES"],
        ["One row per filling machine/line."],
        ["Machine ID", "Machine / Line Name", "Location", "Pack Type Handled",
         "Pack Size Range", "Output (Units/Hour)", "Shift Hours", "Shifts/Day",
         "Working Days/Month", "Effective Monthly Capacity (Units)", "Changeover (Min)"],
        ["M-01", "PET Line 1", "Plant A", "PET Bottle", "500 ML – 1 LTR",
         "6000", "8", "2", "26", "", "45"],
    ]
    MAP_ROWS = [
        ["3.  MATERIAL-TO-MACHINE MAPPING"],
        ["Which SKU runs on which machine."],
        ["SKU Code", "Finished Good / SKU Name", "Brand", "Pack Type", "Pack Size",
         "Primary Machine ID", "Alternate Machine ID(s)", "Output on Primary (Units/Hr)"],
        ["FG0000030", "MUSTARD KACHI GHANI 1 LTR 20 PCS", "JIVO", "PET 26GM",
         "1 LTR", "M-01", "M-04, M-02", "5800"],
        ["FG-BAD", "Unknown line SKU", "JIVO", "PET", "1 LTR", "M-77", "", "100"],
    ]

    def _workbook(self):
        return {
            "README": [["JIVO SUPPLY CHAIN — REFERENCE DATA COLLECTION"]],
            "1. Lead Times": self.LEAD_ROWS,
            "2. Machine Capacities": self.MACHINE_ROWS,
            "3. Material-Machine Map": self.MAP_ROWS,
        }

    def _import(self):
        with mock.patch.object(template_import, "_sheets", return_value=self._workbook()):
            return template_import.import_reference_workbook(
                COMPANY, b"", filename="template.xlsx"
            )

    def test_example_rows_are_never_loaded_as_data(self):
        """Grey italic example rows carry real-looking codes and fictional suppliers.
        Loading them seeds the reference set with suppliers that do not exist."""
        record = self._import()
        self.assertEqual(record.examples_skipped, 1)
        self.assertFalse(
            MaterialLeadTime.objects.filter(company_code=COMPANY, material_code="PM-CAP-26").exists()
        )
        self.assertEqual(record.lead_times_loaded, 2)

    def test_each_sheet_lands_with_its_own_columns(self):
        self._import()
        oil = MaterialLeadTime.objects.get(company_code=COMPANY, material_code="RM-OIL-MUS")
        self.assertEqual(oil.material_type, "RAW")
        self.assertEqual(oil.lead_time_days, 45)
        self.assertEqual(oil.moq, Decimal("20"))

        machine = MachineCapacity.objects.get(company_code=COMPANY, machine_id="M-01")
        self.assertEqual(machine.output_per_hour, Decimal("6000"))
        self.assertEqual(machine.available_hours, Decimal("416"))   # 8 x 2 x 26

        sku = MaterialMachineMap.objects.get(company_code=COMPANY, sku_code="FG0000030")
        self.assertEqual(sku.primary_machine_id, "M-01")
        self.assertEqual(sku.alternates, ["M-04", "M-02"])

    def test_a_sku_pointing_at_an_unknown_machine_warns_but_still_loads(self):
        """The three sheets are owned by different departments and arrive apart, so a
        cross-sheet mismatch must not reject the upload."""
        record = self._import()
        self.assertTrue(any("M-77" in w for w in record.warnings))
        self.assertTrue(
            MaterialMachineMap.objects.filter(company_code=COMPANY, sku_code="FG-BAD").exists()
        )

    def test_reimporting_updates_rather_than_duplicates(self):
        self._import()
        self._import()
        self.assertEqual(MaterialLeadTime.objects.filter(company_code=COMPANY).count(), 2)
        self.assertEqual(ReferenceImport.objects.filter(company_code=COMPANY).count(), 2)

    def test_a_workbook_that_is_not_the_template_is_rejected_clearly(self):
        with mock.patch.object(template_import, "_sheets", return_value={"Sheet1": [["a"]]}):
            with self.assertRaises(SupplyChainError) as ctx:
                template_import.parse_workbook(b"")
        self.assertEqual(ctx.exception.code, "NOT_THE_TEMPLATE")

    def test_one_missing_sheet_warns_and_loads_the_others(self):
        book = self._workbook()
        del book["2. Machine Capacities"]
        with mock.patch.object(template_import, "_sheets", return_value=book):
            record = template_import.import_reference_workbook(COMPANY, b"")
        self.assertEqual(record.machines_loaded, 0)
        self.assertEqual(record.lead_times_loaded, 2)
        self.assertTrue(any("machines" in w for w in record.warnings))

    def test_blank_rows_carrying_a_filled_down_formula_are_not_data(self):
        """Found against the real workbook: Machine Capacities fills "Effective Monthly
        Capacity" down as a formula, so ~20 empty rows carry a cached value and read as
        populated. Parsing the real file returned 24 machines where there are 4."""
        book = self._workbook()
        book["2. Machine Capacities"] = self.MACHINE_ROWS + [
            # No Machine ID — only the formula column holds anything.
            ["", "", "", "", "", "", "", "", "", "0", ""] for _ in range(20)
        ]
        with mock.patch.object(template_import, "_sheets", return_value=book):
            record = template_import.import_reference_workbook(COMPANY, b"")
        self.assertEqual(record.machines_loaded, 1)
        self.assertEqual(MachineCapacity.objects.filter(company_code=COMPANY).count(), 1)
        # And no twenty "row has no Machine ID" warnings drowning the real ones.
        self.assertFalse([w for w in record.warnings if "no Machine ID" in w])

    def test_sheet_names_match_with_or_without_the_template_numbering(self):
        self.assertEqual(template_import._sheet_key("1. Lead Times"), "lead_times")
        self.assertEqual(template_import._sheet_key("Lead Times"), "lead_times")
        self.assertEqual(template_import._sheet_key("3. Material-Machine Map"), "mappings")
        self.assertIsNone(template_import._sheet_key("README"))

    def test_an_unreadable_number_warns_instead_of_failing_the_upload(self):
        book = self._workbook()
        book["1. Lead Times"] = self.LEAD_ROWS[:3] + [
            ["PM-ODD", "Odd", "Packaging", "Cap", "Supplier", "two weeks", "", "Pcs", ""],
        ]
        with mock.patch.object(template_import, "_sheets", return_value=book):
            record = template_import.import_reference_workbook(COMPANY, b"")
        self.assertEqual(record.lead_times_loaded, 1)
        self.assertEqual(
            MaterialLeadTime.objects.get(company_code=COMPANY, material_code="PM-ODD").lead_time_days,
            0,
        )
        self.assertTrue(any("two weeks" in w for w in record.warnings))


class SeedCommandTests(TestCase):
    def test_seeding_produces_a_demonstrable_dashboard(self):
        """The brief's next step is a working dashboard demonstrated for review, and
        that has to be possible before any department returns their sheet."""
        call_command("seed_supply_chain_demo", "--company", COMPANY, "--start-in-days", "20")
        result = planning.dashboard(COMPANY)

        self.assertEqual(MachineCapacity.objects.filter(company_code=COMPANY).count(), 4)
        self.assertEqual(MaterialMachineMap.objects.filter(company_code=COMPANY).count(), 4)
        self.assertEqual(MaterialLeadTime.objects.filter(company_code=COMPANY).count(), 5)

        # Real alarms, not an empty shell.
        self.assertGreater(result["headline"]["needs_ordering_today"], 0)
        # The seed deliberately includes one material with no lead time on file.
        self.assertEqual(result["headline"]["missing_lead_times"], 1)
        self.assertGreater(len(result["production"]["machines"]), 0)
        # The seeded rows are internally consistent, so the convention audit can
        # actually reach a verdict rather than shrugging at its own demo data.
        self.assertEqual(result["floor_convention"]["verdict"], FloorConvention.ADDITIVE)
        self.assertEqual(result["floor_convention"]["totals"]["subtractive"], 0)

    def test_seeding_twice_needs_force(self):
        call_command("seed_supply_chain_demo", "--company", COMPANY)
        call_command("seed_supply_chain_demo", "--company", COMPANY)   # refuses, no crash
        self.assertEqual(MachineCapacity.objects.filter(company_code=COMPANY).count(), 4)
        call_command("seed_supply_chain_demo", "--company", COMPANY, "--force")
        self.assertEqual(MachineCapacity.objects.filter(company_code=COMPANY).count(), 4)


class CompanyIsolationTests(TestCase):
    def test_one_companys_reference_data_never_leaks_into_anothers_alarms(self):
        MaterialLeadTime.objects.create(
            company_code="OTHER_CO", material_code="PM-X", lead_time_days=5,
        )
        plan_row("PM-X", required_qty=100, net_shortage_qty=100)   # belongs to COMPANY
        row = planning.material_alarms(COMPANY, today=date(2026, 8, 10))["rows"][0]
        # The other company's lead time must not be used to date this order.
        self.assertEqual(row["alarm"], AlarmState.NO_LEAD_TIME)

    def test_alarms_can_be_scoped_to_one_forecast(self):
        MaterialLeadTime.objects.create(
            company_code=COMPANY, material_code="PM-A", lead_time_days=5,
        )
        plan_row("PM-A", required_qty=100, net_shortage_qty=100, forecast_id=1)
        plan_row("PM-A", required_qty=200, net_shortage_qty=200, forecast_id=2)
        rows = planning.material_alarms(COMPANY, forecast_id=2, today=date(2026, 8, 10))["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["shortage_qty"], "200")


class FloorEnforcementTests(TestCase):
    """The brief's 35% rule, actually applied rather than assumed."""

    def setUp(self):
        MaterialLeadTime.objects.create(
            company_code=COMPANY, material_code="PM-A", lead_time_days=10, moq=0,
        )

    def _policy(self, **kwargs):
        return SupplyChainPolicy(company_code=COMPANY, **kwargs)

    def test_procedure_mode_passes_the_erp_numbers_through_untouched(self):
        """The default must not move a single number — it is the pre-existing
        behaviour, and a silent shift would be indistinguishable from a bug."""
        plan_row("PM-A", base_required_qty=1000, min_stock=200, stock_in_hand=300,
                 required_qty=900, net_shortage_qty=900)
        SalesTrend.objects.create(company_code=COMPANY, item_code="PM-A", three_month_qty=3000)
        rows = planning.material_alarms(
            COMPANY, today=date(2026, 8, 10), policy=self._policy(floor_source="PROCEDURE"),
        )["rows"]
        self.assertEqual(rows[0]["required_qty"], "900")
        self.assertEqual(rows[0]["min_stock"], "200")
        self.assertEqual(rows[0]["floor_source"], "PROCEDURE")

    def test_policy_mode_recomputes_the_requirement_from_the_brief_s_floor(self):
        """3000 sold over 3 months -> 1000/month -> a 350 floor at 35%. The
        requirement becomes demand + 350 - stock, not demand + the ERP's 200."""
        plan_row("PM-A", base_required_qty=1000, min_stock=200, stock_in_hand=300,
                 required_qty=900, net_shortage_qty=900)
        SalesTrend.objects.create(company_code=COMPANY, item_code="PM-A", three_month_qty=3000)
        rows = planning.material_alarms(
            COMPANY, today=date(2026, 8, 10), policy=self._policy(floor_source="POLICY"),
        )["rows"]
        self.assertEqual(rows[0]["min_stock"], "350")
        self.assertEqual(rows[0]["required_qty"], "1050")   # 1000 + 350 - 300
        self.assertEqual(rows[0]["floor_source"], "POLICY")

    def test_policy_mode_leaves_items_with_no_trend_on_the_erp_numbers(self):
        """Turning the policy on must change nothing for items it cannot recompute."""
        plan_row("PM-A", base_required_qty=1000, min_stock=200, stock_in_hand=300,
                 required_qty=900, net_shortage_qty=900)
        rows = planning.material_alarms(
            COMPANY, today=date(2026, 8, 10), policy=self._policy(floor_source="POLICY"),
        )["rows"]
        self.assertEqual(rows[0]["required_qty"], "900")
        self.assertEqual(rows[0]["floor_source"], "PROCEDURE")

    def test_the_floor_basis_choice_still_moves_the_requirement_by_three_times(self):
        plan_row("PM-A", base_required_qty=1000, min_stock=0, stock_in_hand=0,
                 required_qty=1000, net_shortage_qty=1000)
        SalesTrend.objects.create(company_code=COMPANY, item_code="PM-A", three_month_qty=3000)
        monthly = planning.material_alarms(COMPANY, today=date(2026, 8, 10), policy=self._policy(
            floor_source="POLICY", floor_basis=FloorBasis.MONTHLY_AVERAGE))["rows"][0]
        total = planning.material_alarms(COMPANY, today=date(2026, 8, 10), policy=self._policy(
            floor_source="POLICY", floor_basis=FloorBasis.THREE_MONTH_TOTAL))["rows"][0]
        self.assertEqual(monthly["required_qty"], "1350")   # 1000 + 350
        self.assertEqual(total["required_qty"], "2050")     # 1000 + 1050

    def test_recomputed_shortage_nets_off_open_purchase_orders(self):
        plan_row("PM-A", base_required_qty=1000, min_stock=0, stock_in_hand=0,
                 required_qty=1000, open_po_qty=800, net_shortage_qty=200)
        SalesTrend.objects.create(company_code=COMPANY, item_code="PM-A", three_month_qty=3000)
        row = planning.material_alarms(COMPANY, today=date(2026, 8, 10),
                                       policy=self._policy(floor_source="POLICY"))["rows"][0]
        self.assertEqual(row["required_qty"], "1350")
        self.assertEqual(row["shortage_qty"], "550")   # 1350 - 800

    def test_floor_audit_names_the_items_whose_buffer_has_eroded(self):
        """"Buffers erode unnoticed" is one of the five problems the brief names."""
        plan_row("PM-A", min_stock=50, required_qty=10)      # policy says 350
        plan_row("PM-B", min_stock=350, required_qty=10)     # matches
        plan_row("PM-C", required_qty=10)                    # no trend on file
        for code in ("PM-A", "PM-B"):
            SalesTrend.objects.create(company_code=COMPANY, item_code=code, three_month_qty=3000)

        audit = planning.floor_audit(COMPANY, policy=self._policy())
        self.assertEqual(audit["totals"]["compared"], 2)
        self.assertEqual(audit["totals"]["divergent"], 1)
        self.assertEqual(audit["totals"]["no_trend_on_file"], 1)
        worst = audit["rows"][0]
        self.assertEqual(worst["item_code"], "PM-A")
        self.assertEqual(worst["policy_floor"], "350")
        self.assertEqual(worst["procedure_min_stock"], "50")
        self.assertEqual(worst["difference"], "-300")
        self.assertFalse(worst["matches_policy"])


class FloorConventionAuditTests(TestCase):
    """Settling the brief's own contradiction with evidence."""

    def test_additive_data_is_recognised_as_step_3_s_reading(self):
        # required = demand + floor - stock = 1000 + 200 - 300
        plan_row("X", base_required_qty=1000, min_stock=200, stock_in_hand=300,
                 required_qty=900)
        audit = planning.floor_convention_audit(COMPANY)
        self.assertEqual(audit["verdict"], FloorConvention.ADDITIVE)
        self.assertEqual(audit["totals"]["additive"], 1)
        self.assertEqual(audit["rows"][0]["if_additive"], "900")
        self.assertEqual(audit["rows"][0]["if_subtractive"], "500")

    def test_subtractive_data_is_recognised_as_step_5_s_reading(self):
        # required = demand - floor - stock = 1000 - 200 - 300
        plan_row("X", base_required_qty=1000, min_stock=200, stock_in_hand=300,
                 required_qty=500)
        audit = planning.floor_convention_audit(COMPANY)
        self.assertEqual(audit["verdict"], FloorConvention.SUBTRACTIVE)

    def test_a_zero_floor_cannot_tell_the_two_readings_apart(self):
        """Without this guard the audit would claim a verdict from data that
        carries no information — the two formulas agree when the floor is 0."""
        plan_row("X", base_required_qty=1000, min_stock=0, stock_in_hand=300,
                 required_qty=700)
        audit = planning.floor_convention_audit(COMPANY)
        self.assertEqual(audit["verdict"], FloorConvention.INDETERMINATE)
        self.assertEqual(audit["totals"]["indeterminate"], 1)

    def test_numbers_matching_neither_reading_are_not_forced_into_one(self):
        plan_row("X", base_required_qty=1000, min_stock=200, stock_in_hand=300,
                 required_qty=4242)
        self.assertEqual(
            planning.floor_convention_audit(COMPANY)["verdict"], FloorConvention.INDETERMINATE
        )

    def test_the_verdict_follows_the_majority_of_decidable_rows(self):
        for i in range(3):
            plan_row(f"ADD{i}", base_required_qty=1000, min_stock=200,
                     stock_in_hand=300, required_qty=900)
        plan_row("SUB", base_required_qty=1000, min_stock=200,
                 stock_in_hand=300, required_qty=500)
        audit = planning.floor_convention_audit(COMPANY)
        self.assertEqual(audit["verdict"], FloorConvention.ADDITIVE)
        self.assertEqual(audit["totals"]["additive"], 3)
        self.assertEqual(audit["totals"]["subtractive"], 1)


class AlarmDeliveryTests(TestCase):
    """Alarms that are computed but never sent are still a report."""

    def setUp(self):
        MaterialLeadTime.objects.create(
            company_code=COMPANY, material_code="PM-LONG", lead_time_days=90,
            moq=0, unit="Pcs", material_type="PACKAGING",
        )
        MaterialLeadTime.objects.create(
            company_code=COMPANY, material_code="RM-OIL", lead_time_days=90,
            moq=0, unit="Tons", material_type="RAW",
        )
        plan_row("PM-LONG", required_qty=500, net_shortage_qty=500)
        plan_row("RM-OIL", required_qty=20, net_shortage_qty=20)
        self.sub = AlarmSubscription.objects.create(
            company_code=COMPANY, label="Packaging Procurement",
            permission_codename="can_view_supply_chain", material_type="PACKAGING",
        )

    def _send(self, **kwargs):
        with mock.patch(
            "notifications.services.NotificationService.send_notification_by_permission",
            return_value=3,
        ) as spy:
            results = alarms.send_supply_chain_alarms(
                COMPANY, today=date(2026, 8, 10), **kwargs
            )
        return results, spy

    def test_an_alarm_reaches_the_department_holding_the_permission(self):
        results, spy = self._send()
        self.assertTrue(results[0]["sent"])
        self.assertEqual(results[0]["recipients"], 3)
        kwargs = spy.call_args.kwargs
        self.assertEqual(kwargs["permission_codename"], "can_view_supply_chain")
        self.assertEqual(kwargs["notification_type"], "SUPPLY_CHAIN_ALARM")
        self.assertIn("overdue", kwargs["title"])
        self.assertIn("PM-LONG", kwargs["body"])

    def test_a_packaging_buyer_is_not_paged_about_bulk_oil(self):
        _results, spy = self._send()
        self.assertNotIn("RM-OIL", spy.call_args.kwargs["body"])
        self.assertEqual(spy.call_args.kwargs["extra_data"]["items"], ["PM-LONG"])

    def test_an_unchanged_digest_is_not_sent_twice(self):
        """An overdue order stays overdue every day until someone places it.
        Re-sending nightly is how a notification channel gets muted."""
        self._send()
        results, spy = self._send()
        self.assertFalse(results[0]["sent"])
        self.assertEqual(results[0]["reason"], "unchanged since last send")
        spy.assert_not_called()
        self.assertEqual(AlarmDispatch.objects.filter(company_code=COMPANY).count(), 1)

    def test_force_resends_and_a_changed_alarm_sends_again(self):
        self._send()
        results, _spy = self._send(force=True)
        self.assertTrue(results[0]["sent"])
        # A newly-short material changes the digest, so it sends without force.
        MaterialLeadTime.objects.create(
            company_code=COMPANY, material_code="PM-NEW", lead_time_days=90,
            material_type="PACKAGING",
        )
        plan_row("PM-NEW", required_qty=10, net_shortage_qty=10)
        results, _spy = self._send()
        self.assertTrue(results[0]["sent"])

    def test_a_dry_run_builds_the_digest_and_sends_nothing(self):
        results, spy = self._send(dry_run=True)
        spy.assert_not_called()
        self.assertFalse(results[0]["sent"])
        self.assertIn("PM-LONG", results[0]["body"])
        self.assertEqual(AlarmDispatch.objects.count(), 0)

    def test_nothing_to_report_is_not_an_empty_notification(self):
        SalesPlanningRequirementRow.objects.all().delete()
        results, spy = self._send()
        spy.assert_not_called()
        self.assertEqual(results[0]["reason"], "nothing to report")

    def test_one_departments_delivery_failure_does_not_silence_another(self):
        AlarmSubscription.objects.create(
            company_code=COMPANY, label="Oils Procurement",
            permission_codename="can_view_supply_chain", material_type="RAW",
        )
        with mock.patch(
            "notifications.services.NotificationService.send_notification_by_permission",
            side_effect=[RuntimeError("FCM down"), 2],
        ):
            results = alarms.send_supply_chain_alarms(COMPANY, today=date(2026, 8, 10))
        # Whichever department is processed first, the other still gets its alarm.
        sent = [r for r in results if r["sent"]]
        failed = [r for r in results if not r["sent"]]
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(failed), 1)
        self.assertIn("delivery failed", failed[0]["reason"])
        # And the failed one leaves no dispatch record, so it retries next run.
        self.assertEqual(AlarmDispatch.objects.filter(company_code=COMPANY).count(), 1)

    def test_capacity_alarms_are_opt_in_per_subscription(self):
        MachineCapacity.objects.create(
            company_code=COMPANY, machine_id="M-01", output_per_hour=1,
            shift_hours=1, shifts_per_day=1, working_days_per_month=1,
        )
        MaterialMachineMap.objects.create(
            company_code=COMPANY, sku_code="FG-BIG", primary_machine_id="M-01",
            output_on_primary=1,
        )
        plan_row("FG-BIG", required_qty=10000, net_shortage_qty=10000)

        _results, spy = self._send()
        self.assertNotIn("over capacity", spy.call_args.kwargs["body"])

        self.sub.include_capacity = True
        self.sub.save(update_fields=["include_capacity"])
        _results, spy = self._send(force=True)
        self.assertIn("over capacity", spy.call_args.kwargs["body"])

    def test_no_subscriptions_means_nobody_is_told_and_that_is_visible(self):
        AlarmSubscription.objects.all().delete()
        results, spy = self._send()
        spy.assert_not_called()
        self.assertEqual(results, [])


class SalesTrendLoaderTests(TestCase):
    def test_loading_a_trend_csv_enables_the_policy_floor(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as fh:
            fh.write("item_code,item_name,three_month_qty\nPM-A,Cap,3000\nPM-B,Bottle,600\n")
            path = fh.name
        call_command("load_sales_trend", "--company", COMPANY, "--csv", path)
        self.assertEqual(SalesTrend.objects.filter(company_code=COMPANY).count(), 2)
        self.assertEqual(
            SalesTrend.objects.get(company_code=COMPANY, item_code="PM-A").three_month_qty,
            Decimal("3000"),
        )
        # Re-loading updates rather than duplicating.
        call_command("load_sales_trend", "--company", COMPANY, "--csv", path)
        self.assertEqual(SalesTrend.objects.filter(company_code=COMPANY).count(), 2)


# ─────────────────────────────────────────────────────────────────────────────
# The daily operating loop
# ─────────────────────────────────────────────────────────────────────────────

from .models import (  # noqa: E402
    CoverVerdict,
    DailyRun,
    DailyRunRow,
    DataQualityIssue,
    MaterialStock,
    MonitoredSku,
    OperatingParameters,
    RowVerdictState,
    RunStatus,
    SkuComponent,
    SupplierDelivery,
)
from .services import daily_run as daily_run_service  # noqa: E402
from .services import operations as ops  # noqa: E402


def _sku(plan=Decimal("582653"), days=18, code="FG0000030"):
    return MonitoredSku.objects.create(
        company_code=COMPANY, sku_code=code, sku_name=code,
        plan_quantity=plan, working_days_left=days,
    )


def _component(sku, code, per_unit=Decimal("1"), unit="Pcs"):
    return SkuComponent.objects.create(
        sku=sku, material_code=code, material_name=code,
        quantity_per_unit=per_unit, unit=unit,
    )


def _stock(code, on_hand, committed=0):
    return MaterialStock.objects.create(
        company_code=COMPANY, material_code=code,
        on_hand=Decimal(on_hand), committed=Decimal(committed),
    )


def _deliveries(code, days_list, today=None):
    today = today or date(2026, 8, 10)
    for i, took in enumerate(days_list):
        received = today - timedelta(days=10 + i)
        SupplierDelivery.objects.create(
            company_code=COMPANY, material_code=code,
            ordered_on=received - timedelta(days=took), received_on=received,
        )


class PercentileTests(TestCase):
    def test_nearest_rank_does_not_invent_a_delivery_that_never_happened(self):
        """Interpolating would produce a 38.4-day delivery nobody can point at.
        These are whole days from real POs, so the rank is taken as-is."""
        samples = [10, 20, 30, 40, 50]
        self.assertEqual(daily_run_service.percentile(samples, 80), 40)
        self.assertEqual(daily_run_service.percentile(samples, 50), 30)
        self.assertEqual(daily_run_service.percentile(samples, 100), 50)
        self.assertEqual(daily_run_service.percentile([], 80), None)

    def test_a_thin_history_is_reported_as_unknown_not_averaged(self):
        """Two deliveries averaged into a confident number is worse than saying
        'we do not know' — it looks measured and is not."""
        params = OperatingParameters(company_code=COMPANY, min_delivery_samples=3)
        _deliveries("PM-X", [30, 40])
        days, samples = daily_run_service.measured_lead_time(COMPANY, "PM-X", params)
        self.assertIsNone(days)
        self.assertEqual(samples, 2)

        _deliveries("PM-X", [35])
        days, samples = daily_run_service.measured_lead_time(COMPANY, "PM-X", params)
        self.assertEqual(samples, 3)
        self.assertEqual(days, 40)   # 80th percentile of [30, 35, 40]


class PlaybookWorkedExampleTests(TestCase):
    """The playbook's own example, reproduced line for line.

    These are figures read from live SAP on 10 August 2026 and published to the
    plant. If the engine cannot reproduce them, nobody at the plant should trust
    anything else it says.
    """

    def setUp(self):
        self.today = date(2026, 8, 10)
        OperatingParameters.objects.create(company_code=COMPANY)
        sku = _sku()
        _component(sku, "PM0000235")                     # 1 cap per bottle
        _stock("PM0000235", 695819, 239744)
        # 44 past deliveries whose 80th percentile is 39 days.
        _deliveries("PM0000235", [30] * 35 + [39] * 9, today=self.today)

    def test_the_six_lines_of_arithmetic(self):
        run = daily_run_service.build_daily_run(COMPANY, self.today)
        row = run.rows.get(material_code="PM0000235")

        # 1  582,653 / 18 working days = 32,369.6 bottles a day
        self.assertEqual(row.units_per_day.quantize(Decimal("0.1")), Decimal("32369.6"))
        # 2  x 1 cap per bottle
        self.assertEqual(row.consumption_per_day.quantize(Decimal("0.1")), Decimal("32369.6"))
        # 3  695,819 - 239,744 = 456,075 free
        self.assertEqual(row.free_stock, Decimal("456075"))
        # 4  456,075 / 32,369.6 = 14 days of cover
        self.assertEqual(row.days_of_cover.quantize(Decimal("1")), Decimal("14"))
        # 5  80th percentile of 44 deliveries = 39 days, and it was MEASURED
        self.assertEqual(row.lead_time_days, 39)
        self.assertEqual(row.lead_time_source, "MEASURED")
        self.assertEqual(row.lead_time_samples, 44)
        # 6  14 days of cover against 39 days of lead time
        self.assertEqual(row.verdict, CoverVerdict.RED)
        self.assertEqual(run.red_count, 1)

    def test_cover_is_converted_to_calendar_days_before_being_compared(self):
        """Cover is eaten only on working days; a supplier's 39 days include
        weekends. Comparing them raw overstates how long the stock lasts."""
        run = daily_run_service.build_daily_run(COMPANY, self.today)
        row = run.rows.get(material_code="PM0000235")
        # 14 production days at 30 calendar / 26 working = ~16 calendar days.
        self.assertEqual(row.cover_calendar_days.quantize(Decimal("1")), Decimal("16"))
        self.assertEqual(row.stockout_date, date(2026, 8, 26))
        # Order-by = stockout - lead time. The playbook's own figure is 12 July;
        # this convention gives 18 July, and the difference IS the conversion.
        self.assertEqual(row.order_by_date, date(2026, 7, 18))
        self.assertEqual(row.days_late, 23)

    def test_turning_the_conversion_off_compares_raw_production_days(self):
        params = OperatingParameters.objects.get(company_code=COMPANY)
        params.calendar_days_per_month = params.working_days_per_month
        params.save()
        run = daily_run_service.build_daily_run(COMPANY, self.today)
        row = run.rows.get(material_code="PM0000235")
        self.assertEqual(row.cover_calendar_days.quantize(Decimal("1")), Decimal("14"))
        self.assertEqual(row.verdict, CoverVerdict.RED)   # still red — 14 < 39


class DailyRunEngineTests(TestCase):
    def setUp(self):
        self.today = date(2026, 8, 10)
        OperatingParameters.objects.create(company_code=COMPANY)

    def test_plenty_of_cover_is_green(self):
        sku = _sku(plan=Decimal("1800"), days=18)       # 100/day
        _component(sku, "PM-OK")
        _stock("PM-OK", 100000)                          # 1000 days of cover
        _deliveries("PM-OK", [8, 8, 8])
        run = daily_run_service.build_daily_run(COMPANY, self.today)
        self.assertEqual(run.rows.get(material_code="PM-OK").verdict, CoverVerdict.GREEN)
        self.assertEqual(run.green_count, 1)

    def test_cover_just_above_the_lead_time_is_amber_not_green(self):
        """A material that clears the lead time by a day is not comfortable."""
        sku = _sku(plan=Decimal("1800"), days=18)       # 100/day
        _component(sku, "PM-TIGHT")
        _stock("PM-TIGHT", 1000)                         # 10 production days = ~11.5 calendar
        _deliveries("PM-TIGHT", [10, 10, 10])
        run = daily_run_service.build_daily_run(COMPANY, self.today)
        self.assertEqual(run.rows.get(material_code="PM-TIGHT").verdict, CoverVerdict.AMBER)

    def test_a_material_with_no_stock_record_is_flagged_not_dropped(self):
        """Dropping it is exactly how a real shortage hides."""
        sku = _sku()
        _component(sku, "PM-NOSTOCK")
        _deliveries("PM-NOSTOCK", [10, 10, 10])
        run = daily_run_service.build_daily_run(COMPANY, self.today)
        self.assertEqual(run.rows.count(), 1)
        self.assertTrue(
            DataQualityIssue.objects.filter(run=run, code="NO_STOCK").exists()
        )

    def test_a_material_with_no_history_falls_back_to_the_typed_lead_time(self):
        sku = _sku()
        _component(sku, "PM-TYPED")
        _stock("PM-TYPED", 1000)
        MaterialLeadTime.objects.create(
            company_code=COMPANY, material_code="PM-TYPED", lead_time_days=20,
        )
        run = daily_run_service.build_daily_run(COMPANY, self.today)
        row = run.rows.get(material_code="PM-TYPED")
        self.assertEqual(row.lead_time_days, 20)
        self.assertEqual(row.lead_time_source, "TEMPLATE")
        # Still flagged, because a typed lead time is weaker evidence than history.
        self.assertTrue(
            DataQualityIssue.objects.filter(run=run, code="LEAD_TIME_FROM_TEMPLATE").exists()
        )

    def test_no_lead_time_at_all_is_unknown_and_blocking(self):
        sku = _sku()
        _component(sku, "PM-BLIND")
        _stock("PM-BLIND", 1000)
        run = daily_run_service.build_daily_run(COMPANY, self.today)
        self.assertEqual(run.rows.get(material_code="PM-BLIND").verdict, CoverVerdict.UNKNOWN)
        self.assertEqual(run.unknown_count, 1)
        self.assertTrue(
            DataQualityIssue.objects.filter(run=run, code="NO_LEAD_TIME", blocking=True).exists()
        )

    def test_an_sku_with_no_recipe_reports_it_rather_than_passing_silently(self):
        _sku()
        run = daily_run_service.build_daily_run(COMPANY, self.today)
        self.assertEqual(run.rows.count(), 0)
        self.assertTrue(DataQualityIssue.objects.filter(run=run, code="NO_BOM").exists())

    def test_monitoring_nothing_is_itself_a_finding(self):
        run = daily_run_service.build_daily_run(COMPANY, self.today)
        self.assertTrue(DataQualityIssue.objects.filter(run=run, code="NO_SKU").exists())

    def test_rebuilding_a_day_replaces_it_rather_than_duplicating(self):
        sku = _sku()
        _component(sku, "PM-A")
        _stock("PM-A", 1000)
        _deliveries("PM-A", [10, 10, 10])
        daily_run_service.build_daily_run(COMPANY, self.today)
        daily_run_service.build_daily_run(COMPANY, self.today)
        self.assertEqual(DailyRun.objects.filter(company_code=COMPANY).count(), 1)
        self.assertEqual(DailyRun.objects.get().rows.count(), 1)

    def test_committed_stock_is_not_available_stock(self):
        sku = _sku(plan=Decimal("1800"), days=18)       # 100/day
        _component(sku, "PM-C")
        _stock("PM-C", 1000, committed=900)              # only 100 free = 1 day
        _deliveries("PM-C", [30, 30, 30])
        run = daily_run_service.build_daily_run(COMPANY, self.today)
        row = run.rows.get(material_code="PM-C")
        self.assertEqual(row.free_stock, Decimal("100"))
        self.assertEqual(row.days_of_cover, Decimal("1.00"))
        self.assertEqual(row.verdict, CoverVerdict.RED)


class DailyRunWorkflowTests(TestCase):
    """Generate -> review -> publish -> verdict."""

    def setUp(self):
        self.today = date(2026, 8, 10)
        OperatingParameters.objects.create(company_code=COMPANY)
        sku = _sku()
        _component(sku, "PM0000235")
        _stock("PM0000235", 695819, 239744)
        _deliveries("PM0000235", [30] * 35 + [39] * 9, today=self.today)
        self.run = daily_run_service.build_daily_run(COMPANY, self.today)
        AlarmSubscription.objects.create(
            company_code=COMPANY, label="Buyer",
            permission_codename="can_view_supply_chain",
        )

    def _publish(self):
        with mock.patch(
            "notifications.services.NotificationService.send_notification_by_permission",
            return_value=3,
        ) as spy:
            run, message = ops.publish_run(self.run)
        return run, message, spy

    def test_a_run_cannot_be_published_before_a_person_has_looked_at_it(self):
        """The analyst check is the point — it is what stops twenty-five wrong
        alarms reaching a department that then stops reading them."""
        with self.assertRaises(SupplyChainError) as ctx:
            ops.publish_run(self.run)
        self.assertEqual(ctx.exception.code, "NOT_REVIEWED")

    def test_the_full_happy_path(self):
        ops.review_run(self.run, comment="Caps still short; supplier called yesterday.")
        self.assertEqual(self.run.status, RunStatus.REVIEWED)

        run, message, spy = self._publish()
        self.assertEqual(run.status, RunStatus.PUBLISHED)
        self.assertEqual(run.recipients, 3)
        self.assertIn("1 to order today", message["title"])
        self.assertIn("PM0000235", message["body"])
        self.assertIn("Caps still short", message["body"])
        spy.assert_called_once()

        row = run.rows.get(material_code="PM0000235")
        ops.record_verdict(row, RowVerdictState.REAL, note="Supplier confirmed 5 Sep.")
        progress = ops.verdict_progress(run)
        self.assertEqual(progress, {
            "red_rows": 1, "verdicts_recorded": 1, "outstanding": 0, "complete": True,
        })

    def test_too_many_reds_blocks_the_run_and_says_why(self):
        params = OperatingParameters.objects.get(company_code=COMPANY)
        params.max_red_before_block = 0
        params.save()
        run = daily_run_service.build_daily_run(COMPANY, self.today)
        self.assertEqual(run.status, RunStatus.BLOCKED)

        with self.assertRaises(SupplyChainError) as ctx:
            ops.review_run(run)
        self.assertEqual(ctx.exception.code, "TOO_MANY_REDS")
        self.assertIn("stock or purchase-order data", ctx.exception.message)

    def test_the_block_can_be_overridden_but_only_with_a_written_reason(self):
        params = OperatingParameters.objects.get(company_code=COMPANY)
        params.max_red_before_block = 0
        params.save()
        run = daily_run_service.build_daily_run(COMPANY, self.today)

        with self.assertRaises(SupplyChainError) as ctx:
            ops.review_run(run, override=True)
        self.assertEqual(ctx.exception.code, "OVERRIDE_NEEDS_COMMENT")

        ops.review_run(run, override=True, comment="Genuine — three suppliers on stop.")
        self.assertEqual(run.status, RunStatus.REVIEWED)

    def test_publishing_with_nobody_subscribed_is_refused_not_silent(self):
        AlarmSubscription.objects.all().delete()
        ops.review_run(self.run)
        with self.assertRaises(SupplyChainError) as ctx:
            ops.publish_run(self.run)
        self.assertEqual(ctx.exception.code, "NO_SUBSCRIBERS")

    def test_red_rows_with_no_owner_are_surfaced(self):
        """"A red alarm nobody owns will not get done"."""
        self.assertEqual(ops.unassigned_red_rows(self.run).count(), 1)
        row = self.run.rows.get(material_code="PM0000235")
        ops.assign_owner(row, "Rajesh")
        self.assertEqual(ops.unassigned_red_rows(self.run).count(), 0)

    def test_a_verdict_can_be_corrected(self):
        row = self.run.rows.get(material_code="PM0000235")
        ops.record_verdict(row, RowVerdictState.REAL)
        ops.record_verdict(row, RowVerdictState.WRONG_DATA, note="Stock was miscounted.")
        row.refresh_from_db()
        self.assertEqual(row.row_verdict.outcome, RowVerdictState.WRONG_DATA)
        self.assertEqual(ops.verdict_progress(self.run)["verdicts_recorded"], 1)

    def test_a_nonsense_verdict_is_rejected(self):
        row = self.run.rows.get(material_code="PM0000235")
        with self.assertRaises(SupplyChainError) as ctx:
            ops.record_verdict(row, "MAYBE")
        self.assertEqual(ctx.exception.code, "BAD_VERDICT")


class WeeklyReviewTests(TestCase):
    def setUp(self):
        self.today = date(2026, 8, 10)
        OperatingParameters.objects.create(company_code=COMPANY)

    def _run_with_verdicts(self, run_date, outcomes):
        run = DailyRun.objects.create(company_code=COMPANY, run_date=run_date)
        for i, outcome in enumerate(outcomes):
            row = DailyRunRow.objects.create(
                run=run, sku_code="FG0000030", material_code=f"PM-{i}",
                verdict=CoverVerdict.RED,
            )
            ops.record_verdict(row, outcome)
        return run

    def test_mostly_wrong_data_says_fix_the_data_not_the_software(self):
        self._run_with_verdicts(date(2026, 8, 5), [
            RowVerdictState.WRONG_DATA, RowVerdictState.WRONG_DATA,
            RowVerdictState.WRONG_DATA, RowVerdictState.REAL,
        ])
        result = ops.weekly_review(COMPANY, today=self.today)
        self.assertEqual(result["totals"]["wrong_data"], 3)
        self.assertIn("not on software", result["recommendation"])

    def test_mostly_real_says_widen_the_trial(self):
        self._run_with_verdicts(date(2026, 8, 5), [
            RowVerdictState.REAL, RowVerdictState.REAL,
            RowVerdictState.REAL, RowVerdictState.WRONG_DATA,
        ])
        result = ops.weekly_review(COMPANY, today=self.today)
        self.assertEqual(result["totals"]["real_share_percent"], 75.0)
        self.assertIn("widen it", result["recommendation"])

    def test_an_empty_verdict_log_says_we_learned_nothing(self):
        """The playbook: "if it is empty at the end of the month, we learned
        nothing." Reporting a healthy-looking zero would be worse than useless."""
        result = ops.weekly_review(COMPANY, today=self.today)
        self.assertEqual(result["totals"]["verdicts"], 0)
        self.assertIn("taught us nothing", result["recommendation"])

    def test_verdicts_are_grouped_by_the_week_they_belong_to(self):
        self._run_with_verdicts(date(2026, 8, 3), [RowVerdictState.REAL])       # Mon
        self._run_with_verdicts(date(2026, 8, 5), [RowVerdictState.WRONG_DATA])  # Wed
        self._run_with_verdicts(date(2026, 7, 29), [RowVerdictState.REAL])       # prev week
        result = ops.weekly_review(COMPANY, today=self.today)
        weeks = {w["week_starting"]: w for w in result["weeks"]}
        self.assertEqual(weeks["2026-08-03"]["total"], 2)
        self.assertEqual(weeks["2026-07-27"]["total"], 1)


class PlaybookSeedTests(TestCase):
    def test_the_seeded_trial_sku_reproduces_the_published_red_list(self):
        """The playbook publishes six materials in trouble and one comfortable.
        The seed plus the engine must agree with that."""
        call_command("seed_playbook_demo", "--company", COMPANY, verbosity=0)
        run = daily_run_service.build_daily_run(COMPANY, date(2026, 8, 10))

        by_code = {r.material_code: r for r in run.rows.all()}
        self.assertEqual(len(by_code), 7)
        # Bottles and loose oil have nothing at all.
        self.assertEqual(by_code["PM0000851"].verdict, CoverVerdict.RED)
        self.assertEqual(by_code["RM0000003"].verdict, CoverVerdict.RED)
        self.assertEqual(by_code["PM0000235"].verdict, CoverVerdict.RED)
        # Printed tape has hundreds of days of cover — no action needed.
        self.assertEqual(by_code["PM0000075"].verdict, CoverVerdict.GREEN)
        self.assertGreaterEqual(run.red_count, 5)
        # Every lead time came from real delivery history, not the typed fallback.
        self.assertTrue(all(r.lead_time_source == "MEASURED" for r in by_code.values()))
