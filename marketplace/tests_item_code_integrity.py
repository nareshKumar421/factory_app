"""Item-code integrity: a delivery note must keep saying what it shipped.

Three independent defects let DN 1507264745 (posted 31 Jul 2026 with FG0000422
and FG0000390) re-export as FG0000032 and FG0000005 after a combo was edited on
11 Aug:

  1. the CSV export re-resolved item codes against today's masters;
  2. nothing checked a code against the SAP item master, so cb005 was saved with
     FG0000005 labelled as a 3 LTR tin (it is a 1 LTR pack);
  3. saving a combo deleted and rebuilt its components, cascading away every
     alternative — CB0030 lost its default option FG0000422 that way.

SAP is mocked throughout; nothing here touches HANA or production.
"""
import csv
import io
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from company.models import Company

from .models import (
    ComboComponent, ComboComponentOption, ComboComponentType, ComboDefinition,
    MarketplaceChannel, MarketplaceDispatch, MarketplaceDispatchStatus,
    MarketplaceOrder, MarketplaceOrderLine, MarketplaceSapPostStatus,
    MarketplaceWarehouse, OrderImportBatch, SkuMapping, SkuType,
)

# The real items involved, with their true SAP names.
NIRMAL_1L = "FG0000422"      # COLD PRESS 1 LTR (NIRMAL RISHI) 20PCS  — posted
OLIVE_3L = "FG0000390"       # EXTRA LIGHT OLIVE 3 LTR TIN 2 PCS      — posted
PLAIN_1L = "FG0000032"       # COLD PRESS 1 LTR 20 PCS                — edited to
OLIVE_1L = "FG0000005"       # EXTRA LIGHT OLIVE 1 LTR 16 PCS         — edited to

SAP_ITEM_MASTER = {
    NIRMAL_1L: "COLD PRESS 1 LTR (NIRMAL RISHI) 20PCS",
    OLIVE_3L: "EXTRA LIGHT OLIVE 3 LTR TIN 2 PCS",
    PLAIN_1L: "COLD PRESS 1 LTR 20 PCS",
    OLIVE_1L: "EXTRA LIGHT OLIVE 1 LTR 16 PCS",
}


def sap_master(codes):
    """Stand-in for the SAP item master."""
    return {c: SAP_ITEM_MASTER[c] for c in codes if c in SAP_ITEM_MASTER}


class ItemCodeIntegrityBase(TestCase):
    channel = MarketplaceChannel.FLIPKART

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        self.user = get_user_model().objects.create(
            email="dn@t.com", full_name="Ops", employee_code="DN1", is_active=True)
        MarketplaceWarehouse.objects.create(
            company=self.company, channel=self.channel, name="DL-EC",
            sap_warehouse_code="DL-EC", sap_customer_card_code="CUSTA000910",
            sap_branch_id=1, is_default=True,
        )
        self.batch = OrderImportBatch.objects.create(
            company=self.company, channel=self.channel, filename="dn.csv")

    def _combo(self, code="CB0030", first=NIRMAL_1L, second=OLIVE_3L):
        combo = ComboDefinition.objects.create(
            company=self.company, channel=self.channel, code=code, name="Combo pack")
        for item in (first, second):
            ComboComponent.objects.create(
                combo=combo, component_type=ComboComponentType.FG,
                item_code=item, item_name=SAP_ITEM_MASTER[item],
                quantity=Decimal("1"), uom="PCS",
            )
        return combo

    def _order(self, combo, order_id="OD-DN-1", fsn="FSNCOMBO", sku="SKU-COMBO"):
        SkuMapping.objects.create(
            company=self.company, channel=self.channel, fsn=fsn,
            marketplace_sku=sku, sku_name="Combo pack",
            sku_type=SkuType.COMBO, combo=combo, default_uom="PCS",
        )
        order = MarketplaceOrder.objects.create(
            company=self.company, channel=self.channel, order_id=order_id,
            import_batch=self.batch, buyer_name="Buyer", city="Delhi", state="DL")
        MarketplaceOrderLine.objects.create(
            order=order, marketplace_sku=sku, sku_name="Combo pack",
            fsn=fsn, ordered_quantity=1, tracking_id="TRK-1", invoice_amount=Decimal("100"),
        )
        return order

    def _post_dn(self, order, doc_entry=76001, doc_num="1507264745"):
        """Cut the note the way the real flow does, so the snapshot is written by
        the production code path rather than the test."""
        from .services import delivery_note_service as dns
        from .services.resolve_service import load_mappings

        dispatch = MarketplaceDispatch.objects.create(
            company=self.company, channel=self.channel, order=order,
            status=MarketplaceDispatchStatus.CONFIRMED,
            sap_post_status=MarketplaceSapPostStatus.PENDING,
        )
        mappings = load_mappings(self.company, self.channel)
        includable = [{
            "dispatch": dispatch,
            "amount": Decimal("100"),
            "posted_lines": dns._posted_lines_snapshot(order, "DL-EC", mappings),
        }]
        dns._finalize_posted(includable, self.company, doc_entry, doc_num, self.user)
        dispatch.refresh_from_db()
        return dispatch

    def _export_rows(self, doc_entry=76001):
        from .services.delivery_note_service import export_posted_delivery_note_csv

        _name, text = export_posted_delivery_note_csv(self.company, doc_entry)
        return list(csv.DictReader(io.StringIO(text)))


class PostedSnapshotTests(ItemCodeIntegrityBase):
    """Defect 1 — the export re-resolved against today's masters."""

    def test_editing_the_combo_does_not_rewrite_a_posted_delivery_note(self):
        """The real case: DN 1507264745 must still read FG0000422 / FG0000390."""
        combo = self._combo()
        order = self._order(combo)
        self._post_dn(order)

        # 11 Aug: the combo is edited to the wrong items.
        combo.components.filter(item_code=NIRMAL_1L).update(
            item_code=PLAIN_1L, item_name=SAP_ITEM_MASTER[PLAIN_1L])
        combo.components.filter(item_code=OLIVE_3L).update(
            item_code=OLIVE_1L, item_name=SAP_ITEM_MASTER[OLIVE_1L])

        rows = self._export_rows()
        self.assertEqual(len(rows), 1)
        codes = rows[0]["SAP Item Code"]
        self.assertIn(NIRMAL_1L, codes)
        self.assertIn(OLIVE_3L, codes)
        self.assertNotIn(PLAIN_1L, codes)
        self.assertNotIn(OLIVE_1L, codes)
        self.assertEqual(rows[0]["Source"], "posted")

    def test_the_snapshot_keeps_code_name_and_quantity_aligned(self):
        """The three "; "-joined columns are read positionally by reconciliation."""
        order = self._order(self._combo())
        self._post_dn(order)
        row = self._export_rows()[0]
        self.assertEqual(len(row["SAP Item Code"].split("; ")), 2)
        self.assertEqual(len(row["SAP Item Name"].split("; ")), 2)
        self.assertEqual(len(row["SAP Qty"].split("; ")), 2)
        self.assertEqual(len(row["UOM"].split("; ")), 2)

    def test_the_uom_column_lists_one_entry_per_item(self):
        """UOM used to be reported only when a row held exactly ONE item, so every
        combo exported a blank UoM and the column could not be read positionally
        with the other three."""
        order = self._order(self._combo())
        dispatch = self._post_dn(order)
        row = self._export_rows()[0]
        self.assertEqual(row["UOM"], "PCS; PCS")

        # A missing UoM holds its position rather than shifting the rest left.
        lines = dispatch.sap_posted_lines
        lines[0]["uom"] = ""
        dispatch.sap_posted_lines = lines
        dispatch.save(update_fields=["sap_posted_lines"])
        self.assertEqual(self._export_rows()[0]["UOM"], "-; PCS")

    def test_a_row_whose_items_carry_no_uom_stays_blank(self):
        """SAP's own read-back (DLN1) has no UoM, so those rows must stay empty
        instead of printing a row of placeholders."""
        from .services import delivery_note_service as dns

        order = self._order(self._combo())
        MarketplaceDispatch.objects.create(
            company=self.company, channel=self.channel, order=order,
            status=MarketplaceDispatchStatus.CONFIRMED,
            sap_post_status=MarketplaceSapPostStatus.POSTED,
            sap_delivery_note_doc_entry=76030, sap_delivery_note_num="1507264746",
            sap_posted_lines=[],
        )
        sap_rows = [
            {"item_code": NIRMAL_1L, "item_name": SAP_ITEM_MASTER[NIRMAL_1L],
             "quantity": "1", "warehouse_code": "DL-EC"},
            {"item_code": OLIVE_3L, "item_name": SAP_ITEM_MASTER[OLIVE_3L],
             "quantity": "2", "warehouse_code": "DL-EC"},
        ]
        with patch.object(dns, "_sap_delivery_note_lines", return_value=sap_rows):
            rows = self._export_rows(76030)
        # The note's own rows come from DLN1, which is read without a UoM column.
        note_lines = [r for r in rows if r["Source"] == "sap (note line)"]
        self.assertEqual(len(note_lines), 2)
        self.assertTrue(all(r["UOM"] == "" for r in note_lines))

    def test_a_nameless_item_still_holds_its_column_position(self):
        """Names used to be filtered, so one blank name shifted every later name
        left against its code.

        The dash is now the last resort rather than the first: it appears only for
        a code NOTHING can name — no master, no scan, and no reachable SAP.
        """
        order = self._order(self._combo())
        dispatch = self._post_dn(order)
        lines = dispatch.sap_posted_lines
        lines[0]["item_name"] = ""
        dispatch.sap_posted_lines = lines
        dispatch.save(update_fields=["sap_posted_lines"])
        ComboComponent.objects.filter(item_code=lines[0]["item_code"]).update(item_name="")

        with patch("marketplace.services.sap_gateway.oitm_names", return_value=None):
            row = self._export_rows()[0]
        names = row["SAP Item Name"].split("; ")
        self.assertEqual(len(names), 2)
        self.assertEqual(names[0], "-")
        self.assertEqual(names[1], SAP_ITEM_MASTER[OLIVE_3L])

    def test_a_blank_snapshot_name_is_recovered_from_the_masters(self):
        """A master saved while the item master was unreachable stores no name, the
        snapshot froze that blank in, and the export printed a column of dashes
        beside perfectly good item codes. The name is recovered at export time.

        No SAP call is needed when the database can already answer — ``oitm_names``
        raises here to prove the local tier is doing the work.
        """
        order = self._order(self._combo())
        dispatch = self._post_dn(order)
        lines = dispatch.sap_posted_lines
        for entry in lines:
            entry["item_name"] = ""
        dispatch.sap_posted_lines = lines
        dispatch.save(update_fields=["sap_posted_lines"])

        with patch("marketplace.services.sap_gateway.oitm_names",
                   side_effect=AssertionError("SAP must not be needed")):
            row = self._export_rows()[0]
        self.assertEqual(
            row["SAP Item Name"],
            f"{SAP_ITEM_MASTER[NIRMAL_1L]}; {SAP_ITEM_MASTER[OLIVE_3L]}")

    def test_a_name_no_master_holds_comes_from_the_sap_item_master(self):
        """When nothing local names the code — every master row for it was saved
        blank — the export asks OITM, once for the whole file."""
        order = self._order(self._combo())
        dispatch = self._post_dn(order)
        lines = dispatch.sap_posted_lines
        for entry in lines:
            entry["item_name"] = ""
        dispatch.sap_posted_lines = lines
        dispatch.save(update_fields=["sap_posted_lines"])
        ComboComponent.objects.update(item_name="")

        with patch("marketplace.services.sap_gateway.oitm_names",
                   side_effect=lambda _company, codes: sap_master(codes)) as oitm:
            row = self._export_rows()[0]
        self.assertEqual(
            row["SAP Item Name"],
            f"{SAP_ITEM_MASTER[NIRMAL_1L]}; {SAP_ITEM_MASTER[OLIVE_3L]}")
        # Batched for the file, not one lookup per printed row.
        self.assertEqual(oitm.call_count, 1)

    def test_the_notes_own_rows_are_named_too(self):
        """The rows read back from SAP go through the same repair as the order rows
        — DLN1 gives a code and a quantity, and often no name at all."""
        from .services import delivery_note_service as dns

        order = self._order(self._combo())
        MarketplaceDispatch.objects.create(
            company=self.company, channel=self.channel, order=order,
            status=MarketplaceDispatchStatus.CONFIRMED,
            sap_post_status=MarketplaceSapPostStatus.POSTED,
            sap_delivery_note_doc_entry=76031, sap_delivery_note_num="1507264747",
            sap_posted_lines=[],
        )
        sap_rows = [{"item_code": NIRMAL_1L, "item_name": "", "quantity": "1",
                     "warehouse_code": "DL-EC"}]
        with patch.object(dns, "_sap_delivery_note_lines", return_value=sap_rows):
            rows = self._export_rows(76031)
        note = [r for r in rows if r["Source"] == "sap (note line)"][0]
        self.assertEqual(note["SAP Item Name"], SAP_ITEM_MASTER[NIRMAL_1L])

    def test_the_snapshot_records_the_line_it_belongs_to(self):
        order = self._order(self._combo())
        dispatch = self._post_dn(order)
        line_id = order.lines.first().id
        self.assertTrue(dispatch.sap_posted_lines)
        for entry in dispatch.sap_posted_lines:
            self.assertEqual(entry["order_line_id"], line_id)
            self.assertIn(entry["item_code"], (NIRMAL_1L, OLIVE_3L))


class ReconcilePathTests(ItemCodeIntegrityBase):
    """The approval reconciler finalizes items with no fg/pm keys."""

    def test_the_reconcile_path_snapshots_and_does_not_crash(self):
        from .services import delivery_note_service as dns

        order = self._order(self._combo())
        dispatch = MarketplaceDispatch.objects.create(
            company=self.company, channel=self.channel, order=order,
            status=MarketplaceDispatchStatus.CONFIRMED,
            sap_post_status=MarketplaceSapPostStatus.AWAITING_APPROVAL,
        )
        # Exactly what reconcile_approved_delivery_notes builds.
        includable = [{"dispatch": dispatch, "amount": Decimal("100")}]
        dns._finalize_posted(includable, self.company, 76002, "1507264746", self.user)

        dispatch.refresh_from_db()
        self.assertEqual(dispatch.sap_post_status, MarketplaceSapPostStatus.POSTED)
        codes = {e["item_code"] for e in dispatch.sap_posted_lines}
        self.assertEqual(codes, {NIRMAL_1L, OLIVE_3L})

    def test_a_snapshot_failure_never_fails_a_post_sap_already_accepted(self):
        from .services import delivery_note_service as dns

        order = self._order(self._combo())
        dispatch = MarketplaceDispatch.objects.create(
            company=self.company, channel=self.channel, order=order,
            status=MarketplaceDispatchStatus.CONFIRMED,
            sap_post_status=MarketplaceSapPostStatus.AWAITING_APPROVAL,
        )
        with patch.object(dns, "_posted_lines_snapshot", side_effect=RuntimeError("boom")):
            dns._finalize_posted(
                [{"dispatch": dispatch, "amount": Decimal("100")}],
                self.company, 76003, "1507264747", self.user)

        dispatch.refresh_from_db()
        self.assertEqual(dispatch.sap_post_status, MarketplaceSapPostStatus.POSTED)
        self.assertEqual(dispatch.sap_posted_lines, [])


class ExportFallbackTests(ItemCodeIntegrityBase):
    """Each source is chosen in the right order and labelled honestly."""

    def _legacy_dispatch(self):
        """A note posted before snapshots existed."""
        order = self._order(self._combo())
        return MarketplaceDispatch.objects.create(
            company=self.company, channel=self.channel, order=order,
            status=MarketplaceDispatchStatus.CONFIRMED,
            sap_post_status=MarketplaceSapPostStatus.POSTED,
            sap_delivery_note_doc_entry=76010, sap_delivery_note_num="1507264750",
            sap_posted_lines=[],
        )

    def test_no_snapshot_falls_back_to_the_note_in_sap(self):
        from .services import delivery_note_service as dns

        self._legacy_dispatch()
        sap_rows = [
            {"item_code": NIRMAL_1L, "item_name": SAP_ITEM_MASTER[NIRMAL_1L],
             "quantity": "1", "warehouse_code": "DL-EC"},
        ]
        with patch.object(dns, "_sap_delivery_note_lines", return_value=sap_rows):
            rows = self._export_rows(76010)
        # The order row says what that order shipped — re-derived, because SAP does
        # not record which order its items belonged to. Never left blank: an empty
        # cell tells the reader nothing at all.
        self.assertEqual(rows[0]["Source"], "resolved")
        self.assertIn(NIRMAL_1L, rows[0]["SAP Item Code"])
        # The note's own line follows, carrying SAP's total and the DN columns.
        note_line = next(r for r in rows if r["Source"] == "sap (note line)")
        self.assertEqual(note_line["SAP Item Code"], NIRMAL_1L)
        self.assertEqual(note_line["Order Id"], "")
        self.assertEqual(note_line["DN Number"], "1507264750")

    def test_no_snapshot_and_no_sap_falls_back_to_a_live_resolve(self):
        from .services import delivery_note_service as dns

        self._legacy_dispatch()
        with patch.object(dns, "_sap_delivery_note_lines", return_value=[]):
            rows = self._export_rows(76010)
        self.assertEqual(rows[0]["Source"], "resolved")
        # Re-derived from today's masters — and labelled so nobody mistakes it
        # for what was actually sent.
        self.assertIn(NIRMAL_1L, rows[0]["SAP Item Code"])

    def test_a_notes_sap_lines_are_its_own_rows_not_one_crammed_cell(self):
        """DN 1507264745 covers 8 orders. Its item list belongs to the NOTE, not to
        any order: printing it per order reported 8x the quantity that shipped, and
        attaching it to the first order put every code in one cell and every
        quantity in another, on row 1, with every other row blank. On a 1265-order
        note that is unreadable. One item per row, after the orders."""
        from .services import delivery_note_service as dns

        combo = self._combo()
        for i in range(3):
            order = self._order(combo, order_id=f"OD-MULTI-{i}", fsn=f"FSNM{i}", sku=f"SKU-M{i}")
            MarketplaceDispatch.objects.create(
                company=self.company, channel=self.channel, order=order,
                status=MarketplaceDispatchStatus.CONFIRMED,
                sap_post_status=MarketplaceSapPostStatus.POSTED,
                sap_delivery_note_doc_entry=76020, sap_delivery_note_num="1507264745",
                sap_posted_lines=[],
            )
        sap_rows = [
            {"item_code": NIRMAL_1L, "item_name": SAP_ITEM_MASTER[NIRMAL_1L],
             "quantity": "6", "warehouse_code": "DL-EC"},
            {"item_code": OLIVE_3L, "item_name": SAP_ITEM_MASTER[OLIVE_3L],
             "quantity": "4", "warehouse_code": "DL-EC"},
        ]
        with patch.object(dns, "_sap_delivery_note_lines", return_value=sap_rows):
            rows = self._export_rows(76020)

        orders = [r for r in rows if r["Order Id"]]
        note_lines = [r for r in rows if r["Source"] == "sap (note line)"]
        self.assertEqual(len(orders), 3)
        self.assertEqual(len(note_lines), 2)

        # Each order says what IT shipped, re-derived and labelled as such — no
        # order carries the note's pooled total, and no row is left blank.
        self.assertTrue(all(r["SAP Item Code"] for r in orders))
        self.assertTrue(all(r["Source"] == "resolved" for r in orders))
        self.assertTrue(all("6" not in r["SAP Qty"].split("; ") for r in orders))

        # One item per row, each quantity in its own cell — not a joined list.
        self.assertEqual([r["SAP Item Code"] for r in note_lines], [NIRMAL_1L, OLIVE_3L])
        self.assertEqual([r["SAP Qty"] for r in note_lines], ["6", "4"])
        self.assertTrue(all(r["Source"] == "sap (note line)" for r in note_lines))
        self.assertTrue(all(r["DN Number"] == "1507264745" for r in note_lines))

    def test_a_snapshot_always_wins_over_sap(self):
        from .services import delivery_note_service as dns

        order = self._order(self._combo())
        self._post_dn(order, doc_entry=76011, doc_num="1507264751")
        with patch.object(dns, "_sap_delivery_note_lines") as sap:
            rows = self._export_rows(76011)
        sap.assert_not_called()
        self.assertEqual(rows[0]["Source"], "posted")


class _Ctx:
    """Minimal request context: the serializers read request.company.company."""

    def __init__(self, company):
        self.company = type("C", (), {"company": company})()


class ItemMasterValidationTests(ItemCodeIntegrityBase):
    """Defect 2 — nothing checked a code against the SAP item master."""

    def _serializer(self, data, instance=None):
        from .serializers import ComboDefinitionSerializer

        return ComboDefinitionSerializer(
            instance=instance, data=data, context={"request": _Ctx(self.company)})

    def _payload(self, item_code, item_name, **extra):
        data = {
            "channel": self.channel, "code": "cb005", "name": "Combo",
            "components": [{
                "component_type": ComboComponentType.FG, "item_code": item_code,
                "item_name": item_name, "quantity": "1", "uom": "PCS",
            }],
        }
        data.update(extra)
        return data

    @patch("marketplace.services.sap_gateway.oitm_names", side_effect=lambda c, codes: sap_master(codes))
    def test_the_real_mislabel_is_corrected_to_saps_own_name(self, _m):
        """cb005 was saved with FG0000005 called "EXTRA LIGHT OLIVE 3 LTR TIN 2 PCS".
        The code is real, so the save proceeds — but the name becomes SAP's."""
        s = self._serializer(self._payload(OLIVE_1L, "EXTRA LIGHT OLIVE 3 LTR TIN 2 PCS"))
        self.assertTrue(s.is_valid(), s.errors)
        combo = s.save(company=self.company)
        component = combo.components.get()
        self.assertEqual(component.item_code, OLIVE_1L)
        self.assertEqual(component.item_name, SAP_ITEM_MASTER[OLIVE_1L])

    @patch("marketplace.services.sap_gateway.oitm_names", side_effect=lambda c, codes: sap_master(codes))
    def test_a_code_sap_has_never_heard_of_is_rejected(self, _m):
        s = self._serializer(self._payload("FG9999999", "Invented"))
        self.assertFalse(s.is_valid())
        self.assertIn("components", s.errors)
        self.assertIn("FG9999999", str(s.errors["components"]))

    @patch("marketplace.services.sap_gateway.oitm_names", side_effect=lambda c, codes: sap_master(codes))
    def test_alternatives_are_checked_too(self, _m):
        s = self._serializer(self._payload(
            NIRMAL_1L, SAP_ITEM_MASTER[NIRMAL_1L],
            components=[{
                "component_type": ComboComponentType.FG, "item_code": NIRMAL_1L,
                "item_name": SAP_ITEM_MASTER[NIRMAL_1L], "quantity": "1", "uom": "PCS",
                "options": [{"item_code": "FG9999999", "item_name": "Invented"}],
            }],
        ))
        self.assertFalse(s.is_valid())
        self.assertIn("FG9999999", str(s.errors["components"]))

    @patch("marketplace.services.sap_gateway.oitm_names", return_value=None)
    def test_a_hana_outage_still_lets_the_save_through(self, _m):
        """Master data must stay editable when SAP is down; an unreachable master
        says "cannot verify", never "does not exist"."""
        s = self._serializer(self._payload("FG9999999", "Unverifiable"))
        self.assertTrue(s.is_valid(), s.errors)
        combo = s.save(company=self.company)
        # Saved exactly as given — nothing was confirmed, so nothing is rewritten.
        self.assertEqual(combo.components.get().item_name, "Unverifiable")

    @patch("marketplace.services.sap_gateway.oitm_names", return_value={})
    def test_a_payload_of_only_unknown_codes_is_still_rejected(self, _m):
        """A read that matched nothing returns {} — the same shape an outage used
        to return. Conflating them let exactly these codes through."""
        s = self._serializer(self._payload("FG9999999", "Invented"))
        self.assertFalse(s.is_valid())
        self.assertIn("FG9999999", str(s.errors["components"]))

    @patch("marketplace.services.sap_gateway.oitm_names", side_effect=lambda c, codes: sap_master(codes))
    def test_sku_mapping_codes_are_checked_and_named_from_sap(self, _m):
        from .serializers import SkuMappingSerializer

        s = SkuMappingSerializer(
            data={
                "channel": self.channel, "marketplace_sku": "SKU-1", "fsn": "FSN-1",
                "sku_name": "Item", "sku_type": SkuType.RAW,
                "fg_item_code": OLIVE_1L, "fg_item_name": "WRONG NAME",
            },
            context={"request": _Ctx(self.company)},
        )
        self.assertTrue(s.is_valid(), s.errors)
        mapping = s.save(company=self.company)
        self.assertEqual(mapping.fg_item_name, SAP_ITEM_MASTER[OLIVE_1L])

    @patch("marketplace.services.sap_gateway.oitm_names", side_effect=lambda c, codes: sap_master(codes))
    def test_an_unknown_sku_mapping_code_is_rejected(self, _m):
        from .serializers import SkuMappingSerializer

        s = SkuMappingSerializer(
            data={
                "channel": self.channel, "marketplace_sku": "SKU-2", "fsn": "FSN-2",
                "sku_name": "Item", "sku_type": SkuType.RAW,
                "fg_item_code": "FG9999999", "fg_item_name": "Invented",
            },
            context={"request": _Ctx(self.company)},
        )
        self.assertFalse(s.is_valid())
        self.assertIn("fg_item_code", s.errors)


class ComboEditPreservesOptionsTests(ItemCodeIntegrityBase):
    """Defect 3 — saving a combo deleted and rebuilt every component."""

    def setUp(self):
        super().setUp()
        self.combo = self._combo(code="CB0030")
        self.component = self.combo.components.first()
        self.option = ComboComponentOption.objects.create(
            component=self.component, item_code=NIRMAL_1L,
            item_name=SAP_ITEM_MASTER[NIRMAL_1L], is_default=True,
        )

    def _update(self, components):
        from .serializers import ComboDefinitionSerializer

        s = ComboDefinitionSerializer(
            instance=self.combo,
            data={"channel": self.channel, "code": "CB0030", "name": "Renamed",
                  "components": components},
            context={"request": _Ctx(self.company)},
        )
        self.assertTrue(s.is_valid(), s.errors)
        return s.save()

    def _component_payload(self, **extra):
        payload = {
            "id": self.component.id,
            "component_type": ComboComponentType.FG,
            "item_code": self.component.item_code,
            "item_name": self.component.item_name,
            "quantity": "1", "uom": "PCS",
        }
        payload.update(extra)
        return payload

    @patch("marketplace.services.sap_gateway.oitm_names", side_effect=lambda c, codes: sap_master(codes))
    def test_renaming_a_combo_keeps_its_alternatives(self, _m):
        """The CB0030 regression: a payload with no options key must leave them be."""
        other = self.combo.components.last()
        self._update([
            self._component_payload(),
            {"id": other.id, "component_type": ComboComponentType.FG,
             "item_code": other.item_code, "item_name": other.item_name,
             "quantity": "1", "uom": "PCS"},
        ])
        self.component.refresh_from_db()
        self.assertEqual(self.component.options.count(), 1)
        self.assertEqual(self.component.options.get().item_code, NIRMAL_1L)
        # And the component itself survived rather than being recreated.
        self.assertTrue(ComboComponent.objects.filter(id=self.component.id).exists())
        self.assertTrue(ComboComponentOption.objects.filter(id=self.option.id).exists())

    @patch("marketplace.services.sap_gateway.oitm_names", side_effect=lambda c, codes: sap_master(codes))
    def test_an_explicit_empty_list_clears_them(self, _m):
        other = self.combo.components.last()
        self._update([
            self._component_payload(options=[]),
            {"id": other.id, "component_type": ComboComponentType.FG,
             "item_code": other.item_code, "item_name": other.item_name,
             "quantity": "1", "uom": "PCS"},
        ])
        self.component.refresh_from_db()
        self.assertEqual(self.component.options.count(), 0)

    @patch("marketplace.services.sap_gateway.oitm_names", side_effect=lambda c, codes: sap_master(codes))
    def test_a_component_the_payload_omits_is_deleted(self, _m):
        other = self.combo.components.last()
        self._update([self._component_payload()])
        self.assertFalse(ComboComponent.objects.filter(id=other.id).exists())
        self.assertTrue(ComboComponent.objects.filter(id=self.component.id).exists())

    @patch("marketplace.services.sap_gateway.oitm_names", side_effect=lambda c, codes: sap_master(codes))
    def test_exactly_one_option_stays_default(self, _m):
        other = self.combo.components.last()
        self._update([
            self._component_payload(options=[
                {"id": self.option.id, "item_code": NIRMAL_1L,
                 "item_name": SAP_ITEM_MASTER[NIRMAL_1L]},
                {"item_code": PLAIN_1L, "item_name": SAP_ITEM_MASTER[PLAIN_1L]},
            ]),
            {"id": other.id, "component_type": ComboComponentType.FG,
             "item_code": other.item_code, "item_name": other.item_name,
             "quantity": "1", "uom": "PCS"},
        ])
        self.component.refresh_from_db()
        self.assertEqual(self.component.options.count(), 2)
        self.assertEqual(self.component.options.filter(is_default=True).count(), 1)


class ReconciliationRowsTests(ItemCodeIntegrityBase):
    """A pre-snapshot note's order rows are re-derived, so they can disagree with
    the note. The file says where, instead of leaving it to be found by hand."""

    def _legacy_with_sap(self, sap_rows, doc_entry=76040):
        from .services import delivery_note_service as dns

        # Vary the masters per call so one test can build more than one note.
        tag = str(doc_entry)
        order = self._order(
            self._combo(code=f"CB{tag}"),
            order_id=f"OD-{tag}", fsn=f"FSN{tag}", sku=f"SKU-{tag}")
        MarketplaceDispatch.objects.create(
            company=self.company, channel=self.channel, order=order,
            status=MarketplaceDispatchStatus.CONFIRMED,
            sap_post_status=MarketplaceSapPostStatus.POSTED,
            sap_delivery_note_doc_entry=doc_entry, sap_delivery_note_num="1507264761",
            sap_posted_lines=[])
        with patch.object(dns, "_sap_delivery_note_lines", return_value=sap_rows):
            return self._export_rows(doc_entry)

    def test_a_remapped_item_is_reported_with_its_difference(self):
        """The real case: the combo was repointed after the note was cut, so the
        order rows name an item the note never carried."""
        rows = self._legacy_with_sap([
            {"item_code": NIRMAL_1L, "item_name": SAP_ITEM_MASTER[NIRMAL_1L],
             "quantity": "5", "warehouse_code": "DL-EC"},
        ])
        checks = {r["SAP Item Code"]: r for r in rows if r["Source"] == "check"}
        # The order row resolves to one of each combo component, the note holds 5
        # of the first and none of the second.
        self.assertEqual(checks[NIRMAL_1L]["SAP Qty"], "-4")
        self.assertIn("order rows 1, delivery note 5", checks[NIRMAL_1L]["SAP Item Name"])
        self.assertEqual(checks[OLIVE_3L]["SAP Qty"], "+1")
        self.assertIn("delivery note 0", checks[OLIVE_3L]["SAP Item Name"])
        # The check rows carry the note they are about, and no order.
        self.assertTrue(all(r["DN Number"] == "1507264761" for r in checks.values()))
        self.assertTrue(all(r["Order Id"] == "" for r in checks.values()))

    def test_a_round_difference_keeps_its_zeros(self):
        """A trailing-zero trim turned a shortfall of 120 into one of 12."""
        rows = self._legacy_with_sap([
            {"item_code": OLIVE_3L, "item_name": SAP_ITEM_MASTER[OLIVE_3L],
             "quantity": "120", "warehouse_code": "DL-EC"},
        ], doc_entry=76041)
        check = next(r for r in rows
                     if r["Source"] == "check" and r["SAP Item Code"] == OLIVE_3L)
        self.assertEqual(check["SAP Qty"], "-119")
        # An item only the note carries still gets named, from SAP.
        rows = self._legacy_with_sap([
            {"item_code": OLIVE_1L, "item_name": SAP_ITEM_MASTER[OLIVE_1L],
             "quantity": "40", "warehouse_code": "DL-EC"},
        ], doc_entry=76042)
        only = next(r for r in rows
                    if r["Source"] == "check" and r["SAP Item Code"] == OLIVE_1L)
        self.assertEqual(only["SAP Qty"], "-40")
        self.assertTrue(only["SAP Item Name"].startswith(SAP_ITEM_MASTER[OLIVE_1L]))

    def test_no_check_rows_when_everything_agrees(self):
        rows = self._legacy_with_sap([
            {"item_code": NIRMAL_1L, "item_name": SAP_ITEM_MASTER[NIRMAL_1L],
             "quantity": "1", "warehouse_code": "DL-EC"},
            {"item_code": OLIVE_3L, "item_name": SAP_ITEM_MASTER[OLIVE_3L],
             "quantity": "1", "warehouse_code": "DL-EC"},
        ])
        self.assertEqual([r for r in rows if r["Source"] == "check"], [])

    def test_a_posted_note_never_gets_check_rows(self):
        """A snapshot IS the per-order truth, so there is nothing to reconcile."""
        order = self._order(self._combo())
        self._post_dn(order)
        rows = self._export_rows()
        self.assertEqual([r for r in rows if r["Source"] in ("check", "sap (note line)")], [])
