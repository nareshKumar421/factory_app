"""Which bills share a return note, and what a note may not combine.

A note per bill is still the default and still the safe shape -- the credit note
that follows is raised against an invoice, so a note covering two bills has to be
credited by hand. But one delivery often comes back against several of a
customer's bills and the warehouse wants one sheet for the lot, so the grouping
is the operator's to pick at receipt.

What a note may NOT combine is refused before anything reaches SAP: a posted A/R
Return cannot be withdrawn by the app (160002/160010, a live `Cancel` came back
-1116), so a grouping mistake has to cost a round-trip, never a half-posted
return. Reuses `tests_posting`'s fakes -- no SAP, no HANA.
"""

from unittest import mock

from .models import GoodsReturnStatus
from .tests_posting import WAREHOUSE, FakeWriter, PostingTestCase


class ReturnNoteGroupingTests(PostingTestCase):
    def setUp(self):
        super().setUp()
        # `PostingTestCase` deliberately sells its two bills to different states,
        # which is how the per-invoice place of supply is proved. Combining needs
        # the opposite, so here the bills agree unless a test says otherwise.
        same = {"ship_to_code": "GGN", "ship_state": "HR", "pay_to_code": "GGN"}
        self.client_stub.addresses = {5001: same, 5002: dict(same), 5003: dict(same)}

    def three_bills(self, **kwargs):
        return self.build_return(
            [
                (5001, "1500", [("FG0000151", 10)]),
                (5002, "1501", [("FG0000329", 4)]),
                (5003, "1502", [("FG0000032", 6)]),
            ],
            **kwargs,
        )

    def refs(self, gr):
        return list(gr.invoice_refs.order_by("id"))

    def receive_grouped(self, gr, groups, writer=None):
        writer = writer or self.writer
        with mock.patch("sap_client.client.SAPClient", return_value=self.client_stub), \
             mock.patch("sap_client.context.CompanyContext", return_value=object()), \
             mock.patch(
                 "sap_client.service_layer.returns_writer.ReturnsWriter",
                 return_value=writer,
             ):
            return self.service.receive(
                gr.id, self.user, WAREHOUSE, self.allowed, grouping=groups
            )

    # -- the grouping itself ---------------------------------------------------

    def test_no_grouping_still_posts_one_note_per_bill(self):
        """The default is unchanged, so every return booked until now still posts
        exactly as it did."""
        gr = self.three_bills()
        self.receive(gr)
        self.assertEqual(len(self.writer.posted), 3)

    def test_two_bills_on_one_note_post_a_single_document(self):
        gr = self.three_bills()
        a, b, c = self.refs(gr)
        self.receive_grouped(gr, [[a.id, b.id], [c.id]])

        self.assertEqual(len(self.writer.posted), 2)
        self.assertEqual(
            [line["ItemCode"] for line in self.writer.posted[0]["DocumentLines"]],
            ["FG0000151", "FG0000329"],
        )
        self.assertEqual(
            [line["ItemCode"] for line in self.writer.posted[1]["DocumentLines"]],
            ["FG0000032"],
        )

    def test_every_bill_on_one_note_posts_one_document(self):
        gr = self.three_bills()
        gr = self.receive_grouped(gr, [[ref.id for ref in self.refs(gr)]])

        self.assertEqual(len(self.writer.posted), 1)
        self.assertEqual(gr.status, GoodsReturnStatus.POSTED)

    def test_the_bills_sharing_a_note_all_record_its_document(self):
        """Sharing a doc entry is what says they were combined -- the grouping is
        not stored anywhere else, and does not need to be."""
        gr = self.three_bills()
        a, b, c = self.refs(gr)
        self.receive_grouped(gr, [[a.id, b.id], [c.id]])

        a.refresh_from_db()
        b.refresh_from_db()
        c.refresh_from_db()
        self.assertEqual(a.sap_gr_doc_entry, b.sap_gr_doc_entry)
        self.assertNotEqual(a.sap_gr_doc_entry, c.sap_gr_doc_entry)
        self.assertTrue(all(ref.posted_at for ref in (a, b, c)))

    def test_a_combined_note_names_all_its_bills(self):
        gr = self.three_bills()
        a, b, c = self.refs(gr)
        self.receive_grouped(gr, [[a.id, b.id], [c.id]])

        combined = self.writer.posted[0]
        self.assertEqual(combined["NumAtCard"], f"{gr.entry_no} INV 1500+1501".upper())
        self.assertIn("invoices 1500, 1501", combined["Comments"])

    def test_a_one_bill_note_keeps_the_reference_it_always_had(self):
        """The already-posted lookup keys on it, so it must not drift."""
        gr = self.three_bills()
        a, b, c = self.refs(gr)
        self.receive_grouped(gr, [[a.id], [b.id], [c.id]])
        self.assertEqual(
            self.writer.posted[0]["NumAtCard"], f"{gr.entry_no} INV 1500".upper()
        )

    def test_an_item_returned_off_both_bills_becomes_one_line(self):
        """160020 -- SAP refuses the item twice and asks for the quantities merged."""
        gr = self.build_return(
            [
                (5001, "1500", [("FG0000151", 10)]),
                (5002, "1501", [("FG0000151", 4), ("FG0000329", 2)]),
            ]
        )
        a, b = self.refs(gr)
        self.receive_grouped(gr, [[a.id, b.id]])

        lines = self.writer.posted[0]["DocumentLines"]
        self.assertEqual([line["ItemCode"] for line in lines], ["FG0000151", "FG0000329"])
        self.assertEqual(lines[0]["Quantity"], 14)
        # Each bill's return keeps its own batch on the merged line.
        self.assertEqual([batch["Quantity"] for batch in lines[0]["BatchNumbers"]], [10, 4])
        self.assertEqual(len({b["BatchNumber"] for b in lines[0]["BatchNumbers"]}), 2)

    # -- what a note may not combine -------------------------------------------

    def test_two_customers_cannot_share_a_note(self):
        """A document carries one CardCode; the goods would go back against a
        customer who never bought them."""
        gr = self.build_return(
            [
                (5001, "1500", [("FG0000151", 10)], "CUST001"),
                (5002, "1501", [("FG0000329", 4)], "CUST002"),
            ]
        )
        a, b = self.refs(gr)
        with self.assertRaisesMessage(ValueError, "not all the same customer"):
            self.receive_grouped(gr, [[a.id, b.id]])
        self.assertEqual(self.writer.posted, [])

    def test_two_places_of_supply_cannot_share_a_note(self):
        """One document, one ShipToCode -- the wrong GST flavour is fatal (254000293)."""
        self.client_stub.addresses[5002] = {
            "ship_to_code": "MUM", "ship_state": "MH", "pay_to_code": "MUM"
        }
        gr = self.build_return(
            [
                (5001, "1500", [("FG0000151", 10)]),   # GGN / HR
                (5002, "1501", [("FG0000329", 4)]),    # MUM / MH
            ]
        )
        a, b = self.refs(gr)
        with self.assertRaisesMessage(ValueError, "different addresses"):
            self.receive_grouped(gr, [[a.id, b.id]])
        self.assertEqual(self.writer.posted, [])

    def test_one_item_billed_at_two_tax_codes_cannot_share_a_note(self):
        """Merged, the line could only carry one of them -- and there is no right
        one to pick."""
        gr = self.build_return(
            [
                (5001, "1500", [("FG0000151", 10)]),
                (5002, "1501", [("FG0000151", 4)]),
            ]
        )
        # Same place of supply, so the tax codes are the only thing disagreeing.
        self.client_stub.addresses[5002] = self.client_stub.addresses[5001]
        gr.lines.filter(invoice_ref__sap_invoice_doc_entry=5002).update(tax_code="CG+SG@12")
        a, b = self.refs(gr)
        with self.assertRaisesMessage(ValueError, "cannot share one return note"):
            self.receive_grouped(gr, [[a.id, b.id]])
        self.assertEqual(self.writer.posted, [])

    def test_bills_that_cannot_combine_post_fine_apart(self):
        """The refusal is about sharing a note, not about the bills themselves."""
        gr = self.build_return(
            [
                (5001, "1500", [("FG0000151", 10)], "CUST001"),
                (5002, "1501", [("FG0000329", 4)], "CUST002"),
            ]
        )
        a, b = self.refs(gr)
        gr = self.receive_grouped(gr, [[a.id], [b.id]])
        self.assertEqual(len(self.writer.posted), 2)
        self.assertEqual(gr.status, GoodsReturnStatus.POSTED)

    # -- the grouping has to be a partition of the bills still owing a document --

    def test_a_bill_left_off_every_note_is_refused(self):
        """Left out, its goods would never go back into SAP and nobody would see."""
        gr = self.three_bills()
        a, b, _c = self.refs(gr)
        with self.assertRaisesMessage(ValueError, "not on any return note"):
            self.receive_grouped(gr, [[a.id], [b.id]])
        self.assertEqual(self.writer.posted, [])

    def test_a_bill_on_two_notes_is_refused(self):
        """Twice would return the same stock twice, and SAP would take it."""
        gr = self.three_bills()
        a, b, c = self.refs(gr)
        with self.assertRaisesMessage(ValueError, "more than one return note"):
            self.receive_grouped(gr, [[a.id, b.id], [b.id, c.id]])
        self.assertEqual(self.writer.posted, [])

    def test_an_invoice_from_another_return_is_refused(self):
        gr = self.three_bills()
        refs = self.refs(gr)
        with self.assertRaisesMessage(ValueError, "not on this return"):
            self.receive_grouped(gr, [[ref.id for ref in refs] + [9999]])
        self.assertEqual(self.writer.posted, [])

    def test_an_empty_note_is_refused(self):
        gr = self.three_bills()
        refs = self.refs(gr)
        with self.assertRaisesMessage(ValueError, "has no invoices"):
            self.receive_grouped(gr, [[ref.id for ref in refs], []])
        self.assertEqual(self.writer.posted, [])

    def test_the_grouping_is_checked_before_sap_is_touched(self):
        """A grouping mistake must not cost a half-posted, unwithdrawable return."""
        gr = self.three_bills()
        a, _b, _c = self.refs(gr)
        with self.assertRaises(ValueError):
            self.receive_grouped(gr, [[a.id]])
        gr.refresh_from_db()
        self.assertEqual(gr.status, GoodsReturnStatus.ARRIVED)
        self.assertEqual(self.writer.posted, [])

    # -- retries ----------------------------------------------------------------

    def test_a_retry_groups_only_the_bills_still_owing_a_document(self):
        gr = self.three_bills()
        a, b, c = self.refs(gr)
        # The first run takes bill 1 and SAP refuses the other two.
        first = FakeWriter(refuse=("1501", "1502"))
        self.receive_grouped(gr, [[a.id], [b.id], [c.id]], writer=first)
        gr.refresh_from_db()
        self.assertEqual(gr.status, GoodsReturnStatus.PARTIALLY_POSTED)

        # The retry may combine what is left, but must not name what already posted.
        with self.assertRaisesMessage(ValueError, "already in SAP"):
            self.receive_grouped(gr, [[a.id, b.id, c.id]], writer=FakeWriter(start=9500))

        retry = FakeWriter(start=9500)
        gr = self.receive_grouped(gr, [[b.id, c.id]], writer=retry)
        self.assertEqual(len(retry.posted), 1)
        self.assertEqual(gr.status, GoodsReturnStatus.POSTED)

    def test_a_refused_note_puts_the_error_on_every_bill_it_covered(self):
        """All of them are still owing a document, so all of them come back."""
        gr = self.three_bills()
        a, b, c = self.refs(gr)
        writer = FakeWriter(refuse=("1500",))
        self.receive_grouped(gr, [[a.id, b.id], [c.id]], writer=writer)

        a.refresh_from_db()
        b.refresh_from_db()
        self.assertTrue(a.sap_post_error)
        self.assertTrue(b.sap_post_error)
        self.assertIsNone(a.sap_gr_doc_entry)
        self.assertIsNone(b.sap_gr_doc_entry)
