"""
operations_report/tests.py

What these pin down, each because it would go wrong silently:

- a run's cases follow the run list's rule (closing figure when completed,
  segments while open) and a draft is not production;
- litres come from the run's own volumes, and a run without them is counted in
  cases but not in litres -- never guessed;
- waste is dated by the run it came out of, not the day it was typed, and is
  valued at that run's own price;
- labour counts the gate's intake, never the HOD's allocation of the same
  people, and is priced from the Cost Master -- or left uncosted, not zero;
- salary is the Cost Master's monthly bill spread over its month's days, every
  day alike, and a day with no rate in force is a gap, not a nil payroll;
- a day nobody read the meters is a gap, not zero units;
- a reader holding only one of the two rights gets that half of the report.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Department as PlantDepartment
from company.models import Company, UserCompany, UserRole
from cost_master.models import CostRate, CostType
from factory_expense.constants import LABOUR_COST_TYPE_CODE, SALARY_COST_TYPE_CODE
from goods_return.models import GoodsReturn, GoodsReturnItem
from labour_gate.models import LabourGateEntry
from person_gatein.models import Contractor
from production_execution.models import (
    ProductionLine,
    ProductionMaterialUsage,
    ProductionRun,
    ProductionSegment,
    RunStatus,
    WasteLog,
)

from .services import OperationsReportService, waste_kind

User = get_user_model()

DAY = date(2026, 9, 14)


def no_power():
    """An Electricity++ result with nothing in it, every day entered."""
    return {"by_day_meter": {}, "units": Decimal("0"), "entered_days": []}


class ReportTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        cls.bev = Company.objects.create(name="Jivo Beverages", code="JIVO_BEVERAGES")
        cls.line = ProductionLine.objects.create(company=cls.oil, name="10 Head")
        cls.pouch = ProductionLine.objects.create(company=cls.oil, name="Pouch Machine")
        cls.contractor = Contractor.objects.create(contractor_name="Imran")
        cls.other = Contractor.objects.create(contractor_name="Nayeem")
        cls.packing = PlantDepartment.objects.create(name="Packing")

    def make_run(self, number, *, on=DAY, line=None, status=RunStatus.COMPLETED, cases=100,
            per_case=12, litres=Decimal("1"), company=None):
        return ProductionRun.objects.create(
            company=company or self.oil,
            line=line or self.line,
            run_number=number,
            date=on,
            required_qty=cases,
            total_production=cases if status == RunStatus.COMPLETED else 0,
            pieces_per_case=per_case,
            litres_per_piece=litres,
            status=status,
        )

    def report(self, date_from=DAY, date_to=DAY, company=None, user=None, power=None):
        result = power or no_power()
        with patch("maintenance.electricity.boards.breakdown", return_value=result), patch(
            "maintenance.electricity.boards.company", return_value=result
        ):
            return OperationsReportService(
                company or self.oil, date_from, date_to, user=user
            ).build()

    @staticmethod
    def day(report, on=DAY):
        return next(d for d in report["days"] if d["date"] == on.isoformat())


class ProductionTests(ReportTestCase):
    def test_completed_runs_count_their_closing_figure_in_litres(self):
        self.make_run(1, cases=100, per_case=12, litres=Decimal("1"))
        self.make_run(2, cases=50, per_case=4, litres=Decimal("5"))

        lines = self.day(self.report())["lines"]

        self.assertEqual(lines, [{"line": "10 Head", "runs": 2, "cases": 150.0, "litres": 2200.0}])

    def test_an_open_run_counts_its_segments_and_a_draft_counts_nothing(self):
        open_run = self.make_run(1, status=RunStatus.IN_PROGRESS, line=self.pouch)
        start = timezone.make_aware(datetime(2026, 9, 14, 8))
        ProductionSegment.objects.create(
            production_run=open_run, start_time=start, end_time=start + timedelta(hours=1),
            produced_cases=Decimal("30"),
        )
        self.make_run(2, status=RunStatus.DRAFT)

        lines = self.day(self.report())["lines"]

        self.assertEqual(lines, [{"line": "Pouch Machine", "runs": 1, "cases": 30.0, "litres": 360.0}])

    def test_a_run_without_volumes_makes_its_lines_litres_unknown_not_short(self):
        self.make_run(1, cases=100, litres=None)
        self.make_run(2, cases=50)

        report = self.report()

        self.assertEqual(self.day(report)["lines"][0]["cases"], 150.0)
        self.assertIsNone(self.day(report)["lines"][0]["litres"])

    def test_another_companys_runs_stay_out(self):
        self.make_run(1, company=self.bev, line=ProductionLine.objects.create(company=self.bev, name="Sidel"))

        self.assertEqual(self.day(self.report())["lines"], [])


class WastageTests(ReportTestCase):
    def waste(self, run, *, code="PM1", name="CAPS 1 LTR WHITE", qty=10, uom="PCS", typed=None):
        row = WasteLog.objects.create(
            company=self.oil, production_run=run, material_code=code,
            material_name=name, wastage_qty=qty, uom=uom,
        )
        if typed:
            WasteLog.objects.filter(pk=row.pk).update(
                created_at=timezone.make_aware(datetime(typed.year, typed.month, typed.day, 9))
            )
        return row

    def test_waste_is_dated_by_its_run_and_valued_at_that_runs_price(self):
        run = self.make_run(1)
        ProductionMaterialUsage.objects.create(
            production_run=run, material_code="PM1", material_name="CAPS", unit_price=Decimal("0.5")
        )
        # Typed three days later, as it usually is.
        self.waste(run, qty=100, typed=DAY + timedelta(days=3))

        report = self.report(DAY, DAY + timedelta(days=3))

        self.assertEqual(
            self.day(report)["wastage"],
            [{"item": "Caps", "unit": "pcs", "quantity": 100.0, "value": 50.0, "unpriced": 0}],
        )
        self.assertEqual(self.day(report, DAY + timedelta(days=3))["wastage"], [])

    def test_unpriced_waste_is_counted_and_named_but_not_valued(self):
        self.waste(self.make_run(1), name="LABEL 1 LTR FRONT", code="PM9", qty=40)

        report = self.report()

        self.assertEqual(self.day(report)["wastage"][0]["unpriced"], 1)
        self.assertEqual(self.day(report)["wastage"][0]["value"], 0.0)
        self.assertTrue(any("LABEL 1 LTR FRONT" in w for w in report["meta"]["warnings"]))

    def test_materials_group_by_the_word_their_name_starts_with(self):
        self.assertEqual(waste_kind("PET BOTTLE 1 LTR 26 GM"), "Bottles & jars")
        self.assertEqual(waste_kind("HDPE BOTTLE 5 LTR"), "Bottles & jars")
        self.assertEqual(waste_kind("CARTON 5 LTR 4 PCS"), "Cartons")
        self.assertEqual(waste_kind("SHRINKS 1 LTR 235X310"), "Shrink film")
        self.assertEqual(waste_kind("TAPE LOGO PRINTED"), "Tape")
        self.assertEqual(waste_kind("SOMETHING ELSE"), "Other packing")


class LabourTests(ReportTestCase):
    def gate(self, count, *, shift="DAY", department=None, contractor=None, on=DAY):
        return LabourGateEntry.objects.create(
            company=self.oil, department=department, contractor=contractor or self.contractor,
            work_date=on, shift=shift, count_in=count,
        )

    def rate(self, amount, effective_from=date(2026, 9, 1)):
        cost_type, _ = CostType.objects.get_or_create(
            code=LABOUR_COST_TYPE_CODE,
            defaults={"name": "Factory — Contract Labour", "default_basis": "PER_PERSON_DAY"},
        )
        return CostRate.objects.create(
            cost_type=cost_type, scope="FACTORY", basis="PER_PERSON_DAY",
            rate=Decimal(amount), effective_from=effective_from,
        )

    def test_intake_is_counted_once_and_priced_from_the_cost_master(self):
        self.rate("650")
        self.gate(40)
        self.gate(10, shift="NIGHT")
        self.gate(20, contractor=self.other)
        # The HOD placing 30 of Imran's people: the same people, not more.
        self.gate(30, department=self.packing)

        labour = self.day(self.report())["labour"]

        self.assertEqual(
            labour,
            [
                {"group": "Imran", "heads": 50, "day_shift": 40, "night_shift": 10, "cost": 32500.0},
                {"group": "Nayeem", "heads": 20, "day_shift": 20, "night_shift": 0, "cost": 13000.0},
            ],
        )

    def test_a_day_with_no_rate_is_uncosted_not_free(self):
        self.rate("650", effective_from=DAY + timedelta(days=1))
        self.gate(40)

        report = self.report()

        self.assertIsNone(self.day(report)["labour"][0]["cost"])
        self.assertEqual(self.day(report)["labour"][0]["heads"], 40)


class SalaryTests(ReportTestCase):
    def salary(self, amount, *, department=None, effective_from=date(2026, 9, 1)):
        cost_type, _ = CostType.objects.get_or_create(
            code=SALARY_COST_TYPE_CODE,
            defaults={"name": "Factory — Salary", "default_basis": "PER_MONTH"},
        )
        return CostRate.objects.create(
            cost_type=cost_type, scope="DEPARTMENT" if department else "FACTORY",
            department=department, basis="PER_MONTH", rate=Decimal(amount),
            effective_from=effective_from,
        )

    def test_each_day_carries_its_months_bill_over_the_months_days(self):
        refinery = PlantDepartment.objects.create(name="Refinery")
        self.salary("300000", department=self.packing)
        self.salary("600000", department=refinery)

        # The 30th of September and the 1st of October: 30 days, then 31.
        report = self.report(date(2026, 9, 30), date(2026, 10, 1))

        self.assertEqual(
            self.day(report, date(2026, 9, 30))["salary"],
            [
                {"department": "Refinery", "monthly": 600000.0, "cost": 20000.0},
                {"department": "Packing", "monthly": 300000.0, "cost": 10000.0},
            ],
        )
        self.assertEqual(
            [row["cost"] for row in self.day(report, date(2026, 10, 1))["salary"]],
            [19354.8387, 9677.4194],
        )

    def test_a_day_before_any_rate_is_a_gap_not_a_nil_payroll(self):
        self.salary("300000", effective_from=DAY + timedelta(days=1))

        report = self.report(DAY, DAY + timedelta(days=1))

        self.assertIsNone(self.day(report)["salary"])
        self.assertEqual(
            self.day(report, DAY + timedelta(days=1))["salary"],
            [{"department": "All departments", "monthly": 300000.0, "cost": 10000.0}],
        )
        self.assertFalse(any("factory-salary" in w for w in report["meta"]["warnings"]))

    def test_no_rate_anywhere_in_the_span_is_said(self):
        report = self.report()

        self.assertIsNone(self.day(report)["salary"])
        self.assertTrue(any("'factory-salary'" in w for w in report["meta"]["warnings"]))


class PowerTests(ReportTestCase):
    def test_meters_by_day_and_an_unread_day_is_a_gap(self):
        result = {
            "units": Decimal("150"),
            "entered_days": [DAY],
            "by_day_meter": {
                DAY: {
                    "Blowing": {"units": Decimal("100"), "cost": Decimal("835")},
                    "Lighting": {"units": Decimal("50"), "cost": Decimal("417.5")},
                    "Idle": {"units": Decimal("0"), "cost": Decimal("0")},
                }
            },
        }

        report = self.report(DAY, DAY + timedelta(days=1), power=result)

        self.assertEqual(
            self.day(report)["power"],
            [
                {"area": "Blowing", "kwh": 100.0, "cost": 835.0},
                {"area": "Lighting", "kwh": 50.0, "cost": 417.5},
            ],
        )
        self.assertIsNone(self.day(report, DAY + timedelta(days=1))["power"])


class GoodsReturnTests(ReportTestCase):
    def gr(self, entry_no, *, arrived=None, status="POSTED", company=None, lines=()):
        header = GoodsReturn.objects.create(
            company=company or self.oil, entry_no=entry_no, basis="INVOICE", status=status,
            gated_in_at=arrived,
        )
        for condition, qty, price in lines:
            GoodsReturnItem.objects.create(
                goods_return=header, item_code="FG1", item_name="Oil 1 L", uom="PCS",
                return_quantity=Decimal(qty), unit_price=Decimal(price), condition=condition,
            )
        return header

    def test_returns_count_on_the_day_they_arrived_by_condition_worst_first(self):
        arrived = timezone.make_aware(datetime(2026, 9, 14, 11))
        self.gr("GR-1", arrived=arrived, lines=[("DAMAGED", "10", "150"), ("LEAKED", "4", "150")])
        self.gr("GR-2", arrived=arrived, lines=[("DAMAGED", "6", "0")])
        self.gr("GR-3", arrived=arrived, status="CANCELLED", lines=[("DAMAGED", "99", "150")])
        # Arrived the day after the span: not in it, whenever it was booked.
        self.gr("GR-4", arrived=arrived + timedelta(days=1), lines=[("GOOD", "5", "150")])

        returns = self.day(self.report())["returns"]

        self.assertEqual(
            returns,
            [
                {"condition": "LEAKED", "label": "Leaked", "entries": ["GR-1"], "lines": 1,
                 "quantity": 4.0, "value": 600.0, "unpriced": 0},
                {"condition": "DAMAGED", "label": "Damaged", "entries": ["GR-1", "GR-2"], "lines": 2,
                 "quantity": 16.0, "value": 1500.0, "unpriced": 1},
            ],
        )

    def test_another_companys_returns_stay_out(self):
        self.gr("GR-9", arrived=timezone.make_aware(datetime(2026, 9, 14, 11)), company=self.bev,
                lines=[("DAMAGED", "10", "150")])

        self.assertEqual(self.day(self.report())["returns"], [])


class AccessTests(ReportTestCase):
    def user_with(self, *perms):
        user = User.objects.create_user(email=f"u{User.objects.count()}@x.test", full_name="U", password="x")
        for app_label, codename in perms:
            user.user_permissions.add(
                Permission.objects.get(content_type__app_label=app_label, codename=codename)
            )
        UserCompany.objects.create(
            user=user, company=self.oil, role=UserRole.objects.get_or_create(name="Admin")[0],
            is_default=True, is_active=True,
        )
        return User.objects.get(pk=user.pk)

    def get(self, user, **params):
        client = APIClient()
        client.force_authenticate(user=user)
        client.credentials(HTTP_COMPANY_CODE="JIVO_OIL")
        with patch("maintenance.electricity.boards.breakdown", return_value=no_power()), patch(
            "maintenance.electricity.boards.company", return_value=no_power()
        ):
            return client.get(reverse("operations_report:operations-report-days"), params)

    def test_the_expense_right_alone_opens_the_report_and_withholds_production(self):
        user = self.user_with(("factory_expense", "can_view_factory_expense"))

        response = self.get(user, **{"from": "2026-09-14", "to": "2026-09-14"})

        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(
            sorted(response.data["meta"]["withheld"]), ["production", "returns", "wastage"]
        )
        self.assertIsNone(response.data["days"][0]["lines"])
        self.assertEqual(response.data["days"][0]["labour"], [])

    def test_the_run_cost_right_alone_gets_production_and_not_the_wage_bill(self):
        user = self.user_with(("production_execution", "can_view_run_cost"))

        response = self.get(user, **{"from": "2026-09-14", "to": "2026-09-14"})

        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(
            sorted(response.data["meta"]["withheld"]), ["labour", "power", "returns", "salary"]
        )

    def test_neither_right_is_refused(self):
        response = self.get(self.user_with(), **{"from": "2026-09-14", "to": "2026-09-14"})

        self.assertEqual(response.status_code, 403)

    def test_bad_spans_are_refused(self):
        user = self.user_with(("factory_expense", "can_view_factory_expense"))
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()

        for params in (
            {"from": "2026-09-14"},
            {"from": "2026-09-15", "to": "2026-09-14"},
            {"from": "2026-09-14", "to": tomorrow},
            {"from": "2026-05-01", "to": "2026-09-14"},
        ):
            with self.subTest(params=params):
                self.assertEqual(self.get(user, **params).status_code, 400)
