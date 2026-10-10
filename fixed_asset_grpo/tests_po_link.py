"""The late PO link on a fixed-asset gate entry.

The gate never knew the PO; the Fixed Asset GRPO screen links it afterwards,
on a completed and locked entry, without saving that entry. What is guarded:

* the lines come from SAP (warehouse, tax, G/L, price), ``accepted == received``,
  and the received quantity is held to 110% of SAP's open quantity;
* only the link or post right may link, and grpo's own rights do not;
* one PO per entry; another company's or another type's entry is a 404;
* a change or an unlink discards unsent drafts, and is refused while a GRPO is
  posted, simulated or on its way to SAP (the lock reasons, in order).

    manage.py test fixed_asset_grpo.tests_po_link --settings=config.sqlite_test_settings
"""

from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from gate_core.enums import GateEntryStatus
from grpo.models import GRPOLinePosting, GRPOPosting, GRPOStatus
from raw_material_gatein.models import POItemReceipt, POReceipt
from sap_client.exceptions import SAPUnavailable
from sap_postings.models import SapPosting, SapPostingStatus

from . import fa_test_support as fx
from .constants import KIND
from .models import FixedAssetGRPOClosure, FixedAssetGRPORecord
from .services import fa_po_receipt_lock_reason

READER = "sap_purchasing.fa_po_reader.get_open_fixed_asset_pos"


def _url(entry_id, receipt_id=None):
    base = f"/api/v1/fixed-asset-grpo/entries/{entry_id}/po-receipts/"
    return f"{base}{receipt_id}/" if receipt_id else base


class FAPOLinkTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        fx.build_fa_world(cls)
        # A second, unlinked FA entry: the one these tests link to.
        cls.bare = fx.make_entry(cls, "GE-FA-0002")
        cls.linker = fx.make_user("linker@example.com", "can_link_fixed_asset_po", company=cls.company)
        cls.poster = fx.make_user("poster@example.com", "can_post_fixed_asset_grpo", company=cls.company)
        cls.viewer = fx.make_user("viewer@example.com", "can_view_fixed_asset_grpo", company=cls.company)
        cls.grpo_only = fx.make_user(
            "rmgrpo@example.com", "grpo.add_grpoposting", "grpo.can_preview_grpo", company=cls.company
        )

    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(self.linker)
        patcher = patch(READER, return_value=[fx.open_po()])
        self.reader = patcher.start()
        self.addCleanup(patcher.stop)

    def link(self, entry=None, body=None, user=None):
        if user is not None:
            self.api.force_authenticate(user)
        return self.api.post(
            _url((entry or self.bare).id), body or fx.link_body(), format="json",
            HTTP_COMPANY_CODE=fx.COMPANY,
        )

    # -- linking -----------------------------------------------------------------

    def test_a_link_on_a_locked_completed_entry_takes_sap_s_line_data(self):
        response = self.link()

        self.assertEqual(response.status_code, 201, response.content)
        receipt = POReceipt.objects.get(vehicle_entry=self.bare)
        line = receipt.items.get()
        self.assertEqual(line.accepted_qty, line.received_qty)
        self.assertEqual(
            (line.warehouse_code, line.tax_code, line.gl_account, line.variety),
            ("BH-FA", "GST18", "1204023", "CANOLA"),
        )
        self.assertEqual(receipt.sap_doc_entry, fx.PO_DOC_ENTRY)
        self.assertEqual(receipt.branch_id, 2)
        self.assertEqual(len(receipt.supplier_name), 150)
        self.assertEqual(receipt.invoice_no, "ABD/26-27/814")
        self.bare.refresh_from_db()
        self.assertTrue(self.bare.is_locked)
        self.assertEqual(self.bare.status, GateEntryStatus.COMPLETED)

    def test_the_post_right_may_link_too(self):
        self.assertEqual(self.link(user=self.poster).status_code, 201)

    def test_view_only_and_grpo_rights_may_not_link(self):
        self.assertEqual(self.link(user=self.viewer).status_code, 403)
        self.assertEqual(self.link(user=self.grpo_only).status_code, 403)
        self.assertFalse(POReceipt.objects.filter(vehicle_entry=self.bare).exists())

    def test_a_second_po_is_refused(self):
        self.link()
        response = self.link(body=fx.link_body(po_number="221026999"))

        self.assertEqual(response.status_code, 400)
        self.assertIn("change or remove it first", response.json()["detail"])

    def test_another_type_or_company_is_a_404(self):
        rm_entry = fx.make_entry(self, "GE-RM-0001", entry_type="RAW_MATERIAL")
        elsewhere = fx.make_entry(self, "GE-FA-9001", company=self.other_company)

        self.assertEqual(self.link(entry=rm_entry).status_code, 404)
        self.assertEqual(self.link(entry=elsewhere).status_code, 404)

    def test_a_po_with_no_open_fa_line_is_a_400(self):
        self.reader.return_value = []
        response = self.link()

        self.assertEqual(response.status_code, 400)
        self.assertIn("no open item lines in a fixed-asset warehouse", response.json()["detail"])

    def test_sap_down_is_a_503_and_links_nothing(self):
        self.reader.side_effect = SAPUnavailable("HANA down")
        response = self.link()

        self.assertEqual(response.status_code, 503)
        self.assertFalse(POReceipt.objects.filter(vehicle_entry=self.bare).exists())

    @override_settings(GRPO_OVER_RECEIPT_ENFORCED_COMPANY_CODES=[fx.COMPANY])
    @patch("raw_material_gatein.services.validations._read_bp_group_code", return_value=None)
    def test_over_receipt_is_a_400(self, _group):
        self.reader.return_value = [fx.open_po(remaining=1.0)]
        response = self.link(body=fx.link_body(qty="2.000"))

        self.assertEqual(response.status_code, 400)
        self.assertIn("open + 10% tolerance", response.json()["detail"])
        self.assertFalse(POReceipt.objects.filter(vehicle_entry=self.bare).exists())

    @override_settings(
        GRPO_OVER_RECEIPT_ENFORCED_COMPANY_CODES=[fx.COMPANY],
        GRPO_OVER_RECEIPT_EXEMPT_VENDORS={fx.COMPANY: [fx.SUPPLIER]},
    )
    def test_an_exempt_vendor_may_over_receive(self):
        self.reader.return_value = [fx.open_po(remaining=1.0)]
        self.assertEqual(self.link(body=fx.link_body(qty="2.000")).status_code, 201)

    def test_an_overlapping_booking_is_warned_not_refused(self):
        # GE-FA-0001 already books 1 of the line; only 1 is open.
        self.reader.return_value = [fx.open_po(remaining=1.0)]
        response = self.link()

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(len(response.json()["warnings"]), 1)
        self.assertIn("GE-FA-0001", response.json()["warnings"][0])

    def test_a_simulated_post_counts_toward_the_overlap_warning(self):
        # GE-FA-0001's 1 is held by a simulated GRPO: stored POSTED, so the
        # gate's own bookings skip it, but SAP still shows it open.
        simulated = GRPOPosting.objects.create(
            vehicle_entry=self.entry, po_receipt=self.po_receipt, status=GRPOStatus.POSTED,
        )
        FixedAssetGRPORecord.objects.create(grpo_posting=simulated, simulated=True)
        GRPOLinePosting.objects.create(
            grpo_posting=simulated, po_item_receipt=self.po_item, quantity_posted="1.000",
            base_entry=fx.PO_DOC_ENTRY, base_line=0,
        )
        self.reader.return_value = [fx.open_po(remaining=1.0)]
        response = self.link()

        self.assertEqual(response.status_code, 201, response.content)
        warnings = response.json()["warnings"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("simulated GRPO of GE-FA-0001", warnings[0])

    def test_a_closed_entry_takes_no_link(self):
        FixedAssetGRPOClosure.objects.create(vehicle_entry=self.bare, sap_doc_num="30099")
        response = self.link()

        self.assertEqual(response.status_code, 400)
        self.assertIn("done in SAP by hand", response.json()["detail"])

    # -- changing and unlinking ------------------------------------------------

    def _draft(self, status=GRPOStatus.FAILED):
        draft = GRPOPosting.objects.create(
            vehicle_entry=self.entry, po_receipt=self.po_receipt, status=status,
            request_payload={"vehicle_entry_id": self.entry.id},
        )
        draft.po_receipts.set([self.po_receipt])
        return draft

    def test_an_update_discards_an_unsent_failed_draft(self):
        draft = self._draft()
        response = self.api.put(
            _url(self.entry.id, self.po_receipt.id), fx.link_body(qty="1.000"), format="json",
            HTTP_COMPANY_CODE=fx.COMPANY,
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(GRPOPosting.objects.filter(id=draft.id).exists())
        self.assertEqual(response.json()["po_receipt"]["items"][0]["warehouse_code"], "BH-FA")

    def test_an_unlink_discards_the_draft_and_removes_the_lines(self):
        draft = self._draft()
        response = self.api.delete(
            _url(self.entry.id, self.po_receipt.id), HTTP_COMPANY_CODE=fx.COMPANY
        )

        self.assertEqual(response.status_code, 204, response.content)
        self.assertFalse(GRPOPosting.objects.filter(id=draft.id).exists())
        self.assertFalse(POItemReceipt.objects.filter(po_receipt_id=self.po_receipt.id).exists())

    def test_a_draft_on_its_way_to_sap_locks_the_link(self):
        draft = self._draft(status=GRPOStatus.QUEUED)
        SapPosting.objects.create(
            company=self.company, kind=KIND, source_id=draft.id, title="t",
            status=SapPostingStatus.QUEUED,
        )
        response = self.api.delete(
            _url(self.entry.id, self.po_receipt.id), HTTP_COMPANY_CODE=fx.COMPANY
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("being sent to SAP", response.json()["detail"])
        self.assertTrue(GRPOPosting.objects.filter(id=draft.id).exists())

    def test_a_queued_draft_whose_row_was_cancelled_does_not_lock_the_link(self):
        draft = self._draft(status=GRPOStatus.QUEUED)
        SapPosting.objects.create(
            company=self.company, kind=KIND, source_id=draft.id, title="t",
            status=SapPostingStatus.CANCELLED, cancel_reason="keyed by hand",
        )
        self.assertIsNone(fa_po_receipt_lock_reason(self.po_receipt))

        response = self.api.delete(
            _url(self.entry.id, self.po_receipt.id), HTTP_COMPANY_CODE=fx.COMPANY
        )

        self.assertEqual(response.status_code, 204, response.content)
        self.assertFalse(GRPOPosting.objects.filter(id=draft.id).exists())

    def test_a_simulated_post_says_discard_it_first(self):
        posting = self._draft(status=GRPOStatus.POSTED)
        FixedAssetGRPORecord.objects.create(grpo_posting=posting, simulated=True)

        self.assertIn("discard it in Fixed Asset GRPO history first",
                      fa_po_receipt_lock_reason(self.po_receipt))

    def test_a_real_post_names_its_docnum(self):
        posting = self._draft(status=GRPOStatus.POSTED)
        GRPOPosting.objects.filter(id=posting.id).update(sap_doc_entry=9001, sap_doc_num=30001)

        reason = fa_po_receipt_lock_reason(self.po_receipt)
        self.assertIn("30001", reason)
        response = self.api.put(
            _url(self.entry.id, self.po_receipt.id), fx.link_body(), format="json",
            HTTP_COMPANY_CODE=fx.COMPANY,
        )
        self.assertEqual(response.status_code, 400)

    def test_viewers_may_list_the_link(self):
        self.api.force_authenticate(self.viewer)
        response = self.api.get(_url(self.entry.id), HTTP_COMPANY_CODE=fx.COMPANY)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["po_number"], fx.PO_NUMBER)
        self.assertTrue(response.json()[0]["is_editable"])
