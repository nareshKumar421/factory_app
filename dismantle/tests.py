"""Dismantling a finished good: three SAP documents, in one order only.

What these pin is the shape of the SAP conversation, because that is what was
established by reading the live data and the validation procedure rather than by
guessing, and it is what a later change is most likely to break:

* the recipe is exploded PER PIECE (``ITT1."Quantity" / OITT."Qauntity"``)
* the Receipt from Production comes BEFORE the Goods Issue -- SAP refuses the
  other way round with error 20206
* the receipt's lines carry ``BaseType 202`` / ``BaseEntry`` / ``BaseLine``, and
  the issue's line carries no ``BaseLine`` at all
* every goods-issue line carries a Variety (error 60003)
* a run SAP stops half-way keeps what it accepted and finishes on a retry

No SAP and no HANA: ``SAPClient`` is patched at the seam ``services._client``
imports it from.
"""

from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from company.models import Company
from goods_return.models import (
    GoodsReturn,
    GoodsReturnInvoiceRef,
    GoodsReturnItem,
    GoodsReturnStatus,
)

from .guards import DismantleGuardError
from .models import Dismantle, DismantleSource, DismantleStatus
from .services import DismantleService

WAREHOUSE = "BH-GR"
PARENT = "FG0000005"

# One 16-piece carton of olive oil, as SAP states it: ITT1 quantities are written
# for a box of 16, so per piece is 1 bottle / 1 cap / 2 labels and 1/16 carton.
BOM = [
    {
        "item_code": "RM0000001",
        "item_name": "LOOSE REFINED OLIVE OIL",
        "qty_per_piece": 1.0,
        "uom": "LTR",
        "is_batch_managed": True,
        "item_group": 106,
        "bom_warehouse": "BH-PC",
    },
    {
        "item_code": "PM0000121",
        "item_name": "PET BOTTLE 1 LTR",
        "qty_per_piece": 1.0,
        "uom": "PCS",
        "is_batch_managed": False,
        "item_group": 105,
        "bom_warehouse": "BH-PC",
    },
    {
        "item_code": "PM0000003",
        "item_name": "CARTON 1 LTR 16 PCS",
        "qty_per_piece": 0.0625,
        "uom": "PCS",
        "is_batch_managed": False,
        "item_group": 105,
        "bom_warehouse": "BH-PC",
    },
]


class _Warehouse:
    """Stands in for ``sap_client.dtos.WarehouseDTO`` (code + name)."""

    def __init__(self, warehouse_code, warehouse_name):
        self.warehouse_code = warehouse_code
        self.warehouse_name = warehouse_name


class FakeSAPClient:
    """Answers the reads a dismantle makes and records the writes it does."""

    def __init__(self, company_code=None, **kwargs):
        self.company_code = company_code
        self.calls = []
        self.payloads = {}
        self.refuse = set()
        self.variety = {PARENT: "OLIVE"}
        self.batch_stock = [
            {"batch_number": "GR-20260907-0003-0", "quantity": Decimal("64")}
        ]
        self.known_batches = set()
        # What SAP actually holds under a goods return's batches. Set per test:
        # the numbering formula changed, so this is the only true answer.
        self.goods_return_batches = []
        self.bom_batch_size = 16.0
        self.pieces_per_box = 16.0

    # -- reads ------------------------------------------------------------
    def dismantle_parent_info(self, item_code):
        return {
            "item_code": item_code,
            "item_name": "EXTRA LIGHT OLIVE 1 LTR 16 PCS",
            "uom": "PCS",
            "is_batch_managed": True,
            "pieces_per_box": self.pieces_per_box,
            "bom_batch_size": self.bom_batch_size,
            "is_inventory_item": True,
        }

    def dismantle_bom(self, item_code):
        return [dict(row) for row in BOM]

    def dismantlable_stock(self, warehouse_code, **kwargs):
        return [
            {
                "item_code": PARENT,
                "item_name": "EXTRA LIGHT OLIVE 1 LTR 16 PCS",
                "on_hand": 64.0,
                "uom": "PCS",
                "is_batch_managed": True,
                "pieces_per_box": 16.0,
                "bom_batch_size": 16.0,
            }
        ]

    def available_batches(self, item_code, warehouse):
        # Both this and `return_batches` read OIBT in the real client, so the
        # fake must answer from one store or a test can set up stock that only
        # half of the code can see.
        rows = [dict(batch) for batch in self.batch_stock]
        rows += [
            {
                "batch_number": batch["batch_number"],
                "quantity": Decimal(str(batch["quantity"])),
            }
            for batch in self.goods_return_batches
            if batch["item_code"] == item_code and batch["warehouse_code"] == warehouse
        ]
        return rows

    def return_variety_codes(self, item_codes):
        return {code: self.variety[code] for code in item_codes if code in self.variety}

    def get_return_warehouses(self):
        return [_Warehouse("BH-GR", "Goods Return")]

    def get_active_warehouses(self):
        return [
            _Warehouse("BH-PF", "Production Floor"),
            _Warehouse("BH-GR", "Goods Return"),
            _Warehouse("BH-FG", "Finished Goods"),
        ]

    def return_batches(self, warehouse_codes, **kwargs):
        return list(self.goods_return_batches)

    def existing_batches(self, item_codes, batch_numbers):
        return {
            (item, batch)
            for item in item_codes
            for batch in batch_numbers
            if (item, batch) in self.known_batches
        }

    # -- writes -----------------------------------------------------------
    def _post(self, name, payload, doc_entry, doc_num):
        self.calls.append(name)
        self.payloads[name] = payload
        if name in self.refuse:
            raise RuntimeError(f"SAP refused the {name}")
        return {"DocEntry": doc_entry, "DocNum": doc_num}

    def create_disassembly_order(self, payload):
        return self._post("order", payload, 13327, "826202891")

    def disassembly_order_lines(self, doc_entry):
        self.calls.append("order_lines")
        # SAP renumbers: the app sent three lines, SAP hands back its own
        # LineNumbers, and the receipt has to use these rather than 0,1,2.
        return [
            {"ItemNo": "RM0000001", "LineNumber": 5},
            {"ItemNo": "PM0000121", "LineNumber": 6},
            {"ItemNo": "PM0000003", "LineNumber": 7},
        ]

    def create_production_receipt(self, payload):
        return self._post("receipt", payload, 12560, "826596892")

    def create_production_issue(self, payload):
        return self._post("issue", payload, 12885, "826606892")

    def close_production_order(self, doc_entry):
        self.calls.append("close")


class DismantleTestCase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="OIL")
        self.user = get_user_model().objects.create(
            email="store@example.com", full_name="Store Keeper"
        )
        self.service = DismantleService(self.company)
        self.allowed = [self.company.id]
        self.sap = FakeSAPClient()

    def patched(self):
        return mock.patch("sap_client.client.SAPClient", return_value=self.sap)

    def create_from_stock(self, quantity=16, batch="GR-20260907-0003-0"):
        with self.patched():
            return self.service.create(
                {
                    "source": DismantleSource.STOCK,
                    "warehouse_code": WAREHOUSE,
                    "item_code": PARENT,
                    "batch_number": batch,
                    "quantity": Decimal(quantity),
                },
                self.user,
            )

    def build_posted_return(self, *, item=PARENT, quantity=16):
        gr = GoodsReturn.objects.create(
            company=self.company,
            entry_no=GoodsReturn.generate_entry_no(),
            basis="INVOICE",
            status=GoodsReturnStatus.POSTED,
            customer_code="CUST001",
            customer_name="Sharma Traders",
            sap_return_warehouse=WAREHOUSE,
        )
        ref = GoodsReturnInvoiceRef.objects.create(
            goods_return=gr,
            sap_invoice_doc_entry=5001,
            sap_invoice_doc_num="1500",
            sap_gr_doc_entry=7001,
            sap_gr_doc_num="1600",
        )
        line = GoodsReturnItem.objects.create(
            goods_return=gr,
            invoice_ref=ref,
            source_line_num=0,
            item_code=item,
            item_name="EXTRA LIGHT OLIVE 1 LTR 16 PCS",
            uom="PCS",
            invoice_quantity=1000,
            return_quantity=quantity,
        )
        return gr, line

    def stock_the_batch(self, record, quantity="64"):
        """Put the record's own batch in the fake warehouse.

        A return-sourced dismantle mints its batch from the entry number, which
        carries today's date, so the batch it consumes cannot be written into the
        fixture up front — it has to be stocked once the record exists.
        """
        self.sap.batch_stock = [
            {"batch_number": record.batch_number, "quantity": Decimal(quantity)}
        ]

    def post(self, record):
        with self.patched():
            return self.service.post(record.id, self.user, self.allowed)


class ClientContractTests(DismantleTestCase):
    """The fake must not answer calls the real ``SAPClient`` cannot.

    A fake that obligingly answers anything lets a service call a method the real
    client does not have: every test passes and the endpoint 500s in the browser.
    That is exactly what ``get_return_warehouses`` did — it existed on the HANA
    reader but not on the facade.
    """

    def test_the_fake_answers_nothing_the_real_client_lacks(self):
        from sap_client.client import SAPClient

        faked = {name for name in vars(FakeSAPClient) if not name.startswith("_")}
        self.assertEqual(sorted(n for n in faked if not hasattr(SAPClient, n)), [])


class WarehouseTests(DismantleTestCase):
    def test_the_picker_offers_every_warehouse_returns_first(self):
        with self.patched():
            rows = self.service.warehouses()

        # Returns warehouse first, because that is the common case -- but BH-PF
        # and BH-FG have to be reachable: live SAP dismantles out of those too.
        self.assertEqual(rows[0]["warehouse_code"], "BH-GR")
        self.assertTrue(rows[0]["is_return_warehouse"])
        self.assertEqual(
            {row["warehouse_code"] for row in rows}, {"BH-GR", "BH-PF", "BH-FG"}
        )


class BasketTests(DismantleTestCase):
    """Several items picked in one session become several records."""

    def test_a_basket_becomes_one_record_per_item(self):
        with self.patched():
            records = self.service.create_many(
                [
                    {
                        "source": DismantleSource.STOCK,
                        "warehouse_code": WAREHOUSE,
                        "item_code": PARENT,
                        "batch_number": "GR-20260907-0003-0",
                        "quantity": Decimal("16"),
                    },
                    {
                        "source": DismantleSource.STOCK,
                        "warehouse_code": WAREHOUSE,
                        "item_code": "FG0000021",
                        "batch_number": "GR-20260907-0003-1",
                        "quantity": Decimal("12"),
                    },
                ],
                self.user,
            )

        # SAP has no multi-item disassembly: an order names one parent item.
        self.assertEqual(len(records), 2)
        self.assertEqual(len({r.entry_no for r in records}), 2)
        self.assertEqual(Dismantle.objects.count(), 2)

    def test_a_refused_row_writes_none_of_the_basket(self):
        self.sap.dismantle_bom = lambda item_code: [] if item_code == "FG0000021" else BOM

        with self.assertRaises(DismantleGuardError), self.patched():
            self.service.create_many(
                [
                    {
                        "source": DismantleSource.STOCK,
                        "warehouse_code": WAREHOUSE,
                        "item_code": PARENT,
                        "batch_number": "GR-20260907-0003-0",
                        "quantity": Decimal("16"),
                    },
                    {
                        "source": DismantleSource.STOCK,
                        "warehouse_code": WAREHOUSE,
                        "item_code": "FG0000021",
                        "batch_number": "GR-20260907-0003-1",
                        "quantity": Decimal("12"),
                    },
                ],
                self.user,
            )

        # All or nothing — the first row must not survive the second's refusal,
        # or the operator has to work out which half of the basket landed.
        self.assertEqual(Dismantle.objects.count(), 0)

    def test_an_empty_basket_is_refused(self):
        with self.assertRaises(ValueError):
            self.service.create_many([], self.user)


class DeleteDraftTests(DismantleTestCase):
    def test_a_deleted_draft_leaves_the_list_but_stays_on_file(self):
        record = self.create_from_stock()
        self.service.delete_draft(record.id, self.user, self.allowed)

        self.assertEqual(
            self.service.list_dismantles([self.company.id]).count(), 0
        )
        # Soft: the row is still there, deactivated and marked cancelled.
        record.refresh_from_db()
        self.assertFalse(record.is_active)
        self.assertEqual(record.status, DismantleStatus.CANCELLED)

    def test_deleting_a_draft_gives_its_quantity_back_to_the_returned_line(self):
        gr, line = self.build_posted_return(quantity=16)
        with self.patched():
            record = self.service.create(
                {
                    "source": DismantleSource.GOODS_RETURN,
                    "goods_return_item_id": line.pk,
                    "quantity": Decimal("16"),
                },
                self.user,
            )
        with self.patched():
            self.assertEqual(self.service.returned_lines([self.company.id]), [])

        self.service.delete_draft(record.id, self.user, self.allowed)

        with self.patched():
            rows = self.service.returned_lines([self.company.id])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["remaining_quantity"], Decimal("16"))


class BomExplosionTests(DismantleTestCase):
    def test_components_are_exploded_per_piece(self):
        record = self.create_from_stock(quantity=16)

        by_item = {c.item_code: c for c in record.components.all()}
        self.assertEqual(by_item["RM0000001"].qty_per_piece, Decimal("1"))
        # 16 pieces yields 16 litres, 16 bottles and exactly one carton.
        self.assertEqual(by_item["RM0000001"].quantity, Decimal("16"))
        self.assertEqual(by_item["PM0000121"].quantity, Decimal("16"))
        self.assertEqual(by_item["PM0000003"].quantity, Decimal("1"))

    def test_components_default_into_the_dismantle_warehouse_not_the_bom_one(self):
        record = self.create_from_stock()
        # The BOM says BH-PC (the production store). Material recovered off a
        # return belongs in the returns warehouse until someone passes it.
        self.assertEqual(
            {c.warehouse_code for c in record.components.all()}, {WAREHOUSE}
        )

    def test_batch_managed_components_get_a_minted_batch(self):
        record = self.create_from_stock()
        loose_oil = record.components.get(item_code="RM0000001")
        bottle = record.components.get(item_code="PM0000121")

        self.assertTrue(loose_oil.batch_number.startswith(record.entry_no))
        # Not batch-managed in SAP, so it must not carry one.
        self.assertEqual(bottle.batch_number, "")

    def test_changing_the_quantity_rescales_the_recipe(self):
        record = self.create_from_stock(quantity=16)
        with self.patched():
            record = self.service.update_header(
                record.id, {"quantity": Decimal("32")}, self.user, self.allowed
            )
        self.assertEqual(
            record.components.get(item_code="PM0000121").quantity, Decimal("32")
        )

    def test_an_item_with_no_bom_is_refused_in_words(self):
        self.sap.dismantle_bom = lambda item_code: []
        with self.assertRaises(DismantleGuardError) as ctx, self.patched():
            self.service.create(
                {
                    "source": DismantleSource.STOCK,
                    "warehouse_code": WAREHOUSE,
                    "item_code": PARENT,
                    "quantity": Decimal("1"),
                },
                self.user,
            )
        self.assertIn("no production BOM", str(ctx.exception))

    def test_a_recipe_written_against_the_wrong_batch_size_is_flagged(self):
        # 24 live Oil recipes are written per box against a batch size of 1.
        self.sap.bom_batch_size = 1.0
        record = self.create_from_stock()
        self.assertTrue(record.bom_inflated)


class PostingOrderTests(DismantleTestCase):
    def test_the_three_documents_post_in_sap_s_order(self):
        record = self.post(self.create_from_stock())

        # Receipt BEFORE issue. Reversed, SAP refuses with 20206.
        self.assertEqual(
            [c for c in self.sap.calls if c in ("order", "receipt", "issue")],
            ["order", "receipt", "issue"],
        )
        self.assertEqual(record.status, DismantleStatus.POSTED)
        self.assertEqual(record.sap_order_doc_num, "826202891")
        self.assertEqual(record.sap_receipt_doc_num, "826596892")
        self.assertEqual(record.sap_issue_doc_num, "826606892")
        self.assertTrue(record.order_closed)

    def test_receipt_lines_reference_the_order_by_sap_s_own_line_numbers(self):
        self.post(self.create_from_stock())

        lines = {
            line["ItemCode"]: line
            for line in self.sap.payloads["receipt"]["DocumentLines"]
        }
        self.assertEqual(lines["RM0000001"]["BaseType"], 202)
        self.assertEqual(lines["RM0000001"]["BaseEntry"], 13327)
        # SAP's LineNumber (5), not the app's position (0).
        self.assertEqual(lines["RM0000001"]["BaseLine"], 5)
        self.assertEqual(lines["PM0000003"]["BaseLine"], 7)

    def test_the_issue_consumes_the_parent_with_no_base_line(self):
        record = self.post(self.create_from_stock())

        lines = self.sap.payloads["issue"]["DocumentLines"]
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertEqual(line["ItemCode"], PARENT)
        self.assertEqual(line["Quantity"], 16.0)
        self.assertEqual(line["BaseEntry"], 13327)
        # The parent is the order's header, not one of its component lines.
        self.assertNotIn("BaseLine", line)
        self.assertEqual(
            line["BatchNumbers"], [{"BatchNumber": record.batch_number, "Quantity": 16.0}]
        )

    def test_every_issue_line_carries_a_variety(self):
        self.post(self.create_from_stock())
        line = self.sap.payloads["issue"]["DocumentLines"][0]
        # Error 60003 "Please select Variety" — unconditional on a goods issue.
        self.assertEqual(line["CostingCode"], "OLIVE")

    def test_the_variety_rides_on_all_three_documents(self):
        self.post(self.create_from_stock())

        # Same Dimension-1 profit centre, spelled `DistributionRule` on a
        # production-order line and `CostingCode` on a document line.
        self.assertTrue(
            all(
                line["DistributionRule"] == "OLIVE"
                for line in self.sap.payloads["order"]["ProductionOrderLines"]
            )
        )
        self.assertTrue(
            all(
                line["CostingCode"] == "OLIVE"
                for line in self.sap.payloads["receipt"]["DocumentLines"]
            )
        )

    def test_an_item_with_no_variety_is_refused_before_anything_is_written(self):
        self.sap.variety = {}
        record = self.create_from_stock()
        with self.assertRaises(ValueError) as ctx, self.patched():
            self.service.post(record.id, self.user, self.allowed)

        self.assertIn("Variety", str(ctx.exception))
        self.assertEqual(self.sap.calls, [])
        record.refresh_from_db()
        self.assertIsNone(record.sap_order_doc_entry)

    def test_a_component_that_did_not_survive_is_left_off_both_documents(self):
        record = self.create_from_stock()
        carton = record.components.get(item_code="PM0000003")
        with self.patched():
            self.service.save_components(
                record.id,
                [{"id": carton.pk, "recovered": False}],
                self.user,
                self.allowed,
            )
        record = self.post(Dismantle.objects.get(pk=record.pk))

        ordered = {
            line["ItemNo"] for line in self.sap.payloads["order"]["ProductionOrderLines"]
        }
        received = {
            line["ItemCode"] for line in self.sap.payloads["receipt"]["DocumentLines"]
        }
        self.assertNotIn("PM0000003", ordered)
        self.assertNotIn("PM0000003", received)
        self.assertEqual(record.status, DismantleStatus.POSTED)


class HalfPostedTests(DismantleTestCase):
    def test_a_refused_issue_keeps_the_documents_sap_accepted(self):
        self.sap.refuse = {"issue"}
        record = self.post(self.create_from_stock())

        self.assertEqual(record.status, DismantleStatus.PARTIALLY_POSTED)
        self.assertEqual(record.sap_order_doc_num, "826202891")
        self.assertEqual(record.sap_receipt_doc_num, "826596892")
        self.assertIsNone(record.sap_issue_doc_entry)
        self.assertIn("issue", record.sap_post_error)

    def test_a_retry_resumes_at_the_document_sap_does_not_have(self):
        self.sap.refuse = {"issue"}
        record = self.post(self.create_from_stock())

        self.sap.refuse = set()
        self.sap.calls = []
        record = self.post(record)

        # The order and the receipt are already in SAP; only the issue is written.
        self.assertEqual(
            [c for c in self.sap.calls if c in ("order", "receipt", "issue")], ["issue"]
        )
        self.assertEqual(record.status, DismantleStatus.POSTED)

    def test_a_refused_order_leaves_nothing_behind_and_raises(self):
        self.sap.refuse = {"order"}
        record = self.create_from_stock()
        with self.assertRaises(ValueError), self.patched():
            self.service.post(record.id, self.user, self.allowed)

        record.refresh_from_db()
        self.assertIsNone(record.sap_receipt_doc_entry)
        self.assertEqual(record.status, DismantleStatus.DRAFT)

    def test_a_dismantle_in_sap_cannot_be_deleted_here(self):
        record = self.post(self.create_from_stock())
        with self.assertRaises(ValueError) as ctx:
            self.service.delete_draft(record.id, self.user, self.allowed)
        self.assertIn("cannot be deleted", str(ctx.exception))

    def test_the_order_failing_to_close_still_counts_as_posted(self):
        def refuse_close(doc_entry):
            raise RuntimeError("-2028 order is not closable")

        self.sap.close_production_order = refuse_close
        record = self.post(self.create_from_stock())

        self.assertEqual(record.status, DismantleStatus.POSTED)
        self.assertFalse(record.order_closed)
        self.assertIn("would not close", record.sap_post_error)


class BatchTests(DismantleTestCase):
    def test_a_received_batch_that_already_exists_is_refused(self):
        record = self.create_from_stock()
        loose_oil = record.components.get(item_code="RM0000001")
        self.sap.known_batches = {("RM0000001", loose_oil.batch_number)}

        with self.assertRaises(ValueError) as ctx, self.patched():
            self.service.post(record.id, self.user, self.allowed)

        # Error 590001 "Duplicate Batch not Allowed, Batch No Must be Unique".
        self.assertIn("already exists", str(ctx.exception))
        self.assertEqual(self.sap.calls, [])

    def test_more_than_the_batch_holds_cannot_be_dismantled(self):
        self.sap.batch_stock = [
            {"batch_number": "GR-20260907-0003-0", "quantity": Decimal("4")}
        ]
        record = self.create_from_stock(quantity=16)
        with self.assertRaises(ValueError) as ctx, self.patched():
            self.service.post(record.id, self.user, self.allowed)
        self.assertIn("cannot be dismantled", str(ctx.exception))


class ReturnSourceTests(DismantleTestCase):
    def stock_the_return(self, gr, line, batch=None, quantity="16"):
        """Put the return's stock in SAP under `batch` (its real number)."""
        self.sap.goods_return_batches = [
            {
                "item_code": line.item_code,
                "batch_number": batch or f"{gr.entry_no}-{line.pk}".upper(),
                "warehouse_code": gr.sap_return_warehouse,
                "quantity": float(quantity),
            }
        ]

    def test_a_returned_line_brings_its_own_batch_and_warehouse(self):
        gr, line = self.build_posted_return()
        self.stock_the_return(gr, line)
        with self.patched():
            record = self.service.create(
                {
                    "source": DismantleSource.GOODS_RETURN,
                    "goods_return_item_id": line.pk,
                    "quantity": Decimal("16"),
                },
                self.user,
            )

        self.assertEqual(record.warehouse_code, WAREHOUSE)
        self.assertEqual(record.goods_return_id, gr.pk)
        # The batch the returns module minted when it posted -- nothing re-keyed.
        self.assertEqual(record.batch_number, f"{gr.entry_no}-{line.pk}".upper())

    def test_a_batch_numbered_the_old_way_is_found_not_recomputed(self):
        """Returns booked before the numbering fix carry a POSITION, not an id.

        `goods_return` used to mint `<entry>-0` for the first line and now mints
        `<entry>-<line id>`. Recomputing the name would invent a batch that does
        not exist and refuse the dismantle with "SAP holds 0 of this batch" —
        which is exactly what happened on the floor with GR-20260827-0001.
        """
        gr, line = self.build_posted_return(quantity=100)
        self.stock_the_return(gr, line, batch=f"{gr.entry_no}-0", quantity="100")

        with self.patched():
            record = self.service.create(
                {
                    "source": DismantleSource.GOODS_RETURN,
                    "goods_return_item_id": line.pk,
                    "quantity": Decimal("70"),
                },
                self.user,
            )

        self.assertEqual(record.batch_number, f"{gr.entry_no}-0")
        self.assertNotEqual(record.batch_number, f"{gr.entry_no}-{line.pk}")

    def test_a_draft_stuck_on_a_batch_that_does_not_exist_repairs_itself(self):
        """Drafts raised before the batch was read back are not left stranded.

        They carry a recomputed name that SAP has never heard of, so they sit on
        "SAP holds 0 of this batch" for ever. Checking or posting resolves the
        batch afresh rather than making the operator delete and start again.
        """
        gr, line = self.build_posted_return(quantity=100)
        with self.patched():
            record = self.service.create(
                {
                    "source": DismantleSource.GOODS_RETURN,
                    "goods_return_item_id": line.pk,
                    "quantity": Decimal("70"),
                },
                self.user,
            )
        # Nothing was in SAP when it was created, so it kept the computed name.
        self.assertEqual(record.batch_number, f"{gr.entry_no}-{line.pk}".upper())

        # SAP has it under the old, position-based number all along.
        self.stock_the_return(gr, line, batch=f"{gr.entry_no}-0", quantity="100")
        with self.patched():
            result = self.service.preview(record.id, self.allowed)

        record.refresh_from_db()
        self.assertEqual(record.batch_number, f"{gr.entry_no}-0")
        self.assertEqual(result["errors"], [])
        self.assertTrue(result["can_post"])

    def test_a_consumed_batch_is_never_switched_underneath_a_posted_issue(self):
        gr, line = self.build_posted_return(quantity=100)
        self.stock_the_return(gr, line, quantity="100")
        with self.patched():
            record = self.service.create(
                {
                    "source": DismantleSource.GOODS_RETURN,
                    "goods_return_item_id": line.pk,
                    "quantity": Decimal("70"),
                },
                self.user,
            )
            record = self.service.post(record.id, self.user, self.allowed)
        posted_batch = record.batch_number

        # The batch is spent, so SAP no longer reports stock under it. The record
        # must keep naming the batch that was actually consumed.
        self.sap.goods_return_batches = [
            {
                "item_code": line.item_code,
                "batch_number": f"{gr.entry_no}-0",
                "warehouse_code": gr.sap_return_warehouse,
                "quantity": 30.0,
            }
        ]
        with self.patched():
            self.service.preview(record.id, self.allowed)

        record.refresh_from_db()
        self.assertEqual(record.batch_number, posted_batch)

    def test_the_picker_shows_what_sap_holds_beside_what_the_app_expects(self):
        gr, line = self.build_posted_return(quantity=100)
        self.stock_the_return(gr, line, batch=f"{gr.entry_no}-0", quantity="60")

        with self.patched():
            rows = self.service.returned_lines([self.company.id])

        self.assertEqual(rows[0]["batch_number"], f"{gr.entry_no}-0")
        self.assertEqual(rows[0]["remaining_quantity"], Decimal("100"))
        # 60 in SAP against 100 the app still expects: somebody moved the stock
        # outside the app, and the operator sees it before posting rather than
        # after a refusal.
        self.assertEqual(rows[0]["sap_quantity"], Decimal("60"))

    def test_an_unposted_return_has_no_stock_to_take_apart(self):
        gr, line = self.build_posted_return()
        gr.status = GoodsReturnStatus.ARRIVED
        gr.save(update_fields=["status"])

        with self.assertRaises(ValueError) as ctx, self.patched():
            self.service.create(
                {
                    "source": DismantleSource.GOODS_RETURN,
                    "goods_return_item_id": line.pk,
                    "quantity": Decimal("1"),
                },
                self.user,
            )
        self.assertIn("not in SAP yet", str(ctx.exception))

    def test_the_offered_quantity_is_what_is_left_of_the_line(self):
        gr, line = self.build_posted_return(quantity=16)
        with self.patched():
            self.service.create(
                {
                    "source": DismantleSource.GOODS_RETURN,
                    "goods_return_item_id": line.pk,
                    "quantity": Decimal("10"),
                },
                self.user,
            )

        with self.patched():
            rows = self.service.returned_lines([self.company.id])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["remaining_quantity"], Decimal("6"))

    def test_a_line_dismantled_in_full_drops_off_the_list(self):
        gr, line = self.build_posted_return(quantity=16)
        with self.patched():
            self.service.create(
                {
                    "source": DismantleSource.GOODS_RETURN,
                    "goods_return_item_id": line.pk,
                    "quantity": Decimal("16"),
                },
                self.user,
            )
        with self.patched():
            self.assertEqual(self.service.returned_lines([self.company.id]), [])

    def test_more_than_the_line_returned_is_refused(self):
        gr, line = self.build_posted_return(quantity=4)
        with self.assertRaises(ValueError), self.patched():
            self.service.create(
                {
                    "source": DismantleSource.GOODS_RETURN,
                    "goods_return_item_id": line.pk,
                    "quantity": Decimal("16"),
                },
                self.user,
            )

    def test_the_sap_documents_name_the_return_they_came_from(self):
        gr, line = self.build_posted_return()
        with self.patched():
            record = self.service.create(
                {
                    "source": DismantleSource.GOODS_RETURN,
                    "goods_return_item_id": line.pk,
                    "quantity": Decimal("16"),
                },
                self.user,
            )
        self.stock_the_batch(record)
        self.post(record)

        # SAP keeps no link of its own from a disassembly order to the return, so
        # the comment is the only place it is visible inside SAP.
        comment = self.sap.payloads["receipt"]["Comments"]
        self.assertIn(record.entry_no, comment)
        self.assertIn(gr.entry_no, comment)
