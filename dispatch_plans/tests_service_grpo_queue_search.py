"""Finding a bilty in the Service GRPO queue by any invoice printed on it.

One bilty covers N invoices but the queue shows ONE row per bilty, keyed on a
representative plan. The search haystack used to be built from that
representative's invoice number alone, so the other invoices on the same bilty
matched nothing -- an operator holding Mart bilty 13183 and typing 608260321 off
the paper got an empty queue and concluded the consignment never arrived, while
the row was sitting there under 608260307.
"""
from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
from grpo.services import GRPOService
from vehicle_management.models import Transporter, Vehicle, VehicleType

User = get_user_model()

URL = "/api/v1/dispatch/bilty-grpo/pending/"

# The four Mart invoices that travelled on bilty 13183, Panipat -> Chandigarh.
BILTY = "13183"
BILTY_DATE = date(2026, 8, 24)
INVOICES = ["608260307", "608260309", "608260319", "608260321"]


class ServiceGRPOQueueSearchTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Mart", code="JIVO_MART_Q")
        self.role = UserRole.objects.create(name="Dispatch")
        self.user = User.objects.create(
            email="queue@example.com",
            employee_code="Q1",
            full_name="Queue Tester",
            is_active=True,
        )
        UserCompany.objects.create(
            user=self.user, company=self.company, role=self.role, is_default=True
        )
        self.user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="dispatch_plans",
                codename="can_post_bilty_service_grpo",
            )
        )

        vehicle_type = VehicleType.objects.create(name="TRUCK")
        self.transporter = Transporter.objects.create(name="Delhi Punjab")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="HR67E3663",
            vehicle_type=vehicle_type,
            transporter=self.transporter,
        )

        # All four invoices on one bilty, one truck: exactly one queue row.
        self.plans = [
            DispatchPlan.objects.create(
                company=self.company,
                sap_invoice_doc_entry=38600 + index,
                sap_invoice_doc_num=doc_num,
                booking_status=DispatchPlanStatus.DISPATCHED,
                dispatch_date=BILTY_DATE,
                vehicle=self.vehicle,
                transporter=self.transporter,
                bilty_no=BILTY,
                bilty_date=BILTY_DATE,
                created_by=self.user,
                updated_by=self.user,
            )
            for index, doc_num in enumerate(INVOICES)
        ]

        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.hdr = {"HTTP_COMPANY_CODE": self.company.code}
        self.service = GRPOService(company_code=self.company.code)

    def _get(self, **params):
        """The queue as the page sees it, with the per-page SAP snapshot stubbed."""
        query = {"year": 2026, "month": 8, **params}
        with patch.object(GRPOService, "get_dispatch_bill_snapshots", return_value={}):
            return self.client.get(URL, query, **self.hdr)

    # ------------------------------------------------------------------ #
    # the group carries every invoice on the bilty
    # ------------------------------------------------------------------ #
    def test_the_group_row_knows_every_invoice_on_the_bilty(self):
        entries = self.service.get_pending_service_grpo_entries(year=2026, month=8)

        self.assertEqual(len(entries), 1, "four invoices on one bilty is one row")
        self.assertEqual(
            sorted(getattr(entries[0], "_service_group_invoice_numbers")),
            sorted(INVOICES),
        )
        self.assertEqual(getattr(entries[0], "_service_group_invoice_count"), 4)

    def test_the_queue_reports_the_full_invoice_list(self):
        response = self._get()

        self.assertEqual(response.status_code, 200)
        rows = response.data["results"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(sorted(rows[0]["invoice_numbers"]), sorted(INVOICES))
        self.assertEqual(rows[0]["invoice_count"], 4)

    # ------------------------------------------------------------------ #
    # searchable by any of them
    # ------------------------------------------------------------------ #
    def test_searching_any_invoice_on_the_bilty_finds_the_row(self):
        for doc_num in INVOICES:
            with self.subTest(invoice=doc_num):
                response = self._get(search=doc_num)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    len(response.data["results"]),
                    1,
                    f"invoice {doc_num} is on bilty {BILTY} and must find its row",
                )
                self.assertEqual(response.data["results"][0]["bilty_no"], BILTY)

    def test_searching_the_bilty_number_still_finds_the_row(self):
        response = self._get(search=BILTY)

        self.assertEqual(len(response.data["results"]), 1)

    def test_an_invoice_on_no_bilty_here_finds_nothing(self):
        """Widening the haystack must not turn the search into a match-all."""
        response = self._get(search="999999999")

        self.assertEqual(len(response.data["results"]), 0)

    # ------------------------------------------------------------------ #
    # a lone invoice is still findable
    # ------------------------------------------------------------------ #
    def test_a_single_invoice_bilty_is_searchable_by_its_own_number(self):
        DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=40001,
            sap_invoice_doc_num="608260400",
            booking_status=DispatchPlanStatus.DISPATCHED,
            dispatch_date=BILTY_DATE,
            vehicle=self.vehicle,
            transporter=self.transporter,
            bilty_no="13999",
            bilty_date=BILTY_DATE,
            created_by=self.user,
            updated_by=self.user,
        )

        response = self._get(search="608260400")

        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["bilty_no"], "13999")

    # ------------------------------------------------------------------ #
    # the month scope
    # ------------------------------------------------------------------ #
    def test_a_dispatched_bilty_is_scoped_to_its_dispatch_month(self):
        """Why the row 'disappears': the queue defaults to the current month.

        Deliberate -- the dispatched backlog is unbounded and each row costs a SAP
        snapshot -- but it is the first reason an August bilty looks absent in
        September, so it is pinned here rather than left as folklore.
        """
        found = self._get(search=BILTY, year=2026, month=8)
        self.assertEqual(len(found.data["results"]), 1)

        missing = self._get(search=BILTY, year=2026, month=9)
        self.assertEqual(len(missing.data["results"]), 0)
