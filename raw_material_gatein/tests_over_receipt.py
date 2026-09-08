"""Over-receipt tolerance: the gate must refuse what SAP will refuse at posting.

`SBO_SP_TransactionNotification` caps a GRPO line at 110% of the PO line's *open*
quantity (`PDN1."Quantity" > PDN1."BaseOpnQty" * 1.10`, error 200017). The gate used
to cap on 110% of the quantity originally *ordered*, which on a mostly-consumed PO
line is a far bigger number — a 12,000 PCS receipt onto a line with 9,000 PCS open
sailed through a 165,000 ceiling and only failed in SAP.
"""

from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.exceptions import ValidationError as DRFValidationError

from company.models import Company
from driver_management.models import Driver, VehicleEntry
from gate_core.enums import GateEntryStatus
from raw_material_gatein.models import POReceipt
from raw_material_gatein.services.validations import (
    is_over_receipt_enforced,
    is_over_receipt_exempt,
    over_receipt_ceiling,
    validate_received_quantity,
)
from raw_material_gatein.views import _save_po_items
from vehicle_management.models import Vehicle


class OverReceiptCeilingTests(SimpleTestCase):
    def test_ceiling_is_ten_percent_over_the_open_quantity(self):
        self.assertEqual(over_receipt_ceiling(Decimal("9000")), Decimal("9900.00"))
        self.assertEqual(over_receipt_ceiling(Decimal("89000")), Decimal("97900.00"))

    def test_a_receipt_within_the_open_quantity_is_allowed(self):
        validate_received_quantity(Decimal("500000"), Decimal("89000"), Decimal("87000"))

    def test_exactly_the_ceiling_is_allowed(self):
        """SAP's test is strictly `>`, so the ceiling itself passes."""
        validate_received_quantity(Decimal("150000"), Decimal("9000"), Decimal("9900"))

    def test_over_the_open_quantity_is_refused_even_though_the_order_was_huge(self):
        """The BABAJI-3562 shape: 12,000 onto a line with 9,000 open of 150,000 ordered.

        Under the old ordered-qty ceiling of 165,000 this was accepted.
        """
        with self.assertRaises(ValueError) as ctx:
            validate_received_quantity(
                Decimal("150000"), Decimal("9000"), Decimal("12000"), uom="PCS"
            )

        message = str(ctx.exception)
        self.assertIn("9,000 PCS is still open", message)
        self.assertIn("9,900 PCS", message)

    def test_a_fully_received_line_takes_nothing_more(self):
        with self.assertRaises(ValueError) as ctx:
            validate_received_quantity(Decimal("500000"), Decimal("0"), Decimal("1"))

        self.assertIn("fully received", str(ctx.exception))

    def test_zero_and_negative_receipts_are_still_refused(self):
        for qty in (Decimal("0"), Decimal("-5")):
            with self.assertRaises(ValueError) as ctx:
                validate_received_quantity(Decimal("100"), Decimal("100"), qty)
            self.assertIn("greater than zero", str(ctx.exception))

    def test_an_exempt_vendor_skips_the_ceiling_but_not_the_zero_check(self):
        validate_received_quantity(
            Decimal("150000"), Decimal("9000"), Decimal("12000"), exempt=True
        )

        with self.assertRaises(ValueError):
            validate_received_quantity(
                Decimal("150000"), Decimal("9000"), Decimal("0"), exempt=True
            )

    def test_the_item_is_named_so_the_operator_knows_which_row_to_fix(self):
        with self.assertRaises(ValueError) as ctx:
            validate_received_quantity(
                Decimal("150000"),
                Decimal("9000"),
                Decimal("12000"),
                item_label="PM0000085 (line 0)",
            )

        self.assertTrue(str(ctx.exception).startswith("PM0000085 (line 0) "))


@override_settings(
    GRPO_OVER_RECEIPT_ENFORCED_COMPANY_CODES=["JIVO_OIL", "JIVO_MART"],
    GRPO_OVER_RECEIPT_EXEMPT_VENDORS={"JIVO_OIL": ["VENDA000483", "VENDA001614"]},
)
class OverReceiptExemptionTests(SimpleTestCase):
    """Mirrors the exemptions carved into SAP's own check, so the gate does not
    block a receipt SAP would accept."""

    def test_a_listed_vendor_is_exempt_without_an_sap_read(self):
        with patch(
            "raw_material_gatein.services.validations._read_bp_group_code"
        ) as read_group:
            self.assertTrue(is_over_receipt_exempt("JIVO_OIL", "VENDA001614"))
            read_group.assert_not_called()

    def test_a_branch_vendor_is_exempt(self):
        with patch(
            "raw_material_gatein.services.validations._read_bp_group_code",
            return_value=101,
        ):
            self.assertTrue(is_over_receipt_exempt("JIVO_OIL", "VENDA001595"))

    def test_an_ordinary_vendor_is_not_exempt(self):
        with patch(
            "raw_material_gatein.services.validations._read_bp_group_code",
            return_value=100,
        ):
            self.assertFalse(is_over_receipt_exempt("JIVO_OIL", "VENDA001595"))

    def test_a_failed_group_read_does_not_grant_an_exemption(self):
        """A flaky SAP read must not silently open the gate."""
        with patch(
            "raw_material_gatein.services.validations._read_bp_group_code",
            return_value=None,
        ):
            self.assertFalse(is_over_receipt_exempt("JIVO_OIL", "VENDA001595"))

    def test_a_vendor_listed_for_another_company_is_not_exempt_here(self):
        with patch(
            "raw_material_gatein.services.validations._read_bp_group_code",
            return_value=100,
        ):
            self.assertFalse(is_over_receipt_exempt("JIVO_MART", "VENDA001614"))

    def test_a_passed_in_group_code_skips_the_lookup(self):
        with patch(
            "raw_material_gatein.services.validations._read_bp_group_code"
        ) as read_group:
            self.assertTrue(
                is_over_receipt_exempt("JIVO_OIL", "VENDA001595", bp_group_code=101)
            )
            read_group.assert_not_called()


class SavePOItemsTests(TestCase):
    """`_save_po_items` is the gate's write path — it must take both the ceiling and
    the stored ordered quantity from SAP, never from the request body."""

    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            email="over-receipt@example.com",
            password="password",
            full_name="Over Receipt User",
            employee_code="OVREC001",
        )
        self.company = Company.objects.create(name="Over Receipt Co", code="JIVO_OIL")
        self.vehicle = Vehicle.objects.create(vehicle_number="HR69E9959")
        self.driver = Driver.objects.create(
            name="Over Receipt Driver",
            mobile_no="9888881234",
            license_no="OVREC-DL",
        )
        self.entry = VehicleEntry.objects.create(
            entry_no="OVREC-001",
            company=self.company,
            vehicle=self.vehicle,
            driver=self.driver,
            entry_type="RAW_MATERIAL",
            status=GateEntryStatus.QC_PENDING,
            created_by=self.user,
        )
        self.po_receipt = POReceipt.objects.create(
            vehicle_entry=self.entry,
            po_number="220626068",
            supplier_code="VENDA001595",
            supplier_name="BABAJI UDYOG PVT. LTD.",
            sap_doc_entry=4001,
            created_by=self.user,
        )
        # PO 220626068 line 0: 150,000 PCS ordered, 9,000 still open.
        self.sap_items_map = {
            0: {
                "po_item_code": "PM0000085",
                "ordered_qty": 150000.0,
                "remaining_qty": 9000.0,
                "rate": 1.29,
                "tax_code": "IGST18",
                "warehouse_code": "PM-DG",
                "account_code": "",
                "variety": "",
            }
        }

        exempt_patch = patch(
            "raw_material_gatein.views.is_over_receipt_exempt", return_value=False
        )
        exempt_patch.start()
        self.addCleanup(exempt_patch.stop)

    def _item(self, received_qty, ordered_qty="150000"):
        return {
            "line_num": 0,
            "po_item_code": "PM0000085",
            "item_name": "CTC CAP 36 MM",
            "ordered_qty": Decimal(ordered_qty),
            "received_qty": Decimal(received_qty),
            "uom": "PCS",
        }

    def test_a_receipt_over_the_open_quantity_is_refused(self):
        """BABAJI-3562: 12,000 PCS onto a line with 9,000 open. This used to be
        accepted because the ceiling was 150,000 x 1.1."""
        with self.assertRaises(DRFValidationError) as ctx:
            _save_po_items(
                self.po_receipt, [self._item("12000")], self.sap_items_map,
                self.user, "JIVO_OIL",
            )

        self.assertIn("9,900 PCS", str(ctx.exception.detail["error"]))
        self.assertEqual(self.po_receipt.items.count(), 0)

    def test_a_receipt_within_the_open_quantity_is_saved(self):
        _save_po_items(
            self.po_receipt, [self._item("9000")], self.sap_items_map,
            self.user, "JIVO_OIL",
        )

        item = self.po_receipt.items.get()
        self.assertEqual(item.received_qty, Decimal("9000.000"))

    def test_an_inflated_ordered_qty_in_the_request_cannot_widen_the_ceiling(self):
        """The client used to set its own limit: ordered_qty came off the request."""
        with self.assertRaises(DRFValidationError):
            _save_po_items(
                self.po_receipt,
                [self._item("12000", ordered_qty="99999999")],
                self.sap_items_map,
                self.user,
                "JIVO_OIL",
            )

    def test_the_stored_ordered_qty_comes_from_sap(self):
        _save_po_items(
            self.po_receipt,
            [self._item("9000", ordered_qty="7")],
            self.sap_items_map,
            self.user,
            "JIVO_OIL",
        )

        item = self.po_receipt.items.get()
        self.assertEqual(item.ordered_qty, Decimal("150000.000"))
        # short_qty is derived from it, so a bogus ordered qty would poison it too.
        self.assertEqual(item.short_qty, Decimal("141000.000"))

    def test_an_exempt_vendor_may_over_receive(self):
        with patch(
            "raw_material_gatein.views.is_over_receipt_exempt", return_value=True
        ):
            _save_po_items(
                self.po_receipt, [self._item("12000")], self.sap_items_map,
                self.user, "JIVO_OIL",
            )

        self.assertEqual(self.po_receipt.items.get().received_qty, Decimal("12000.000"))


@override_settings(GRPO_OVER_RECEIPT_ENFORCED_COMPANY_CODES=["JIVO_OIL"])
class OverReceiptCompanyScopeTests(SimpleTestCase):
    """SAP only enforces the tolerance in Oil.

    Its posted-GRPO rule (PDN1, error 200017) is commented out in the Mart and
    Beverages procedures; what survives there guards GRPO *drafts* (DRF1), which the
    Service Layer never creates. Enforcing it in those companies would block receipts
    SAP accepts today.
    """

    def test_oil_enforces(self):
        self.assertTrue(is_over_receipt_enforced("JIVO_OIL"))

    def test_mart_and_beverages_do_not(self):
        self.assertFalse(is_over_receipt_enforced("JIVO_MART"))
        self.assertFalse(is_over_receipt_enforced("JIVO_BEVERAGES"))

    def test_an_unknown_or_blank_company_does_not_enforce(self):
        self.assertFalse(is_over_receipt_enforced("TC001"))
        self.assertFalse(is_over_receipt_enforced(""))
        self.assertFalse(is_over_receipt_enforced(None))

    def test_an_unenforced_company_is_exempt_without_any_sap_read(self):
        with patch(
            "raw_material_gatein.services.validations._read_bp_group_code"
        ) as read_group:
            self.assertTrue(is_over_receipt_exempt("JIVO_BEVERAGES", "VENDA001595"))
            read_group.assert_not_called()

    def test_an_ordinary_vendor_in_oil_is_still_checked(self):
        with patch(
            "raw_material_gatein.services.validations._read_bp_group_code",
            return_value=100,
        ):
            self.assertFalse(is_over_receipt_exempt("JIVO_OIL", "VENDA001595"))
