from decimal import Decimal
from datetime import date
from unittest.mock import patch, MagicMock
from io import BytesIO

from django.test import TestCase
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APITestCase, APIClient
from rest_framework import status

from gate_core.enums import GateEntryStatus
from company.models import Company
from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
from driver_management.models import VehicleEntry, Driver
from vehicle_management.models import Transporter, Vehicle, VehicleType
from raw_material_gatein.models import POReceipt, POItemReceipt
from quality_control.enums import ArrivalSlipStatus, InspectionStatus, InspectionWorkflowStatus
from quality_control.models import MaterialArrivalSlip, RawMaterialInspection
from quality_control.services.rules import compute_entry_status
from grpo.models import GRPOPosting, GRPOLinePosting, GRPOStatus, GRPOAttachment, SAPAttachmentStatus
from grpo.serializers import ServiceGRPOPostRequestSerializer, ServiceGRPOPreviewSerializer
from grpo.services import GRPOService
from weighment.models import Weighment

User = get_user_model()


class GRPOModelTests(TestCase):
    """Tests for GRPO models"""

    @classmethod
    def setUpTestData(cls):
        # Create company
        cls.company = Company.objects.create(
            name="Test Company",
            code="TC001"
        )

        # Create user
        cls.user = User.objects.create_user(
            email="testuser@example.com",
            password="testpass123",
            full_name="Test User",
            employee_code="EMP001"
        )

        # Create vehicle
        cls.vehicle_type = VehicleType.objects.create(name="TRUCK")
        cls.vehicle = Vehicle.objects.create(
            vehicle_number="MH12AB1234",
            vehicle_type=cls.vehicle_type
        )

        # Create driver
        cls.driver = Driver.objects.create(
            name="Test Driver",
            mobile_no="9876543210",
            license_no="DL123456"
        )

        # Create vehicle entry
        cls.vehicle_entry = VehicleEntry.objects.create(
            entry_no="VE-2024-001",
            company=cls.company,
            vehicle=cls.vehicle,
            driver=cls.driver,
            entry_type="RAW_MATERIAL",
            status=GateEntryStatus.COMPLETED
        )

        # Create PO receipt with SAP doc entry
        cls.po_receipt = POReceipt.objects.create(
            vehicle_entry=cls.vehicle_entry,
            po_number="PO-001",
            supplier_code="SUP001",
            supplier_name="Test Supplier",
            sap_doc_entry=12345
        )

    def test_grpo_posting_creation(self):
        """Test GRPOPosting model creation"""
        grpo = GRPOPosting.objects.create(
            vehicle_entry=self.vehicle_entry,
            po_receipt=self.po_receipt,
            status=GRPOStatus.PENDING
        )

        self.assertEqual(grpo.status, GRPOStatus.PENDING)
        self.assertIsNone(grpo.sap_doc_entry)
        self.assertIsNone(grpo.sap_doc_num)

    def test_grpo_posting_str(self):
        """Test GRPOPosting string representation"""
        grpo = GRPOPosting.objects.create(
            vehicle_entry=self.vehicle_entry,
            po_receipt=self.po_receipt,
            status=GRPOStatus.POSTED,
            sap_doc_num=12345
        )

        self.assertIn("PO-001", str(grpo))
        self.assertIn("POSTED", str(grpo))

    def test_grpo_m2m_po_receipts(self):
        """Test that GRPOPosting can link to multiple PO receipts via M2M"""
        po_receipt_2 = POReceipt.objects.create(
            vehicle_entry=self.vehicle_entry,
            po_number="PO-002",
            supplier_code="SUP001",
            supplier_name="Test Supplier",
            sap_doc_entry=12346
        )

        grpo = GRPOPosting.objects.create(
            vehicle_entry=self.vehicle_entry,
            po_receipt=self.po_receipt,
            status=GRPOStatus.PENDING
        )
        grpo.po_receipts.set([self.po_receipt, po_receipt_2])

        self.assertEqual(grpo.po_receipts.count(), 2)
        self.assertIn("PO-001", str(grpo))
        self.assertIn("PO-002", str(grpo))

    def test_grpo_line_posting_with_po_linking(self):
        """Test GRPOLinePosting stores base_entry and base_line"""
        grpo = GRPOPosting.objects.create(
            vehicle_entry=self.vehicle_entry,
            po_receipt=self.po_receipt,
            status=GRPOStatus.POSTED,
            sap_doc_num=100
        )
        po_item = POItemReceipt.objects.create(
            po_receipt=self.po_receipt,
            po_item_code="ITEM001",
            item_name="Test Item",
            ordered_qty=Decimal("100.000"),
            received_qty=Decimal("100.000"),
            sap_line_num=0,
            uom="KG"
        )
        line = GRPOLinePosting.objects.create(
            grpo_posting=grpo,
            po_item_receipt=po_item,
            quantity_posted=Decimal("95.000"),
            base_entry=12345,
            base_line=0
        )
        self.assertEqual(line.base_entry, 12345)
        self.assertEqual(line.base_line, 0)


class GRPOServiceTests(TestCase):
    """Tests for GRPO service layer"""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            name="Test Company",
            code="TC001"
        )

        cls.user = User.objects.create_user(
            email="testuser@example.com",
            password="testpass123",
            full_name="Test User",
            employee_code="EMP001"
        )

        cls.vehicle_type = VehicleType.objects.create(name="TRUCK")
        cls.vehicle = Vehicle.objects.create(
            vehicle_number="MH12AB1234",
            vehicle_type=cls.vehicle_type
        )

        cls.driver = Driver.objects.create(
            name="Test Driver",
            mobile_no="9876543210",
            license_no="DL123456"
        )

        cls.vehicle_entry = VehicleEntry.objects.create(
            entry_no="VE-2024-001",
            company=cls.company,
            vehicle=cls.vehicle,
            driver=cls.driver,
            entry_type="RAW_MATERIAL",
            status=GateEntryStatus.COMPLETED
        )
        cls.weighment = Weighment.objects.create(
            vehicle_entry=cls.vehicle_entry,
            gross_weight=Decimal("10000.000"),
            tare_weight=Decimal("3000.000"),
            created_by=cls.user,
            updated_by=cls.user,
        )

        cls.po_receipt = POReceipt.objects.create(
            vehicle_entry=cls.vehicle_entry,
            po_number="PO-001",
            supplier_code="SUP001",
            supplier_name="Test Supplier",
            sap_doc_entry=12345,
            branch_id=1,
            vendor_ref="VINV-2026-001"
        )

        cls.po_item = POItemReceipt.objects.create(
            po_receipt=cls.po_receipt,
            po_item_code="ITEM001",
            item_name="Test Item",
            ordered_qty=Decimal("100.000"),
            received_qty=Decimal("100.000"),
            accepted_qty=Decimal("95.000"),
            rejected_qty=Decimal("5.000"),
            sap_line_num=0,
            unit_price=Decimal("85.500000"),
            tax_code="GST18",
            warehouse_code="WH-01",
            gl_account="40001001",
            uom="KG"
        )

    def _attach_qc_inspection(
        self,
        po_item,
        final_status=InspectionStatus.ACCEPTED,
        workflow_status=InspectionWorkflowStatus.QAM_APPROVED,
        report_no="RPT-GRPO-QC-001",
    ):
        arrival_slip = MaterialArrivalSlip.objects.create(
            po_item_receipt=po_item,
            particulars=po_item.item_name,
            arrival_datetime=timezone.now(),
            party_name=po_item.po_receipt.supplier_name,
            billing_qty=po_item.received_qty,
            billing_uom=po_item.uom,
            truck_no_as_per_bill=po_item.po_receipt.vehicle_entry.vehicle.vehicle_number,
            status=ArrivalSlipStatus.SUBMITTED,
            is_submitted=True,
            submitted_at=timezone.now(),
            submitted_by=self.user,
        )
        return RawMaterialInspection.objects.create(
            arrival_slip=arrival_slip,
            report_no=report_no,
            internal_lot_no=f"LOT-{report_no}",
            inspection_date=timezone.now().date(),
            description_of_material=po_item.item_name,
            sap_code=po_item.po_item_code,
            supplier_name=po_item.po_receipt.supplier_name,
            purchase_order_no=po_item.po_receipt.po_number,
            final_status=final_status,
            workflow_status=workflow_status,
        )

    def test_get_pending_grpo_entries(self):
        """Test fetching pending GRPO entries"""
        service = GRPOService(company_code="TC001")
        entries = service.get_pending_grpo_entries()

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].entry_no, "VE-2024-001")

    def test_qam_approved_hold_does_not_complete_qc(self):
        """QAM approval with HOLD final status remains in QC flow."""
        self.vehicle_entry.status = GateEntryStatus.QC_COMPLETED
        self.vehicle_entry.save(update_fields=["status"])
        self._attach_qc_inspection(
            self.po_item,
            final_status=InspectionStatus.HOLD,
            workflow_status=InspectionWorkflowStatus.QAM_APPROVED,
            report_no="RPT-20260606-0002",
        )

        self.assertEqual(
            compute_entry_status(self.vehicle_entry),
            GateEntryStatus.QC_AWAITING_QAM,
        )

    def test_pending_grpo_entries_exclude_qam_approved_hold_qc(self):
        """Stale QC_COMPLETED entries are hidden while an item is HOLD."""
        self.vehicle_entry.status = GateEntryStatus.QC_COMPLETED
        self.vehicle_entry.save(update_fields=["status"])
        self._attach_qc_inspection(
            self.po_item,
            final_status=InspectionStatus.HOLD,
            workflow_status=InspectionWorkflowStatus.QAM_APPROVED,
            report_no="RPT-20260606-0002",
        )

        service = GRPOService(company_code="TC001")
        entry_numbers = [entry.entry_no for entry in service.get_pending_grpo_entries()]
        preview = service.get_grpo_preview_data(self.vehicle_entry.id)

        self.assertNotIn("VE-2024-001", entry_numbers)
        self.assertFalse(preview[0]["is_ready_for_grpo"])
        self.assertEqual(preview[0]["items"][0]["qc_status"], InspectionStatus.HOLD)

    def test_pending_grpo_entries_allow_final_accepted_qc(self):
        """QAM approval proceeds only after the final QC decision is ACCEPTED."""
        self.vehicle_entry.status = GateEntryStatus.QC_COMPLETED
        self.vehicle_entry.save(update_fields=["status"])
        self._attach_qc_inspection(
            self.po_item,
            final_status=InspectionStatus.ACCEPTED,
            workflow_status=InspectionWorkflowStatus.QAM_APPROVED,
            report_no="RPT-GRPO-QC-ACCEPTED",
        )

        service = GRPOService(company_code="TC001")
        entry_numbers = [entry.entry_no for entry in service.get_pending_grpo_entries()]

        self.assertIn("VE-2024-001", entry_numbers)
        self.assertEqual(compute_entry_status(self.vehicle_entry), GateEntryStatus.QC_COMPLETED)

    def test_post_grpo_rejects_stale_qc_completed_with_hold_item(self):
        """Posting cannot create partial GRPO rows while any related item is HOLD."""
        self.vehicle_entry.status = GateEntryStatus.QC_COMPLETED
        self.vehicle_entry.save(update_fields=["status"])
        self._attach_qc_inspection(
            self.po_item,
            final_status=InspectionStatus.HOLD,
            workflow_status=InspectionWorkflowStatus.QAM_APPROVED,
            report_no="RPT-20260606-0002",
        )

        service = GRPOService(company_code="TC001")
        before_count = GRPOPosting.objects.count()

        with self.assertRaisesMessage(ValueError, "QC is not final"):
            service.post_grpo(
                vehicle_entry_id=self.vehicle_entry.id,
                po_receipt_ids=[self.po_receipt.id],
                user=self.user,
                items=[{
                    "po_item_receipt_id": self.po_item.id,
                    "accepted_qty": Decimal("95.000"),
                }],
                branch_id=1,
            )

        self.assertEqual(GRPOPosting.objects.count(), before_count)

    def test_post_grpo_rejects_when_related_unselected_po_item_is_hold(self):
        """Related HOLD QC items block posting even when their PO is not selected."""
        self.vehicle_entry.status = GateEntryStatus.QC_COMPLETED
        self.vehicle_entry.save(update_fields=["status"])
        related_po = POReceipt.objects.create(
            vehicle_entry=self.vehicle_entry,
            po_number="PO-RELATED-HOLD",
            supplier_code="SUP001",
            supplier_name="Test Supplier",
            sap_doc_entry=54321,
            branch_id=1,
        )
        related_item = POItemReceipt.objects.create(
            po_receipt=related_po,
            po_item_code="ITEM-HOLD",
            item_name="Related Hold Item",
            ordered_qty=Decimal("10.000"),
            received_qty=Decimal("10.000"),
            accepted_qty=Decimal("10.000"),
            rejected_qty=Decimal("0.000"),
            sap_line_num=0,
            unit_price=Decimal("10.000000"),
            uom="KG",
        )
        self._attach_qc_inspection(
            related_item,
            final_status=InspectionStatus.HOLD,
            workflow_status=InspectionWorkflowStatus.QAM_APPROVED,
            report_no="RPT-GRPO-QC-RELATED-HOLD",
        )

        service = GRPOService(company_code="TC001")

        with self.assertRaisesMessage(ValueError, "PO PO-RELATED-HOLD"):
            service.post_grpo(
                vehicle_entry_id=self.vehicle_entry.id,
                po_receipt_ids=[self.po_receipt.id],
                user=self.user,
                items=[{
                    "po_item_receipt_id": self.po_item.id,
                    "accepted_qty": Decimal("95.000"),
                }],
                branch_id=1,
            )

    def test_inactive_gate_entries_are_hidden_from_grpo(self):
        """Soft-deleted gate entries should not appear in material GRPO surfaces."""
        inactive_entry = VehicleEntry.objects.create(
            entry_no="VE-2024-INACTIVE",
            company=self.company,
            vehicle=self.vehicle,
            driver=self.driver,
            entry_type="RAW_MATERIAL",
            status=GateEntryStatus.QC_COMPLETED,
            is_active=False,
        )
        inactive_po = POReceipt.objects.create(
            vehicle_entry=inactive_entry,
            po_number="PO-INACTIVE",
            supplier_code="SUP999",
            supplier_name="Inactive Supplier",
            sap_doc_entry=99999,
        )
        POItemReceipt.objects.create(
            po_receipt=inactive_po,
            po_item_code="ITEM-INACTIVE",
            item_name="Inactive Item",
            ordered_qty=Decimal("1000.000"),
            received_qty=Decimal("1000.000"),
            accepted_qty=Decimal("1000.000"),
            rejected_qty=Decimal("0.000"),
            sap_line_num=0,
            uom="KG",
        )
        GRPOPosting.objects.create(
            vehicle_entry=inactive_entry,
            po_receipt=inactive_po,
            status=GRPOStatus.PENDING,
        )

        service = GRPOService(company_code="TC001")

        pending_entry_numbers = [entry.entry_no for entry in service.get_pending_grpo_entries()]
        all_entry_numbers = [entry.entry_no for entry in service.get_all_grpo_visible_entries()]
        history_entry_numbers = [
            posting.vehicle_entry.entry_no for posting in service.get_grpo_posting_history()
        ]
        summary = service.get_grpo_dashboard_summary()

        self.assertNotIn(inactive_entry.entry_no, pending_entry_numbers)
        self.assertNotIn(inactive_entry.entry_no, all_entry_numbers)
        self.assertNotIn(inactive_entry.entry_no, history_entry_numbers)
        self.assertEqual(summary["qc_accepted_qty"], Decimal("95.000"))
        self.assertEqual(summary["posting_pending_count"], 0)
        with self.assertRaisesMessage(
            ValueError,
            f"Vehicle entry {inactive_entry.id} not found",
        ):
            service.get_grpo_preview_data(inactive_entry.id)

    @patch.object(GRPOService, "_get_sap_tax_codes")
    def test_service_line_tax_code_uses_rcm_igst_for_interstate(self, mock_tax_codes):
        """Interstate RCM service freight must use the SAP RCM IGST code."""
        mock_tax_codes.return_value = {
            "GST05R": {
                "code": "GST05R",
                "name": "SGST @ 2.5 % + CGST @ 2.5 % RCM",
                "rate": Decimal("5"),
            },
            "RIGST@5": {
                "code": "RIGST@5",
                "name": "RCM IGST @5%",
                "rate": Decimal("5"),
            },
            "CG+SG@5": {
                "code": "CG+SG@5",
                "name": "CGST @ 2.5 % + SGST @ 2.5 %",
                "rate": Decimal("5"),
            },
            "IGST@5": {
                "code": "IGST@5",
                "name": "IGST @5%",
                "rate": Decimal("5"),
            },
        }

        service = GRPOService(company_code="TC001")

        self.assertEqual(
            service._resolve_service_line_tax_code("GST05R", "HR", "DL"),
            "RIGST@5",
        )
        self.assertEqual(
            service._resolve_service_line_tax_code("CG+SG@5", "HR", "DL"),
            "IGST@5",
        )

    @patch.object(GRPOService, "_get_sap_tax_codes")
    def test_service_line_tax_code_keeps_intrastate_rcm_code(self, mock_tax_codes):
        """Intrastate RCM service freight keeps the CGST/SGST RCM code."""
        mock_tax_codes.return_value = {
            "GST05R": {
                "code": "GST05R",
                "name": "SGST @ 2.5 % + CGST @ 2.5 % RCM",
                "rate": Decimal("5"),
            },
            "RIGST@5": {
                "code": "RIGST@5",
                "name": "RCM IGST @5%",
                "rate": Decimal("5"),
            },
        }

        service = GRPOService(company_code="TC001")

        self.assertEqual(
            service._resolve_service_line_tax_code("GST05R", "Haryana", "HR"),
            "GST05R",
        )

    @patch.object(GRPOService, "_filter_purchase_delivery_note_udfs")
    @patch.object(GRPOService, "_get_dispatch_bill_snapshot")
    @patch.object(GRPOService, "_get_active_dimension_codes")
    @patch.object(GRPOService, "_get_sap_tax_codes")
    @patch.object(GRPOService, "_get_sap_bp_state")
    @patch.object(GRPOService, "_get_sap_branch_states")
    @patch("grpo.services.SAPClient")
    def test_service_grpo_uses_vendor_state_for_service_tax(
        self,
        mock_sap_client,
        mock_branch_states,
        mock_vendor_state,
        mock_tax_codes,
        mock_dimension_codes,
        mock_bill_snapshot,
        mock_filter_udfs,
    ):
        """Service freight tax must use the transporter/vendor state, not invoice state."""
        mock_branch_states.return_value = {2: "HR"}
        mock_vendor_state.return_value = "DL"
        mock_tax_codes.return_value = {
            "GST05R": {
                "code": "GST05R",
                "name": "SGST @ 2.5 % + CGST @ 2.5 % RCM",
                "rate": Decimal("5"),
            },
            "RIGST@5": {
                "code": "RIGST@5",
                "name": "RCM IGST @5%",
                "rate": Decimal("5"),
            },
        }
        mock_dimension_codes.return_value = None
        mock_bill_snapshot.return_value = {
            "doc_num": "626050517",
            "state": "HR",
            "card_code": "CUST001",
            "item_summary": "Transport freight",
            "total_litres": "1000.000",
            "doc_total": "50000.00",
        }
        mock_instance = MagicMock()
        mock_instance.create_grpo.return_value = {
            "DocEntry": 9001,
            "DocNum": 7001,
            "DocTotal": 1050.00,
        }
        mock_sap_client.return_value = mock_instance

        dispatch_plan = DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=626050517,
            sap_invoice_doc_num="626050517",
            booking_status=DispatchPlanStatus.BOOKED,
            place_of_supply="HR",
            transporter_name="ARNAV TRANSPORT SERVICE",
            vehicle_no="HR55AA1234",
            total_freight=Decimal("1000.00"),
        )

        service = GRPOService(company_code="TC001")
        grpo = service.post_service_grpo(
            dispatch_plan_id=dispatch_plan.id,
            user=self.user,
            vendor_code="VENDA000956",
            branch_id=2,
            service_description="Transport freight",
            amount=Decimal("1000.00"),
            tax_code="GST05R",
            gl_account="5670001",
            place_of_supply="HR",
            effective_month="2026-05",
            location_code=2,
            location_name="HARYANA",
            sac_entry=40,
            sac_code="9965",
            include_bilty_attachment=False,
            extra_charges=[
                {
                    "expense_code": 1,
                    "amount": Decimal("50.00"),
                    "remarks": "Freight handling",
                    "tax_code": "GST05R",
                }
            ],
        )

        payload = mock_instance.create_grpo.call_args[0][0]

        self.assertEqual(payload["DocumentLines"][0]["TaxCode"], "RIGST@5")
        self.assertEqual(payload["DocumentLines"][0]["U_Variety"], "Oil")
        self.assertEqual(payload["ShipPlace"], "DL")
        self.assertEqual(payload["DocumentAdditionalExpenses"][0]["TaxCode"], "RIGST@5")
        grpo.refresh_from_db()
        self.assertEqual(grpo.place_of_supply, "DL")

    @patch.object(GRPOService, "_get_active_dimension_codes")
    def test_service_grpo_effective_month_maps_to_dimension_code(self, mock_dimension_codes):
        """SAP expects Expense Effective Month in line dimension 2 as MM-YYYY."""
        mock_dimension_codes.return_value = {
            "04-2026": "04-2026",
            "05-2026": "05-2026",
        }
        service = GRPOService(company_code="TC001")

        self.assertEqual(service._first_day_of_month("2026-04"), date(2026, 4, 1))
        self.assertEqual(
            service._format_effective_month_dimension(date(2026, 4, 1)),
            "04-2026",
        )
        self.assertEqual(
            service._resolve_active_dimension_code(
                2,
                service._format_effective_month_dimension(date(2026, 4, 1)),
            ),
            "04-2026",
        )

    @patch.object(GRPOService, "_get_active_dimension_codes")
    def test_service_grpo_product_dimension_uses_active_sap_code(self, mock_dimension_codes):
        """Service line dimension 1 should use SAP's active product/service code."""
        mock_dimension_codes.return_value = {
            "WATER": "WATER",
            "CANOLA": "CANOLA",
        }
        service = GRPOService(company_code="TC001")

        self.assertEqual(
            service._resolve_active_dimension_code(1, "Beverage", "Water"),
            "WATER",
        )

    @patch.object(GRPOService, "_get_active_dimension_codes")
    def test_service_grpo_product_dimension_uses_invoice_item_summary(
        self, mock_dimension_codes
    ):
        """Specific invoice items should map to SAP variety codes, not generic Oil."""
        mock_dimension_codes.return_value = {
            "SUNFLOWR": "SUNFLOWER",
            "CANOLA": "CANOLA",
        }
        service = GRPOService(company_code="TC001")

        self.assertEqual(
            service._resolve_product_dimension_code(
                "FG0000053 - COLD PRESS SUNFLOWER 5 LTR",
                "Oil",
                "Oil",
            ),
            "SUNFLOWR",
        )

    @patch.object(GRPOService, "_get_active_dimension_codes")
    def test_service_grpo_product_dimension_maps_generic_oil_to_active_variety(
        self, mock_dimension_codes
    ):
        """Generic Oil selection must still resolve to a real SAP Variety dimension."""
        mock_dimension_codes.return_value = {
            "CRUDE": "Crude Oil",
            "BEV": "Beverage",
        }
        service = GRPOService(company_code="TC001")

        self.assertEqual(
            service._resolve_product_dimension_code(
                "Transport freight",
                "Oil",
                "Transport",
            ),
            "CRUDE",
        )

    @patch.object(GRPOService, "_filter_purchase_delivery_note_udfs")
    @patch.object(GRPOService, "_get_dispatch_bill_snapshot")
    @patch.object(GRPOService, "_get_active_dimension_codes")
    @patch.object(GRPOService, "_get_sap_tax_codes")
    @patch.object(GRPOService, "_get_sap_bp_state")
    @patch.object(GRPOService, "_get_sap_branch_states")
    @patch("grpo.services.SAPClient")
    def test_service_grpo_posts_required_variety_costing_code(
        self,
        mock_sap_client,
        mock_branch_states,
        mock_vendor_state,
        mock_tax_codes,
        mock_dimension_codes,
        mock_bill_snapshot,
        mock_filter_udfs,
    ):
        """SAP needs service GRPO Variety in CostingCode, not only U_Variety."""
        mock_branch_states.return_value = {2: "HR"}
        mock_vendor_state.return_value = "HR"
        mock_tax_codes.return_value = {
            "GST05R": {
                "code": "GST05R",
                "name": "SGST @ 2.5 % + CGST @ 2.5 % RCM",
                "rate": Decimal("5"),
            },
        }

        def dimension_codes(company_code, dim_code):
            return {
                1: {"CRUDE": "Crude Oil"},
                2: {"05-2026": "05-2026"},
                5: {"HR": "Haryana"},
            }.get(dim_code, {})

        mock_dimension_codes.side_effect = dimension_codes
        mock_bill_snapshot.return_value = {
            "doc_num": "626050517",
            "state": "HR",
            "card_code": "CUST001",
            "item_summary": "Transport freight",
            "total_litres": "1000.000",
            "doc_total": "50000.00",
        }
        mock_instance = MagicMock()
        mock_instance.create_grpo.return_value = {
            "DocEntry": 9001,
            "DocNum": 7001,
            "DocTotal": 1000.00,
        }
        mock_sap_client.return_value = mock_instance

        dispatch_plan = DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=626050517,
            sap_invoice_doc_num="626050517",
            booking_status=DispatchPlanStatus.BOOKED,
            place_of_supply="HR",
            transporter_name="ARNAV TRANSPORT SERVICE",
            vehicle_no="HR55AA1234",
            total_freight=Decimal("1000.00"),
        )

        service = GRPOService(company_code="TC001")
        service.post_service_grpo(
            dispatch_plan_id=dispatch_plan.id,
            user=self.user,
            vendor_code="VENDA000956",
            branch_id=2,
            service_description="Transport freight",
            amount=Decimal("1000.00"),
            tax_code="GST05R",
            gl_account="5670001",
            place_of_supply="HR",
            effective_month="2026-05",
            location_code=2,
            location_name="HARYANA",
            sac_entry=40,
            sac_code="9965",
            product_variety="Oil",
            include_bilty_attachment=False,
        )

        payload = mock_instance.create_grpo.call_args[0][0]
        self.assertEqual(payload["DocumentLines"][0]["CostingCode"], "CRUDE")
        self.assertEqual(payload["DocumentLines"][0]["U_Variety"], "Oil")

    @patch.object(GRPOService, "_get_dispatch_bill_snapshot")
    @patch.object(GRPOService, "_get_active_dimension_codes")
    @patch.object(GRPOService, "_get_sap_bp_state")
    @patch.object(GRPOService, "_get_sap_branch_states")
    @patch("grpo.services.SAPClient")
    def test_service_grpo_fails_before_sap_when_variety_dimension_missing(
        self,
        mock_sap_client,
        mock_branch_states,
        mock_vendor_state,
        mock_dimension_codes,
        mock_bill_snapshot,
    ):
        """Do not send a Service GRPO payload when SAP Variety cannot be resolved."""
        mock_branch_states.return_value = {2: "HR"}
        mock_vendor_state.return_value = "HR"

        def dimension_codes(company_code, dim_code):
            return {
                1: {"BEV": "Beverage"},
                2: {"05-2026": "05-2026"},
            }.get(dim_code, {})

        mock_dimension_codes.side_effect = dimension_codes
        mock_bill_snapshot.return_value = {
            "doc_num": "626050517",
            "state": "HR",
            "card_code": "CUST001",
            "item_summary": "Transport freight",
        }

        dispatch_plan = DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=626050517,
            sap_invoice_doc_num="626050517",
            booking_status=DispatchPlanStatus.BOOKED,
            place_of_supply="HR",
            total_freight=Decimal("1000.00"),
        )

        service = GRPOService(company_code="TC001")
        with self.assertRaisesMessage(ValueError, "SAP Variety is required"):
            service.post_service_grpo(
                dispatch_plan_id=dispatch_plan.id,
                user=self.user,
                vendor_code="VENDA000956",
                branch_id=2,
                service_description="Transport freight",
                amount=Decimal("1000.00"),
                tax_code="GST05R",
                gl_account="5670001",
                place_of_supply="HR",
                effective_month="2026-05",
                location_code=2,
                location_name="HARYANA",
                sac_entry=40,
                sac_code="9965",
                product_variety="Oil",
                include_bilty_attachment=False,
            )

        mock_sap_client.return_value.create_grpo.assert_not_called()

    def test_get_grpo_preview_data(self):
        """Test getting GRPO preview data returns all PO details for pre-fill"""
        service = GRPOService(company_code="TC001")
        preview_data = service.get_grpo_preview_data(self.vehicle_entry.id)

        self.assertEqual(len(preview_data), 1)
        po_data = preview_data[0]

        # PO header fields
        self.assertEqual(po_data["po_number"], "PO-001")
        self.assertEqual(po_data["supplier_code"], "SUP001")
        self.assertEqual(po_data["sap_doc_entry"], 12345)
        self.assertEqual(po_data["branch_id"], 1)
        self.assertEqual(po_data["vendor_ref"], "VINV-2026-001")
        self.assertTrue(po_data["is_ready_for_grpo"])

        # Item-level pre-filled fields
        self.assertEqual(len(po_data["items"]), 1)
        item = po_data["items"][0]
        self.assertEqual(item["unit_price"], Decimal("85.500000"))
        self.assertEqual(item["tax_code"], "GST18")
        self.assertEqual(item["warehouse_code"], "WH-01")
        self.assertEqual(item["gl_account"], "40001001")
        self.assertEqual(item["sap_line_num"], 0)

    def test_get_grpo_preview_invalid_entry(self):
        """Test getting preview data for non-existent entry"""
        service = GRPOService(company_code="TC001")

        with self.assertRaises(ValueError) as context:
            service.get_grpo_preview_data(99999)

        self.assertIn("not found", str(context.exception))

    @patch('grpo.services.SAPClient')
    def test_post_grpo_success_with_po_linking(self, mock_sap_client):
        """Test successful GRPO posting includes PO linking fields"""
        mock_instance = MagicMock()
        mock_instance.create_grpo.return_value = {
            "DocEntry": 123,
            "DocNum": 456,
            "DocTotal": 4750.00
        }
        mock_sap_client.return_value = mock_instance

        service = GRPOService(company_code="TC001")
        grpo = service.post_grpo(
            vehicle_entry_id=self.vehicle_entry.id,
            po_receipt_ids=[self.po_receipt.id],
            user=self.user,
            items=[{
                "po_item_receipt_id": self.po_item.id,
                "accepted_qty": Decimal("95.000"),
            }],
            branch_id=1,
            warehouse_code="WH01",
        )

        self.assertEqual(grpo.status, GRPOStatus.POSTED)
        self.assertEqual(grpo.sap_doc_entry, 123)
        self.assertEqual(grpo.sap_doc_num, 456)

        # Verify SAP payload includes PO linking
        call_args = mock_instance.create_grpo.call_args[0][0]
        doc_line = call_args["DocumentLines"][0]
        self.assertEqual(doc_line["BaseEntry"], 12345)
        self.assertEqual(doc_line["BaseLine"], 0)
        self.assertEqual(doc_line["BaseType"], 22)
        self.assertEqual(doc_line["WarehouseCode"], "WH01")

        # Verify structured comments
        self.assertIn("App: FactoryApp v2", call_args["Comments"])
        self.assertIn("PO: PO-001", call_args["Comments"])

        # Check line posting created with base_entry/base_line
        self.assertEqual(grpo.lines.count(), 1)
        line = grpo.lines.first()
        self.assertEqual(line.quantity_posted, Decimal("95.000"))
        self.assertEqual(line.base_entry, 12345)
        self.assertEqual(line.base_line, 0)

    @patch('grpo.services.SAPClient')
    def test_post_grpo_uses_material_attachment_metadata_fallback(self, mock_sap_client):
        """Material GRPO keeps posting when SAP needs metadata-only attachment fallback."""
        mock_instance = MagicMock()
        mock_instance.upload_attachment.return_value = {"AbsoluteEntry": 789}
        mock_instance.create_grpo.return_value = {
            "DocEntry": 123,
            "DocNum": 456,
            "DocTotal": 4750.00
        }
        mock_sap_client.return_value = mock_instance

        service = GRPOService(company_code="TC001")
        test_file = SimpleUploadedFile(
            "invoice.pdf",
            b"pdf_content",
            content_type="application/pdf",
        )
        grpo = service.post_grpo(
            vehicle_entry_id=self.vehicle_entry.id,
            po_receipt_ids=[self.po_receipt.id],
            user=self.user,
            items=[{
                "po_item_receipt_id": self.po_item.id,
                "accepted_qty": Decimal("95.000"),
            }],
            branch_id=1,
            attachments=[test_file],
        )

        self.assertEqual(grpo.status, GRPOStatus.POSTED)
        self.assertTrue(
            mock_instance.upload_attachment.call_args.kwargs["allow_metadata_fallback"]
        )
        payload = mock_instance.create_grpo.call_args.args[0]
        self.assertEqual(payload["AttachmentEntry"], 789)
        attachment = grpo.attachments.get()
        self.assertEqual(attachment.sap_attachment_status, SAPAttachmentStatus.LINKED)
        self.assertEqual(attachment.sap_absolute_entry, 789)
        attachment.file.delete(save=False)

    @patch('grpo.services.SAPClient')
    def test_post_grpo_does_not_require_weighment(self, mock_sap_client):
        """Material GRPO can be posted without a gate weighment row."""
        mock_instance = MagicMock()
        mock_instance.create_grpo.return_value = {
            "DocEntry": 125,
            "DocNum": 458,
            "DocTotal": 2500.00
        }
        mock_sap_client.return_value = mock_instance

        entry = VehicleEntry.objects.create(
            entry_no="VE-2024-NOWEIGH",
            company=self.company,
            vehicle=self.vehicle,
            driver=self.driver,
            entry_type="RAW_MATERIAL",
            status=GateEntryStatus.COMPLETED,
        )
        po_receipt = POReceipt.objects.create(
            vehicle_entry=entry,
            po_number="PO-NOWEIGH-001",
            supplier_code="SUP001",
            supplier_name="Test Supplier",
            sap_doc_entry=22345,
            branch_id=1,
        )
        po_item = POItemReceipt.objects.create(
            po_receipt=po_receipt,
            po_item_code="ITEM-NW",
            item_name="No Weighment Item",
            ordered_qty=Decimal("50.000"),
            received_qty=Decimal("50.000"),
            accepted_qty=Decimal("50.000"),
            rejected_qty=Decimal("0.000"),
            sap_line_num=0,
            unit_price=Decimal("50.000000"),
            uom="KG",
        )

        service = GRPOService(company_code="TC001")
        grpo = service.post_grpo(
            vehicle_entry_id=entry.id,
            po_receipt_ids=[po_receipt.id],
            user=self.user,
            items=[{
                "po_item_receipt_id": po_item.id,
                "accepted_qty": Decimal("50.000"),
            }],
            branch_id=1,
        )

        self.assertEqual(grpo.status, GRPOStatus.POSTED)
        self.assertFalse(Weighment.objects.filter(vehicle_entry=entry).exists())

    @patch('grpo.services.SAPClient')
    def test_post_grpo_updates_tare_weight_on_weighment(self, mock_sap_client):
        """GRPO tare input should update the same weighment row and recalculate net."""
        mock_instance = MagicMock()
        mock_instance.create_grpo.return_value = {
            "DocEntry": 124,
            "DocNum": 457,
            "DocTotal": 4750.00
        }
        mock_sap_client.return_value = mock_instance

        weighment = Weighment.objects.get(vehicle_entry=self.vehicle_entry)
        weighment.tare_weight = None
        weighment.save()

        service = GRPOService(company_code="TC001")
        service.post_grpo(
            vehicle_entry_id=self.vehicle_entry.id,
            po_receipt_ids=[self.po_receipt.id],
            user=self.user,
            items=[{
                "po_item_receipt_id": self.po_item.id,
                "accepted_qty": Decimal("95.000"),
            }],
            branch_id=1,
            tare_weight=Decimal("2500.000"),
        )

        weighment.refresh_from_db()
        self.assertEqual(weighment.tare_weight, Decimal("2500.000"))
        self.assertEqual(weighment.net_weight, Decimal("7500.000"))

    @patch('grpo.services.SAPClient')
    def test_post_grpo_with_line_level_fields(self, mock_sap_client):
        """Test GRPO posting with unit_price, tax_code, gl_account, variety"""
        mock_instance = MagicMock()
        mock_instance.create_grpo.return_value = {
            "DocEntry": 200,
            "DocNum": 500,
            "DocTotal": 9500.00
        }
        mock_sap_client.return_value = mock_instance

        service = GRPOService(company_code="TC001")
        grpo = service.post_grpo(
            vehicle_entry_id=self.vehicle_entry.id,
            po_receipt_ids=[self.po_receipt.id],
            user=self.user,
            items=[{
                "po_item_receipt_id": self.po_item.id,
                "accepted_qty": Decimal("95.000"),
                "unit_price": Decimal("100.50"),
                "tax_code": "GST18",
                "gl_account": "500100",
                "variety": "Grade-A",
            }],
            branch_id=1,
            vendor_ref="INV-2024-001",
        )

        call_args = mock_instance.create_grpo.call_args[0][0]

        # Header fields
        self.assertEqual(call_args["NumAtCard"], "INV-2024-001")

        # Line-level fields
        doc_line = call_args["DocumentLines"][0]
        self.assertEqual(doc_line["UnitPrice"], 100.50)
        self.assertEqual(doc_line["TaxCode"], "GST18")
        self.assertEqual(doc_line["AccountCode"], "500100")
        self.assertEqual(doc_line["U_Variety"], "Grade-A")

    @patch('grpo.services.SAPClient')
    def test_post_grpo_with_extra_charges(self, mock_sap_client):
        """Test GRPO posting with DocumentAdditionalExpenses"""
        mock_instance = MagicMock()
        mock_instance.create_grpo.return_value = {
            "DocEntry": 300,
            "DocNum": 600,
            "DocTotal": 15000.00
        }
        mock_sap_client.return_value = mock_instance

        service = GRPOService(company_code="TC001")
        grpo = service.post_grpo(
            vehicle_entry_id=self.vehicle_entry.id,
            po_receipt_ids=[self.po_receipt.id],
            user=self.user,
            items=[{
                "po_item_receipt_id": self.po_item.id,
                "accepted_qty": Decimal("95.000"),
            }],
            branch_id=1,
            extra_charges=[
                {
                    "expense_code": 1,
                    "amount": Decimal("5000.00"),
                    "remarks": "Freight",
                    "tax_code": "GST18"
                }
            ],
        )

        call_args = mock_instance.create_grpo.call_args[0][0]
        self.assertIn("DocumentAdditionalExpenses", call_args)
        expense = call_args["DocumentAdditionalExpenses"][0]
        self.assertEqual(expense["ExpenseCode"], 1)
        self.assertEqual(expense["LineTotal"], 5000.00)
        self.assertEqual(expense["Remarks"], "Freight")
        self.assertEqual(expense["TaxCode"], "GST18")

    def test_post_grpo_already_posted(self):
        """Test posting GRPO that was already posted"""
        GRPOPosting.objects.create(
            vehicle_entry=self.vehicle_entry,
            po_receipt=self.po_receipt,
            status=GRPOStatus.POSTED,
            sap_doc_num=789
        )

        service = GRPOService(company_code="TC001")

        with self.assertRaises(ValueError) as context:
            service.post_grpo(
                vehicle_entry_id=self.vehicle_entry.id,
                po_receipt_ids=[self.po_receipt.id],
                user=self.user,
                items=[{
                    "po_item_receipt_id": self.po_item.id,
                    "accepted_qty": Decimal("95.000"),
                }],
                branch_id=1,
            )

        self.assertIn("already posted", str(context.exception))

    def test_post_grpo_entry_not_completed(self):
        """Test posting GRPO for non-completed entry"""
        self.vehicle_entry.status = GateEntryStatus.IN_PROGRESS
        self.vehicle_entry.save()

        service = GRPOService(company_code="TC001")

        with self.assertRaises(ValueError) as context:
            service.post_grpo(
                vehicle_entry_id=self.vehicle_entry.id,
                po_receipt_ids=[self.po_receipt.id],
                user=self.user,
                items=[{
                    "po_item_receipt_id": self.po_item.id,
                    "accepted_qty": Decimal("95.000"),
                }],
                branch_id=1,
            )

        self.assertIn("not completed", str(context.exception))

        # Reset status
        self.vehicle_entry.status = GateEntryStatus.COMPLETED
        self.vehicle_entry.save()

    def test_structured_comments(self):
        """Test structured comments building"""
        service = GRPOService(company_code="TC001")
        comments = service._build_structured_comments(
            self.user, [self.po_receipt], self.vehicle_entry, "QC passed"
        )
        self.assertIn("App: FactoryApp v2", comments)
        self.assertIn("PO: PO-001", comments)
        self.assertIn("Gate Entry: VE-2024-001", comments)
        self.assertIn("QC passed", comments)

    def test_structured_comments_are_sap_safe_length(self):
        """Generated SAP document comments should not exceed SAP's short field limit."""
        service = GRPOService(company_code="TC001")
        comments = service._build_structured_comments(
            self.user,
            [self.po_receipt],
            self.vehicle_entry,
            "x" * 500,
        )

        self.assertLessEqual(len(comments), service.SAP_DOCUMENT_COMMENTS_MAX_LENGTH)
        self.assertTrue(comments.endswith("..."))

    def test_service_structured_comments_only_include_app_and_user(self):
        """Service GRPO auto-comments should stay minimal because line fields hold details."""
        service = GRPOService(company_code="TC001")
        dispatch_plan = MagicMock()
        dispatch_plan.sap_invoice_doc_num = "726055003"
        dispatch_plan.sap_invoice_doc_entry = 12345
        dispatch_plan.vehicle_no = "DL1LAB1234"
        dispatch_plan.bilty_no = "NCR-1092"

        comments = service._build_service_structured_comments(
            self.user,
            dispatch_plan,
            "x" * 500,
            {
                "invoice_number": "726055003",
                "eway_bill": "372213652647,302213652240",
                "place_of_supply": "HR",
                "delivery_point": "Bakhapur Sonipat",
                "location_name": "HARYANA",
                "sac_code": "9965",
                "product_variety": "Oil",
                "effective_month": "2026-05-01",
                "total_litres": Decimal("0"),
                "charged_weight": Decimal("1275"),
            },
        )

        full_name = self.user.get_full_name() if hasattr(self.user, "get_full_name") else str(self.user)
        username = getattr(self.user, "username", getattr(self.user, "email", str(self.user)))
        expected_user = f"{full_name} ({username})"
        self.assertEqual(comments, f"App: JI | User: {expected_user}")
        self.assertNotIn("Dispatch Bill", comments)
        self.assertNotIn("Effective Month", comments)

    def test_service_grpo_effective_month_uses_month_year_api_format(self):
        """Service GRPO API should accept and render effective month as YYYY-MM."""
        serializer = ServiceGRPOPostRequestSerializer(data={
            "dispatch_plan_id": 1,
            "vendor_code": "VENDA000807",
            "branch_id": 2,
            "service_description": "Transport freight",
            "amount": "1000.00",
            "effective_month": "2026-05",
        })

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["effective_month"].isoformat(), "2026-05-01")

        field = ServiceGRPOPreviewSerializer().fields["default_effective_month"]
        self.assertEqual(field.to_representation(serializer.validated_data["effective_month"]), "2026-05")

    def test_service_grpo_infers_beverage_water_from_mineral_item_summary(self):
        service = GRPOService(company_code="TC001")
        item_summary = "FG0000324 - PET BOTTLE 500 ML JIVO NATURAL MINERAL SPECIAL EDITION"

        product_variety = service._infer_product_variety(item_summary)

        self.assertEqual(product_variety, "Beverage")
        self.assertEqual(
            service._infer_service_description(item_summary, product_variety),
            "Water",
        )

    @patch.object(GRPOService, "_get_active_budget_codes", return_value={})
    @patch.object(
        GRPOService,
        "_get_active_dimension_codes",
        return_value={
            "MUSTARD": "Mustard Oil",
            "05-2026": "05-2026",
        },
    )
    @patch.object(
        GRPOService,
        "_get_dispatch_bill_snapshot",
        return_value={
            "doc_num": "626050517",
            "state": "HR",
            "city": "SONIPAT",
            "card_code": "CUST001",
            "card_name": "Test Customer",
            "item_summary": "COLD PRESS MUSTARD OIL",
            "total_litres": "1000.000",
            "total_weight": "900.000",
            "doc_total": "50000.00",
        },
    )
    def test_service_grpo_display_falls_back_to_linked_vehicle_entry(
        self,
        _mock_snapshot,
        _mock_dimension_codes,
        _mock_budget_codes,
    ):
        transporter = Transporter.objects.create(
            name="ARNAV TRANSPORT SERVICE",
            contact_person="Arnav Contact",
            mobile_no="9811111111",
            gstin="07ABCDE1234F1Z5",
        )
        linked_vehicle = Vehicle.objects.create(
            vehicle_number="HR55AA1234",
            vehicle_type=self.vehicle_type,
            transporter=transporter,
        )
        linked_driver = Driver.objects.create(
            name="Ramesh Driver",
            mobile_no="9898989898",
            license_no="DL0420260001",
        )
        linked_entry = VehicleEntry.objects.create(
            entry_no="VE-DISP-001",
            company=self.company,
            vehicle=linked_vehicle,
            driver=linked_driver,
            entry_type="SALES_DISPATCH",
            status=GateEntryStatus.COMPLETED,
        )
        dispatch_plan = DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=626050517,
            sap_invoice_doc_num="626050517",
            booking_status=DispatchPlanStatus.BOOKED,
            linked_vehicle_entry=linked_entry,
            vehicle_no="",
            driver_name="",
            transporter_name="",
            transporter_gstin="",
            bilty_no="BLTY-001",
            bilty_date=date(2026, 5, 1),
            freight=Decimal("1250.00"),
            total_freight=Decimal("1250.00"),
        )
        service = GRPOService(company_code="TC001")

        pending_plan = service.get_pending_service_grpo_entries()[0]
        self.assertEqual(
            service.get_service_display_vehicle_no(pending_plan),
            "HR55AA1234",
        )
        self.assertEqual(
            service.get_service_display_driver_name(pending_plan),
            "Ramesh Driver",
        )
        self.assertEqual(
            service.get_service_display_linked_entry_no(pending_plan),
            "VE-DISP-001",
        )

        preview = service.get_service_grpo_preview_data(dispatch_plan.id)
        self.assertEqual(preview["vehicle_no"], "HR55AA1234")
        self.assertEqual(preview["driver_name"], "Ramesh Driver")
        self.assertEqual(preview["transporter_name"], "ARNAV TRANSPORT SERVICE")
        self.assertEqual(preview["transporter_gstin"], "07ABCDE1234F1Z5")
        self.assertEqual(preview["linked_vehicle_entry_id"], linked_entry.id)
        self.assertEqual(preview["linked_vehicle_entry_no"], "VE-DISP-001")

        serializer_data = ServiceGRPOPreviewSerializer(preview).data
        self.assertEqual(serializer_data["vehicle_no"], "HR55AA1234")
        self.assertEqual(serializer_data["driver_name"], "Ramesh Driver")
        self.assertEqual(serializer_data["transporter_name"], "ARNAV TRANSPORT SERVICE")
        self.assertEqual(serializer_data["transporter_gstin"], "07ABCDE1234F1Z5")
        self.assertEqual(serializer_data["linked_vehicle_entry_id"], linked_entry.id)
        self.assertEqual(serializer_data["linked_vehicle_entry_no"], "VE-DISP-001")


class GRPOAPITests(APITestCase):
    """Tests for GRPO API endpoints"""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(
            name="Test Company",
            code="TC001"
        )

        cls.user = User.objects.create_user(
            email="testuser@example.com",
            password="testpass123",
            full_name="Test User",
            employee_code="EMP001"
        )

        cls.vehicle_type = VehicleType.objects.create(name="TRUCK")
        cls.vehicle = Vehicle.objects.create(
            vehicle_number="MH12AB1234",
            vehicle_type=cls.vehicle_type
        )

        cls.driver = Driver.objects.create(
            name="Test Driver",
            mobile_no="9876543210",
            license_no="DL123456"
        )

        cls.vehicle_entry = VehicleEntry.objects.create(
            entry_no="VE-2024-001",
            company=cls.company,
            vehicle=cls.vehicle,
            driver=cls.driver,
            entry_type="RAW_MATERIAL",
            status=GateEntryStatus.COMPLETED
        )

        cls.po_receipt = POReceipt.objects.create(
            vehicle_entry=cls.vehicle_entry,
            po_number="PO-001",
            supplier_code="SUP001",
            supplier_name="Test Supplier",
            sap_doc_entry=12345
        )

        cls.po_item = POItemReceipt.objects.create(
            po_receipt=cls.po_receipt,
            po_item_code="ITEM001",
            item_name="Test Item",
            ordered_qty=Decimal("100.000"),
            received_qty=Decimal("100.000"),
            accepted_qty=Decimal("95.000"),
            rejected_qty=Decimal("5.000"),
            sap_line_num=0,
            uom="KG"
        )

    def setUp(self):
        self.client = APIClient()

    def test_pending_grpo_list_unauthenticated(self):
        """Test that unauthenticated requests are rejected"""
        response = self.client.get("/api/v1/grpo/pending/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_grpo_preview_unauthenticated(self):
        """Test that unauthenticated requests are rejected"""
        response = self.client.get(f"/api/v1/grpo/preview/{self.vehicle_entry.id}/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_post_grpo_unauthenticated(self):
        """Test that unauthenticated requests are rejected"""
        response = self.client.post("/api/v1/grpo/post/", {
            "vehicle_entry_id": self.vehicle_entry.id,
            "po_receipt_id": self.po_receipt.id
        })
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_grpo_history_unauthenticated(self):
        """Test that unauthenticated requests are rejected"""
        response = self.client.get("/api/v1/grpo/history/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class GRPOSerializerTests(TestCase):
    """Tests for GRPO serializers"""

    def test_grpo_post_request_serializer_valid_full(self):
        """Test full GRPO post request with all new fields"""
        from grpo.serializers import GRPOPostRequestSerializer

        data = {
            "vehicle_entry_id": 1,
            "po_receipt_id": 2,
            "items": [
                {
                    "po_item_receipt_id": 10,
                    "accepted_qty": "95.000",
                    "unit_price": "50.00",
                    "tax_code": "GST18",
                    "gl_account": "500100",
                    "variety": "Grade-A"
                }
            ],
            "branch_id": 1,
            "warehouse_code": "WH01",
            "comments": "Test comment",
            "vendor_ref": "INV-2024-001",
            "tare_weight": "2500.000",
            "extra_charges": [
                {
                    "expense_code": 1,
                    "amount": "5000.00",
                    "remarks": "Freight",
                    "tax_code": "GST18"
                }
            ]
        }

        serializer = GRPOPostRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)

        # Verify parsed values
        vd = serializer.validated_data
        self.assertEqual(vd["vendor_ref"], "INV-2024-001")
        self.assertEqual(vd["tare_weight"], Decimal("2500.000"))
        self.assertEqual(len(vd["extra_charges"]), 1)
        self.assertEqual(vd["extra_charges"][0]["expense_code"], 1)
        self.assertEqual(vd["items"][0]["unit_price"], Decimal("50.00"))
        self.assertEqual(vd["items"][0]["variety"], "Grade-A")

    def test_extra_charge_rejects_zero_expense_code(self):
        """Extra charges must use a real SAP Additional Expense code."""
        from grpo.serializers import ServiceGRPOPostRequestSerializer

        data = {
            "dispatch_plan_id": 8,
            "vendor_code": "VENDA001571",
            "branch_id": 2,
            "service_description": "Oil",
            "amount": "8480.00",
            "extra_charges": [
                {
                    "expense_code": 0,
                    "amount": "2130.00",
                    "remarks": "unloading",
                }
            ],
        }

        serializer = ServiceGRPOPostRequestSerializer(data=data)

        self.assertFalse(serializer.is_valid())
        self.assertIn("extra_charges", serializer.errors)
        self.assertEqual(
            serializer.errors["extra_charges"][0]["expense_code"][0].code,
            "min_value",
        )

    def test_extra_charge_rejects_zero_amount(self):
        """Empty extra-charge rows should be rejected before SAP posting."""
        from grpo.serializers import GRPOPostRequestSerializer

        data = {
            "vehicle_entry_id": 1,
            "po_receipt_id": 2,
            "items": [{"po_item_receipt_id": 10, "accepted_qty": "95.000"}],
            "branch_id": 1,
            "tare_weight": "2500.000",
            "extra_charges": [
                {
                    "expense_code": 3,
                    "amount": "0.00",
                    "remarks": "Freight",
                }
            ],
        }

        serializer = GRPOPostRequestSerializer(data=data)

        self.assertFalse(serializer.is_valid())
        self.assertIn("extra_charges", serializer.errors)
        self.assertEqual(
            serializer.errors["extra_charges"][0]["amount"][0].code,
            "min_value",
        )

    def test_service_grpo_options_serializer_includes_expense_codes(self):
        """Service GRPO options should expose SAP additional expense codes."""
        from grpo.serializers import ServiceGRPOOptionsSerializer

        data = {
            "branches": [],
            "tax_codes": [],
            "gl_accounts": [],
            "sac_codes": [],
            "locations": [],
            "varieties": [
                {
                    "variety_code": "CRUDE",
                    "variety_name": "Crude Oil",
                }
            ],
            "projects": [],
            "sub_accounts": [],
            "expense_codes": [
                {
                    "expense_code": 3,
                    "expense_name": "FREIGHT OUTWARD",
                    "expense_account": "5670001",
                    "revenue_account": "4200009",
                    "sac_code": "00996791",
                }
            ],
        }

        serializer = ServiceGRPOOptionsSerializer(data)

        self.assertEqual(serializer.data["expense_codes"][0]["expense_code"], 3)
        self.assertEqual(
            serializer.data["expense_codes"][0]["expense_name"],
            "FREIGHT OUTWARD",
        )
        self.assertEqual(serializer.data["varieties"][0]["variety_code"], "CRUDE")

    def test_grpo_post_request_serializer_minimal(self):
        """Test minimal GRPO post request (only required fields)"""
        from grpo.serializers import GRPOPostRequestSerializer

        data = {
            "vehicle_entry_id": 1,
            "po_receipt_id": 2,
            "items": [
                {"po_item_receipt_id": 10, "accepted_qty": "95.000", "variety": "TMT-500D"}
            ],
            "branch_id": 1,
        }

        serializer = GRPOPostRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_grpo_post_request_serializer_allows_missing_tare_weight(self):
        """GRPO post payload does not require tare weight."""
        from grpo.serializers import GRPOPostRequestSerializer

        data = {
            "vehicle_entry_id": 1,
            "po_receipt_id": 2,
            "items": [
                {"po_item_receipt_id": 10, "accepted_qty": "95.000", "variety": "TMT-500D"}
            ],
            "branch_id": 1,
        }

        serializer = GRPOPostRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertNotIn("tare_weight", serializer.validated_data)

    def test_grpo_post_request_serializer_invalid(self):
        """Test invalid GRPO post request"""
        from grpo.serializers import GRPOPostRequestSerializer

        data = {
            "vehicle_entry_id": 1
            # Missing required po_receipt_ids/po_receipt_id, items, branch_id
        }

        serializer = GRPOPostRequestSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("items", serializer.errors)
        self.assertIn("branch_id", serializer.errors)

    def test_grpo_item_input_serializer_with_optional_fields(self):
        """Test item input serializer accepts optional fields"""
        from grpo.serializers import GRPOItemInputSerializer

        data = {
            "po_item_receipt_id": 1,
            "accepted_qty": "100.000",
            "unit_price": "50.50",
            "tax_code": "GST18",
            "gl_account": "500100",
            "variety": "Grade-A"
        }

        serializer = GRPOItemInputSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_extra_charge_serializer(self):
        """Test extra charge serializer validation"""
        from grpo.serializers import ExtraChargeInputSerializer

        data = {
            "expense_code": 1,
            "amount": "5000.00",
            "remarks": "Freight charges",
            "tax_code": "GST18"
        }

        serializer = ExtraChargeInputSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)


class GRPOAttachmentModelTests(TestCase):
    """Tests for GRPOAttachment model"""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Company", code="TC001")
        cls.user = User.objects.create_user(
            email="testuser@example.com", password="testpass123",
            full_name="Test User", employee_code="EMP001"
        )
        cls.vehicle_type = VehicleType.objects.create(name="TRUCK")
        cls.vehicle = Vehicle.objects.create(
            vehicle_number="MH12AB1234", vehicle_type=cls.vehicle_type
        )
        cls.driver = Driver.objects.create(
            name="Test Driver", mobile_no="9876543210", license_no="DL123456"
        )
        cls.vehicle_entry = VehicleEntry.objects.create(
            entry_no="VE-2024-001", company=cls.company,
            vehicle=cls.vehicle, driver=cls.driver,
            entry_type="RAW_MATERIAL", status=GateEntryStatus.COMPLETED
        )
        cls.po_receipt = POReceipt.objects.create(
            vehicle_entry=cls.vehicle_entry, po_number="PO-001",
            supplier_code="SUP001", supplier_name="Test Supplier",
            sap_doc_entry=12345
        )
        cls.grpo_posting = GRPOPosting.objects.create(
            vehicle_entry=cls.vehicle_entry, po_receipt=cls.po_receipt,
            status=GRPOStatus.POSTED, sap_doc_entry=12345, sap_doc_num=456
        )

    def test_attachment_creation(self):
        """Test GRPOAttachment model creation"""
        test_file = SimpleUploadedFile("invoice.pdf", b"file_content", content_type="application/pdf")
        attachment = GRPOAttachment.objects.create(
            grpo_posting=self.grpo_posting,
            file=test_file,
            original_filename="invoice.pdf",
            sap_attachment_status=SAPAttachmentStatus.PENDING,
            uploaded_by=self.user
        )
        self.assertEqual(attachment.original_filename, "invoice.pdf")
        self.assertEqual(attachment.sap_attachment_status, SAPAttachmentStatus.PENDING)
        self.assertIsNone(attachment.sap_absolute_entry)
        # Clean up file
        attachment.file.delete(save=False)

    def test_attachment_str(self):
        """Test GRPOAttachment string representation"""
        test_file = SimpleUploadedFile("report.pdf", b"content", content_type="application/pdf")
        attachment = GRPOAttachment.objects.create(
            grpo_posting=self.grpo_posting,
            file=test_file,
            original_filename="report.pdf",
        )
        self.assertIn("report.pdf", str(attachment))
        attachment.file.delete(save=False)

    def test_attachment_cascade_delete(self):
        """Test attachments are deleted when GRPO posting is deleted"""
        # Create a new posting for this test
        po_receipt2 = POReceipt.objects.create(
            vehicle_entry=self.vehicle_entry, po_number="PO-002",
            supplier_code="SUP001", supplier_name="Test Supplier",
            sap_doc_entry=99999
        )
        posting = GRPOPosting.objects.create(
            vehicle_entry=self.vehicle_entry, po_receipt=po_receipt2,
            status=GRPOStatus.POSTED, sap_doc_entry=99999
        )
        test_file = SimpleUploadedFile("test.pdf", b"content")
        GRPOAttachment.objects.create(
            grpo_posting=posting, file=test_file,
            original_filename="test.pdf"
        )
        posting_id = posting.id
        self.assertEqual(GRPOAttachment.objects.filter(grpo_posting_id=posting_id).count(), 1)
        posting.delete()
        self.assertEqual(GRPOAttachment.objects.filter(grpo_posting_id=posting_id).count(), 0)


class GRPOAttachmentServiceTests(TestCase):
    """Tests for GRPO attachment service methods"""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Company", code="TC001")
        cls.user = User.objects.create_user(
            email="testuser@example.com", password="testpass123",
            full_name="Test User", employee_code="EMP001"
        )
        cls.vehicle_type = VehicleType.objects.create(name="TRUCK")
        cls.vehicle = Vehicle.objects.create(
            vehicle_number="MH12AB1234", vehicle_type=cls.vehicle_type
        )
        cls.driver = Driver.objects.create(
            name="Test Driver", mobile_no="9876543210", license_no="DL123456"
        )
        cls.vehicle_entry = VehicleEntry.objects.create(
            entry_no="VE-2024-001", company=cls.company,
            vehicle=cls.vehicle, driver=cls.driver,
            entry_type="RAW_MATERIAL", status=GateEntryStatus.COMPLETED
        )
        cls.po_receipt = POReceipt.objects.create(
            vehicle_entry=cls.vehicle_entry, po_number="PO-001",
            supplier_code="SUP001", supplier_name="Test Supplier",
            sap_doc_entry=12345
        )

    def _create_posted_grpo(self):
        """Helper to create a POSTED GRPO"""
        grpo, _ = GRPOPosting.objects.get_or_create(
            vehicle_entry=self.vehicle_entry,
            po_receipt=self.po_receipt,
            defaults={
                "status": GRPOStatus.POSTED,
                "sap_doc_entry": 12345,
                "sap_doc_num": 456,
                "posted_by": self.user
            }
        )
        if grpo.status != GRPOStatus.POSTED:
            grpo.status = GRPOStatus.POSTED
            grpo.sap_doc_entry = 12345
            grpo.sap_doc_num = 456
            grpo.save()
        return grpo

    @patch('grpo.services.SAPClient')
    def test_upload_attachment_success(self, mock_sap_client):
        """Test successful attachment upload and linking"""
        mock_instance = MagicMock()
        mock_instance.get_grpo_attachment_entry.return_value = None
        mock_instance.upload_attachment.return_value = {"AbsoluteEntry": 789}
        mock_instance.link_attachment_to_grpo.return_value = {
            "DocEntry": 12345, "AttachmentEntry": 789
        }
        mock_sap_client.return_value = mock_instance

        grpo = self._create_posted_grpo()
        service = GRPOService(company_code="TC001")
        test_file = SimpleUploadedFile("invoice.pdf", b"pdf_content", content_type="application/pdf")

        attachment = service.upload_grpo_attachment(
            grpo_posting_id=grpo.id,
            file=test_file,
            user=self.user
        )

        self.assertEqual(attachment.sap_attachment_status, SAPAttachmentStatus.LINKED)
        self.assertEqual(attachment.sap_absolute_entry, 789)
        self.assertEqual(attachment.original_filename, "invoice.pdf")
        self.assertIsNone(attachment.sap_error_message)

        # Verify SAP calls
        mock_instance.upload_attachment.assert_called_once()
        mock_instance.link_attachment_to_grpo.assert_called_once_with(
            doc_entry=12345, absolute_entry=789
        )

        # Clean up
        attachment.file.delete(save=False)

    @patch('grpo.services.SAPClient')
    def test_upload_attachment_sap_failure(self, mock_sap_client):
        """Test attachment upload when SAP fails - file should be saved locally"""
        from sap_client.exceptions import SAPConnectionError

        mock_instance = MagicMock()
        mock_instance.get_grpo_attachment_entry.return_value = None
        mock_instance.upload_attachment.side_effect = SAPConnectionError("SAP unavailable")
        mock_sap_client.return_value = mock_instance

        grpo = self._create_posted_grpo()
        service = GRPOService(company_code="TC001")
        test_file = SimpleUploadedFile("invoice.pdf", b"pdf_content")

        attachment = service.upload_grpo_attachment(
            grpo_posting_id=grpo.id,
            file=test_file,
            user=self.user
        )

        # File should be saved locally even though SAP failed
        self.assertEqual(attachment.sap_attachment_status, SAPAttachmentStatus.FAILED)
        self.assertIsNotNone(attachment.sap_error_message)
        self.assertIn("SAP unavailable", attachment.sap_error_message)
        self.assertTrue(attachment.file)

        # Clean up
        attachment.file.delete(save=False)

    def test_upload_attachment_non_posted_grpo(self):
        """Test that attachments cannot be added to non-POSTED GRPOs"""
        grpo = self._create_posted_grpo()
        grpo.status = GRPOStatus.PENDING
        grpo.save()

        service = GRPOService(company_code="TC001")
        test_file = SimpleUploadedFile("invoice.pdf", b"content")

        with self.assertRaises(ValueError) as context:
            service.upload_grpo_attachment(
                grpo_posting_id=grpo.id,
                file=test_file,
                user=self.user
            )
        self.assertIn("Only POSTED", str(context.exception))

    def test_upload_attachment_no_sap_doc_entry(self):
        """Test that attachments need SAP DocEntry"""
        grpo = self._create_posted_grpo()
        grpo.sap_doc_entry = None
        grpo.save()

        service = GRPOService(company_code="TC001")
        test_file = SimpleUploadedFile("invoice.pdf", b"content")

        with self.assertRaises(ValueError) as context:
            service.upload_grpo_attachment(
                grpo_posting_id=grpo.id,
                file=test_file,
                user=self.user
            )
        self.assertIn("no SAP DocEntry", str(context.exception))

    @patch('grpo.services.SAPClient')
    def test_retry_attachment_upload_success(self, mock_sap_client):
        """Test retrying a failed attachment upload"""
        mock_instance = MagicMock()
        mock_instance.get_grpo_attachment_entry.return_value = None
        mock_instance.upload_attachment.return_value = {"AbsoluteEntry": 999}
        mock_instance.link_attachment_to_grpo.return_value = {
            "DocEntry": 12345, "AttachmentEntry": 999
        }
        mock_sap_client.return_value = mock_instance

        grpo = self._create_posted_grpo()
        test_file = SimpleUploadedFile("invoice.pdf", b"content")
        attachment = GRPOAttachment.objects.create(
            grpo_posting=grpo, file=test_file,
            original_filename="invoice.pdf",
            sap_attachment_status=SAPAttachmentStatus.FAILED,
            sap_error_message="Previous error"
        )

        service = GRPOService(company_code="TC001")
        result = service.retry_attachment_upload(attachment_id=attachment.id)

        self.assertEqual(result.sap_attachment_status, SAPAttachmentStatus.LINKED)
        self.assertEqual(result.sap_absolute_entry, 999)
        self.assertIsNone(result.sap_error_message)

        # Clean up
        result.file.delete(save=False)

    @patch('grpo.services.SAPClient')
    def test_retry_skips_upload_if_already_uploaded(self, mock_sap_client):
        """Test retry skips upload when sap_absolute_entry already set"""
        mock_instance = MagicMock()
        mock_instance.get_grpo_attachment_entry.return_value = None
        mock_instance.link_attachment_to_grpo.return_value = {
            "DocEntry": 12345, "AttachmentEntry": 555
        }
        mock_sap_client.return_value = mock_instance

        grpo = self._create_posted_grpo()
        test_file = SimpleUploadedFile("invoice.pdf", b"content")
        attachment = GRPOAttachment.objects.create(
            grpo_posting=grpo, file=test_file,
            original_filename="invoice.pdf",
            sap_attachment_status=SAPAttachmentStatus.FAILED,
            sap_absolute_entry=555,  # Already uploaded
            sap_error_message="Link failed"
        )

        service = GRPOService(company_code="TC001")
        result = service.retry_attachment_upload(attachment_id=attachment.id)

        # Upload should NOT be called since absolute_entry exists
        mock_instance.upload_attachment.assert_not_called()
        mock_instance.link_attachment_to_grpo.assert_called_once_with(
            doc_entry=12345, absolute_entry=555
        )
        self.assertEqual(result.sap_attachment_status, SAPAttachmentStatus.LINKED)

        # Clean up
        result.file.delete(save=False)

    def test_retry_already_linked(self):
        """Test that already linked attachments cannot be retried"""
        grpo = self._create_posted_grpo()
        test_file = SimpleUploadedFile("invoice.pdf", b"content")
        attachment = GRPOAttachment.objects.create(
            grpo_posting=grpo, file=test_file,
            original_filename="invoice.pdf",
            sap_attachment_status=SAPAttachmentStatus.LINKED,
            sap_absolute_entry=789
        )

        service = GRPOService(company_code="TC001")
        with self.assertRaises(ValueError) as context:
            service.retry_attachment_upload(attachment_id=attachment.id)
        self.assertIn("already", str(context.exception))

        # Clean up
        attachment.file.delete(save=False)


class GRPOAttachmentAPITests(APITestCase):
    """Tests for GRPO attachment API endpoints"""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Company", code="TC001")
        cls.user = User.objects.create_user(
            email="testuser@example.com", password="testpass123",
            full_name="Test User", employee_code="EMP001"
        )
        cls.vehicle_type = VehicleType.objects.create(name="TRUCK")
        cls.vehicle = Vehicle.objects.create(
            vehicle_number="MH12AB1234", vehicle_type=cls.vehicle_type
        )
        cls.driver = Driver.objects.create(
            name="Test Driver", mobile_no="9876543210", license_no="DL123456"
        )
        cls.vehicle_entry = VehicleEntry.objects.create(
            entry_no="VE-2024-001", company=cls.company,
            vehicle=cls.vehicle, driver=cls.driver,
            entry_type="RAW_MATERIAL", status=GateEntryStatus.COMPLETED
        )
        cls.po_receipt = POReceipt.objects.create(
            vehicle_entry=cls.vehicle_entry, po_number="PO-001",
            supplier_code="SUP001", supplier_name="Test Supplier",
            sap_doc_entry=12345
        )
        cls.grpo_posting = GRPOPosting.objects.create(
            vehicle_entry=cls.vehicle_entry, po_receipt=cls.po_receipt,
            status=GRPOStatus.POSTED, sap_doc_entry=12345, sap_doc_num=456
        )

    def setUp(self):
        self.client = APIClient()

    def test_attachment_list_unauthenticated(self):
        """Test unauthenticated access to attachment list"""
        url = f"/api/v1/grpo/{self.grpo_posting.id}/attachments/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_attachment_upload_unauthenticated(self):
        """Test unauthenticated upload is rejected"""
        url = f"/api/v1/grpo/{self.grpo_posting.id}/attachments/"
        test_file = SimpleUploadedFile("invoice.pdf", b"content")
        response = self.client.post(url, {"file": test_file}, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_attachment_delete_unauthenticated(self):
        """Test unauthenticated delete is rejected"""
        url = f"/api/v1/grpo/{self.grpo_posting.id}/attachments/1/"
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_attachment_retry_unauthenticated(self):
        """Test unauthenticated retry is rejected"""
        url = f"/api/v1/grpo/{self.grpo_posting.id}/attachments/1/retry/"
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class GRPOPostingSerializerWithAttachmentsTest(TestCase):
    """Test that GRPOPostingSerializer includes attachments"""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Company", code="TC001")
        cls.user = User.objects.create_user(
            email="testuser@example.com", password="testpass123",
            full_name="Test User", employee_code="EMP001"
        )
        cls.vehicle_type = VehicleType.objects.create(name="TRUCK")
        cls.vehicle = Vehicle.objects.create(
            vehicle_number="MH12AB1234", vehicle_type=cls.vehicle_type
        )
        cls.driver = Driver.objects.create(
            name="Test Driver", mobile_no="9876543210", license_no="DL123456"
        )
        cls.vehicle_entry = VehicleEntry.objects.create(
            entry_no="VE-2024-001", company=cls.company,
            vehicle=cls.vehicle, driver=cls.driver,
            entry_type="RAW_MATERIAL", status=GateEntryStatus.COMPLETED
        )
        cls.po_receipt = POReceipt.objects.create(
            vehicle_entry=cls.vehicle_entry, po_number="PO-001",
            supplier_code="SUP001", supplier_name="Test Supplier",
            sap_doc_entry=12345
        )

    def test_posting_serializer_includes_attachments(self):
        """Test that GRPOPostingSerializer includes attachments field"""
        from grpo.serializers import GRPOPostingSerializer

        grpo = GRPOPosting.objects.create(
            vehicle_entry=self.vehicle_entry, po_receipt=self.po_receipt,
            status=GRPOStatus.POSTED, sap_doc_entry=12345, sap_doc_num=456
        )

        # Create an attachment
        test_file = SimpleUploadedFile("invoice.pdf", b"content")
        GRPOAttachment.objects.create(
            grpo_posting=grpo, file=test_file,
            original_filename="invoice.pdf",
            sap_attachment_status=SAPAttachmentStatus.LINKED,
            sap_absolute_entry=789
        )

        serializer = GRPOPostingSerializer(grpo)
        data = serializer.data

        self.assertIn("attachments", data)
        self.assertEqual(len(data["attachments"]), 1)
        self.assertEqual(data["attachments"][0]["original_filename"], "invoice.pdf")
        self.assertEqual(data["attachments"][0]["sap_attachment_status"], "LINKED")
        self.assertEqual(data["attachments"][0]["sap_absolute_entry"], 789)

        # Clean up
        grpo.attachments.first().file.delete(save=False)


class MergedGRPOServiceTests(TestCase):
    """Tests for merged GRPO posting (multiple POs → single GRPO)"""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Company", code="TC001")
        cls.user = User.objects.create_user(
            email="merge_test@example.com", password="testpass123",
            full_name="Merge Tester", employee_code="EMP002"
        )
        cls.vehicle_type = VehicleType.objects.create(name="TRUCK_M")
        cls.vehicle = Vehicle.objects.create(
            vehicle_number="MH14XY5678", vehicle_type=cls.vehicle_type
        )
        cls.driver = Driver.objects.create(
            name="Merge Driver", mobile_no="9988776655", license_no="DL654321"
        )
        cls.vehicle_entry = VehicleEntry.objects.create(
            entry_no="VE-2024-100", company=cls.company,
            vehicle=cls.vehicle, driver=cls.driver,
            entry_type="RAW_MATERIAL", status=GateEntryStatus.COMPLETED
        )
        cls.weighment = Weighment.objects.create(
            vehicle_entry=cls.vehicle_entry,
            gross_weight=Decimal("20000.000"),
            tare_weight=Decimal("5000.000"),
            created_by=cls.user,
            updated_by=cls.user,
        )

        # PO 1 — supplier Zomato
        cls.po1 = POReceipt.objects.create(
            vehicle_entry=cls.vehicle_entry, po_number="PO-Z-001",
            supplier_code="ZOMATO01", supplier_name="Zomato",
            sap_doc_entry=5001, branch_id=1
        )
        cls.po1_item = POItemReceipt.objects.create(
            po_receipt=cls.po1, po_item_code="ITEM-A",
            item_name="Steel Rod", ordered_qty=Decimal("200.000"),
            received_qty=Decimal("200.000"), sap_line_num=0,
            unit_price=Decimal("50.000000"), tax_code="GST18",
            warehouse_code="WH-01", gl_account="40001001", uom="KG"
        )

        # PO 2 — same supplier Zomato
        cls.po2 = POReceipt.objects.create(
            vehicle_entry=cls.vehicle_entry, po_number="PO-Z-002",
            supplier_code="ZOMATO01", supplier_name="Zomato",
            sap_doc_entry=5002, branch_id=1
        )
        cls.po2_item = POItemReceipt.objects.create(
            po_receipt=cls.po2, po_item_code="ITEM-B",
            item_name="Copper Wire", ordered_qty=Decimal("100.000"),
            received_qty=Decimal("100.000"), sap_line_num=0,
            unit_price=Decimal("120.000000"), tax_code="GST18",
            warehouse_code="WH-01", gl_account="40001002", uom="KG"
        )

        # PO 3 — different supplier
        cls.po3 = POReceipt.objects.create(
            vehicle_entry=cls.vehicle_entry, po_number="PO-S-001",
            supplier_code="SWIGGY01", supplier_name="Swiggy",
            sap_doc_entry=5003, branch_id=2
        )
        cls.po3_item = POItemReceipt.objects.create(
            po_receipt=cls.po3, po_item_code="ITEM-C",
            item_name="Aluminum Sheet", ordered_qty=Decimal("50.000"),
            received_qty=Decimal("50.000"), sap_line_num=0, uom="KG"
        )

    @patch('grpo.services.SAPClient')
    def test_merged_grpo_success(self, mock_sap_client):
        """Test merging two POs from same supplier into single GRPO"""
        mock_instance = MagicMock()
        mock_instance.create_grpo.return_value = {
            "DocEntry": 9001, "DocNum": 9100, "DocTotal": 22000.00
        }
        mock_sap_client.return_value = mock_instance

        service = GRPOService(company_code="TC001")
        grpo = service.post_grpo(
            vehicle_entry_id=self.vehicle_entry.id,
            po_receipt_ids=[self.po1.id, self.po2.id],
            user=self.user,
            items=[
                {"po_item_receipt_id": self.po1_item.id, "accepted_qty": Decimal("180.000"), "variety": "TMT-500D"},
                {"po_item_receipt_id": self.po2_item.id, "accepted_qty": Decimal("90.000"), "variety": "Grade-A"},
            ],
            branch_id=1,
        )

        self.assertEqual(grpo.status, GRPOStatus.POSTED)
        self.assertEqual(grpo.sap_doc_num, 9100)

        # Verify M2M relationship
        self.assertEqual(grpo.po_receipts.count(), 2)
        po_numbers = set(grpo.po_receipts.values_list("po_number", flat=True))
        self.assertEqual(po_numbers, {"PO-Z-001", "PO-Z-002"})

        # Verify SAP payload has lines from both POs with different BaseEntries
        call_args = mock_instance.create_grpo.call_args[0][0]
        self.assertEqual(call_args["CardCode"], "ZOMATO01")
        self.assertEqual(len(call_args["DocumentLines"]), 2)

        line1 = call_args["DocumentLines"][0]
        self.assertEqual(line1["BaseEntry"], 5001)
        self.assertEqual(line1["ItemCode"], "ITEM-A")

        line2 = call_args["DocumentLines"][1]
        self.assertEqual(line2["BaseEntry"], 5002)
        self.assertEqual(line2["ItemCode"], "ITEM-B")

        # Verify comments mention merged
        self.assertIn("Merged: 2 POs", call_args["Comments"])
        self.assertIn("PO-Z-001", call_args["Comments"])
        self.assertIn("PO-Z-002", call_args["Comments"])

        # Verify line postings
        self.assertEqual(grpo.lines.count(), 2)

    def test_merged_grpo_different_suppliers_rejected(self):
        """Test merging POs from different suppliers is rejected"""
        service = GRPOService(company_code="TC001")

        with self.assertRaises(ValueError) as ctx:
            service.post_grpo(
                vehicle_entry_id=self.vehicle_entry.id,
                po_receipt_ids=[self.po1.id, self.po3.id],
                user=self.user,
                items=[
                    {"po_item_receipt_id": self.po1_item.id, "accepted_qty": Decimal("100.000"), "variety": "TMT"},
                    {"po_item_receipt_id": self.po3_item.id, "accepted_qty": Decimal("50.000"), "variety": "TMT"},
                ],
                branch_id=1,
            )

        self.assertIn("different suppliers", str(ctx.exception))

    def test_merged_grpo_different_branch_rejected(self):
        """Test merging POs with different branch IDs is rejected"""
        # Temporarily set po2 to different branch
        self.po2.branch_id = 99
        self.po2.save()

        service = GRPOService(company_code="TC001")

        with self.assertRaises(ValueError) as ctx:
            service.post_grpo(
                vehicle_entry_id=self.vehicle_entry.id,
                po_receipt_ids=[self.po1.id, self.po2.id],
                user=self.user,
                items=[
                    {"po_item_receipt_id": self.po1_item.id, "accepted_qty": Decimal("100.000"), "variety": "TMT"},
                    {"po_item_receipt_id": self.po2_item.id, "accepted_qty": Decimal("50.000"), "variety": "TMT"},
                ],
                branch_id=1,
            )

        self.assertIn("different branch IDs", str(ctx.exception))

        # Restore
        self.po2.branch_id = 1
        self.po2.save()

    @patch('grpo.services.SAPClient')
    def test_merged_grpo_already_posted_po_rejected(self, mock_sap_client):
        """Test merging when one PO is already posted"""
        mock_instance = MagicMock()
        mock_instance.create_grpo.return_value = {
            "DocEntry": 9002, "DocNum": 9200, "DocTotal": 10000.00
        }
        mock_sap_client.return_value = mock_instance

        # Post po1 first
        service = GRPOService(company_code="TC001")
        service.post_grpo(
            vehicle_entry_id=self.vehicle_entry.id,
            po_receipt_ids=[self.po1.id],
            user=self.user,
            items=[
                {"po_item_receipt_id": self.po1_item.id, "accepted_qty": Decimal("180.000"), "variety": "TMT"},
            ],
            branch_id=1,
        )

        # Now try to merge po1 + po2 — should fail because po1 already posted
        with self.assertRaises(ValueError) as ctx:
            service.post_grpo(
                vehicle_entry_id=self.vehicle_entry.id,
                po_receipt_ids=[self.po1.id, self.po2.id],
                user=self.user,
                items=[
                    {"po_item_receipt_id": self.po1_item.id, "accepted_qty": Decimal("100.000"), "variety": "TMT"},
                    {"po_item_receipt_id": self.po2_item.id, "accepted_qty": Decimal("50.000"), "variety": "TMT"},
                ],
                branch_id=1,
            )

        self.assertIn("already posted", str(ctx.exception))

    def test_merged_grpo_invalid_po_ids(self):
        """Test merging with non-existent PO receipt IDs"""
        service = GRPOService(company_code="TC001")

        with self.assertRaises(ValueError) as ctx:
            service.post_grpo(
                vehicle_entry_id=self.vehicle_entry.id,
                po_receipt_ids=[self.po1.id, 99999],
                user=self.user,
                items=[
                    {"po_item_receipt_id": self.po1_item.id, "accepted_qty": Decimal("100.000"), "variety": "TMT"},
                ],
                branch_id=1,
            )

        self.assertIn("not found", str(ctx.exception))

    @patch('grpo.services.SAPClient')
    def test_single_po_in_list_works(self, mock_sap_client):
        """Test that passing a single PO in po_receipt_ids list works (backward compat)"""
        mock_instance = MagicMock()
        mock_instance.create_grpo.return_value = {
            "DocEntry": 9003, "DocNum": 9300, "DocTotal": 5000.00
        }
        mock_sap_client.return_value = mock_instance

        service = GRPOService(company_code="TC001")
        grpo = service.post_grpo(
            vehicle_entry_id=self.vehicle_entry.id,
            po_receipt_ids=[self.po2.id],
            user=self.user,
            items=[
                {"po_item_receipt_id": self.po2_item.id, "accepted_qty": Decimal("90.000"), "variety": "Grade-A"},
            ],
            branch_id=1,
        )

        self.assertEqual(grpo.status, GRPOStatus.POSTED)
        self.assertEqual(grpo.po_receipts.count(), 1)
        # Comments should NOT say "Merged"
        call_args = mock_instance.create_grpo.call_args[0][0]
        self.assertNotIn("Merged", call_args["Comments"])

    def test_preview_with_po_receipt_ids_filter(self):
        """Test preview API filters by po_receipt_ids"""
        service = GRPOService(company_code="TC001")
        preview = service.get_grpo_preview_data(
            self.vehicle_entry.id, po_receipt_ids=[self.po1.id]
        )
        self.assertEqual(len(preview), 1)
        self.assertEqual(preview[0]["po_number"], "PO-Z-001")

    def test_preview_all_pos(self):
        """Test preview returns all POs when no filter"""
        service = GRPOService(company_code="TC001")
        preview = service.get_grpo_preview_data(self.vehicle_entry.id)
        self.assertEqual(len(preview), 3)  # po1, po2, po3


class MergedGRPOSerializerTests(TestCase):
    """Tests for merged GRPO serializer changes"""

    def test_serializer_accepts_po_receipt_ids_list(self):
        """Test GRPOPostRequestSerializer accepts po_receipt_ids as list"""
        from grpo.serializers import GRPOPostRequestSerializer

        data = {
            "vehicle_entry_id": 1,
            "po_receipt_ids": [10, 20, 30],
            "items": [
                {"po_item_receipt_id": 1, "accepted_qty": "100.000", "variety": "TMT-500D"},
                {"po_item_receipt_id": 2, "accepted_qty": "50.000", "variety": "Grade-A"},
            ],
            "branch_id": 1,
            "tare_weight": "5000.000",
        }

        serializer = GRPOPostRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["po_receipt_ids"], [10, 20, 30])

    def test_serializer_legacy_po_receipt_id_converted_to_list(self):
        """Test legacy po_receipt_id is normalized to po_receipt_ids list"""
        from grpo.serializers import GRPOPostRequestSerializer

        data = {
            "vehicle_entry_id": 1,
            "po_receipt_id": 42,
            "items": [
                {"po_item_receipt_id": 1, "accepted_qty": "100.000", "variety": "TMT-500D"},
            ],
            "branch_id": 1,
            "tare_weight": "5000.000",
        }

        serializer = GRPOPostRequestSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["po_receipt_ids"], [42])

    def test_serializer_rejects_missing_both_ids(self):
        """Test serializer rejects when neither po_receipt_ids nor po_receipt_id provided"""
        from grpo.serializers import GRPOPostRequestSerializer

        data = {
            "vehicle_entry_id": 1,
            "items": [
                {"po_item_receipt_id": 1, "accepted_qty": "100.000", "variety": "TMT-500D"},
            ],
            "branch_id": 1,
        }

        serializer = GRPOPostRequestSerializer(data=data)
        self.assertFalse(serializer.is_valid())

    def test_posting_serializer_shows_merged_info(self):
        """Test GRPOPostingSerializer shows merged PO details"""
        from grpo.serializers import GRPOPostingSerializer

        company = Company.objects.create(name="Ser Test Co", code="STC01")
        user = User.objects.create_user(
            email="ser_test@example.com", password="test123",
            full_name="Ser Tester", employee_code="SEMP01"
        )
        vtype = VehicleType.objects.create(name="TRUCK_ST")
        vehicle = Vehicle.objects.create(vehicle_number="MH99ZZ0001", vehicle_type=vtype)
        driver = Driver.objects.create(name="ST Driver", mobile_no="1111111111", license_no="DL999")
        ve = VehicleEntry.objects.create(
            entry_no="VE-SER-001", company=company, vehicle=vehicle,
            driver=driver, entry_type="RAW_MATERIAL", status=GateEntryStatus.COMPLETED
        )
        po1 = POReceipt.objects.create(
            vehicle_entry=ve, po_number="PO-M1", supplier_code="S1",
            supplier_name="Supplier 1", sap_doc_entry=1001
        )
        po2 = POReceipt.objects.create(
            vehicle_entry=ve, po_number="PO-M2", supplier_code="S1",
            supplier_name="Supplier 1", sap_doc_entry=1002
        )

        grpo = GRPOPosting.objects.create(
            vehicle_entry=ve, po_receipt=po1, status=GRPOStatus.POSTED,
            sap_doc_entry=2001, sap_doc_num=2100
        )
        grpo.po_receipts.set([po1, po2])

        serializer = GRPOPostingSerializer(grpo)
        data = serializer.data

        self.assertTrue(data["is_merged"])
        self.assertIn("PO-M1", data["po_number"])
        self.assertIn("PO-M2", data["po_number"])
        self.assertEqual(len(data["po_numbers"]), 2)
        self.assertEqual(len(data["merged_po_receipts"]), 2)
