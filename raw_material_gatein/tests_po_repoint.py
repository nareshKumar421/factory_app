"""Moving a received PO onto a replacement PO, and warning before it is needed.

The situation both cover (GE-2026-8871, 2026-09-15): 1,960 PCS of PM0000914 were
gated in against PO 220826133 at 09:33 with 3,862 open, a different truck's GRPO
consumed all 3,862 at 12:20, and the first truck's GRPO could no longer post.
Open quantity is read, never reserved.
"""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings

from company.models import Company
from driver_management.models import Driver, VehicleEntry
from gate_core.enums import GateEntryStatus
from grpo.models import GRPOLinePosting, GRPOPosting, GRPOStatus
from raw_material_gatein.models import POItemReceipt, POReceipt, POReplacementLog
from raw_material_gatein.services import (
    RepointError,
    booking_overlap_warning,
    repoint_po_receipt,
)
from vehicle_management.models import Vehicle, VehicleType


def _sap_po(doc_entry, po_number, lines, supplier_code="VENDA000936"):
    return SimpleNamespace(
        doc_entry=doc_entry,
        po_number=po_number,
        supplier_code=supplier_code,
        supplier_name="Test Vendor",
        branch_id=2,
        vendor_ref="",
        doc_date=date(2026, 9, 13),
        items=[
            SimpleNamespace(
                po_item_code=code,
                item_name=code,
                ordered_qty=ordered,
                received_qty=ordered - remaining,
                remaining_qty=remaining,
                uom="PCS",
                rate=rate,
                line_num=line_num,
                tax_code="CG+SG@5",
                warehouse_code="BH-PM",
                account_code="1103005",
                variety="OLIVE",
            )
            for code, line_num, ordered, remaining, rate in lines
        ],
    )


class RepointTestBase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Oil", code="JIVO_OIL")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="HR55AB1234",
            vehicle_type=VehicleType.objects.create(name="TRUCK"),
        )
        self.driver = Driver.objects.create(
            name="Test Driver", mobile_no="9876543210", license_no="DL123456"
        )
        self.entry = self._entry("GE-2026-8871")

        self.po_receipt = POReceipt.objects.create(
            vehicle_entry=self.entry,
            po_number="220826133",
            supplier_code="VENDA000936",
            supplier_name="Test Vendor",
            sap_doc_entry=13462,
            branch_id=2,
        )
        self.item = POItemReceipt.objects.create(
            po_receipt=self.po_receipt,
            po_item_code="PM0000914",
            item_name="Label 1L",
            ordered_qty=Decimal("20000.000"),
            received_qty=Decimal("1960.000"),
            accepted_qty=Decimal("1960.000"),
            sap_line_num=0,
            unit_price=Decimal("34.950000"),
            tax_code="CG+SG@5",
            warehouse_code="BH-PM",
            gl_account="1103005",
            uom="PCS",
        )

    def _entry(self, entry_no, status=GateEntryStatus.COMPLETED):
        return VehicleEntry.objects.create(
            company=self.company,
            entry_no=entry_no,
            vehicle=self.vehicle,
            driver=self.driver,
            entry_type="RAW_MATERIAL",
            status=status,
        )

    def _patch_sap(self, new_po):
        patcher = patch("raw_material_gatein.services.po_repoint.SAPClient")
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        mock.return_value.get_open_po_by_number.return_value = new_po
        return mock


@override_settings(GRPO_OVER_RECEIPT_ENFORCED_COMPANY_CODES=["JIVO_OIL"])
class RepointPOReceiptTests(RepointTestBase):
    def _repoint(self, po_number="220926064", reason="PO ran out"):
        return repoint_po_receipt(
            self.po_receipt,
            new_po_number=po_number,
            reason=reason,
            company_code="JIVO_OIL",
        )

    def test_the_receipt_moves_to_the_replacement_po(self):
        self._patch_sap(
            _sap_po(13802, "220926064", [("PM0000914", 3, 20000.0, 18656.0, 34.95)])
        )

        summary = self._repoint()

        self.po_receipt.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(self.po_receipt.po_number, "220926064")
        self.assertEqual(self.po_receipt.sap_doc_entry, 13802)
        self.assertEqual(self.po_receipt.po_date, date(2026, 9, 13))
        # Matched on item code, so the new PO's own line number is taken.
        self.assertEqual(self.item.sap_line_num, 3)
        self.assertEqual(summary["old_po_number"], "220826133")
        self.assertEqual(summary["new_doc_entry"], 13802)

    def test_the_received_quantity_and_qc_result_are_kept(self):
        """The material and its inspection were never in question — only the PO."""
        self._patch_sap(
            _sap_po(13802, "220926064", [("PM0000914", 0, 20000.0, 18656.0, 34.95)])
        )

        self._repoint()

        self.item.refresh_from_db()
        self.assertEqual(self.item.received_qty, Decimal("1960.000"))
        self.assertEqual(self.item.accepted_qty, Decimal("1960.000"))
        self.assertEqual(POItemReceipt.objects.filter(po_receipt=self.po_receipt).count(), 1)
        self.assertEqual(self.item.id, POItemReceipt.objects.first().id)

    def test_the_new_line_terms_are_taken_from_the_new_po(self):
        self._patch_sap(
            _sap_po(13802, "220926064", [("PM0000914", 0, 15000.0, 15000.0, 36.50)])
        )

        self._repoint()

        self.item.refresh_from_db()
        self.assertEqual(self.item.unit_price, Decimal("36.500000"))
        self.assertEqual(self.item.ordered_qty, Decimal("15000.000"))

    def test_a_saved_draft_is_repriced_onto_the_new_line(self):
        """A draft posts its saved price verbatim — left stale it would post the
        exhausted PO's price against the replacement PO's line."""
        posting = GRPOPosting.objects.create(
            vehicle_entry=self.entry,
            po_receipt=self.po_receipt,
            status=GRPOStatus.FAILED,
            request_payload={
                "items": [
                    {"po_item_receipt_id": self.item.id, "unit_price": 34.95,
                     "tax_code": "CG+SG@5"}
                ]
            },
        )
        posting.po_receipts.add(self.po_receipt)
        self._patch_sap(
            _sap_po(13802, "220926064", [("PM0000914", 0, 20000.0, 18656.0, 36.50)])
        )

        summary = self._repoint()

        posting.refresh_from_db()
        self.assertEqual(posting.request_payload["items"][0]["unit_price"], 36.50)
        self.assertEqual(summary["drafts_updated"], 1)

    def test_the_move_is_logged_with_its_reason(self):
        self._patch_sap(
            _sap_po(13802, "220926064", [("PM0000914", 0, 20000.0, 18656.0, 34.95)])
        )

        self._repoint(reason="220826133 consumed by GRPO 2026096695")

        log = POReplacementLog.objects.get(vehicle_entry=self.entry)
        self.assertEqual(log.old_po_number, "220826133")
        self.assertEqual(log.new_po_number, "220926064")
        self.assertFalse(log.supplier_changed)
        self.assertIn("2026096695", log.reason)

    def test_a_locked_entry_is_still_allowed(self):
        """Completing an RM entry locks it, so the lock cannot be a blocker here —
        it would refuse every entry that can reach this state."""
        self.entry.is_locked = True
        self.entry.save(update_fields=["is_locked"])
        self._patch_sap(
            _sap_po(13802, "220926064", [("PM0000914", 0, 20000.0, 18656.0, 34.95)])
        )

        self._repoint()

        self.po_receipt.refresh_from_db()
        self.assertEqual(self.po_receipt.po_number, "220926064")

    def test_a_cancelled_entry_is_refused(self):
        self.entry.status = GateEntryStatus.CANCELLED
        self.entry.save(update_fields=["status"])
        self._patch_sap(
            _sap_po(13802, "220926064", [("PM0000914", 0, 20000.0, 18656.0, 34.95)])
        )

        with self.assertRaises(RepointError) as ctx:
            self._repoint()

        self.assertIn("cancelled", str(ctx.exception))

    def test_a_completed_qc_finished_entry_is_still_allowed(self):
        """Exactly the case that needs it: the problem only surfaces at posting."""
        self.entry.status = GateEntryStatus.COMPLETED
        self.entry.save(update_fields=["status"])
        self._patch_sap(
            _sap_po(13802, "220926064", [("PM0000914", 0, 20000.0, 18656.0, 34.95)])
        )

        self._repoint()

        self.po_receipt.refresh_from_db()
        self.assertEqual(self.po_receipt.po_number, "220926064")

    def test_a_posted_grpo_blocks_the_move(self):
        posting = GRPOPosting.objects.create(
            vehicle_entry=self.entry,
            po_receipt=self.po_receipt,
            status=GRPOStatus.POSTED,
        )
        GRPOLinePosting.objects.create(
            grpo_posting=posting,
            po_item_receipt=self.item,
            quantity_posted=Decimal("1960.000"),
            base_entry=13462,
            base_line=0,
        )
        self._patch_sap(
            _sap_po(13802, "220926064", [("PM0000914", 0, 20000.0, 18656.0, 34.95)])
        )

        with self.assertRaises(RepointError) as ctx:
            self._repoint()

        self.assertIn("already been posted", str(ctx.exception))
        self.po_receipt.refresh_from_db()
        self.assertEqual(self.po_receipt.po_number, "220826133")

    def test_a_replacement_without_the_item_is_refused(self):
        self._patch_sap(
            _sap_po(13765, "220926050", [("PM0000411", 0, 10000.0, 7912.0, 33.28)])
        )

        with self.assertRaises(RepointError) as ctx:
            self._repoint(po_number="220926050")

        self.assertIn("no open line for PM0000914", str(ctx.exception))

    def test_a_replacement_without_room_is_refused(self):
        """Otherwise the move only defers the same failure to the next attempt."""
        self._patch_sap(
            _sap_po(13802, "220926064", [("PM0000914", 0, 20000.0, 100.0, 34.95)])
        )

        with self.assertRaises(RepointError) as ctx:
            self._repoint()

        self.assertIn("does not have room", str(ctx.exception))

    def test_a_replacement_within_the_10_percent_tolerance_is_allowed(self):
        self._patch_sap(
            _sap_po(13802, "220926064", [("PM0000914", 0, 20000.0, 1800.0, 34.95)])
        )

        self._repoint()

        self.po_receipt.refresh_from_db()
        self.assertEqual(self.po_receipt.po_number, "220926064")

    def test_another_vendors_po_is_refused(self):
        self._patch_sap(
            _sap_po(
                13900, "220926099",
                [("PM0000914", 0, 20000.0, 20000.0, 34.95)],
                supplier_code="VENDA000111",
            )
        )

        with self.assertRaises(RepointError) as ctx:
            self._repoint(po_number="220926099")

        self.assertIn("VENDA000111", str(ctx.exception))

    def test_a_po_with_nothing_open_is_refused(self):
        self._patch_sap(None)

        with self.assertRaises(RepointError) as ctx:
            self._repoint(po_number="220826133X")

        self.assertIn("not found in SAP", str(ctx.exception))

    def test_a_reason_is_required(self):
        self._patch_sap(
            _sap_po(13802, "220926064", [("PM0000914", 0, 20000.0, 18656.0, 34.95)])
        )

        with self.assertRaises(RepointError):
            self._repoint(reason="   ")

    def test_a_po_already_on_the_entry_is_refused(self):
        POReceipt.objects.create(
            vehicle_entry=self.entry,
            po_number="220926064",
            supplier_code="VENDA000936",
            supplier_name="Test Vendor",
            sap_doc_entry=13802,
        )
        self._patch_sap(
            _sap_po(13802, "220926064", [("PM0000914", 0, 20000.0, 18656.0, 34.95)])
        )

        with self.assertRaises(RepointError) as ctx:
            self._repoint()

        self.assertIn("already on this gate entry", str(ctx.exception))


class BookingOverlapWarningTests(RepointTestBase):
    """Two trucks gated in against one PO line, with room for only one."""

    def _warn(self, received_qty="1960", remaining_qty="3862", exclude=None):
        return booking_overlap_warning(
            13462,
            0,
            item_label="PM0000914 (line 0)",
            received_qty=Decimal(received_qty),
            remaining_qty=Decimal(remaining_qty),
            uom="PCS",
            exclude_po_receipt_id=exclude,
        )

    def _other_truck(self, entry_no, qty, status=GateEntryStatus.QC_COMPLETED):
        entry = self._entry(entry_no, status=status)
        receipt = POReceipt.objects.create(
            vehicle_entry=entry,
            po_number="220826133",
            supplier_code="VENDA000936",
            supplier_name="Test Vendor",
            sap_doc_entry=13462,
        )
        return POItemReceipt.objects.create(
            po_receipt=receipt,
            po_item_code="PM0000914",
            item_name="Label 1L",
            ordered_qty=Decimal("20000.000"),
            received_qty=Decimal(qty),
            sap_line_num=0,
            uom="PCS",
        )

    def test_no_warning_when_the_line_covers_everyone(self):
        self._other_truck("GE-2026-6718", "1000")

        self.assertIsNone(self._warn(exclude=self.po_receipt.id))

    def test_an_over_promised_line_warns_and_names_the_other_truck(self):
        self._other_truck("GE-2026-6718", "3862")

        warning = self._warn(exclude=self.po_receipt.id)

        self.assertIsNotNone(warning)
        self.assertIn("GE-2026-6718", warning)
        self.assertIn("3,862 PCS", warning)
        self.assertIn("only 3,862 PCS is open", warning)

    def test_a_truck_whose_grpo_has_posted_is_not_counted_twice(self):
        """Its share has already left OpenQty — counting it would double-count."""
        other = self._other_truck("GE-2026-6718", "3862")
        posting = GRPOPosting.objects.create(
            vehicle_entry=other.po_receipt.vehicle_entry,
            po_receipt=other.po_receipt,
            status=GRPOStatus.POSTED,
        )
        GRPOLinePosting.objects.create(
            grpo_posting=posting,
            po_item_receipt=other,
            quantity_posted=Decimal("3862.000"),
            base_entry=13462,
            base_line=0,
        )

        self.assertIsNone(self._warn(exclude=self.po_receipt.id))

    def test_a_cancelled_entry_is_not_counted(self):
        self._other_truck("GE-2026-6718", "3862", status=GateEntryStatus.CANCELLED)

        self.assertIsNone(self._warn(exclude=self.po_receipt.id))

    def test_a_different_po_line_is_not_counted(self):
        other = self._other_truck("GE-2026-6718", "3862")
        other.sap_line_num = 1
        other.save(update_fields=["sap_line_num"])

        self.assertIsNone(self._warn(exclude=self.po_receipt.id))
