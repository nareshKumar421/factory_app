"""Short Dispatch: the return note for stock a posted bill says went out.

What these pin is the difference between this module and a customer return, not
the A/R Return payload itself -- that is `goods_return`'s and is tested there:

* the invoice is the authority for every field except the short quantity;
* a bill cannot be returned twice for more than it was billed, across entries;
* a refusal by SAP leaves no record at all, because there is no draft stage.

No SAP and no HANA. `SAPClient`, `ReturnsWriter` and `DispatchPlansService` are
patched at the seams the service imports them from.
"""

from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.exceptions import PermissionDenied

from company.models import Company

from .models import ShortDispatch, ShortDispatchItem
from .services import ShortDispatchService

WAREHOUSE = "BH-PC"


def bill_line(line_num, item_code, quantity, *, warehouse=WAREHOUSE, tax_code="CG+SG@5"):
    return {
        "line_num": line_num,
        "item_code": item_code,
        "item_name": f"{item_code} description",
        "quantity": quantity,
        "uom": "PCS",
        "rate": 250.0,
        "tax_code": tax_code,
        "warehouse_code": warehouse,
    }


class FakeDispatchPlansService:
    """Answers `get_bill_by_number` from a bill dict the test hands over."""

    bill = None

    def __init__(self, company_code=None):
        self.company_code = company_code

    def get_bill_by_number(self, number):
        bill = type(self).bill
        if bill is None or str(bill["doc_num"]) != str(number).strip():
            return None
        return bill


class FakeSAPClient:
    """Every read a short dispatch makes, answered from the item codes it asks."""

    def __init__(self, company_code=None, **kwargs):
        self.company_code = company_code
        self.batch_allocations = []
        self.batch_flags = {}
        self.existing_returns = {}
        self.print_payloads = {}

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
        return {"ship_to_code": "GGN", "ship_state": "HR", "pay_to_code": "GGN-BILL"}

    def customer_last_invoice_addresses(self, card_code):
        return {}

    def customer_address_state(self, card_code, address_name):
        return ""

    def find_goods_return_by_reference(self, card_code, num_at_card):
        return self.existing_returns.get(num_at_card)

    def posted_batch_allocations(self, doc_entry, **kwargs):
        return self.batch_allocations

    def batch_managed_flags(self, item_codes):
        return {code: self.batch_flags.get(code, True) for code in item_codes}

    def get_active_warehouses(self, *args, **kwargs):
        return []

    def goods_return_print(self, doc_entry):
        return self.print_payloads.get(doc_entry)


class FakeWriter:
    """Records what was posted; `refuse=True` makes SAP reject the document."""

    def __init__(self, context=None, refuse=False, start=9000):
        self.context = context
        self.refuse = refuse
        self.posted = []
        self.next_entry = start

    def create(self, payload):
        if self.refuse:
            raise RuntimeError("160021 no cost is defined for the item")
        self.posted.append(payload)
        self.next_entry += 1
        return {"DocEntry": self.next_entry, "DocNum": str(170000 + self.next_entry)}


class ShortDispatchTestCase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="OIL")
        self.other_company = Company.objects.create(name="Jivo Mart", code="MART")
        self.user = get_user_model().objects.create(
            email="store@example.com", full_name="Store Keeper"
        )
        self.service = ShortDispatchService(self.company)
        self.allowed = [self.company.id]

        self.sap = FakeSAPClient()
        self.writer = FakeWriter()
        self.set_bill(
            [bill_line(0, "FG0000151", 100), bill_line(1, "FG0000329", 40)]
        )

    def set_bill(self, lines, *, doc_entry=5001, doc_num="1500", card_code="CUST001"):
        FakeDispatchPlansService.bill = {
            "doc_entry": doc_entry,
            "doc_num": doc_num,
            "doc_date": "2026-09-14",
            "card_code": card_code,
            "card_name": "Sharma Traders",
            "items": lines,
        }

    def _patches(self, writer=None):
        writer = writer or self.writer
        return [
            mock.patch("sap_client.client.SAPClient", return_value=self.sap),
            mock.patch("sap_client.context.CompanyContext", return_value=object()),
            mock.patch(
                "sap_client.service_layer.returns_writer.ReturnsWriter",
                return_value=writer,
            ),
            mock.patch(
                "dispatch_plans.services.DispatchPlansService", FakeDispatchPlansService
            ),
        ]

    def run_service(self, call, writer=None):
        patches = self._patches(writer)
        for patch in patches:
            patch.start()
        try:
            return call()
        finally:
            for patch in reversed(patches):
                patch.stop()

    def create(self, lines, *, warehouse=WAREHOUSE, invoice="1500", remarks="", writer=None):
        return self.run_service(
            lambda: self.service.create_and_post(
                {
                    "invoice_number": invoice,
                    "warehouse_code": warehouse,
                    "lines": lines,
                    "remarks": remarks,
                },
                self.user,
            ),
            writer,
        )

    def lookup(self, invoice="1500"):
        return self.run_service(lambda: self.service.lookup_invoice(invoice))


class PostingTests(ShortDispatchTestCase):
    def test_posts_one_return_with_only_the_short_lines(self):
        entry = self.create(
            [
                {"source_line_num": 0, "short_quantity": 10, "reason": "SHORT"},
                {"source_line_num": 1, "short_quantity": 0},
            ]
        )

        self.assertEqual(len(self.writer.posted), 1)
        payload = self.writer.posted[0]
        self.assertEqual([line["ItemCode"] for line in payload["DocumentLines"]], ["FG0000151"])
        self.assertEqual(payload["DocumentLines"][0]["Quantity"], 10)
        self.assertEqual(entry.sap_return_doc_num, "179001")
        self.assertIsNotNone(entry.posted_at)
        self.assertEqual(entry.posted_by, self.user)

    def test_line_fields_come_from_the_invoice_not_the_request(self):
        entry = self.create(
            [
                {
                    "source_line_num": 0,
                    "short_quantity": 10,
                    # Nothing else on the request is trusted; the bill decides.
                    "item_code": "FG-TAMPERED",
                    "unit_price": 1,
                }
            ]
        )
        line = entry.lines.get()
        self.assertEqual(line.item_code, "FG0000151")
        self.assertEqual(line.unit_price, Decimal("250.0000"))
        self.assertEqual(line.invoice_quantity, Decimal("100.000"))
        self.assertEqual(line.source_warehouse_code, WAREHOUSE)

    def test_customer_and_invoice_are_snapshotted_from_the_bill(self):
        entry = self.create([{"source_line_num": 0, "short_quantity": 5}])
        self.assertEqual(entry.customer_code, "CUST001")
        self.assertEqual(entry.customer_name, "Sharma Traders")
        self.assertEqual(entry.sap_invoice_doc_entry, 5001)
        self.assertEqual(entry.sap_invoice_doc_num, "1500")

    def test_stock_returns_into_the_warehouse_the_form_chose(self):
        self.create([{"source_line_num": 0, "short_quantity": 5}], warehouse="BH-BT")
        line = self.writer.posted[0]["DocumentLines"][0]
        self.assertEqual(line["WarehouseCode"], "BH-BT")

    def test_customer_is_not_credited_by_the_return(self):
        """The stock comes back at zero price -- the credit note is finance's own
        separate document, and pricing it here would double it."""
        self.create([{"source_line_num": 0, "short_quantity": 5}])
        self.assertEqual(self.writer.posted[0]["DocumentLines"][0]["UnitPrice"], 0)

    def test_reference_names_the_entry_and_the_bill(self):
        entry = self.create([{"source_line_num": 0, "short_quantity": 5}])
        self.assertEqual(self.writer.posted[0]["NumAtCard"], f"{entry.entry_no} INV 1500")

    def test_place_of_supply_comes_from_the_invoice(self):
        self.create([{"source_line_num": 0, "short_quantity": 5}])
        payload = self.writer.posted[0]
        self.assertEqual(payload["ShipToCode"], "GGN")
        self.assertEqual(payload["PayToCode"], "GGN-BILL")
        self.assertEqual(payload["BPL_IDAssignedToInvoice"], 7)

    def test_an_already_posted_reference_is_not_posted_again(self):
        # The reference is unique per (entry, invoice), so a document carrying it
        # *is* this one -- and a return nobody can cancel must not be duplicated.
        self.sap.find_goods_return_by_reference = lambda card_code, num_at_card: {
            "doc_entry": 4242,
            "doc_num": "160042",
        }
        entry = self.create([{"source_line_num": 0, "short_quantity": 5}])
        self.assertEqual(self.writer.posted, [])
        self.assertEqual(entry.sap_return_doc_entry, 4242)
        self.assertEqual(entry.sap_return_doc_num, "160042")


class BatchTests(ShortDispatchTestCase):
    def test_billed_batch_is_recorded_and_a_fresh_one_is_posted(self):
        """SAP refuses a return into a batch that already exists, so the document
        mints its own -- and the batch physically on the floor survives only as the
        line's own field and its SAP free text."""
        self.sap.batch_allocations = [
            {
                "line_num": 0,
                "item_code": "FG0000151",
                "batch_number": "2609A",
                "warehouse": WAREHOUSE,
                "quantity": Decimal("100"),
                "direction": 1,
                "is_issue": True,
            }
        ]
        entry = self.create([{"source_line_num": 0, "short_quantity": 5}])

        line = entry.lines.get()
        self.assertEqual(line.original_batch_number, "2609A")

        posted = self.writer.posted[0]["DocumentLines"][0]
        self.assertEqual(posted["BatchNumbers"][0]["BatchNumber"], f"{entry.entry_no}-{line.pk}")
        self.assertEqual(posted["BatchNumbers"][0]["Quantity"], 5)
        self.assertIn("Billed batch 2609A", posted["FreeText"])

    def test_the_receipt_side_of_a_batch_row_is_ignored(self):
        """IBT1 carries both directions; only what the invoice issued is the batch
        the goods actually went out on."""
        self.sap.batch_allocations = [
            {"line_num": 0, "item_code": "FG0000151", "batch_number": "IN01", "is_issue": False},
            {"line_num": 0, "item_code": "FG0000151", "batch_number": "OUT01", "is_issue": True},
        ]
        entry = self.create([{"source_line_num": 0, "short_quantity": 5}])
        self.assertEqual(entry.lines.get().original_batch_number, "OUT01")

    def test_an_item_without_batch_management_posts_no_batch(self):
        self.sap.batch_flags = {"FG0000151": False}
        self.create([{"source_line_num": 0, "short_quantity": 5}])
        self.assertNotIn("BatchNumbers", self.writer.posted[0]["DocumentLines"][0])

    def test_an_unreadable_batch_flag_still_sends_a_batch(self):
        """Finished goods overwhelmingly are batch-managed; omitting the batch on
        one that needs it fails the whole document (-4014)."""
        self.sap.batch_managed_flags = mock.Mock(side_effect=RuntimeError("HANA down"))
        self.create([{"source_line_num": 0, "short_quantity": 5}])
        self.assertIn("BatchNumbers", self.writer.posted[0]["DocumentLines"][0])


class QuantityGuardTests(ShortDispatchTestCase):
    def test_nothing_short_is_refused(self):
        with self.assertRaisesMessage(ValueError, "at least one item"):
            self.create([{"source_line_num": 0, "short_quantity": 0}])
        self.assertFalse(ShortDispatch.objects.exists())

    def test_more_than_was_billed_is_refused(self):
        with self.assertRaisesMessage(ValueError, "billed for 100"):
            self.create([{"source_line_num": 0, "short_quantity": 101}])

    def test_a_line_not_on_the_invoice_is_refused(self):
        with self.assertRaisesMessage(ValueError, "is not on invoice 1500"):
            self.create([{"source_line_num": 9, "short_quantity": 1}])

    def test_one_item_on_two_lines_must_be_combined(self):
        self.set_bill([bill_line(0, "FG0000151", 60), bill_line(1, "FG0000151", 40)])
        with self.assertRaisesMessage(ValueError, "160020"):
            self.create(
                [
                    {"source_line_num": 0, "short_quantity": 5},
                    {"source_line_num": 1, "short_quantity": 5},
                ]
            )

    def test_a_second_entry_cannot_exceed_what_the_first_left(self):
        """Two short dispatches against one bill are allowed -- a second shortfall
        can be found later -- but between them they cannot return more than was
        billed, which SAP itself would not catch."""
        self.create([{"source_line_num": 0, "short_quantity": 90}])
        with self.assertRaisesMessage(ValueError, "at most 10"):
            self.create([{"source_line_num": 0, "short_quantity": 20}])

        entry = self.create([{"source_line_num": 0, "short_quantity": 10}])
        self.assertEqual(entry.lines.get().short_quantity, Decimal("10.000"))

    def test_a_cancelled_entrys_quantity_is_not_counted_against_the_bill(self):
        first = self.create([{"source_line_num": 0, "short_quantity": 90}])
        first.is_active = False
        first.save(update_fields=["is_active"])

        entry = self.create([{"source_line_num": 0, "short_quantity": 100}])
        self.assertEqual(entry.lines.get().short_quantity, Decimal("100.000"))


class RollbackTests(ShortDispatchTestCase):
    def test_a_sap_refusal_leaves_no_record(self):
        """There is no draft stage: either SAP took the document and the entry
        exists, or the operator still has the form in front of them."""
        with self.assertRaisesMessage(ValueError, "SAP rejected the return note"):
            self.create(
                [{"source_line_num": 0, "short_quantity": 5}],
                writer=FakeWriter(refuse=True),
            )
        self.assertFalse(ShortDispatch.objects.exists())
        self.assertFalse(ShortDispatchItem.objects.exists())

    def test_a_guard_refusal_leaves_no_record(self):
        self.sap.return_costs = lambda item_codes, warehouse: {
            code: Decimal("0") for code in item_codes
        }
        with self.assertRaisesMessage(ValueError, "160021"):
            self.create([{"source_line_num": 0, "short_quantity": 5}])
        self.assertFalse(ShortDispatch.objects.exists())

    def test_an_internal_branch_cannot_be_returned_to(self):
        self.sap.customer_group_code = lambda card_code: 100
        with self.assertRaisesMessage(ValueError, "160012"):
            self.create([{"source_line_num": 0, "short_quantity": 5}])
        self.assertFalse(ShortDispatch.objects.exists())


class InvoiceLookupTests(ShortDispatchTestCase):
    def test_lines_carry_what_is_left_to_return(self):
        self.create([{"source_line_num": 0, "short_quantity": 30}])
        payload = self.lookup()

        first = payload["lines"][0]
        self.assertEqual(first["already_short"], 30)
        self.assertEqual(first["remaining_quantity"], 70)
        self.assertEqual(payload["lines"][1]["remaining_quantity"], 40)

    def test_default_warehouse_is_where_most_of_the_bill_was_picked(self):
        self.set_bill(
            [
                bill_line(0, "FG0000151", 100, warehouse="BH-PC"),
                bill_line(1, "FG0000329", 40, warehouse="BH-BT"),
                bill_line(2, "FG0000032", 10, warehouse="BH-BT"),
            ]
        )
        self.assertEqual(self.lookup()["default_warehouse_code"], "BH-BT")

    def test_earlier_entries_against_the_bill_are_reported(self):
        entry = self.create([{"source_line_num": 0, "short_quantity": 5}])
        existing = self.lookup()["existing_entries"]
        self.assertEqual([row["entry_no"] for row in existing], [entry.entry_no])

    def test_an_unknown_invoice_is_a_business_error(self):
        with self.assertRaisesMessage(ValueError, "No SAP invoice found for 9999"):
            self.lookup("9999")

    def test_an_unreadable_batch_read_does_not_break_the_lookup(self):
        self.sap.posted_batch_allocations = mock.Mock(side_effect=RuntimeError("HANA down"))
        self.assertEqual(self.lookup()["lines"][0]["original_batch_number"], "")


class ScopeTests(ShortDispatchTestCase):
    def test_another_companys_entry_is_refused(self):
        entry = self.create([{"source_line_num": 0, "short_quantity": 5}])
        with self.assertRaises(PermissionDenied):
            self.service.get_entry(entry.id, [self.other_company.id])

    def test_the_list_is_scoped_to_the_callers_companies(self):
        self.create([{"source_line_num": 0, "short_quantity": 5}])
        self.assertEqual(self.service.list_entries(self.allowed).count(), 1)
        self.assertEqual(self.service.list_entries([self.other_company.id]).count(), 0)

    def test_search_finds_an_entry_by_the_bill_it_corrected(self):
        self.create([{"source_line_num": 0, "short_quantity": 5}])
        self.assertEqual(self.service.list_entries(self.allowed, search="1500").count(), 1)
        self.assertEqual(self.service.list_entries(self.allowed, search="1501").count(), 0)


class PrintTests(ShortDispatchTestCase):
    def test_print_reads_the_posted_document_from_sap(self):
        entry = self.create([{"source_line_num": 0, "short_quantity": 5}])
        self.sap.print_payloads = {entry.sap_return_doc_entry: {"doc_num": "170001", "lines": []}}

        payload = self.run_service(lambda: self.service.print_payload(entry.id, self.allowed))
        self.assertEqual(payload["entry_no"], entry.entry_no)
        self.assertEqual(payload["invoice_doc_num"], "1500")

    def test_print_refuses_an_entry_sap_does_not_hold(self):
        entry = self.create([{"source_line_num": 0, "short_quantity": 5}])
        with self.assertRaisesMessage(ValueError, "SAP has no return"):
            self.run_service(lambda: self.service.print_payload(entry.id, self.allowed))
