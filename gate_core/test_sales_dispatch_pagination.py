"""Numbered-page pagination on the docking board list endpoint.

``GET /sales-dispatch/`` stays a plain array when no ``page`` is sent (export,
gate-out board, other callers), and switches to a ``{count, num_pages, page,
page_size, results}`` envelope -- ordered newest planned-dispatch first -- when
``page`` is present.
"""
from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
from driver_management.models import Driver, VehicleEntry
from gate_core.models import (
    SalesDispatchDocumentType,
    SalesDispatchGateOut,
    SalesDispatchGateOutDocument,
    SalesDispatchGateOutStatus,
)
from vehicle_management.models import Transporter, Vehicle

LIST_URL = "/api/v1/gate-core/sales-dispatch/"


class SalesDispatchPaginationTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Beverages", code="JIVO_BEV")
        self.role = UserRole.objects.create(name="Gate")
        self.user = get_user_model().objects.create_user(
            email="pg@example.com", password="p", full_name="PG", employee_code="PG1",
        )
        UserCompany.objects.create(
            user=self.user, company=self.company, role=self.role, is_default=True
        )
        self.user.user_permissions.add(
            *Permission.objects.filter(content_type__app_label="gate_core")
        )
        self.transporter = Transporter.objects.create(name="T")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="DL01LY5728", transporter=self.transporter
        )
        self.driver = Driver.objects.create(name="D", mobile_no="9000000000", license_no="DL-1")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)

        # Five dockings on ascending planned dispatch dates. Newest-first ordering
        # means DK-5 (Jan 5) should lead and DK-1 (Jan 1) trail.
        self.entry_nos = []
        for i in range(1, 6):
            de = 20000 + i
            num = f"6260780{i:02d}"
            plan = DispatchPlan.objects.create(
                company=self.company, sap_invoice_doc_entry=de, sap_invoice_doc_num=num,
                booking_status=DispatchPlanStatus.BOOKED, vehicle=self.vehicle,
                dispatch_date=date(2026, 1, i), created_by=self.user, updated_by=self.user,
            )
            ve = VehicleEntry.objects.create(
                entry_no=f"DOCKV-{i}", company=self.company, vehicle=self.vehicle,
                driver=self.driver, entry_type="SALES_DISPATCH", status="IN_PROGRESS",
                created_by=self.user, updated_by=self.user,
            )
            docking = SalesDispatchGateOut.objects.create(
                company=self.company, entry_no=f"DK-{i}", vehicle_entry=ve, dispatch_plan=plan,
                vehicle=self.vehicle, transporter=self.transporter, driver=self.driver,
                document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=de, sap_doc_num=num,
                status=SalesDispatchGateOutStatus.DOCKED, created_by=self.user, updated_by=self.user,
            )
            SalesDispatchGateOutDocument.objects.create(
                sales_dispatch=docking, company=self.company, dispatch_plan=plan,
                document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=de, sap_doc_num=num,
                created_by=self.user, updated_by=self.user,
            )
            self.entry_nos.append(f"DK-{i}")

    def test_no_page_param_returns_plain_array(self):
        resp = self.client.get(LIST_URL, {"all_companies": 1})
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.data, list)
        self.assertEqual(len(resp.data), 5)

    def test_paginated_envelope_shape_and_counts(self):
        resp = self.client.get(LIST_URL, {"all_companies": 1, "page": 1, "page_size": 2})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["count"], 5)
        self.assertEqual(resp.data["num_pages"], 3)
        self.assertEqual(resp.data["page"], 1)
        self.assertEqual(resp.data["page_size"], 2)
        self.assertEqual(len(resp.data["results"]), 2)

    def test_newest_dispatch_date_leads_and_pages_are_disjoint(self):
        page1 = self.client.get(
            LIST_URL, {"all_companies": 1, "page": 1, "page_size": 2}
        ).data["results"]
        page2 = self.client.get(
            LIST_URL, {"all_companies": 1, "page": 2, "page_size": 2}
        ).data["results"]
        page3 = self.client.get(
            LIST_URL, {"all_companies": 1, "page": 3, "page_size": 2}
        ).data["results"]

        ordered = [row["entry_no"] for row in page1 + page2 + page3]
        self.assertEqual(ordered, ["DK-5", "DK-4", "DK-3", "DK-2", "DK-1"])
        # No row appears on two pages.
        self.assertEqual(len(set(ordered)), 5)

    def test_out_of_range_page_clamps_to_last(self):
        resp = self.client.get(LIST_URL, {"all_companies": 1, "page": 99, "page_size": 2})
        self.assertEqual(resp.status_code, 200)
        # Paginator.get_page clamps an over-run to the final page.
        self.assertEqual(resp.data["page"], 3)
        self.assertEqual(len(resp.data["results"]), 1)
        self.assertEqual(resp.data["results"][0]["entry_no"], "DK-1")
