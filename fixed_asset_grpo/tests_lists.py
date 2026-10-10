"""The Fixed Asset GRPO lists, the entry page and "done in SAP by hand".

What is guarded:

* every list shows fixed-asset entries of the request's company only, and
  leaves out entries closed by hand and entries before the cut-over;
* the summary counts FA postings only (a raw-material posting changes nothing);
* every one-entry endpoint is a 404 for another company's entry or another
  gate type, preview and print included;
* the rights: viewing does not close, close needs a SAP number or a note, and
  a real post or a GRPO on its way to SAP refuses the close.

    manage.py test fixed_asset_grpo.tests_lists --settings=config.sqlite_test_settings
"""

from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from gate_core.enums import GateEntryStatus
from grpo.models import GRPOPosting, GRPOStatus
from sap_postings.models import SapPosting, SapPostingStatus

from . import fa_test_support as fx
from .constants import KIND
from .models import FixedAssetGRPOClosure, FixedAssetGRPORecord

BASE = "/api/v1/fixed-asset-grpo"


@override_settings(**fx.FA_SETTINGS)
class FAListTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        fx.build_fa_world(cls)                                   # GE-FA-0001: PO linked
        cls.needs_po = fx.make_entry(cls, "GE-FA-0002")          # completed, no PO
        cls.at_gate = fx.make_entry(
            cls, "GE-FA-0003", status=GateEntryStatus.IN_PROGRESS, locked=False
        )
        cls.rm_entry = fx.make_entry(cls, "GE-RM-0001", entry_type="RAW_MATERIAL")
        fx.link_po(cls.rm_entry, po_number="RM-PO-1")
        cls.elsewhere = fx.make_entry(cls, "GE-FA-9001", company=cls.other_company)
        cls.viewer = fx.make_user("viewer@example.com", "can_view_fixed_asset_grpo", company=cls.company)
        cls.poster = fx.make_user("poster@example.com", "can_post_fixed_asset_grpo", company=cls.company)
        cls.rm_user = fx.make_user(
            "rm@example.com", "grpo.can_view_pending_grpo", "grpo.can_view_grpo_history",
            "grpo.view_grpoposting", "grpo.add_grpoposting", company=cls.company,
        )

    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(self.viewer)

    def get(self, path, **params):
        return self.api.get(f"{BASE}/{path}", params, HTTP_COMPANY_CODE=fx.COMPANY)

    @staticmethod
    def ids(response, key="vehicle_entry_id"):
        return {row[key] for row in response.json()["results"]}

    # -- lists ---------------------------------------------------------------------

    def test_needs_po_lists_completed_fa_entries_without_a_po(self):
        response = self.get("needs-po/")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.ids(response), {self.needs_po.id})
        row = response.json()["results"][0]
        self.assertEqual((row["supplier_name"], row["invoice_number"]),
                         ("ABD Engineering", "ABD/26-27/814"))

    def test_pending_lists_fa_entries_with_an_unposted_po(self):
        response = self.get("pending/")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.ids(response), {self.entry.id})

    def test_a_closed_entry_leaves_the_lists(self):
        FixedAssetGRPOClosure.objects.create(vehicle_entry=self.entry, sap_doc_num="30099")
        FixedAssetGRPOClosure.objects.create(vehicle_entry=self.needs_po, note="HO keyed it")

        self.assertEqual(self.ids(self.get("pending/")), set())
        self.assertEqual(self.ids(self.get("needs-po/")), set())
        all_entries = self.get("all-entries/", include_closed="1")
        states = {row["vehicle_entry_id"]: row["state"] for row in all_entries.json()["results"]}
        self.assertEqual(states[self.entry.id], "CLOSED")

    @override_settings(FIXED_ASSET_GRPO_CUTOVER="2999-01-01")
    def test_entries_before_the_cutover_leave_every_list(self):
        self.assertEqual(self.ids(self.get("pending/")), set())
        self.assertEqual(self.ids(self.get("needs-po/")), set())
        self.assertEqual(self.ids(self.get("all-entries/")), set())

    def test_all_entries_says_where_each_fa_entry_stands(self):
        response = self.get("all-entries/")

        states = {row["vehicle_entry_id"]: row["state"] for row in response.json()["results"]}
        self.assertEqual(states, {
            self.entry.id: "READY",
            self.needs_po.id: "NEEDS_PO",
            self.at_gate.id: "AT_GATE",
        })

    def test_the_summary_counts_fixed_asset_postings_only(self):
        rm_posting = GRPOPosting.objects.create(
            vehicle_entry=self.rm_entry, status=GRPOStatus.POSTED, sap_doc_entry=1, sap_doc_num=1,
        )
        sim = GRPOPosting.objects.create(vehicle_entry=self.entry, status=GRPOStatus.POSTED)
        FixedAssetGRPORecord.objects.create(grpo_posting=sim, simulated=True)

        summary = self.get("summary/").json()
        self.assertEqual(summary["posted_count"], 0)
        self.assertEqual(summary["simulated_count"], 1)
        self.assertEqual(summary["needs_po_count"], 1)
        self.assertTrue(rm_posting.pk)

    def test_history_shows_fa_postings_with_the_simulated_flag(self):
        sim = GRPOPosting.objects.create(vehicle_entry=self.entry, status=GRPOStatus.POSTED)
        FixedAssetGRPORecord.objects.create(grpo_posting=sim, simulated=True, sap_payload={"document": {}})
        GRPOPosting.objects.create(vehicle_entry=self.rm_entry, status=GRPOStatus.POSTED)

        response = self.get("history/")
        rows = response.json()["results"]
        self.assertEqual([row["id"] for row in rows], [sim.id])
        self.assertTrue(rows[0]["simulated"])
        self.assertEqual(self.get("history/", simulated="false").json()["results"], [])

    # -- one entry: scoping ---------------------------------------------------------

    def test_one_entry_endpoints_are_404_for_other_types_and_companies(self):
        for entry in (self.rm_entry, self.elsewhere):
            for path in (f"entries/{entry.id}/", f"entries/{entry.id}/preview/",
                         f"entries/{entry.id}/po-receipts/"):
                self.assertEqual(self.get(path).status_code, 404, path)

    def test_history_detail_and_print_are_scoped(self):
        rm_posting = GRPOPosting.objects.create(
            vehicle_entry=self.rm_entry, status=GRPOStatus.POSTED, sap_doc_entry=5, sap_doc_num=5,
        )
        self.assertEqual(self.get(f"history/{rm_posting.id}/").status_code, 404)
        with patch("sap_client.client.SAPClient") as sap:
            self.assertEqual(self.get(f"history/{rm_posting.id}/print/").status_code, 404)
            sap.assert_not_called()

    def test_the_entry_page_carries_the_link_the_draft_and_the_sap_flags(self):
        response = self.get(f"entries/{self.entry.id}/")

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["po_receipt_ids"], [self.po_receipt.id])
        self.assertEqual(body["po_receipts"][0]["items"][0]["warehouse_code"], "BH-FA")
        self.assertIsNone(body["open_draft"])
        self.assertIsNone(body["closure"])
        self.assertEqual(
            set(body["sap"]), {"enabled", "simulate", "allow_live", "is_test_db", "cutover", "warehouses"}
        )

    @patch("grpo.services.SAPClient")
    def test_the_preview_is_grpo_s_preview(self, sap):
        sap.return_value.get_po_additional_expenses.return_value = {}
        sap.return_value.batch_managed_flags.return_value = {}
        response = self.get(f"entries/{self.entry.id}/preview/")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()[0]["po_number"], fx.PO_NUMBER)

    # -- rights ------------------------------------------------------------------------

    def test_grpo_s_rights_open_nothing_here(self):
        self.api.force_authenticate(self.rm_user)
        for path in ("summary/", "pending/", "needs-po/", "history/", f"entries/{self.entry.id}/"):
            self.assertEqual(self.get(path).status_code, 403, path)

    def test_a_viewer_cannot_close(self):
        response = self.api.post(
            f"{BASE}/entries/{self.needs_po.id}/close/", {"note": "x"}, format="json",
            HTTP_COMPANY_CODE=fx.COMPANY,
        )
        self.assertEqual(response.status_code, 403)

    # -- done in SAP by hand ----------------------------------------------------------

    def close(self, entry, body):
        self.api.force_authenticate(self.poster)
        return self.api.post(
            f"{BASE}/entries/{entry.id}/close/", body, format="json", HTTP_COMPANY_CODE=fx.COMPANY
        )

    def test_close_needs_a_number_or_a_note(self):
        self.assertEqual(self.close(self.needs_po, {}).status_code, 400)
        self.assertEqual(self.close(self.needs_po, {"sap_doc_num": "30099"}).status_code, 201)
        self.assertTrue(FixedAssetGRPOClosure.objects.filter(vehicle_entry=self.needs_po).exists())

    def test_close_then_reopen(self):
        self.close(self.needs_po, {"note": "keyed by HO"})
        response = self.api.delete(
            f"{BASE}/entries/{self.needs_po.id}/close/", HTTP_COMPANY_CODE=fx.COMPANY
        )
        self.assertEqual(response.status_code, 204)
        self.assertFalse(FixedAssetGRPOClosure.objects.filter(vehicle_entry=self.needs_po).exists())

    def test_close_is_refused_after_a_real_post(self):
        GRPOPosting.objects.create(
            vehicle_entry=self.entry, status=GRPOStatus.POSTED, sap_doc_entry=9001, sap_doc_num=30001,
        )
        response = self.close(self.entry, {"note": "x"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("30001", response.json()["detail"])

    def test_close_is_refused_while_a_grpo_is_on_its_way(self):
        draft = GRPOPosting.objects.create(vehicle_entry=self.entry, status=GRPOStatus.QUEUED)
        SapPosting.objects.create(
            company=self.company, kind=KIND, source_id=draft.id, title="t",
            status=SapPostingStatus.QUEUED,
        )
        response = self.close(self.entry, {"note": "x"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("on its way to SAP", response.json()["detail"])

    def test_a_queued_draft_whose_row_was_cancelled_is_not_on_its_way(self):
        # The handler left it QUEUED (switch off / SAP down); someone then
        # cancelled the row on the SAP postings screen because it was keyed by
        # hand. sap_postings' cancel leaves the draft QUEUED.
        draft = GRPOPosting.objects.create(vehicle_entry=self.entry, status=GRPOStatus.QUEUED)
        SapPosting.objects.create(
            company=self.company, kind=KIND, source_id=draft.id, title="t",
            status=SapPostingStatus.CANCELLED, cancel_reason="keyed by hand",
        )

        response = self.close(self.entry, {"sap_doc_num": "30099"})
        self.assertEqual(response.status_code, 201, response.content)

        response = self.api.delete(f"{BASE}/drafts/{draft.id}/", HTTP_COMPANY_CODE=fx.COMPANY)
        self.assertEqual(response.status_code, 204, response.content)
        self.assertFalse(GRPOPosting.objects.filter(id=draft.id).exists())

    def test_a_draft_is_discarded_but_not_while_it_is_being_sent(self):
        draft = GRPOPosting.objects.create(vehicle_entry=self.entry, status=GRPOStatus.FAILED)
        self.api.force_authenticate(self.poster)

        response = self.api.delete(f"{BASE}/drafts/{draft.id}/", HTTP_COMPANY_CODE=fx.COMPANY)
        self.assertEqual(response.status_code, 204)

        busy = GRPOPosting.objects.create(vehicle_entry=self.entry, status=GRPOStatus.DRAFT)
        SapPosting.objects.create(
            company=self.company, kind=KIND, source_id=busy.id, title="t",
            status=SapPostingStatus.SENDING,
        )
        response = self.api.delete(f"{BASE}/drafts/{busy.id}/", HTTP_COMPANY_CODE=fx.COMPANY)
        self.assertEqual(response.status_code, 400)
