"""Late bill -> existing open docking merge (auto at linking + manual fallback).

A truck gated in for several bills should end up as ONE docking. When a bill is
booked after its truck's docking already exists, it must fold into that docking
instead of sitting as a separate pending row — but only before the load is
photo-locked, only for the same physical truck, and never twice.
"""

from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
from driver_management.models import Driver, VehicleEntry
from gate_core.models import (
    EmptyVehicleGateIn,
    EmptyVehicleGateInCover,
    SalesDispatchBoxScan,
    SalesDispatchDocumentType,
    SalesDispatchGateOut,
    SalesDispatchGateOutDocument,
    SalesDispatchGateOutItem,
    SalesDispatchGateOutStatus,
    VehicleArrival,
)
from gate_core.serializers import EmptyVehicleGateInSerializer
from gate_core.serializers_sales_dispatch import SalesDispatchGateOutListSerializer
from gate_core.services.sales_dispatch_docking import (
    add_plan_to_open_docking,
    find_open_docking_for_plan,
    merge_bill_into_open_docking,
)
from gate_core.views_sales_dispatch import SalesDispatchGateOutListCreateView
from sap_client.exceptions import SAPConnectionError
from vehicle_management.models import Transporter, Vehicle


class _FakeDocService:
    """Stands in for SalesDispatchDocumentService.get_document (no SAP call)."""

    def __init__(self, document=None, error=None):
        self._document = document
        self._error = error

    def get_document(self, document_type, doc_entry):
        if self._error is not None:
            raise self._error
        return self._document


class DockingMergeTests(TestCase):
    DOC_A = 80001
    DOC_B = 80002

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.role = UserRole.objects.create(name="Gate")
        self.user = get_user_model().objects.create_user(
            email="merge@example.com",
            password="testpass123",
            full_name="Merge User",
            employee_code="MRG001",
        )
        UserCompany.objects.create(
            user=self.user, company=self.company, role=self.role, is_default=True
        )
        self.user.user_permissions.add(
            *Permission.objects.filter(content_type__app_label="gate_core")
        )
        self.transporter = Transporter.objects.create(name="Test Transporter")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="DL01LAC9967", transporter=self.transporter
        )
        self.driver = Driver.objects.create(
            name="Test Driver", mobile_no="9000000000", license_no="DL-MRG-1"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.company_header = {"HTTP_COMPANY_CODE": self.company.code}

    # ----- builders -------------------------------------------------------

    def _gate_in(self):
        vehicle_entry = VehicleEntry.objects.create(
            entry_no="EVGI-MERGE-1",
            company=self.company,
            vehicle=self.vehicle,
            driver=self.driver,
            entry_type="EMPTY_VEHICLE",
            status="COMPLETED",
            created_by=self.user,
            updated_by=self.user,
        )
        return EmptyVehicleGateIn.objects.create(
            company=self.company,
            entry_no="EVGI-MERGE-1",
            vehicle_entry=vehicle_entry,
            vehicle=self.vehicle,
            driver=self.driver,
            reason="DISPATCH",
            gate_in_date=timezone.localdate(),
            in_time=timezone.now().time(),
            created_by=self.user,
            updated_by=self.user,
        )

    def _plan(self, doc_entry, gate_in=None):
        plan = DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=doc_entry,
            sap_invoice_doc_num=str(doc_entry),
            booking_status=DispatchPlanStatus.BOOKED,
            dispatch_date=timezone.localdate(),
            vehicle=self.vehicle,
            linked_vehicle_entry=gate_in.vehicle_entry if gate_in else None,
            created_by=self.user,
            updated_by=self.user,
        )
        if gate_in is not None:
            EmptyVehicleGateInCover.objects.create(
                empty_vehicle_gate_in=gate_in,
                dispatch_plan=plan,
                sap_doc_entry=doc_entry,
                sap_doc_num=str(doc_entry),
                created_by=self.user,
                updated_by=self.user,
            )
        return plan

    def _docking(self, plan, status=SalesDispatchGateOutStatus.DOCKED, branch_id=1):
        vehicle_entry = VehicleEntry.objects.create(
            entry_no=f"DOCKV-{plan.sap_invoice_doc_entry}",
            company=self.company,
            vehicle=self.vehicle,
            driver=self.driver,
            entry_type="SALES_DISPATCH",
            status="IN_PROGRESS",
            created_by=self.user,
            updated_by=self.user,
        )
        docking = SalesDispatchGateOut.objects.create(
            company=self.company,
            entry_no=f"DOCK-{plan.sap_invoice_doc_entry}",
            vehicle_entry=vehicle_entry,
            dispatch_plan=plan,
            vehicle=self.vehicle,
            transporter=self.transporter,
            driver=self.driver,
            document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=plan.sap_invoice_doc_entry,
            sap_doc_num=plan.sap_invoice_doc_num,
            sap_branch_id=branch_id,
            customer_code="CUSTA",
            customer_name="Customer A",
            vehicle_no=self.vehicle.vehicle_number,
            status=status,
            created_by=self.user,
            updated_by=self.user,
        )
        document = SalesDispatchGateOutDocument.objects.create(
            sales_dispatch=docking,
            company=self.company,
            dispatch_plan=plan,
            document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=plan.sap_invoice_doc_entry,
            sap_doc_num=plan.sap_invoice_doc_num,
            customer_code="CUSTA",
            customer_name="Customer A",
            total_weight=Decimal("80.000"),
            total_boxes=Decimal("10.000"),
            total_quantity=Decimal("10.000"),
            item_summary="Item A",
            created_by=self.user,
            updated_by=self.user,
        )
        SalesDispatchGateOutItem.objects.create(
            sales_dispatch=docking,
            document=document,
            line_num=0,
            item_code="ITEM-A",
            item_name="Item A",
            quantity=Decimal("10.000"),
            uom="BOX",
            created_by=self.user,
            updated_by=self.user,
        )
        return docking

    def _fake_document(self, doc_entry, branch_id=1, weight="80.000"):
        return {
            "document_type": "INVOICE",
            "doc_entry": doc_entry,
            "doc_num": str(doc_entry),
            "doc_total": "500.00",
            "branch_id": branch_id,
            "card_code": "CUSTB",
            "card_name": "Customer B",
            "total_weight": weight,
            "total_boxes": "10.000",
            "total_quantity": "10.000",
            "item_summary": "Item B",
            "items": [
                {
                    "line_num": 0,
                    "item_code": "ITEM-B",
                    "item_name": "Item B",
                    "quantity": "10.000",
                    "uom": "BOX",
                    "total_weight": weight,
                }
            ],
        }

    # ----- service: add_plan_to_open_docking ------------------------------

    def test_add_appends_document_items_and_recomputes_header(self):
        gate_in = self._gate_in()
        plan_a = self._plan(self.DOC_A, gate_in)
        docking = self._docking(plan_a)
        plan_b = self._plan(self.DOC_B, gate_in)

        result = add_plan_to_open_docking(
            docking, plan_b, self.user, document_service=_FakeDocService(self._fake_document(self.DOC_B))
        )

        self.assertEqual(result.id, docking.id)
        docking.refresh_from_db()
        docs = docking.documents.filter(is_active=True)
        self.assertEqual(docs.count(), 2)
        self.assertTrue(docs.filter(sap_doc_entry=self.DOC_B, dispatch_plan=plan_b).exists())
        # Header aggregates now span both bills.
        self.assertIn(str(self.DOC_A), docking.sap_doc_num)
        self.assertIn(str(self.DOC_B), docking.sap_doc_num)
        self.assertEqual(docking.total_weight, Decimal("160.000"))
        # The new bill's item is appended after the existing line numbers.
        new_item = SalesDispatchGateOutItem.objects.get(item_code="ITEM-B")
        self.assertEqual(new_item.line_num, 1)

    def test_photo_locked_docking_blocks_add(self):
        gate_in = self._gate_in()
        plan_a = self._plan(self.DOC_A, gate_in)
        docking = self._docking(plan_a, status=SalesDispatchGateOutStatus.PHOTO_ATTACHED)
        plan_b = self._plan(self.DOC_B, gate_in)

        result = add_plan_to_open_docking(
            docking, plan_b, self.user, document_service=_FakeDocService(self._fake_document(self.DOC_B))
        )

        self.assertIsNone(result)
        self.assertEqual(docking.documents.filter(is_active=True).count(), 1)

    def test_branch_mismatch_not_added(self):
        gate_in = self._gate_in()
        plan_a = self._plan(self.DOC_A, gate_in)
        docking = self._docking(plan_a, branch_id=1)
        plan_b = self._plan(self.DOC_B, gate_in)

        result = add_plan_to_open_docking(
            docking,
            plan_b,
            self.user,
            document_service=_FakeDocService(self._fake_document(self.DOC_B, branch_id=2)),
        )

        self.assertIsNone(result)
        self.assertEqual(docking.documents.filter(is_active=True).count(), 1)

    def test_add_is_idempotent_once_docked(self):
        gate_in = self._gate_in()
        plan_a = self._plan(self.DOC_A, gate_in)
        docking = self._docking(plan_a)
        plan_b = self._plan(self.DOC_B, gate_in)
        service = _FakeDocService(self._fake_document(self.DOC_B))

        add_plan_to_open_docking(docking, plan_b, self.user, document_service=service)
        second = add_plan_to_open_docking(docking, plan_b, self.user, document_service=service)

        self.assertIsNone(second)
        self.assertEqual(docking.documents.filter(is_active=True).count(), 2)

    # ----- service: find + merge orchestrator -----------------------------

    def test_find_open_docking_for_plan(self):
        gate_in = self._gate_in()
        plan_a = self._plan(self.DOC_A, gate_in)
        docking = self._docking(plan_a)
        plan_b = self._plan(self.DOC_B, gate_in)

        self.assertEqual(find_open_docking_for_plan(plan_b).id, docking.id)

        docking.status = SalesDispatchGateOutStatus.PHOTO_ATTACHED
        docking.save(update_fields=["status"])
        self.assertIsNone(find_open_docking_for_plan(plan_b))

    def test_merge_end_to_end(self):
        gate_in = self._gate_in()
        plan_a = self._plan(self.DOC_A, gate_in)
        docking = self._docking(plan_a)
        plan_b = self._plan(self.DOC_B, gate_in)

        result = merge_bill_into_open_docking(
            plan_b, self.user, document_service=_FakeDocService(self._fake_document(self.DOC_B))
        )

        self.assertIsNotNone(result)
        self.assertEqual(docking.documents.filter(is_active=True).count(), 2)

    def test_merge_returns_none_without_open_docking(self):
        gate_in = self._gate_in()
        plan_b = self._plan(self.DOC_B, gate_in)  # linked, but no docking exists yet

        result = merge_bill_into_open_docking(
            plan_b, self.user, document_service=_FakeDocService(self._fake_document(self.DOC_B))
        )

        self.assertIsNone(result)

    def test_merge_swallows_sap_outage(self):
        gate_in = self._gate_in()
        plan_a = self._plan(self.DOC_A, gate_in)
        docking = self._docking(plan_a)
        plan_b = self._plan(self.DOC_B, gate_in)

        result = merge_bill_into_open_docking(
            plan_b, self.user, document_service=_FakeDocService(error=SAPConnectionError("down"))
        )

        self.assertIsNone(result)  # bill stays a pending dockable row
        self.assertEqual(docking.documents.filter(is_active=True).count(), 1)

    # ----- manual fallback endpoint ---------------------------------------

    def _add_url(self, docking):
        return f"/api/v1/gate-core/sales-dispatch/{docking.id}/documents/add/"

    def test_endpoint_adds_bill_to_open_docking(self):
        gate_in = self._gate_in()
        plan_a = self._plan(self.DOC_A, gate_in)
        docking = self._docking(plan_a)
        plan_b = self._plan(self.DOC_B, gate_in)

        from gate_core.services.sales_dispatch_documents import SalesDispatchDocumentService

        with mock.patch.object(
            SalesDispatchDocumentService, "get_document", return_value=self._fake_document(self.DOC_B)
        ):
            response = self.client.post(
                self._add_url(docking),
                {"dispatch_plan_id": plan_b.id},
                format="json",
                **self.company_header,
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(docking.documents.filter(is_active=True).count(), 2)

    def test_endpoint_requires_plan_id(self):
        gate_in = self._gate_in()
        docking = self._docking(self._plan(self.DOC_A, gate_in))

        response = self.client.post(
            self._add_url(docking), {}, format="json", **self.company_header
        )
        self.assertEqual(response.status_code, 400)

    def test_endpoint_blocks_photo_locked_docking(self):
        gate_in = self._gate_in()
        plan_a = self._plan(self.DOC_A, gate_in)
        docking = self._docking(plan_a, status=SalesDispatchGateOutStatus.PHOTO_ATTACHED)
        plan_b = self._plan(self.DOC_B, gate_in)

        response = self.client.post(
            self._add_url(docking),
            {"dispatch_plan_id": plan_b.id},
            format="json",
            **self.company_header,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(docking.documents.filter(is_active=True).count(), 1)

    def test_endpoint_rejects_bill_from_a_different_truck(self):
        gate_in = self._gate_in()
        docking = self._docking(self._plan(self.DOC_A, gate_in))
        # A bill gated in under a different vehicle entry (different physical truck).
        other_entry = VehicleEntry.objects.create(
            entry_no="EVGI-OTHER",
            company=self.company,
            vehicle=self.vehicle,
            driver=self.driver,
            entry_type="EMPTY_VEHICLE",
            status="COMPLETED",
            created_by=self.user,
            updated_by=self.user,
        )
        stranger = DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=self.DOC_B,
            sap_invoice_doc_num=str(self.DOC_B),
            booking_status=DispatchPlanStatus.BOOKED,
            dispatch_date=timezone.localdate(),
            vehicle=self.vehicle,
            linked_vehicle_entry=other_entry,
            created_by=self.user,
            updated_by=self.user,
        )

        response = self.client.post(
            self._add_url(docking),
            {"dispatch_plan_id": stranger.id},
            format="json",
            **self.company_header,
        )
        self.assertEqual(response.status_code, 400)

    # ----- docking CREATE reuses the truck's open docking -----------------

    def _append(self, dispatch_plan, documents, plans_by_doc_entry):
        """Invoke the create view's reuse branch directly (no request stack)."""
        view = SalesDispatchGateOutListCreateView()
        return view._append_to_open_docking(
            dispatch_plan,
            documents[0],
            documents,
            plans_by_doc_entry,
            _FakeDocService(documents[0]),
            self.user,
        )

    def test_create_reuses_open_docking_for_same_truck(self):
        gate_in = self._gate_in()
        plan_a = self._plan(self.DOC_A, gate_in)
        docking = self._docking(plan_a)
        plan_b = self._plan(self.DOC_B, gate_in)

        result = self._append(plan_b, [self._fake_document(self.DOC_B)], {self.DOC_B: plan_b})

        self.assertIsNotNone(result)
        self.assertEqual(result.id, docking.id)
        # No second docking was created; the bill folded into the existing one.
        self.assertEqual(SalesDispatchGateOut.objects.count(), 1)
        self.assertEqual(docking.documents.filter(is_active=True).count(), 2)

    def test_create_skips_reuse_when_photo_locked(self):
        gate_in = self._gate_in()
        plan_a = self._plan(self.DOC_A, gate_in)
        self._docking(plan_a, status=SalesDispatchGateOutStatus.PHOTO_ATTACHED)
        plan_b = self._plan(self.DOC_B, gate_in)

        result = self._append(plan_b, [self._fake_document(self.DOC_B)], {self.DOC_B: plan_b})

        self.assertIsNone(result)  # caller creates a fresh docking

    def test_create_skips_reuse_for_stock_transfer(self):
        gate_in = self._gate_in()
        self._docking(self._plan(self.DOC_A, gate_in))
        plan_b = self._plan(self.DOC_B, gate_in)
        stock_doc = {**self._fake_document(self.DOC_B), "document_type": "STOCK_TRANSFER"}

        result = self._append(plan_b, [stock_doc], {self.DOC_B: plan_b})

        self.assertIsNone(result)

    def test_create_skips_reuse_when_a_document_has_no_plan(self):
        gate_in = self._gate_in()
        self._docking(self._plan(self.DOC_A, gate_in))
        plan_b = self._plan(self.DOC_B, gate_in)

        result = self._append(plan_b, [self._fake_document(self.DOC_B)], {})

        self.assertIsNone(result)

    def test_create_skips_reuse_without_an_open_docking(self):
        gate_in = self._gate_in()
        plan_b = self._plan(self.DOC_B, gate_in)  # linked, but no docking exists

        result = self._append(plan_b, [self._fake_document(self.DOC_B)], {self.DOC_B: plan_b})

        self.assertIsNone(result)

    # ----- arrival_no exposed for the vehicle-grouping boards -------------

    def _arrival(self):
        return VehicleArrival.objects.create(
            arrival_no="ARV-TEST-1",
            vehicle=self.vehicle,
            driver=self.driver,
            gate_in_date=timezone.localdate(),
            in_time=timezone.now().time(),
            created_by=self.user,
            updated_by=self.user,
        )

    def test_docking_list_serializer_exposes_arrival_no(self):
        arrival = self._arrival()
        docking = self._docking(self._plan(self.DOC_A, self._gate_in()))
        docking.arrival = arrival
        docking.save(update_fields=["arrival"])

        data = SalesDispatchGateOutListSerializer(docking).data
        self.assertEqual(data["arrival"], arrival.id)
        self.assertEqual(data["arrival_no"], "ARV-TEST-1")

    def test_empty_vehicle_serializer_exposes_arrival_no(self):
        arrival = self._arrival()
        gate_in = self._gate_in()
        gate_in.arrival = arrival
        gate_in.save(update_fields=["arrival"])

        data = EmptyVehicleGateInSerializer(gate_in).data
        self.assertEqual(data["arrival"], arrival.id)
        self.assertEqual(data["arrival_no"], "ARV-TEST-1")

    # ----- unwind: C1 remove recompute/detach + cancel settlement -----------

    def _remove_url(self, docking, document):
        return f"/api/v1/gate-core/sales-dispatch/{docking.id}/documents/{document.id}/remove/"

    def _open_two_bill_docking(self):
        gate_in = self._gate_in()
        docking = self._docking(self._plan(self.DOC_A, gate_in))
        plan_b = self._plan(self.DOC_B, gate_in)
        add_plan_to_open_docking(
            docking, plan_b, self.user, document_service=_FakeDocService(self._fake_document(self.DOC_B))
        )
        docking.refresh_from_db()
        return docking, docking.documents.get(sap_doc_entry=self.DOC_B)

    def test_remove_recomputes_header_and_detaches_bill(self):
        docking, doc_b = self._open_two_bill_docking()
        self.assertEqual(docking.total_weight, Decimal("160.000"))  # 80 + 80

        response = self.client.post(self._remove_url(docking, doc_b), {}, format="json", **self.company_header)
        self.assertEqual(response.status_code, 200, response.data)

        doc_b.refresh_from_db()
        self.assertFalse(doc_b.is_active)
        self.assertIsNone(doc_b.dispatch_plan_id)  # detached -> no longer derives this docking's stage
        docking.refresh_from_db()
        self.assertEqual(docking.total_weight, Decimal("80.000"))  # b's weight removed from the header

    def test_remove_unattributes_box_scans(self):
        docking, doc_b = self._open_two_bill_docking()
        scan = SalesDispatchBoxScan.objects.create(
            company=self.company, sales_dispatch=docking, document=doc_b,
            box_barcode="BC-B-1", scanned_by=self.user, created_by=self.user, updated_by=self.user,
        )

        self.client.post(self._remove_url(docking, doc_b), {}, format="json", **self.company_header)

        scan.refresh_from_db()
        self.assertIsNone(scan.document_id)  # not left dangling on the inactive document

    def test_cancel_detaches_bills_so_they_revert_to_gate_in_stage(self):
        from dispatch_plans.services import compute_pipeline_status

        gate_in = self._gate_in()
        plan_a = self._plan(self.DOC_A, gate_in)
        docking = self._docking(plan_a)
        self.assertEqual(compute_pipeline_status(plan_a)["module_label"], "docked at dock")

        response = self.client.post(
            f"/api/v1/gate-core/sales-dispatch/{docking.id}/cancel/",
            {"reason": "re-dock"}, format="json", **self.company_header,
        )
        self.assertEqual(response.status_code, 200, response.data)

        docking.refresh_from_db()
        self.assertEqual(docking.status, SalesDispatchGateOutStatus.CANCELLED)
        self.assertIsNone(docking.dispatch_plan_id)  # header detached
        self.assertIsNone(docking.documents.get(sap_doc_entry=self.DOC_A).dispatch_plan_id)
        # Reverts to its live gate-in stage (ready to dock), NOT stuck REJECTED:
        self.assertEqual(compute_pipeline_status(plan_a)["module_label"], "pending at dock")

    def test_empty_out_departs_the_arrival(self):
        from gate_core.views import release_dispatch_plans_for_empty_out

        arrival = self._arrival()  # status INSIDE
        gate_in = self._gate_in()
        gate_in.arrival = arrival
        gate_in.save(update_fields=["arrival"])

        release_dispatch_plans_for_empty_out(gate_in.vehicle_entry, self.user)

        gate_in.refresh_from_db()
        arrival.refresh_from_db()
        self.assertIsNotNone(gate_in.retired_at)
        self.assertEqual(arrival.status, "DEPARTED")  # single-company empty-out closes the trip
