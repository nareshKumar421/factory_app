"""Posting a goods return to SAP: one A/R Return per source invoice.

A return booked against two invoices is two returns in SAP, not one combined
document -- the credit note that follows is raised per invoice, and each bill's
place of supply and tax flavour are its own. These tests pin the split itself and
the things that only bite once there is more than one document: a reference and a
batch number that cannot be shared, and a run SAP half-accepts.

No SAP and no HANA. `SAPClient` and `ReturnsWriter` are patched at the seams
`_post_sap_returns` imports them from.
"""

from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from company.models import Company
from driver_management.models import Driver
from vehicle_management.models import Vehicle

from .models import (
    GoodsReturn,
    GoodsReturnInvoiceRef,
    GoodsReturnItem,
    GoodsReturnStatus,
)
from .services import GoodsReturnService

WAREHOUSE = "OIL-GR"


class FakeSAPClient:
    """The reads a return makes, answered from the item codes it asks about."""

    def __init__(self, company_code=None, **kwargs):
        self.company_code = company_code
        # doc entry -> ship-to, so a per-invoice place of supply is observable.
        self.addresses = {
            5001: {"ship_to_code": "GGN", "ship_state": "HR", "pay_to_code": "GGN"},
            5002: {"ship_to_code": "MUM", "ship_state": "MH", "pay_to_code": "MUM"},
        }
        self.existing_returns = {}
        self.address_calls = []

    def customer_group_code(self, card_code):
        return 101

    def warehouse_branch_id(self, warehouse_code):
        return 7

    def branch_state(self, branch_id):
        return "HR"

    def return_variety_codes(self, item_codes):
        return {code: "OLIVE" for code in item_codes}

    def return_costs(self, item_codes, warehouse):
        return {code: Decimal("100.00") for code in item_codes}

    def return_tax_codes(self, card_code, item_codes):
        return {code: "CG+SG@5" for code in item_codes}

    def ar_tax_codes(self):
        return {
            "CG+SG@5": {"code": "CG+SG@5", "name": "CGST 2.5 + SGST 2.5", "rate": Decimal(5)},
            "IGST@5": {"code": "IGST@5", "name": "IGST 5", "rate": Decimal(5)},
        }

    def invoice_addresses(self, doc_entry):
        self.address_calls.append(doc_entry)
        return self.addresses.get(doc_entry, {})

    def customer_last_invoice_addresses(self, card_code):
        return {}

    def customer_address_state(self, card_code, address_name):
        return ""

    def find_goods_return_by_reference(self, card_code, num_at_card):
        return self.existing_returns.get(num_at_card)


class FakeWriter:
    """Records what was posted; `refuse` names the references SAP rejects."""

    def __init__(self, context=None, refuse=(), start=9000):
        self.context = context
        self.refuse = set(refuse)
        self.posted = []
        self.next_entry = start

    def create(self, payload):
        reference = payload["NumAtCard"]
        if any(token in reference for token in self.refuse):
            raise RuntimeError(f"-5002 refused {reference}")
        self.posted.append(payload)
        self.next_entry += 1
        return {"DocEntry": self.next_entry, "DocNum": str(160000 + self.next_entry)}


class PostingTestCase(TestCase):
    """A gated-in, invoice-basis return ready to be received."""

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="OIL")
        self.user = get_user_model().objects.create(
            email="clerk@example.com", full_name="Return Clerk"
        )
        self.vehicle = Vehicle.objects.create(vehicle_number="PB01AB1234")
        self.driver = Driver.objects.create(
            name="Ranjit", mobile_no="9990001111", license_no="DL-1"
        )
        self.service = GoodsReturnService(self.company)
        self.allowed = [self.company.id]

        self.client_stub = FakeSAPClient()
        self.writer = FakeWriter()

    def build_return(self, invoices, *, basis="INVOICE", customer_ref_no=""):
        """`invoices` is [(doc_entry, doc_num, [(item, qty)])]; [] means no invoice.

        A fourth element names the bill's own customer, for the returns that carry
        more than one distributor's bills; without it the bill takes the header's.
        """
        gr = GoodsReturn.objects.create(
            company=self.company,
            entry_no=GoodsReturn.generate_entry_no(),
            basis=basis,
            status=GoodsReturnStatus.ARRIVED,
            customer_code="CUST001",
            customer_name="Sharma Traders",
            customer_ref_no=customer_ref_no,
            vehicle=self.vehicle,
            driver=self.driver,
        )
        for doc_entry, doc_num, items, *rest in invoices:
            card_code = rest[0] if rest else ""
            ref = None
            if doc_entry is not None:
                ref = GoodsReturnInvoiceRef.objects.create(
                    goods_return=gr,
                    sap_invoice_doc_entry=doc_entry,
                    sap_invoice_doc_num=doc_num,
                    customer_code=card_code,
                    customer_name=f"{card_code} Traders" if card_code else "",
                )
            for line_num, (item, qty) in enumerate(items):
                GoodsReturnItem.objects.create(
                    goods_return=gr,
                    invoice_ref=ref,
                    source_line_num=line_num,
                    item_code=item,
                    item_name=item,
                    uom="PCS",
                    invoice_quantity=1000,
                    return_quantity=qty,
                    tax_code="CG+SG@5",
                )
        return gr

    def receive(self, gr, writer=None):
        writer = writer or self.writer
        with mock.patch("sap_client.client.SAPClient", return_value=self.client_stub), \
             mock.patch("sap_client.context.CompanyContext", return_value=object()), \
             mock.patch(
                 "sap_client.service_layer.returns_writer.ReturnsWriter",
                 return_value=writer,
             ):
            return self.service.receive(gr.id, self.user, WAREHOUSE, self.allowed)


class OneDocumentPerInvoiceTests(PostingTestCase):
    def test_two_invoices_post_two_returns(self):
        gr = self.build_return(
            [
                (5001, "1500", [("FG0000151", 10)]),
                (5002, "1501", [("FG0000329", 4)]),
            ]
        )
        gr = self.receive(gr)

        self.assertEqual(len(self.writer.posted), 2)
        self.assertEqual(gr.status, GoodsReturnStatus.POSTED)

    def test_each_document_carries_only_its_own_invoices_lines(self):
        gr = self.build_return(
            [
                (5001, "1500", [("FG0000151", 10), ("FG0000032", 5)]),
                (5002, "1501", [("FG0000329", 4)]),
            ]
        )
        self.receive(gr)

        first, second = self.writer.posted
        self.assertEqual(
            [line["ItemCode"] for line in first["DocumentLines"]],
            ["FG0000151", "FG0000032"],
        )
        self.assertEqual(
            [line["ItemCode"] for line in second["DocumentLines"]], ["FG0000329"]
        )

    def test_each_invoice_records_its_own_document(self):
        gr = self.build_return(
            [(5001, "1500", [("FG0000151", 10)]), (5002, "1501", [("FG0000329", 4)])]
        )
        self.receive(gr)

        refs = list(gr.invoice_refs.order_by("id"))
        self.assertEqual([ref.sap_gr_doc_entry for ref in refs], [9001, 9002])
        self.assertEqual(
            [ref.sap_gr_doc_num for ref in refs], ["169001", "169002"]
        )
        self.assertTrue(all(ref.sap_return_warehouse == WAREHOUSE for ref in refs))
        self.assertTrue(all(ref.posted_at is not None for ref in refs))

    def test_the_header_keeps_the_first_document_as_its_handle(self):
        gr = self.build_return(
            [(5001, "1500", [("FG0000151", 10)]), (5002, "1501", [("FG0000329", 4)])]
        )
        gr = self.receive(gr)
        self.assertEqual(gr.sap_gr_doc_entry, 9001)
        self.assertEqual(gr.sap_gr_doc_num, "169001")

    def test_each_document_names_only_its_own_invoice(self):
        gr = self.build_return(
            [(5001, "1500", [("FG0000151", 10)]), (5002, "1501", [("FG0000329", 4)])]
        )
        self.receive(gr)

        first, second = self.writer.posted
        self.assertIn("1500", first["Comments"])
        self.assertNotIn("1501", first["Comments"])
        self.assertIn("1501", second["Comments"])

    def test_the_customer_reference_differs_per_document(self):
        # A shared NumAtCard would be refused as a duplicate reference (-5002),
        # and would make the app mistake one invoice's return for another's.
        gr = self.build_return(
            [(5001, "1500", [("FG0000151", 10)]), (5002, "1501", [("FG0000329", 4)])]
        )
        self.receive(gr)

        references = [payload["NumAtCard"] for payload in self.writer.posted]
        self.assertEqual(len(set(references)), 2)
        self.assertIn("1500", references[0])
        self.assertIn("1501", references[1])
        self.assertTrue(all(ref == ref.upper() for ref in references))

    def test_an_item_returned_off_both_invoices_is_no_longer_refused(self):
        # On one combined document SAP refuses duplicate item lines (160020), so
        # the same item on two bills used to block the whole return.
        gr = self.build_return(
            [(5001, "1500", [("FG0000151", 10)]), (5002, "1501", [("FG0000151", 4)])]
        )
        self.receive(gr)

        self.assertEqual(len(self.writer.posted), 2)
        quantities = [
            payload["DocumentLines"][0]["Quantity"] for payload in self.writer.posted
        ]
        self.assertEqual(quantities, [10, 4])

    def test_the_same_item_on_two_documents_gets_two_batches(self):
        # Both documents numbering their lines from zero would mint one batch
        # twice, which SAP refuses (10001226 Batch ... already exists).
        gr = self.build_return(
            [(5001, "1500", [("FG0000151", 10)]), (5002, "1501", [("FG0000151", 4)])]
        )
        self.receive(gr)

        batches = [
            payload["DocumentLines"][0]["BatchNumbers"][0]["BatchNumber"]
            for payload in self.writer.posted
        ]
        self.assertEqual(len(set(batches)), 2)

    def test_the_same_item_twice_is_consolidated_into_one_line(self):
        """160020 asks for the quantities merged, and now they are.

        Two lines of one item on one bill is ordinary -- the goods came back in
        two batches, or in two conditions -- and used to be refused outright
        because the app could only post a line per line.
        """
        gr = self.build_return(
            [(5001, "1500", [("FG0000151", 10), ("FG0000151", 4)])]
        )
        self.receive(gr)

        lines = self.writer.posted[0]["DocumentLines"]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["ItemCode"], "FG0000151")
        self.assertEqual(lines[0]["Quantity"], 14)
        # One batch per source line, so the two physical returns stay apart.
        self.assertEqual(
            [batch["Quantity"] for batch in lines[0]["BatchNumbers"]], [10, 4]
        )
        self.assertEqual(len({b["BatchNumber"] for b in lines[0]["BatchNumbers"]}), 2)

    def test_each_document_takes_its_own_invoices_place_of_supply(self):
        gr = self.build_return(
            [(5001, "1500", [("FG0000151", 10)]), (5002, "1501", [("FG0000329", 4)])]
        )
        self.receive(gr)

        first, second = self.writer.posted
        self.assertEqual(first["ShipToCode"], "GGN")
        self.assertEqual(second["ShipToCode"], "MUM")
        # ...and the tax flavour that follows: HR branch -> HR is intra-state,
        # HR branch -> MH is inter-state.
        self.assertEqual(first["DocumentLines"][0]["TaxCode"], "CG+SG@5")
        self.assertEqual(second["DocumentLines"][0]["TaxCode"], "IGST@5")

    def test_the_customers_debit_note_number_rides_in_comments(self):
        """Their number, in SAP's Comments -- never in NumAtCard.

        NumAtCard is the app's handle on a document it has already posted and
        has to stay unique per (return, invoice); two returns may quote the same
        debit note, and SAP would refuse the second with -5002.
        """
        gr = self.build_return(
            [(None, "", [("FG0000151", 10)])],
            basis="DEBIT_NOTE",
            customer_ref_no="DN-4471",
        )
        self.receive(gr)

        posted = self.writer.posted[0]
        self.assertIn("DN-4471", posted["Comments"])
        self.assertNotIn("DN-4471", posted["NumAtCard"])
        self.assertIn(gr.entry_no, posted["NumAtCard"])

    def test_a_debit_note_return_without_a_number_still_posts(self):
        """The number is optional -- plenty of letter pads carry none."""
        gr = self.build_return([(None, "", [("FG0000151", 10)])], basis="LETTER_PAD")
        self.receive(gr)

        self.assertIn("customer letter pad", self.writer.posted[0]["Comments"])

    def test_a_debit_note_return_still_posts_one_document(self):
        gr = self.build_return(
            [(None, "", [("FG0000151", 10)])], basis="DEBIT_NOTE"
        )
        gr = self.receive(gr)

        self.assertEqual(len(self.writer.posted), 1)
        self.assertIn("DEBIT_NOTE", self.writer.posted[0]["NumAtCard"])
        self.assertEqual(gr.status, GoodsReturnStatus.POSTED)

    def test_a_hand_keyed_item_rides_on_the_first_invoices_document(self):
        gr = self.build_return(
            [
                (5001, "1500", [("FG0000151", 10)]),
                (5002, "1501", [("FG0000329", 4)]),
                (None, "", [("FG0000032", 2)]),
            ]
        )
        self.receive(gr)

        self.assertEqual(len(self.writer.posted), 2)
        self.assertEqual(
            [line["ItemCode"] for line in self.writer.posted[0]["DocumentLines"]],
            ["FG0000151", "FG0000032"],
        )


class HalfAcceptedRunTests(PostingTestCase):
    """SAP takes one invoice's return and refuses another's."""

    def setUp(self):
        super().setUp()
        self.gr = self.build_return(
            [(5001, "1500", [("FG0000151", 10)]), (5002, "1501", [("FG0000329", 4)])]
        )

    def test_the_accepted_document_is_kept_not_rolled_back(self):
        gr = self.receive(self.gr, FakeWriter(refuse=["1501"]))

        self.assertEqual(gr.status, GoodsReturnStatus.PARTIALLY_POSTED)
        first, second = gr.invoice_refs.order_by("id")
        self.assertEqual(first.sap_gr_doc_entry, 9001)
        self.assertIsNone(second.sap_gr_doc_entry)

    def test_the_refusal_is_recorded_against_the_invoice_it_belongs_to(self):
        gr = self.receive(self.gr, FakeWriter(refuse=["1501"]))

        first, second = gr.invoice_refs.order_by("id")
        self.assertEqual(first.sap_post_error, "")
        self.assertIn("-5002", second.sap_post_error)

    def test_the_caller_is_told_which_invoice_failed(self):
        gr = self.receive(self.gr, FakeWriter(refuse=["1501"]))
        message = GoodsReturnService._posting_failure_message(gr.posting_failures)
        self.assertIn("1501", message)
        self.assertNotIn("invoice 1500", message)

    def test_receiving_again_posts_only_the_refused_invoice(self):
        self.receive(self.gr, FakeWriter(refuse=["1501"]))

        retry = FakeWriter(start=9500)
        gr = self.receive(self.gr, retry)

        self.assertEqual(len(retry.posted), 1)
        self.assertIn("1501", retry.posted[0]["NumAtCard"])
        self.assertEqual(gr.status, GoodsReturnStatus.POSTED)
        first, second = gr.invoice_refs.order_by("id")
        self.assertEqual(first.sap_gr_doc_entry, 9001)  # untouched
        self.assertEqual(second.sap_gr_doc_entry, 9501)

    def test_a_retry_cannot_send_the_rest_to_another_warehouse(self):
        self.receive(self.gr, FakeWriter(refuse=["1501"]))
        with self.assertRaisesMessage(ValueError, "same warehouse"):
            with mock.patch("sap_client.client.SAPClient", return_value=self.client_stub), \
                 mock.patch("sap_client.context.CompanyContext", return_value=object()), \
                 mock.patch(
                     "sap_client.service_layer.returns_writer.ReturnsWriter",
                     return_value=FakeWriter(),
                 ):
                self.service.receive(self.gr.id, self.user, "OIL-OTHER", self.allowed)

    def test_a_run_sap_refuses_outright_leaves_nothing_behind(self):
        with self.assertRaises(ValueError):
            self.receive(self.gr, FakeWriter(refuse=["1500", "1501"]))

        self.gr.refresh_from_db()
        self.assertEqual(self.gr.status, GoodsReturnStatus.ARRIVED)
        self.assertIsNone(self.gr.sap_gr_doc_entry)

    def test_a_return_sap_already_holds_is_not_posted_twice(self):
        # A crash between SAP accepting a document and the app recording it: the
        # reference is unique per (return, invoice), so it is found and adopted.
        reference = f"{self.gr.entry_no} INV 1500".upper()
        self.client_stub.existing_returns[reference] = {
            "doc_entry": 8888,
            "doc_num": "168888",
        }
        gr = self.receive(self.gr)

        self.assertEqual(len(self.writer.posted), 1)  # only the second invoice
        first, second = gr.invoice_refs.order_by("id")
        self.assertEqual(first.sap_gr_doc_entry, 8888)
        self.assertEqual(second.sap_gr_doc_entry, 9001)
        self.assertEqual(gr.status, GoodsReturnStatus.POSTED)


class PrintSelectionTests(PostingTestCase):
    """Which of a multi-document return's Return Notes gets printed."""

    def test_a_document_from_another_return_is_refused(self):
        gr = self.build_return(
            [(5001, "1500", [("FG0000151", 10)]), (5002, "1501", [("FG0000329", 4)])]
        )
        self.receive(gr)
        with self.assertRaisesMessage(ValueError, "does not belong"):
            GoodsReturnService._printable_doc_entry(gr, 7777)

    def test_either_of_the_returns_own_documents_can_be_printed(self):
        gr = self.build_return(
            [(5001, "1500", [("FG0000151", 10)]), (5002, "1501", [("FG0000329", 4)])]
        )
        gr = self.receive(gr)
        self.assertEqual(GoodsReturnService._printable_doc_entry(gr, 9002), 9002)
        # No choice means the header's own document.
        self.assertEqual(GoodsReturnService._printable_doc_entry(gr, None), 9001)

    def test_an_unposted_return_has_nothing_to_print(self):
        gr = self.build_return([(5001, "1500", [("FG0000151", 10)])])
        with self.assertRaisesMessage(ValueError, "not been posted"):
            GoodsReturnService._printable_doc_entry(gr, None)


class MixedCustomerTests(PostingTestCase):
    """One truck, several distributors' bills — each posting under its own.

    A return is a truckload, not a customer: a vehicle coming back off a market
    run carries the bills of whoever it called on. Since every bill already posts
    its own A/R Return, the customer belongs to the bill, and these tests pin that
    nothing on the posting path reaches for the header's instead.
    """

    def test_each_document_is_raised_on_its_own_bills_customer(self):
        gr = self.build_return(
            [
                (5001, "1500", [("FG0000151", 10)], "CUST001"),
                (5002, "1501", [("FG0000329", 4)], "CUST002"),
            ]
        )
        self.receive(gr)

        self.assertEqual(
            [payload["CardCode"] for payload in self.writer.posted],
            ["CUST001", "CUST002"],
        )

    def test_the_tax_codes_are_asked_of_the_bills_own_customer(self):
        """A code read off the wrong customer's history is a document SAP refuses."""
        asked = []
        self.client_stub.return_tax_codes = lambda card_code, item_codes: (
            asked.append((card_code, tuple(item_codes)))
            or {code: "CG+SG@5" for code in item_codes}
        )
        gr = self.build_return(
            [
                (5001, "1500", [("FG0000151", 10)], "CUST001"),
                (5002, "1501", [("FG0000329", 4)], "CUST002"),
            ]
        )
        # Nothing snapshotted, so both documents have to ask.
        gr.lines.update(tax_code="")
        self.receive(gr)

        self.assertEqual(
            asked, [("CUST001", ("FG0000151",)), ("CUST002", ("FG0000329",))]
        )

    def test_a_bill_with_no_addresses_falls_back_to_its_own_customer(self):
        """The place-of-supply fallback reads an address book — the right one."""
        self.client_stub.addresses = {}
        asked = []
        self.client_stub.customer_last_invoice_addresses = lambda card_code: (
            asked.append(card_code) or {}
        )
        gr = self.build_return(
            [
                (5001, "1500", [("FG0000151", 10)], "CUST001"),
                (5002, "1501", [("FG0000329", 4)], "CUST002"),
            ]
        )
        self.receive(gr)

        self.assertEqual(asked, ["CUST001", "CUST002"])

    def test_a_branch_customer_on_one_bill_stops_the_whole_return(self):
        """160012 is checked per bill, and before anything is written.

        The header's customer being a real one is no longer proof the others are,
        and a return SAP would refuse on its second document has to fail before
        the first one is posted — SAP will not let the app cancel it.
        """
        self.client_stub.customer_group_code = lambda card_code: (
            100 if card_code == "CUST002" else 101
        )
        gr = self.build_return(
            [
                (5001, "1500", [("FG0000151", 10)], "CUST001"),
                (5002, "1501", [("FG0000329", 4)], "CUST002"),
            ]
        )
        with self.assertRaisesMessage(ValueError, "internal branch"):
            self.receive(gr)
        self.assertEqual(self.writer.posted, [])

    def test_a_bill_without_its_own_customer_still_takes_the_headers(self):
        """The returns booked before the customer moved onto the bill."""
        gr = self.build_return([(5001, "1500", [("FG0000151", 10)])])
        self.receive(gr)
        self.assertEqual(self.writer.posted[0]["CardCode"], "CUST001")
