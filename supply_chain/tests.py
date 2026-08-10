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
    AlarmState,
    FloorBasis,
    MachineCapacity,
    MaterialLeadTime,
    MaterialMachineMap,
    ReferenceImport,
    SupplyChainPolicy,
)
from .services import SupplyChainError
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
