"""GRPO posting re-checks the over-receipt tolerance against live open quantities.

The gate already caps each line at 110% of the PO line's open quantity, but open
quantity moves between gate-in and posting: another GRPO can consume the same PO
line, and QC can raise the accepted quantity. Without a re-read, SAP's 200017 is the
first anyone hears of it — after the gate entry is closed and QC is done.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from grpo.services import GRPOService


def _line(base_entry, base_line, qty, item_code="PM0000085"):
    return {
        "po_item_receipt": SimpleNamespace(po_item_code=item_code),
        "quantity_posted": Decimal(str(qty)),
        "base_entry": base_entry,
        "base_line": base_line,
    }


@override_settings(
    GRPO_OVER_RECEIPT_ENFORCED_COMPANY_CODES=["JIVO_OIL"],
    GRPO_OVER_RECEIPT_EXEMPT_VENDORS={"JIVO_OIL": []},
)
class GRPOOverReceiptRecheckTests(SimpleTestCase):
    def setUp(self):
        self.service = GRPOService(company_code="JIVO_OIL")

        group_patch = patch.object(
            GRPOService, "_get_sap_bp_group_code", return_value=100
        )
        group_patch.start()
        self.addCleanup(group_patch.stop)

        self.client = MagicMock()
        client_patch = patch("grpo.services.SAPClient", return_value=self.client)
        client_patch.start()
        self.addCleanup(client_patch.stop)

    def _validate(self, lines, supplier_code="VENDA001595"):
        self.service._validate_over_receipt_tolerance(supplier_code, lines)

    def test_a_quantity_within_the_live_open_qty_posts(self):
        self.client.get_po_open_qtys.return_value = {(4001, 1): 89000.0}

        self._validate([_line(4001, 1, "87000")])

    def test_a_quantity_over_the_live_open_qty_is_refused(self):
        self.client.get_po_open_qtys.return_value = {(4001, 0): 9000.0}

        with self.assertRaises(ValueError) as ctx:
            self._validate([_line(4001, 0, "12000")])

        message = str(ctx.exception)
        self.assertIn("PM0000085 (PO line 0)", message)
        self.assertIn("only 9000 is still open", message)
        self.assertIn("9900", message)

    def test_open_qty_consumed_after_gate_in_is_caught_here(self):
        """The gate saw 12,000 open and allowed 12,000; by posting time it is 500."""
        self.client.get_po_open_qtys.return_value = {(4001, 0): 500.0}

        with self.assertRaises(ValueError):
            self._validate([_line(4001, 0, "12000")])

    def test_lines_against_the_same_po_line_are_totalled(self):
        """SAP totals them before checking BaseOpnQty, so neither line alone tells."""
        self.client.get_po_open_qtys.return_value = {(4001, 0): 9000.0}

        with self.assertRaises(ValueError):
            self._validate([_line(4001, 0, "6000"), _line(4001, 0, "6000")])

    def test_a_po_line_that_has_vanished_is_refused(self):
        self.client.get_po_open_qtys.return_value = {}

        with self.assertRaises(ValueError) as ctx:
            self._validate([_line(4001, 0, "100")])

        self.assertIn("no longer on the purchase order", str(ctx.exception))

    def test_lines_without_po_linkage_are_not_checked(self):
        """An unlinked line has no BaseOpnQty for SAP to compare against."""
        self._validate([_line(None, None, "999999")])

        self.client.get_po_open_qtys.assert_not_called()

    @override_settings(
        GRPO_OVER_RECEIPT_ENFORCED_COMPANY_CODES=["JIVO_OIL"],
        GRPO_OVER_RECEIPT_EXEMPT_VENDORS={"JIVO_OIL": ["VENDA000483"]},
    )
    def test_an_exempt_vendor_skips_the_re_check(self):
        self._validate([_line(4001, 0, "999999")], supplier_code="VENDA000483")

        self.client.get_po_open_qtys.assert_not_called()

    def test_a_branch_vendor_skips_the_re_check(self):
        with patch.object(GRPOService, "_get_sap_bp_group_code", return_value=101):
            self._validate([_line(4001, 0, "999999")])

        self.client.get_po_open_qtys.assert_not_called()


@override_settings(GRPO_OVER_RECEIPT_ENFORCED_COMPANY_CODES=["JIVO_OIL"])
class GRPOOverReceiptCompanyScopeTests(SimpleTestCase):
    """A company SAP does not enforce the rule in is not re-checked, and does not pay
    for the extra SAP read either."""

    def setUp(self):
        self.client = MagicMock()
        client_patch = patch("grpo.services.SAPClient", return_value=self.client)
        client_patch.start()
        self.addCleanup(client_patch.stop)

    def test_beverages_posts_without_the_re_check(self):
        service = GRPOService(company_code="JIVO_BEVERAGES")

        service._validate_over_receipt_tolerance(
            "VENDA001595", [_line(4001, 0, "999999")]
        )

        self.client.get_po_open_qtys.assert_not_called()

    def test_mart_posts_without_the_re_check(self):
        service = GRPOService(company_code="JIVO_MART")

        service._validate_over_receipt_tolerance(
            "VENDA001595", [_line(4001, 0, "999999")]
        )

        self.client.get_po_open_qtys.assert_not_called()


@override_settings(
    GRPO_OVER_RECEIPT_ENFORCED_COMPANY_CODES=["JIVO_OIL"],
    GRPO_OVER_RECEIPT_EXEMPT_VENDORS={"JIVO_OIL": []},
)
class GRPOExhaustedPOSuggestsTheReplacementTests(SimpleTestCase):
    """A refusal names the vendor's other open PO for the same material.

    The PO a truck was gated in against runs out because a *different* truck's
    GRPO consumed it, and purchase has usually already raised the replacement —
    so the one thing the operator needs is its number (GE-2026-8871, 2026-09-15).
    """

    def setUp(self):
        self.service = GRPOService(company_code="JIVO_OIL")

        group_patch = patch.object(
            GRPOService, "_get_sap_bp_group_code", return_value=100
        )
        group_patch.start()
        self.addCleanup(group_patch.stop)

        self.client = MagicMock()
        self.client.get_po_open_qtys.return_value = {(13462, 0): 0.0}
        self.client.get_open_pos.return_value = []
        client_patch = patch("grpo.services.SAPClient", return_value=self.client)
        client_patch.start()
        self.addCleanup(client_patch.stop)

    def _po(self, doc_entry, po_number, item_code, remaining):
        return SimpleNamespace(
            doc_entry=doc_entry,
            po_number=po_number,
            items=[
                SimpleNamespace(po_item_code=item_code, remaining_qty=remaining)
            ],
        )

    def test_the_replacement_po_is_named(self):
        self.client.get_open_pos.return_value = [
            self._po(13802, "220926064", "PM0000914", 18656.0)
        ]

        with self.assertRaises(ValueError) as ctx:
            self.service._validate_over_receipt_tolerance(
                "VENDA000936", [_line(13462, 0, "1960", item_code="PM0000914")]
            )

        message = str(ctx.exception)
        self.assertIn("only 0 is still open", message)
        self.assertIn("PO 220926064 has 18656 open for PM0000914", message)

    def test_the_exhausted_po_is_not_offered_as_its_own_replacement(self):
        self.client.get_open_pos.return_value = [
            self._po(13462, "220826133", "PM0000914", 0.0)
        ]

        with self.assertRaises(ValueError) as ctx:
            self.service._validate_over_receipt_tolerance(
                "VENDA000936", [_line(13462, 0, "1960", item_code="PM0000914")]
            )

        self.assertNotIn("another open purchase order", str(ctx.exception))

    def test_another_po_for_a_different_item_is_not_offered(self):
        self.client.get_open_pos.return_value = [
            self._po(13765, "220926050", "PM0000411", 7912.0)
        ]

        with self.assertRaises(ValueError) as ctx:
            self.service._validate_over_receipt_tolerance(
                "VENDA000936", [_line(13462, 0, "1960", item_code="PM0000914")]
            )

        self.assertNotIn("220926050", str(ctx.exception))

    def test_a_failed_lookup_still_reports_the_real_error(self):
        self.client.get_open_pos.side_effect = RuntimeError("HANA down")

        with self.assertRaises(ValueError) as ctx:
            self.service._validate_over_receipt_tolerance(
                "VENDA000936", [_line(13462, 0, "1960", item_code="PM0000914")]
            )

        self.assertIn("only 0 is still open", str(ctx.exception))

    def test_a_vanished_po_line_also_gets_a_suggestion(self):
        self.client.get_po_open_qtys.return_value = {}
        self.client.get_open_pos.return_value = [
            self._po(13802, "220926064", "PM0000914", 18656.0)
        ]

        with self.assertRaises(ValueError) as ctx:
            self.service._validate_over_receipt_tolerance(
                "VENDA000936", [_line(13462, 0, "1960", item_code="PM0000914")]
            )

        self.assertIn("PO 220926064", str(ctx.exception))
