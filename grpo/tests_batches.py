"""Batch (lot) capture on a material GRPO.

SAP refuses a receipt line for a batch-managed item unless the payload names
the batch: ``-4014 Cannot add row without complete selection of batch/serial
numbers``, which reads on the screen as an unexplained "cannot add row". These
cover the two halves of the fix -- the preview telling the screen which lines
need a batch, and the post building (and validating) the ``BatchNumbers`` block.
"""

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from company.models import Company
from driver_management.models import Driver, VehicleEntry
from gate_core.enums import GateEntryStatus
from grpo.models import GRPOStatus
from grpo.services import GRPOService
from quality_control.enums import (
    ArrivalSlipStatus,
    InspectionStatus,
    InspectionWorkflowStatus,
)
from quality_control.models import MaterialArrivalSlip, RawMaterialInspection
from raw_material_gatein.models import POItemReceipt, POReceipt
from vehicle_management.models import Vehicle, VehicleType

User = get_user_model()


def _stub_sap_open_qtys(mock_instance, open_qty=1_000_000.0):
    """Every PO line still wide open, so the over-receipt rule stays out of
    the way (it has its own suite in grpo/tests_over_receipt.py)."""

    def _open_qtys(_doc_entries):
        return {
            (receipt.sap_doc_entry, item.sap_line_num): open_qty
            for receipt in POReceipt.objects.all()
            for item in receipt.items.all()
            if receipt.sap_doc_entry is not None and item.sap_line_num is not None
        }

    mock_instance.get_po_open_qtys.side_effect = _open_qtys


class GRPOBatchCaptureTests(TestCase):
    """Batch-managed lines carry their lots into the SAP payload."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Company", code="TC001")
        cls.user = User.objects.create_user(
            email="batchuser@example.com",
            password="testpass123",
            full_name="Batch User",
            employee_code="EMP-BATCH",
        )
        cls.vehicle = Vehicle.objects.create(
            vehicle_number="MH12AB9999",
            vehicle_type=VehicleType.objects.create(name="TANKER"),
        )
        cls.driver = Driver.objects.create(
            name="Test Driver", mobile_no="9876543210", license_no="DL999999"
        )
        cls.vehicle_entry = VehicleEntry.objects.create(
            entry_no="VE-BATCH-001",
            company=cls.company,
            vehicle=cls.vehicle,
            driver=cls.driver,
            entry_type="RAW_MATERIAL",
            status=GateEntryStatus.COMPLETED,
        )
        cls.po_receipt = POReceipt.objects.create(
            vehicle_entry=cls.vehicle_entry,
            po_number="PO-BATCH-001",
            supplier_code="SUP001",
            supplier_name="Test Supplier",
            sap_doc_entry=12345,
            branch_id=1,
            vendor_ref="AABV/26-27/306",
            po_date=date(2026, 1, 15),
        )
        # Batch-managed in SAP (an oil RM).
        cls.oil_item = POItemReceipt.objects.create(
            po_receipt=cls.po_receipt,
            po_item_code="RM0000025",
            item_name="SOYABEAN REFINED LOOSE OIL",
            ordered_qty=Decimal("100.000"),
            received_qty=Decimal("100.000"),
            accepted_qty=Decimal("100.000"),
            sap_line_num=0,
            unit_price=Decimal("85.500000"),
            uom="KG",
        )
        # Not batch-managed (packing material).
        cls.pm_item = POItemReceipt.objects.create(
            po_receipt=cls.po_receipt,
            po_item_code="PM0000012",
            item_name="1 LTR BOTTLE",
            ordered_qty=Decimal("50.000"),
            received_qty=Decimal("50.000"),
            accepted_qty=Decimal("50.000"),
            sap_line_num=1,
            unit_price=Decimal("4.000000"),
            uom="PCS",
        )

    # -- helpers ---------------------------------------------------------

    def _attach_qc_inspection(self, po_item, *, supplier_lot="", report_no="RPT-B-1"):
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
            supplier_batch_lot_no=supplier_lot,
            purchase_order_no=po_item.po_receipt.po_number,
            final_status=InspectionStatus.ACCEPTED,
            workflow_status=InspectionWorkflowStatus.QAM_APPROVED,
        )

    def _posted_sap_payload(self, mock_instance):
        return mock_instance.create_grpo.call_args[0][0]

    def _stub_sap(self, mock_sap_client, *, batch_flags=None, flags_error=None):
        mock_instance = MagicMock()
        mock_instance.create_grpo.return_value = {
            "DocEntry": 900,
            "DocNum": 901,
            "DocTotal": 12345.00,
        }
        if flags_error is not None:
            mock_instance.batch_managed_flags.side_effect = flags_error
        else:
            mock_instance.batch_managed_flags.return_value = batch_flags or {}
        mock_sap_client.return_value = mock_instance
        _stub_sap_open_qtys(mock_instance)
        return mock_instance

    def _post(self, service, items):
        return service.post_grpo(
            vehicle_entry_id=self.vehicle_entry.id,
            po_receipt_ids=[self.po_receipt.id],
            user=self.user,
            items=items,
            branch_id=1,
            warehouse_code="WH-01",
        )

    # -- preview ---------------------------------------------------------

    @patch("grpo.services.SAPClient")
    def test_preview_flags_batch_managed_items(self, mock_sap_client):
        """The screen is told which lines SAP will refuse without a batch."""
        self._stub_sap(
            mock_sap_client,
            batch_flags={"RM0000025": True, "PM0000012": False},
        )

        preview = GRPOService(company_code="TC001").get_grpo_preview_data(
            self.vehicle_entry.id
        )

        items = {item["item_code"]: item for item in preview[0]["items"]}
        self.assertTrue(items["RM0000025"]["is_batch_managed"])
        self.assertFalse(items["PM0000012"]["is_batch_managed"])

    @patch("grpo.services.SAPClient")
    def test_preview_suggests_the_suppliers_lot_from_qc(self, mock_sap_client):
        """QC already wrote the supplier's lot down — offer it, don't re-type it."""
        self._attach_qc_inspection(self.oil_item, supplier_lot="AABV/26-27/306")
        self._stub_sap(mock_sap_client, batch_flags={"RM0000025": True})

        preview = GRPOService(company_code="TC001").get_grpo_preview_data(
            self.vehicle_entry.id
        )

        item = next(
            i for i in preview[0]["items"] if i["item_code"] == "RM0000025"
        )
        self.assertEqual(item["suggested_batch_number"], "AABV/26-27/306")

    @patch("grpo.services.SAPClient")
    def test_preview_falls_back_to_the_internal_lot(self, mock_sap_client):
        """No supplier lot on the inspection — our own lot is still traceable."""
        self._attach_qc_inspection(self.oil_item, supplier_lot="")
        self._stub_sap(mock_sap_client, batch_flags={"RM0000025": True})

        preview = GRPOService(company_code="TC001").get_grpo_preview_data(
            self.vehicle_entry.id
        )

        item = next(
            i for i in preview[0]["items"] if i["item_code"] == "RM0000025"
        )
        self.assertEqual(item["suggested_batch_number"], "LOT-RPT-B-1")

    @patch("grpo.services.SAPClient")
    def test_preview_survives_a_batch_flag_lookup_failure(self, mock_sap_client):
        """An unreachable HANA must not take the whole preview down with it."""
        self._stub_sap(mock_sap_client, flags_error=Exception("HANA down"))

        preview = GRPOService(company_code="TC001").get_grpo_preview_data(
            self.vehicle_entry.id
        )

        self.assertEqual(len(preview), 1)
        self.assertFalse(preview[0]["items"][0]["is_batch_managed"])

    # -- posting ---------------------------------------------------------

    @patch("grpo.services.SAPClient")
    def test_post_sends_batch_numbers_for_a_batch_managed_line(self, mock_sap_client):
        mock_instance = self._stub_sap(
            mock_sap_client, batch_flags={"RM0000025": True, "PM0000012": False}
        )

        posting = self._post(
            GRPOService(company_code="TC001"),
            [
                {
                    "po_item_receipt_id": self.oil_item.id,
                    "accepted_qty": Decimal("100.000"),
                    "batches": [
                        {
                            "batch_number": "AABV/26-27/306",
                            "quantity": Decimal("100.000"),
                        }
                    ],
                },
                {
                    "po_item_receipt_id": self.pm_item.id,
                    "accepted_qty": Decimal("50.000"),
                },
            ],
        )

        self.assertEqual(posting.status, GRPOStatus.POSTED)
        lines = self._posted_sap_payload(mock_instance)["DocumentLines"]
        oil_line = next(l for l in lines if l["ItemCode"] == "RM0000025")
        pm_line = next(l for l in lines if l["ItemCode"] == "PM0000012")
        self.assertEqual(
            oil_line["BatchNumbers"],
            [
                {
                    "BatchNumber": "AABV/26-27/306",
                    "Quantity": Decimal("100.000"),
                    "BaseLineNumber": lines.index(oil_line),
                }
            ],
        )
        # An item SAP does not manage by batch must carry no batch block.
        self.assertNotIn("BatchNumbers", pm_line)

    @patch("grpo.services.SAPClient")
    def test_post_splits_one_line_across_several_lots(self, mock_sap_client):
        """One tanker, two supplier lots — both ride the same receipt line."""
        mock_instance = self._stub_sap(mock_sap_client, batch_flags={"RM0000025": True})

        self._post(
            GRPOService(company_code="TC001"),
            [
                {
                    "po_item_receipt_id": self.oil_item.id,
                    "accepted_qty": Decimal("100.000"),
                    "batches": [
                        {"batch_number": "LOT-A", "quantity": Decimal("60.000")},
                        {
                            "batch_number": "LOT-B",
                            "quantity": Decimal("40.000"),
                            "manufacturing_date": date(2026, 9, 1),
                            "expiry_date": date(2028, 8, 31),
                        },
                    ],
                },
                {
                    "po_item_receipt_id": self.pm_item.id,
                    "accepted_qty": Decimal("0"),
                },
            ],
        )

        line = self._posted_sap_payload(mock_instance)["DocumentLines"][0]
        self.assertEqual([b["BatchNumber"] for b in line["BatchNumbers"]], ["LOT-A", "LOT-B"])
        self.assertEqual(line["BatchNumbers"][1]["ManufacturingDate"], "2026-09-01")
        self.assertEqual(line["BatchNumbers"][1]["ExpiryDate"], "2028-08-31")
        # Both splits point at their own document line.
        self.assertEqual({b["BaseLineNumber"] for b in line["BatchNumbers"]}, {0})

    @patch("grpo.services.SAPClient")
    def test_post_refuses_a_batch_managed_line_with_no_batch(self, mock_sap_client):
        """The whole point: a readable message instead of SAP's -4014."""
        mock_instance = self._stub_sap(mock_sap_client, batch_flags={"RM0000025": True})

        with self.assertRaises(ValueError) as ctx:
            self._post(
                GRPOService(company_code="TC001"),
                [
                    {
                        "po_item_receipt_id": self.oil_item.id,
                        "accepted_qty": Decimal("100.000"),
                    },
                    {
                        "po_item_receipt_id": self.pm_item.id,
                        "accepted_qty": Decimal("50.000"),
                    },
                ],
            )

        self.assertIn("RM0000025", str(ctx.exception))
        self.assertIn("batch-managed", str(ctx.exception))
        mock_instance.create_grpo.assert_not_called()

    @patch("grpo.services.SAPClient")
    def test_post_refuses_batches_that_do_not_add_up(self, mock_sap_client):
        mock_instance = self._stub_sap(mock_sap_client, batch_flags={"RM0000025": True})

        with self.assertRaises(ValueError) as ctx:
            self._post(
                GRPOService(company_code="TC001"),
                [
                    {
                        "po_item_receipt_id": self.oil_item.id,
                        "accepted_qty": Decimal("100.000"),
                        "batches": [
                            {"batch_number": "LOT-A", "quantity": Decimal("60.000")}
                        ],
                    },
                    {"po_item_receipt_id": self.pm_item.id, "accepted_qty": Decimal("0")},
                ],
            )

        self.assertIn("add up to 60.000", str(ctx.exception))
        mock_instance.create_grpo.assert_not_called()

    @patch("grpo.services.SAPClient")
    def test_post_refuses_the_same_lot_typed_twice(self, mock_sap_client):
        mock_instance = self._stub_sap(mock_sap_client, batch_flags={"RM0000025": True})

        with self.assertRaises(ValueError) as ctx:
            self._post(
                GRPOService(company_code="TC001"),
                [
                    {
                        "po_item_receipt_id": self.oil_item.id,
                        "accepted_qty": Decimal("100.000"),
                        "batches": [
                            {"batch_number": "LOT-A", "quantity": Decimal("60.000")},
                            {"batch_number": "lot-a", "quantity": Decimal("40.000")},
                        ],
                    },
                    {"po_item_receipt_id": self.pm_item.id, "accepted_qty": Decimal("0")},
                ],
            )

        self.assertIn("twice", str(ctx.exception))
        mock_instance.create_grpo.assert_not_called()

    @patch("grpo.services.SAPClient")
    def test_post_reports_every_missing_batch_at_once(self, mock_sap_client):
        """Fixing one lot per failed post is the worst way to find them."""
        self._stub_sap(
            mock_sap_client, batch_flags={"RM0000025": True, "PM0000012": True}
        )

        with self.assertRaises(ValueError) as ctx:
            self._post(
                GRPOService(company_code="TC001"),
                [
                    {
                        "po_item_receipt_id": self.oil_item.id,
                        "accepted_qty": Decimal("100.000"),
                    },
                    {
                        "po_item_receipt_id": self.pm_item.id,
                        "accepted_qty": Decimal("50.000"),
                    },
                ],
            )

        self.assertIn("RM0000025", str(ctx.exception))
        self.assertIn("PM0000012", str(ctx.exception))

    @patch("grpo.services.SAPClient")
    def test_post_drops_batches_for_an_item_sap_does_not_batch(self, mock_sap_client):
        """A stale draft cannot poison the post: SAP rejects a batch block on a
        line that takes none."""
        mock_instance = self._stub_sap(mock_sap_client, batch_flags={"PM0000012": False})

        self._post(
            GRPOService(company_code="TC001"),
            [
                {"po_item_receipt_id": self.oil_item.id, "accepted_qty": Decimal("0")},
                {
                    "po_item_receipt_id": self.pm_item.id,
                    "accepted_qty": Decimal("50.000"),
                    "batches": [
                        {"batch_number": "STALE", "quantity": Decimal("50.000")}
                    ],
                },
            ],
        )

        line = self._posted_sap_payload(mock_instance)["DocumentLines"][0]
        self.assertEqual(line["ItemCode"], "PM0000012")
        self.assertNotIn("BatchNumbers", line)

    @patch("grpo.services.SAPClient")
    def test_post_passes_batches_through_when_the_item_master_is_unreadable(
        self, mock_sap_client
    ):
        """HANA down at post time: send what the operator typed rather than
        dropping a batch SAP may well need."""
        mock_instance = self._stub_sap(mock_sap_client, flags_error=Exception("HANA down"))

        self._post(
            GRPOService(company_code="TC001"),
            [
                {
                    "po_item_receipt_id": self.oil_item.id,
                    "accepted_qty": Decimal("100.000"),
                    "batches": [
                        {"batch_number": "LOT-A", "quantity": Decimal("100.000")}
                    ],
                },
                {"po_item_receipt_id": self.pm_item.id, "accepted_qty": Decimal("0")},
            ],
        )

        line = self._posted_sap_payload(mock_instance)["DocumentLines"][0]
        self.assertEqual(line["BatchNumbers"][0]["BatchNumber"], "LOT-A")

    @patch("grpo.services.SAPClient")
    def test_posted_line_records_its_batches(self, mock_sap_client):
        """So a receipt can be traced to a lot without a round trip to SAP."""
        self._stub_sap(mock_sap_client, batch_flags={"RM0000025": True})

        posting = self._post(
            GRPOService(company_code="TC001"),
            [
                {
                    "po_item_receipt_id": self.oil_item.id,
                    "accepted_qty": Decimal("100.000"),
                    "batches": [
                        {"batch_number": "LOT-A", "quantity": Decimal("100.000")}
                    ],
                },
                {"po_item_receipt_id": self.pm_item.id, "accepted_qty": Decimal("0")},
            ],
        )

        line = posting.lines.get(po_item_receipt=self.oil_item)
        self.assertEqual(
            line.batches,
            [
                {
                    "BatchNumber": "LOT-A",
                    "Quantity": "100.000",
                    "BaseLineNumber": 0,
                }
            ],
        )
