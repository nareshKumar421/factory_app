"""The Dispatch Sheet register.

What only this endpoint can get wrong: which day-window it defaults to, which
of the two sheets a row lands on, that the plan's own figures beat SAP's, and
that SAP being down leaves a readable register rather than an error page.
"""

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPConnectionError
from vehicle_management.models import Transporter, Vehicle, VehicleType

from .models import DispatchPlan, DispatchPlanStatus
from .views_sheet import STREAM_OIL, STREAM_WATER, _stream_of

User = get_user_model()


class DispatchSheetAPITests(TestCase):
    URL = "/api/v1/dispatch-plans/sheet/"

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        role = UserRole.objects.create(name="Dispatch desk")
        self.user = User.objects.create_user(
            email="sheet@example.com",
            password="testpass123",
            full_name="Sheet Keeper",
            employee_code="SHEET01",
        )
        UserCompany.objects.create(
            user=self.user, company=self.company, role=role, is_active=True
        )
        self.user.user_permissions.add(
            Permission.objects.get(codename="can_view_dispatch_sheet")
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

        vehicle_type = VehicleType.objects.create(name="Truck")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="HR69F9627", vehicle_type=vehicle_type
        )
        self.transporter = Transporter.objects.create(
            name="Delhi Punjab", mobile_no="9872987038"
        )

    def _plan(self, doc_entry, **kwargs):
        defaults = dict(
            company=self.company,
            sap_invoice_doc_entry=doc_entry,
            sap_invoice_doc_num=str(626030000 + doc_entry),
            dispatch_date=date(2026, 4, 1),
            booking_status=DispatchPlanStatus.DISPATCHED,
            customer_name="RK World",
            vehicle=self.vehicle,
            transporter=self.transporter,
        )
        defaults.update(kwargs)
        return DispatchPlan.objects.create(**defaults)

    def _get(self, **params):
        return self.client.get(
            self.URL, params, HTTP_COMPANY_CODE=self.company.code
        )

    def _no_sap(self):
        """SAP unreachable, so only the plan's own half of each row is filled."""
        mock = MagicMock()
        mock.side_effect = SAPConnectionError("HANA is asleep")
        return patch("dispatch_plans.views_sheet.DispatchPlansService", mock)

    def _sap(self, enrichment):
        service = MagicMock()
        service.get_sheet_enrichment.return_value = enrichment
        return patch(
            "dispatch_plans.views_sheet.DispatchPlansService",
            MagicMock(return_value=service),
        )

    # -- the window -----------------------------------------------------------

    def test_window_is_on_the_dispatch_date_not_the_invoice_date(self):
        self._plan(1, dispatch_date=date(2026, 4, 1))
        self._plan(2, dispatch_date=date(2026, 4, 9))

        with self._no_sap():
            response = self._get(date_from="2026-04-01", date_to="2026-04-05")

        self.assertEqual(response.status_code, 200)
        entries = [row["sap_invoice_doc_entry"] for row in response.json()["data"]]
        self.assertEqual(entries, [1])

    def test_cancelled_plans_are_not_lines_of_the_register(self):
        self._plan(1)
        self._plan(2, booking_status=DispatchPlanStatus.CANCELLED)

        with self._no_sap():
            response = self._get(date_from="2026-04-01", date_to="2026-04-30")

        entries = [row["sap_invoice_doc_entry"] for row in response.json()["data"]]
        self.assertEqual(entries, [1])

    def test_a_cancelled_plan_can_still_be_asked_for_by_name(self):
        self._plan(2, booking_status=DispatchPlanStatus.CANCELLED)

        with self._no_sap():
            response = self._get(
                date_from="2026-04-01", date_to="2026-04-30", booking_status="CANCELLED"
            )

        entries = [row["sap_invoice_doc_entry"] for row in response.json()["data"]]
        self.assertEqual(entries, [2])

    def test_a_backwards_window_is_refused(self):
        with self._no_sap():
            response = self._get(date_from="2026-04-30", date_to="2026-04-01")
        self.assertEqual(response.status_code, 400)

    # -- the row --------------------------------------------------------------

    def test_row_carries_every_column_the_workbook_has(self):
        self._plan(
            1,
            bilty_no="12054",
            priority="High",
            kanta_weight=Decimal("11996.000"),
            freight=Decimal("2.50"),
            total_freight=Decimal("29990.00"),
            remarks="Lucky- 8130168713",
            location="EMPORIUM INDUSTRIAL PARKS",
            place_of_supply="Haryana",
        )

        with self._sap(
            {
                1: {
                    "invoice_date": "2026-03-31",
                    "card_name": "SAP says somebody else",
                    "ship_to_address": "SAP says somewhere else",
                    "state": "SAP says somewhere else",
                    "total_litres": 11996.0,
                    "total_boxes": 620.0,
                    "item_summary": "JIVO CANOLA OIL 1 LTR",
                }
            }
        ):
            response = self._get(date_from="2026-04-01", date_to="2026-04-30")

        row = response.json()["data"][0]
        self.assertEqual(row["dispatch_date"], "2026-04-01")
        self.assertEqual(row["invoice_date"], "2026-03-31")
        # The plan's own figures win: the desk edits those, and a correction
        # typed there is the point of it being editable.
        self.assertEqual(row["party"], "RK World")
        self.assertEqual(row["location"], "EMPORIUM INDUSTRIAL PARKS")
        self.assertEqual(row["state"], "Haryana")
        self.assertEqual(row["invoice_no"], "626030001")
        self.assertEqual(row["bilty_no"], "12054")
        self.assertEqual(row["vehicle_no"], "HR69F9627")
        self.assertEqual(row["transport_name"], "Delhi Punjab")
        self.assertEqual(row["mobile_no"], "9872987038")
        self.assertEqual(row["litres"], 11996.0)
        self.assertEqual(row["total_boxes"], 620.0)
        self.assertEqual(row["priority"], "High")
        self.assertEqual(row["kanta_weight"], 11996.0)
        self.assertEqual(row["freight"], 2.5)
        self.assertEqual(row["total_freight"], 29990.0)
        self.assertEqual(row["remarks"], "Lucky- 8130168713")

    def test_sap_fills_only_the_cells_the_plan_left_empty(self):
        self._plan(1, customer_name="", location="", place_of_supply="")

        with self._sap(
            {
                1: {
                    "invoice_date": "2026-03-31",
                    "card_name": "CHIRAG ENTERPRISES MUMBAI",
                    "ship_to_address": "ANJUR MANKOLI ROAD, BHIWANDI",
                    "state": "MH",
                    "total_litres": 10913.0,
                    "total_boxes": 500.0,
                    "item_summary": "JIVO OLIVE OIL 5 LTR",
                }
            }
        ):
            response = self._get(date_from="2026-04-01", date_to="2026-04-30")

        row = response.json()["data"][0]
        self.assertEqual(row["party"], "CHIRAG ENTERPRISES MUMBAI")
        self.assertEqual(row["location"], "ANJUR MANKOLI ROAD, BHIWANDI")
        self.assertEqual(row["state"], "MH")

    def test_register_still_reads_when_sap_is_down(self):
        self._plan(1, bilty_no="1756")

        with self._no_sap():
            response = self._get(date_from="2026-04-01", date_to="2026-04-30")

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(body["meta"]["sap_available"])
        row = body["data"][0]
        self.assertEqual(row["bilty_no"], "1756")
        self.assertIsNone(row["invoice_date"])
        self.assertIsNone(row["total_boxes"])

    # -- which of the two sheets ----------------------------------------------

    def test_water_rows_are_told_from_oil_rows_by_what_was_carried(self):
        self._plan(1)
        self._plan(2)

        with self._sap(
            {
                1: {"item_summary": "JIVO CANOLA OIL 1 LTR", "total_litres": 100},
                2: {"item_summary": "JIVO NATURAL MINERAL WATER 1 LTR", "total_litres": 200},
            }
        ):
            response = self._get(date_from="2026-04-01", date_to="2026-04-30")

        streams = {
            row["sap_invoice_doc_entry"]: row["stream"] for row in response.json()["data"]
        }
        self.assertEqual(streams, {1: STREAM_OIL, 2: STREAM_WATER})

    def test_one_sheet_can_be_asked_for_on_its_own(self):
        self._plan(1)
        self._plan(2)

        with self._sap(
            {
                1: {"item_summary": "JIVO CANOLA OIL 1 LTR"},
                2: {"item_summary": "JIVO WATER 1 LTR"},
            }
        ):
            response = self._get(
                date_from="2026-04-01", date_to="2026-04-30", stream="water"
            )

        body = response.json()
        self.assertEqual([row["sap_invoice_doc_entry"] for row in body["data"]], [2])
        self.assertEqual(body["meta"]["water_count"], 1)
        self.assertEqual(body["meta"]["oil_count"], 0)

    def test_a_plan_made_before_sap_answers_falls_back_to_its_stamped_variety(self):
        plan = self._plan(1, product_variety="Beverage")
        self.assertEqual(_stream_of(plan, ""), STREAM_WATER)
        # A live reading of what was carried beats the stamp.
        self.assertEqual(_stream_of(plan, "JIVO MUSTARD OIL 1 LTR"), STREAM_OIL)

    # -- who may read it ------------------------------------------------------

    def test_the_register_is_closed_to_a_user_with_no_dispatch_right(self):
        stranger = User.objects.create_user(
            email="stranger@example.com",
            password="testpass123",
            full_name="Stranger",
            employee_code="STR01",
        )
        UserCompany.objects.create(
            user=stranger,
            company=self.company,
            role=UserRole.objects.create(name="Nobody"),
            is_active=True,
        )
        self.client.force_authenticate(stranger)

        with self._no_sap():
            response = self._get(date_from="2026-04-01", date_to="2026-04-30")

        self.assertEqual(response.status_code, 403)
