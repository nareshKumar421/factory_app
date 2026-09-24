"""Docking one company on a cross-company truck docks every company on it.

The split this guards against: the docking page decided "this truck carries
several companies" from a list it fetched separately, so a Save before that list
loaded docked one company only. The rest came back later as a fresh "pending at
dock" row and went out on a second gatepass. The backend now docks the whole
truck, and the truck-photo lock refuses while any company on it is un-docked.
"""
import datetime as dt
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
from driver_management.models import Driver
from gate_core.models import SalesDispatchGateOut, SalesDispatchGateOutStatus
from gate_core.services.empty_vehicle_dispatch import create_vehicle_arrival
from gate_core.services.sales_dispatch_docking import undocked_booked_bills
from gate_core.services.sales_dispatch_documents import SalesDispatchDocumentService
from gate_core.views_sales_dispatch import pending_dispatch_plan_queryset_for_companies
from sap_client.exceptions import SAPConnectionError
from vehicle_management.models import Transporter, Vehicle

OIL_DOC = 91001
MART_DOC = 91002


def _fake_document(doc_entry):
    return {
        "document_type": "INVOICE",
        "doc_entry": doc_entry,
        "doc_num": str(doc_entry),
        "doc_total": "500.00",
        "branch_id": 1,
        "card_code": f"CUST{doc_entry}",
        "card_name": f"Customer {doc_entry}",
        "total_boxes": "10.000",
        "total_quantity": "10.000",
        "items": [
            {
                "line_num": 0,
                "item_code": "ITEM",
                "item_name": "Item",
                "quantity": "10.000",
                "uom": "BOX",
            }
        ],
    }


@override_settings(LATE_DISPATCH_GATE_IN_CUTOFF=dt.time(23, 59, 59))
class TruckDockingFanOutTests(TestCase):
    def setUp(self):
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.mart = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        self.role = UserRole.objects.create(name="Gate")
        self.user = get_user_model().objects.create_user(
            email="fanout@example.com", password="p", full_name="Fan Out", employee_code="FAN1",
        )
        self.user.user_permissions.add(*Permission.objects.filter(content_type__app_label="gate_core"))
        self.memberships = {
            company.id: UserCompany.objects.create(
                user=self.user, company=company, role=self.role, is_active=True
            )
            for company in (self.oil, self.mart)
        }
        self.vehicle = Vehicle.objects.create(
            vehicle_number="DL01FAN0001", transporter=Transporter.objects.create(name="T")
        )
        self.driver = Driver.objects.create(name="D", mobile_no="9000000000", license_no="DL-FAN-1")
        self.oil_plan = self._booked(self.oil, OIL_DOC)
        self.mart_plan = self._booked(self.mart, MART_DOC)
        self.arrival = create_vehicle_arrival(
            vehicle=self.vehicle,
            driver=self.driver,
            company_ids=[self.oil.id, self.mart.id],
            gate_in_date=timezone.localdate(),
            in_time=dt.time(9, 0),
            tare_weight=Decimal("1500.000"),
            user=self.user,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _booked(self, company, doc_entry):
        return DispatchPlan.objects.create(
            company=company,
            sap_invoice_doc_entry=doc_entry,
            sap_invoice_doc_num=str(doc_entry),
            booking_status=DispatchPlanStatus.BOOKED,
            dispatch_date=timezone.localdate(),
            vehicle=self.vehicle,
            created_by=self.user,
            updated_by=self.user,
        )

    def _dock(self, plan, get_document=None):
        get_document = get_document or (lambda document_type, doc_entry: _fake_document(doc_entry))
        with mock.patch.object(SalesDispatchDocumentService, "get_document", side_effect=get_document):
            return self.client.post(
                "/api/v1/gate-core/sales-dispatch/",
                {
                    "document_type": "INVOICE",
                    "sap_doc_entry": plan.sap_invoice_doc_entry,
                    "documents": [
                        {
                            "document_type": "INVOICE",
                            "sap_doc_entry": plan.sap_invoice_doc_entry,
                            "dispatch_plan_id": plan.id,
                        }
                    ],
                    "vehicle_id": self.vehicle.id,
                    "driver_id": self.driver.id,
                    "dispatch_plan_id": plan.id,
                },
                format="json",
                HTTP_COMPANY_CODE=self.mart.code,
            )

    def _dockings(self):
        return {
            docking.company.code: docking
            for docking in SalesDispatchGateOut.objects.filter(
                vehicle=self.vehicle, is_active=True
            ).select_related("company")
        }

    def _pending(self):
        return set(
            pending_dispatch_plan_queryset_for_companies([self.oil.id, self.mart.id]).values_list(
                "id", flat=True
            )
        )

    def test_docking_one_company_docks_the_whole_truck(self):
        response = self._dock(self.mart_plan)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["warnings"], [])
        dockings = self._dockings()
        self.assertEqual(set(dockings), {"JIVO_OIL", "JIVO_MART"})
        self.assertEqual({d.arrival_id for d in dockings.values()}, {self.arrival.id})
        self.assertEqual(
            list(dockings["JIVO_OIL"].documents.values_list("sap_doc_entry", flat=True)), [OIL_DOC]
        )
        self.assertEqual(self._pending(), set())  # nothing left "pending at dock"
        self.assertEqual(undocked_booked_bills(dockings["JIVO_MART"]), [])

    def test_a_client_docking_company_by_company_gets_the_same_docking_back(self):
        first = self._dock(self.mart_plan)
        oil_docking = self._dockings()["JIVO_OIL"]

        second = self._dock(self.oil_plan)  # the frontend's per-company loop, second pass

        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(second.data["id"], oil_docking.id)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(SalesDispatchGateOut.objects.filter(vehicle=self.vehicle).count(), 2)

    def test_sap_down_for_a_sibling_leaves_it_pending_and_locks_the_photo(self):
        def get_document(document_type, doc_entry):
            if doc_entry == OIL_DOC:
                raise SAPConnectionError("down")
            return _fake_document(doc_entry)

        response = self._dock(self.mart_plan, get_document=get_document)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            [w["code"] for w in response.data["warnings"]], ["TRUCK_COMPANY_NOT_DOCKED"]
        )
        mart_docking = self._dockings()["JIVO_MART"]
        self.assertNotIn("JIVO_OIL", self._dockings())
        self.assertEqual(self._pending(), {self.oil_plan.id})
        # The Mart docking alone is complete, but the truck is not: the lock sees Oil.
        self.assertEqual(
            undocked_booked_bills(mart_docking),
            [{"sap_doc_entry": OIL_DOC, "sap_doc_num": str(OIL_DOC), "company_code": "JIVO_OIL"}],
        )
        photo = self.client.post(
            f"/api/v1/gate-core/sales-dispatch/{mart_docking.id}/attachments/",
            {
                "attachment_type": "TRUCK_PHOTO",
                "file": SimpleUploadedFile("truck.jpg", b"img", content_type="image/jpeg"),
                "latitude": "28.6",
                "longitude": "77.2",
            },
            format="multipart",
            HTTP_COMPANY_CODE=self.mart.code,
        )
        self.assertEqual(photo.status_code, 400, photo.data)
        self.assertTrue(photo.data["requires_partial_override"])
        self.assertIn("91001 (JIVO_OIL)", photo.data["detail"])
        mart_docking.refresh_from_db()
        self.assertEqual(mart_docking.status, SalesDispatchGateOutStatus.DOCKED)

    def test_a_company_the_user_is_not_in_is_left_pending_with_a_warning(self):
        self.memberships[self.oil.id].delete()

        response = self._dock(self.mart_plan)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            [w["code"] for w in response.data["warnings"]], ["TRUCK_COMPANY_NOT_DOCKED"]
        )
        self.assertEqual(set(self._dockings()), {"JIVO_MART"})
        self.assertEqual(self._pending(), {self.oil_plan.id})
