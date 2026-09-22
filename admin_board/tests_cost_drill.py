"""
admin_board/tests_cost_drill.py

What the cost tile opens onto: today, per line, and the rows behind each line.

THE CONTRACT TEST IS THE POINT OF THIS FILE. The screen reads
``slice.rows.length`` before it renders anything, so a slice shipped without a
``rows`` key does not degrade the cost tile — it throws inside the render and
takes the WHOLE board down to an error card, including the six tiles that read
perfectly. ``test_every_slice_carries_the_keys_the_screen_reads`` is the guard
against that, and it is deliberately dumb: it asserts the keys exist on every
slice, whatever each of them happens to contain.

The rest is arithmetic that is easy to get subtly wrong in one direction only:
a breakdown whose rows do not add up to the line above them is the panel
disagreeing with the tile that opened it, which is worse than no panel.
"""

from datetime import date
from decimal import Decimal

from django.test import TestCase

from accounts.models import Department
from company.models import Company

from .services import AdminBoardService

TODAY = date(2026, 9, 15)

#: Every key the front end dereferences on a cost slice without guarding it.
SLICE_KEYS = {"today", "today_detail", "today_detail_value", "today_detail_unit", "rows"}


def wall_board(**over):
    """The Factory Expense wall board's payload, as this tile reads it.

    Stubbed rather than built from Cost Master rows because what is under test
    here is the RESHAPING — which of the wall's keys this tile reads and how it
    spreads them — not the wall's own arithmetic, which the factory_expense
    suite already pins down.

    Note ``buckets[...]['today']`` is absent on purpose: the wall sets it to
    whatever span was selected, this board selects the whole month, and a tile
    that read it would print the month under a heading saying "today". If this
    service ever starts reading that key these tests fail with a KeyError,
    which is the outcome wanted.
    """
    payload = {
        "buckets": {
            "LABOUR": {"mtd": Decimal("0"), "warning": None},
            "SALARY": {"mtd": Decimal("30000"), "warning": None},
            "ELECTRICITY": {"mtd": Decimal("0"), "warning": None},
            "MAINTENANCE": {"mtd": Decimal("4500"), "warning": None},
        },
        "trend": [
            {
                "date": "2026-09-14",
                "is_today": False,
                "salary": Decimal("2000"),
                "maintenance": Decimal("3000"),
            },
            {
                "date": "2026-09-15",
                "is_today": True,
                "salary": Decimal("2000"),
                "maintenance": Decimal("1500"),
            },
        ],
        "salary_departments": [
            {
                "department": "Packing",
                "department_id": 1,
                "monthly": Decimal("45000"),
                "daily": Decimal("1500"),
            },
            {
                "department": "Admin",
                "department_id": 2,
                "monthly": Decimal("15000"),
                "daily": Decimal("500"),
            },
        ],
        "maintenance_items": [
            {"label": "BRG-6204 × 2", "kind": "Spare", "amount": Decimal("3000")},
            {"label": "IND-0042", "kind": "Indent", "amount": Decimal("1500")},
        ],
        "warnings": [],
    }
    payload.update(over)
    return lambda **kwargs: payload


def cost(**over):
    """The cost tile, over a stubbed wall board."""
    service = AdminBoardService("JIVO_OIL", cost_board=wall_board(**over), today=TODAY)
    return service._cost()


def line(payload, key):
    return next(entry for entry in payload["slices"] if entry["key"] == key)


class CostSliceContractTests(TestCase):
    """Every slice carries every key the screen reads off it."""

    def setUp(self):
        Company.objects.create(name="Jivo Oil", code="JIVO_OIL")

    def test_every_slice_carries_the_keys_the_screen_reads(self):
        for slice_ in cost()["slices"]:
            missing = SLICE_KEYS - set(slice_)
            self.assertEqual(missing, set(), f"{slice_['key']} is missing {missing}")

    def test_rows_is_a_list_on_a_line_with_nothing_behind_it(self):
        # Never absent and never None: the panel asks a line how many rows it
        # has before deciding whether it opens, and both of those throw.
        self.assertEqual(line(cost(), "labour")["rows"], [])

    def test_the_tile_says_what_the_day_and_the_average_day_cost(self):
        payload = cost()
        # 2,000 of salary accrued plus 1,500 of maintenance booked today.
        self.assertEqual(payload["today_total"], 3_500.0)
        # 34,500 over the 15 elapsed days. Elapsed, not producing: a salary
        # accrues on a Sunday and so does a spare consumed on one.
        self.assertEqual(payload["total"], 34_500.0)
        self.assertEqual(payload["avg_per_day"], 2_300.0)

    def test_the_day_is_read_off_the_trend_not_the_wall_s_today_bucket(self):
        # The wall's own `today` key holds the selected span, which for this
        # board is the whole month. Reading it would report 4,500 of
        # maintenance spent today, on the day 1,500 of it was.
        self.assertEqual(line(cost(), "maintenance")["today"], 1_500.0)

    def test_a_board_with_no_trend_reports_nil_today_rather_than_failing(self):
        payload = cost(trend=[])
        self.assertEqual(payload["today_total"], 0.0)
        self.assertEqual(line(payload, "salary")["today"], 0.0)


class SalaryRowTests(TestCase):
    """The payroll lines, accrued the way the line above them is."""

    def setUp(self):
        Company.objects.create(name="Jivo Oil", code="JIVO_OIL")

    def test_each_row_is_accrued_over_the_days_elapsed(self):
        rows = line(cost(), "salary")["rows"]
        self.assertEqual(
            [(row["label"], row["amount"], row["today"]) for row in rows],
            [("Packing", 22_500.0, 1_500.0), ("Admin", 7_500.0, 500.0)],
        )

    def test_the_rows_add_up_to_the_line_they_sit_under(self):
        salary = line(cost(), "salary")
        # 45,000 + 15,000 is the MONTH'S BILL, and the tile shows half of it on
        # the 15th. Rows carrying the whole bill would read as the panel
        # contradicting the tile that opened it.
        self.assertEqual(sum(row["amount"] for row in salary["rows"]), salary["amount"])
        self.assertEqual(sum(row["today"] for row in salary["rows"]), salary["today"])

    def test_the_row_names_the_monthly_bill_it_is_a_fraction_of(self):
        packing = line(cost(), "salary")["rows"][0]
        self.assertEqual(packing["detail"], "₹0.45 L/month over 30 days")

    def test_the_accrual_counts_nothing_so_it_prints_no_unit(self):
        # It is a fraction of a monthly bill, not a tally of anything. A badge
        # reading "2 departments today" beside it would be counting the rate
        # rows rather than anything that happened today.
        salary = line(cost(), "salary")
        self.assertIsNone(salary["today_detail_value"])
        self.assertIsNone(salary["today_detail_unit"])


class MaintenanceRowTests(TestCase):
    """The spares and indents, and the one thing they cannot say."""

    def setUp(self):
        Company.objects.create(name="Jivo Oil", code="JIVO_OIL")

    def test_a_row_reports_today_as_unknown_rather_than_nil(self):
        # The LINE knows what it cost today, because the register dates a spare
        # movement. The line ITEMS the wall board hands over carry no date, so
        # None — "this payload cannot say" — is the only honest answer, and the
        # panel prints it differently from a real nil.
        rows = line(cost(), "maintenance")["rows"]
        self.assertEqual([row["today"] for row in rows], [None, None])

    def test_the_rows_are_the_biggest_first_and_add_up_to_the_line(self):
        maintenance = line(cost(), "maintenance")
        self.assertEqual(
            [(row["label"], row["detail"], row["amount"]) for row in maintenance["rows"]],
            [("BRG-6204 × 2", "Spare", 3_000.0), ("IND-0042", "Indent", 1_500.0)],
        )
        self.assertEqual(
            sum(row["amount"] for row in maintenance["rows"]), maintenance["amount"]
        )


class LabourRowTests(TestCase):
    """The floors behind the labour line, and what walked onto them today."""

    def setUp(self):
        from cost_master.models import CostRate, CostType
        from labour_gate.models import LabourGateEntry
        from person_gatein.models import Contractor

        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.boiling = Department.objects.create(name="Boiling Floor 1")
        self.scrap = Department.objects.create(name="Scrap")
        self.contractor = Contractor.objects.create(contractor_name="Balbir")

        cost_type = CostType.objects.create(
            code="factory-labour", name="Factory labour", default_basis="PER_PERSON_DAY"
        )
        CostRate.objects.create(
            cost_type=cost_type,
            scope="FACTORY",
            basis="PER_PERSON_DAY",
            rate=Decimal("600"),
            effective_from=date(2026, 9, 1),
        )

        def entry(day, department, count):
            LabourGateEntry.objects.create(
                company=self.company,
                department=department,
                contractor=self.contractor,
                work_date=day,
                count_in=count,
            )

        entry(date(2026, 9, 2), None, 40)
        entry(date(2026, 9, 2), self.boiling, 25)
        entry(date(2026, 9, 3), self.scrap, 4)
        entry(TODAY, self.boiling, 10)

    def _labour(self):
        return line(cost(), "labour")

    def test_one_row_per_floor_that_was_staffed_biggest_first(self):
        rows = self._labour()["rows"]
        self.assertEqual(
            [(row["label"], row["detail"], row["amount"], row["today"]) for row in rows],
            [
                ("Boiling Floor 1", "35 man-days", 21_000.0, 6_000.0),
                ("Scrap", "4 man-days", 2_400.0, 0.0),
            ],
        )

    def test_a_floor_nobody_was_booked_to_is_absent_rather_than_a_nil_row(self):
        # Dock is one of the five the line names and nobody worked it. A row of
        # zeroes would claim it was staffed for nothing.
        Department.objects.create(name="Dock")
        self.assertNotIn("Dock", [row["label"] for row in self._labour()["rows"]])

    def test_the_rows_add_up_to_the_line_they_sit_under(self):
        labour = self._labour()
        self.assertEqual(sum(row["amount"] for row in labour["rows"]), labour["amount"])
        self.assertEqual(sum(row["today"] for row in labour["rows"]), labour["today"])

    def test_today_names_the_heads_behind_the_money(self):
        labour = self._labour()
        self.assertEqual(labour["today"], 6_000.0)
        self.assertEqual(labour["today_detail"], "10 on the floors today")
        self.assertEqual(labour["today_detail_value"], 10)
        self.assertEqual(labour["today_detail_unit"], "in")

    def test_a_quiet_day_on_a_busy_month_says_so_rather_than_printing_nothing(self):
        from labour_gate.models import LabourGateEntry

        LabourGateEntry.objects.filter(work_date=TODAY).delete()
        labour = self._labour()
        self.assertEqual(labour["today"], 0.0)
        self.assertEqual(labour["today_detail"], "nobody booked to these floors today")

    def test_unpriced_people_still_count_towards_today_s_head_count(self):
        # The money and the head count answer different questions. Losing the
        # heads with the rate would report an empty factory on a day 10 people
        # worked, which is a worse wrong answer than "10 in, nothing priced".
        from cost_master.models import CostRate

        CostRate.objects.all().delete()
        labour = self._labour()
        self.assertEqual(labour["today"], 0.0)
        self.assertEqual(labour["today_detail_value"], 10)


class ElectricityRowTests(TestCase):
    """The meters behind the power line, and the one that was not read today."""

    def setUp(self):
        from maintenance.models import DailyElectricityReading, ElectricityMeter

        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")

        def meter(name):
            row = ElectricityMeter.objects.create(
                name=name,
                rate_per_unit=Decimal("7"),
                multiplying_factor=Decimal("1"),
            )
            row.companies.set([self.oil])
            return row

        def reading(row, day, opening, closing):
            DailyElectricityReading.objects.create(
                meter=row,
                date=day,
                opening_reading=Decimal(opening),
                closing_reading=Decimal(closing),
                multiplying_factor=Decimal("1"),
                rate_per_unit=Decimal("7"),
            )

        self.floor = meter("Production Floor OIL")
        self.kwh = meter("KWH")
        reading(self.floor, date(2026, 9, 2), "0", "1000")
        reading(self.floor, TODAY, "1000", "1500")
        reading(self.kwh, date(2026, 9, 2), "0", "2000")

    def _power(self):
        return line(cost(), "electricity")

    def test_one_row_per_meter_read_this_month_with_its_own_rate(self):
        rows = self._power()["rows"]
        self.assertEqual(
            [(row["label"], row["detail"], row["amount"]) for row in rows],
            [
                ("KWH", "2,000 units at ₹7.00/unit", 14_000.0),
                ("Production Floor OIL", "1,500 units at ₹7.00/unit", 10_500.0),
            ],
        )

    def test_a_meter_nobody_read_today_says_unknown_not_nil(self):
        # A meter carries on drawing power whether or not somebody wrote the
        # number down. Nil here would report that KWH's line stopped.
        by_meter = {row["label"]: row["today"] for row in self._power()["rows"]}
        self.assertEqual(by_meter["Production Floor OIL"], 3_500.0)
        self.assertIsNone(by_meter["KWH"])

    def test_today_names_the_units_behind_the_money(self):
        power = self._power()
        self.assertEqual(power["today"], 3_500.0)
        self.assertEqual(power["today_detail"], "500 units today")
        self.assertEqual(power["today_detail_value"], 500.0)
        self.assertEqual(power["today_detail_unit"], "units")

    def test_a_day_with_no_reading_entered_says_that_rather_than_nothing(self):
        from maintenance.models import DailyElectricityReading

        DailyElectricityReading.objects.filter(date=TODAY).delete()
        power = self._power()
        self.assertEqual(power["today"], 0.0)
        # NOT "nothing today". The plant did not stop drawing power; the
        # register is behind, and the tile has to say which of the two it is.
        self.assertEqual(power["today_detail"], "no reading entered today")
        self.assertIsNone(power["today_detail_unit"])
