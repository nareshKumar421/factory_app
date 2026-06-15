from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from company.models import Company
from driver_management.models import Driver, VehicleEntry
from gate_core.enums import GateEntryStatus
from quality_control.enums import ArrivalSlipStatus
from quality_control.models import MaterialArrivalSlip
from raw_material_gatein.models import POItemReceipt, POReceipt
from raw_material_gatein.views import _po_receipt_lock_reason, _serialize_po_receipt
from vehicle_management.models import Vehicle


class POReceiptEditLockTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            email="po-lock@example.com",
            password="password",
            full_name="PO Lock User",
            employee_code="POLOCK001",
        )
        self.company = Company.objects.create(name="PO Lock Co", code="PO_LOCK")
        self.vehicle = Vehicle.objects.create(vehicle_number="HR55LOCK001")
        self.driver = Driver.objects.create(
            name="PO Lock Driver",
            mobile_no="9888880001",
            license_no="PO-LOCK-DL",
        )
        self.entry = VehicleEntry.objects.create(
            entry_no="PO-LOCK-001",
            company=self.company,
            vehicle=self.vehicle,
            driver=self.driver,
            entry_type="RAW_MATERIAL",
            status=GateEntryStatus.ARRIVAL_SLIP_REJECTED,
            created_by=self.user,
            updated_by=self.user,
        )
        self.po_receipt = POReceipt.objects.create(
            vehicle_entry=self.entry,
            po_number="PO-LOCK-001",
            supplier_code="SUP-LOCK",
            supplier_name="PO Lock Supplier",
            created_by=self.user,
        )
        self.po_item = POItemReceipt.objects.create(
            po_receipt=self.po_receipt,
            po_item_code="ITEM-LOCK",
            item_name="PO Lock Item",
            sap_line_num=1,
            ordered_qty=Decimal("10.000"),
            received_qty=Decimal("10.000"),
            uom="KG",
            created_by=self.user,
        )

    def create_arrival_slip(self, *, status, is_submitted, submitted_at=None):
        return MaterialArrivalSlip.objects.create(
            po_item_receipt=self.po_item,
            particulars=self.po_item.item_name,
            arrival_datetime=timezone.now(),
            weighing_required=False,
            party_name=self.po_receipt.supplier_name,
            billing_qty=Decimal("10.000"),
            billing_uom=self.po_item.uom,
            truck_no_as_per_bill=self.vehicle.vehicle_number,
            status=status,
            is_submitted=is_submitted,
            submitted_at=submitted_at,
            submitted_by=self.user if submitted_at else None,
            created_by=self.user,
        )

    def test_sent_back_draft_slip_with_submission_history_keeps_po_editable(self):
        self.create_arrival_slip(
            status=ArrivalSlipStatus.DRAFT,
            is_submitted=False,
            submitted_at=timezone.now(),
        )

        self.assertIsNone(_po_receipt_lock_reason(self.po_receipt))
        self.assertTrue(_serialize_po_receipt(self.po_receipt)["is_editable"])

    def test_rejected_slip_with_submission_history_keeps_po_editable(self):
        self.create_arrival_slip(
            status=ArrivalSlipStatus.REJECTED,
            is_submitted=False,
            submitted_at=timezone.now(),
        )

        self.assertIsNone(_po_receipt_lock_reason(self.po_receipt))
        self.assertTrue(_serialize_po_receipt(self.po_receipt)["is_editable"])

    def test_currently_submitted_slip_locks_po_editing(self):
        self.create_arrival_slip(
            status=ArrivalSlipStatus.SUBMITTED,
            is_submitted=True,
            submitted_at=timezone.now(),
        )

        self.assertEqual(
            _po_receipt_lock_reason(self.po_receipt),
            "This PO cannot be edited after its arrival slip is submitted to QC.",
        )
        self.assertFalse(_serialize_po_receipt(self.po_receipt)["is_editable"])
