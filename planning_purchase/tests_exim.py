"""SAP's open POs and the monthly plan workbook, moved here from EXIM.

  - open PO lines come back with their days open and overdue, and the rights
    are this module's own or EXIM's;
  - a plan upload is the next version of its month, its figures recomputed
    from the weeks, a disagreement reported; the latest is the newest version
    of the newest month; removing one needs its own right;
  - EXIM's uploads copy across once, and an upload made here is not replaced.
"""

import io
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
from openpyxl import Workbook
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole

from . import open_pos
from .management.commands.import_exim_monthly_plans import import_plans
from .models_monthly_plan import MonthlyPlanRow, MonthlyPlanUpload

D = Decimal
BASE = "/api/v1/planning-purchase/"
TODAY = timezone.localdate()


def plan_workbook(month="OCT 2026", premium_w1=100, sheet_total=None):
    """The planning team's layout: a banner with the month and the COMMODITY /
    PREMIUM labels over their blocks, a totals strip, the header, the SKUs."""
    wb = Workbook()
    ws = wb.active
    header = ["CODE", "BRAND", "HEAD", "CATEGORY", "SUB-CATEGORY", "SKU", "PER LTRS", "LTRS/BOX", "CASE PACK",
              "MONTHLY PLANNING", "1ST WEEK", "2ND WEEK", "3RD WEEK", "4TH WEEK",
              "MONTHLY PLANNING", "1ST WEEK", "2ND WEEK", "3RD WEEK", "4TH WEEK", "ECOM PLANNING", "TOTAL PLANNING"]
    banner = [f"PRODUCTION PLANING MONTH OF {month}"] + [None] * 8 + ["COMMODITY"] * 5 + ["PREMIUM"] * 5 + [None, None]
    ws.append(banner)
    ws.append([None] * len(header))
    ws.append(header)
    ws.append(["FG1", "JIVO", "COMMODITY", "OIL", "SOYA", "Soya 1L", 1, 12, 12,
               400, 100, 100, 100, 100, 0, 0, 0, 0, 0, 50, 450])
    ws.append(["FG2", "JIVO", "PREMIUM", "OIL", "OLIVE", "Olive 1L", 1, 12, 12,
               0, 0, 0, 0, 0, 400, premium_w1, 100, 100, 100, 0, sheet_total if sheet_total is not None else 400])
    buffer = io.BytesIO()
    wb.save(buffer)
    return SimpleUploadedFile("plan.xlsx", buffer.getvalue(),
                              content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


class Mixin:
    def setUp(self):
        super().setUp()
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.role = UserRole.objects.create(name="Planning")
        self.headers = {"HTTP_COMPANY_CODE": "JIVO_OIL"}

    def client_for(self, *rights):
        n = get_user_model().objects.count() + 1
        user = get_user_model().objects.create_user(email=f"p{n}@example.com", password="x", full_name="P",
                                                    employee_code=f"PP{n}")
        UserCompany.objects.create(user=user, company=self.company, role=self.role, is_default=True)
        for right in rights:
            label, codename = right.split(".")
            user.user_permissions.add(Permission.objects.get(content_type__app_label=label, codename=codename))
        client = APIClient()
        client.force_authenticate(user)
        return client


class OpenPOTests(Mixin, APITestCase):
    LINE = {
        "doc_entry": 1, "po_number": "220926106", "po_date": TODAY - timedelta(days=12),
        "due_date": TODAY - timedelta(days=2), "ship_date": None, "vendor_code": "V", "vendor_name": "V",
        "vendor_ref": "", "raised_by": "LOVPREET SINGH", "line": 0, "item_code": "RM0000066", "item_name": "PEANUT",
        "item_group": "RAW MATERIAL", "unit": "MTS", "warehouse": "BH-GJ", "ordered": D("82"), "received": D("40"),
        "open_qty": D("42"), "price": D("170000"), "currency": "INR", "open_value": D("7140000.00"),
    }

    def test_open_lines_with_days_and_the_rights_that_open_them(self):
        with mock.patch("planning_purchase.open_pos.read_open_po_lines", return_value=[dict(self.LINE)]):
            data = self.client_for("exim.view_open_pos").get(f"{BASE}open-pos/", **self.headers).data
            self.assertEqual(self.client_for("planning_purchase.can_view_open_pos")
                             .get(f"{BASE}open-pos/", **self.headers).status_code, 200)
            self.assertEqual(self.client_for("planning_purchase.can_view_production_plan")
                             .get(f"{BASE}open-pos/", **self.headers).status_code, 403)
        row = data["rows"][0]
        self.assertEqual((row["days_open"], row["overdue_days"]), (12, 2))
        self.assertEqual(data["totals"]["open_value"], D("7140000.00"))
        self.assertEqual(data["raised_by"], ["LOVPREET SINGH"])


class MonthlyPlanTests(Mixin, APITestCase):
    def upload(self, client, **kw):
        return client.post(f"{BASE}monthly-plans/", {"file": plan_workbook(**kw)}, format="multipart",
                           **self.headers)

    def test_an_upload_is_the_next_version_of_its_month(self):
        planner = self.client_for("planning_purchase.can_upload_monthly_plan")
        first = self.upload(planner)
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual((first.data["upload"]["month"], first.data["upload"]["version"]), ("2026-10-01", 1))
        self.assertEqual(D(first.data["upload"]["grand_total"]), D("850"))
        second = self.upload(planner)
        self.assertEqual((second.data["upload"]["version"], second.data["replaced_version"]), (2, 1))
        latest = planner.get(f"{BASE}monthly-plans/latest/", **self.headers).data
        self.assertEqual((latest["version"], latest["is_latest"], len(latest["rows"])), (2, True, 2))
        listing = planner.get(f"{BASE}monthly-plans/", **self.headers).data["uploads"]
        self.assertEqual([(u["version"], u["is_latest"]) for u in listing], [(2, True), (1, False)])

    def test_figures_follow_the_weeks_and_a_disagreement_is_reported(self):
        response = self.upload(self.client_for("exim.add_planningupload"), premium_w1=150, sheet_total=400)
        self.assertEqual(response.status_code, 201)
        self.assertTrue(any("premium monthly" in m for m in response.data["mismatches"]))
        row = MonthlyPlanRow.objects.get(code="FG2")
        self.assertEqual((row.premium_monthly, row.total_planning), (D("450.00"), D("450.00")))

    def test_viewing_uploading_and_removing_are_separate(self):
        viewer = self.client_for("planning_purchase.can_view_production_plan")
        self.assertEqual(self.upload(viewer).status_code, 403)
        planner = self.client_for("exim.add_planningupload")
        pk = self.upload(planner).data["upload"]["id"]
        self.assertEqual(viewer.get(f"{BASE}monthly-plans/{pk}/", **self.headers).status_code, 200)
        self.assertEqual(planner.delete(f"{BASE}monthly-plans/{pk}/", **self.headers).status_code, 403)
        remover = self.client_for("exim.delete_planningupload")
        self.assertEqual(remover.delete(f"{BASE}monthly-plans/{pk}/", **self.headers).status_code, 200)
        self.assertFalse(MonthlyPlanUpload.objects.exists())

    def test_a_sheet_that_names_no_month_needs_one(self):
        response = self.upload(self.client_for("planning_purchase.can_upload_monthly_plan"), month="NEXT MONTH")
        self.assertEqual(response.data["code"], "month_unknown")


class EximPlanCopyTests(Mixin, TestCase):
    def snapshot(self):
        row = {f: D("0") for f in (
            "commodity_monthly", "commodity_w1", "commodity_w2", "commodity_w3", "commodity_w4", "premium_monthly",
            "premium_w1", "premium_w2", "premium_w3", "premium_w4", "ecom_planning", "total_planning")}
        row.update(upload_id=7, code="FG1", brand="JIVO", head="COMMODITY", category="OIL", sub_category="SOYA",
                   sku="Soya", per_ltrs=D("1"), ltrs_per_box=D("12"), case_pack=D("12"), commodity_monthly=D("400"),
                   commodity_w1=D("400"), total_planning=D("400"), source_row=4)
        return {
            "uploads": [{"id": 7, "month": date(2026, 9, 1), "version": 1, "title": "SEP", "source_file": "sep.xlsx",
                         "uploaded_by": "ravinder@exim.com", "uploaded_at": datetime(2026, 8, 27, tzinfo=dt_timezone.utc),
                         "notes": ""}],
            "rows": [row],
        }

    def test_exims_uploads_copy_once(self):
        report = import_plans(self.snapshot(), company=self.company)
        self.assertEqual((report["counts"]["create"], report["counts"]["rows"]), (1, 1))
        upload = MonthlyPlanUpload.objects.get()
        self.assertEqual((upload.uploaded_by_label, upload.grand_total, upload.exim_id), ("ravinder@exim.com",
                                                                                         D("400.00"), 7))
        self.assertEqual(import_plans(self.snapshot(), company=self.company)["counts"]["kept"], 1)

    def test_a_version_uploaded_here_is_not_replaced(self):
        MonthlyPlanUpload.objects.create(company=self.company, month=date(2026, 9, 1), version=1, source_file="x")
        report = import_plans(self.snapshot(), company=self.company)
        self.assertEqual(report["counts"]["clash"], 1)
        self.assertEqual(MonthlyPlanUpload.objects.count(), 1)
