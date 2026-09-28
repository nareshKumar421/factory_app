"""The credit-note queue's guards and extras that SAP Portal had and JI lacked.

* **The duplicate guard**: an approval over a credit note SAP already posted
  for the same party and amount is refused (409 ``DUPLICATE_CREDIT_NOTE``)
  unless the approver confirms. Approving one credits the party twice.
* **The draft-aware stale guard**: a request OWDD still calls pending, whose
  draft SAP already decided, is refused (409 ``STALE_REQUEST``).
* **A typed SAP password** for approvers with none stored; used once, never
  echoed.
* **The approver's comment** in SAP's remarks.
* **Attachments**: the credit note's files and its base documents', and only
  those, downloadable with the queue's view right.

SAP is mocked where the views look ``SAPClient`` and the document services up.

    python manage.py test warehouse.tests_credit_note_guards --settings=config.sqlite_test_settings
"""

from unittest.mock import patch

from django.test import override_settings

from warehouse.tests_credit_note_extras import BASE, CN_STAGE, _CreditNoteExtrasTestCase, inbox_state

POSTED = [{"doc_entry": 41202, "doc_num": 626092650, "doc_date": "2026-09-16"}]


@patch("warehouse.views_credit_note_approval.SAPClient")
class DecisionGuardTests(_CreditNoteExtrasTestCase):
    def _decide(self, sap, body=None, **inbox):
        client = sap.return_value
        client.credit_note_approval_stage.return_value = dict(CN_STAGE)
        client.approval_inbox_stage.return_value = inbox_state(**inbox)
        client.decide_credit_note_approval.return_value = {
            "message": "Credit note approved in SAP.", "signed_as": "USER37",
        }
        return self.client.patch(
            f"{BASE}75424/status/", {"status": "APPROVED", **(body or {})}, format="json"
        )

    def test_approving_over_a_posted_duplicate_is_refused(self, sap):
        response = self._decide(sap, posted_duplicates=POSTED)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "DUPLICATE_CREDIT_NOTE")
        self.assertEqual(response.data["duplicate_of"], POSTED)
        self.assertIn("#626092650", response.data["error"])
        sap.return_value.decide_credit_note_approval.assert_not_called()
        # The check is asked for on an approval, and it fails closed in the reader.
        self.assertTrue(sap.return_value.approval_inbox_stage.call_args.kwargs["with_duplicates"])

    def test_the_approver_can_confirm_a_separate_credit_note(self, sap):
        response = self._decide(sap, {"confirm_duplicate": True}, posted_duplicates=POSTED)
        self.assertEqual(response.status_code, 200)
        sap.return_value.decide_credit_note_approval.assert_called_once()

    def test_rejecting_a_duplicate_needs_no_confirmation(self, sap):
        client = sap.return_value
        client.credit_note_approval_stage.return_value = dict(CN_STAGE)
        client.approval_inbox_stage.return_value = inbox_state(posted_duplicates=POSTED)
        client.decide_credit_note_approval.return_value = {"message": "ok", "signed_as": "USER37"}
        response = self.client.patch(
            f"{BASE}75424/status/",
            {"status": "REJECTED", "rejection_reason": "Duplicate of 626092650"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(client.decide_credit_note_approval.call_args.kwargs["approve"])

    def test_a_request_whose_draft_sap_already_decided_is_refused(self, sap):
        """OWDD still says W; the draft does not. The queue's own reader sees W."""
        response = self._decide(sap, status="APPROVED", stale_pending=True)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "STALE_REQUEST")
        sap.return_value.decide_credit_note_approval.assert_not_called()

    def test_the_duplicate_guard_runs_before_the_draft_is_changed(self, sap):
        self._decide(sap, {"without_qty_posting": True}, posted_duplicates=POSTED)
        sap.return_value.set_draft_lines_without_qty_posting.assert_not_called()
        sap.return_value.verify_approval_signer.assert_not_called()

    def test_the_approvers_comment_rides_in_sap_remarks(self, sap):
        self._decide(sap, {"approval_comment": "Checked against invoice 4411"})
        remarks = sap.return_value.decide_credit_note_approval.call_args.kwargs["remarks"]
        self.assertIn("Checked against invoice 4411", remarks)
        self.assertIn("Honey Singh", remarks)

    def test_no_comment_keeps_the_old_remarks(self, sap):
        self._decide(sap)
        remarks = sap.return_value.decide_credit_note_approval.call_args.kwargs["remarks"]
        self.assertEqual(remarks, "Approved by Honey Singh (Factory app)")

    @override_settings(SAP_APPROVER_CREDENTIALS={"JIVO_OIL": {}})
    def test_without_a_stored_password_a_typed_one_signs(self, sap):
        response = self._decide(sap, {"sap_password": "typed-once"})
        self.assertEqual(response.status_code, 200)
        kwargs = sap.return_value.decide_credit_note_approval.call_args.kwargs
        self.assertEqual(kwargs["password"], "typed-once")
        self.assertEqual(kwargs["approver"], "USER37")
        self.assertNotIn("typed-once", str(response.data))

    @override_settings(SAP_APPROVER_CREDENTIALS={"JIVO_OIL": {}})
    def test_without_either_password_nothing_is_sent(self, sap):
        response = self._decide(sap)
        self.assertEqual(response.status_code, 400)
        sap.return_value.decide_credit_note_approval.assert_not_called()

    @override_settings(SAP_APPROVER_CREDENTIALS={"JIVO_OIL": {}})
    def test_a_typed_password_still_signs_the_without_qty_check(self, sap):
        self._decide(sap, {"sap_password": "typed-once", "without_qty_posting": True})
        self.assertEqual(
            sap.return_value.verify_approval_signer.call_args.kwargs["password"], "typed-once"
        )

    def test_a_typed_password_cannot_sign_someone_elses_stage(self, sap):
        client = sap.return_value
        client.credit_note_approval_stage.return_value = {**CN_STAGE, "approver_code": "USER24"}
        response = self.client.patch(
            f"{BASE}75424/status/", {"status": "APPROVED", "sap_password": "x"}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        client.decide_credit_note_approval.assert_not_called()


@override_settings(SAP_APPROVER_CREDENTIALS={"JIVO_OIL": {}})
@patch("warehouse.views_credit_note_approval.SAPClient")
class ListWithoutStoredPasswordTests(_CreditNoteExtrasTestCase):
    def test_the_callers_own_row_is_decidable_by_typing_a_password(self, sap):
        sap.return_value.list_credit_note_approvals.return_value = [
            {**CN_STAGE, "family": "AR"},
        ]
        row = self.client.get(BASE).data[0]
        self.assertTrue(row["can_decide"])
        self.assertFalse(row["credentials_configured"])


DRAFT_DETAIL = {
    "attachment_entry": 9001,
    "base_documents": [
        {"type_label": "A/R Invoice", "doc_num": 4411, "base_entry": 812, "attachment_entry": 9002},
        # The same entry twice (two lines from one invoice) is listed once.
        {"type_label": "A/R Invoice", "doc_num": 4411, "base_entry": 812, "attachment_entry": 9002},
        {"type_label": "Delivery", "doc_num": 77, "base_entry": 55, "attachment_entry": None},
    ],
}
FILES = [{"line": 1, "file_name": "invoice.pdf"}]


@patch("warehouse.views_credit_note_approval.document_services")
@patch("warehouse.views_credit_note_approval.SAPClient")
class AttachmentTests(_CreditNoteExtrasTestCase):
    def test_lists_this_credit_notes_files_and_its_base_documents(self, sap, docs):
        sap.return_value.approval_inbox_stage.return_value = inbox_state()
        docs.document_detail.return_value = DRAFT_DETAIL
        docs.attachment_lines.return_value = FILES
        response = self.client.get(f"{BASE}75424/attachments/")
        self.assertEqual(response.status_code, 200)
        sources = response.data["sources"]
        self.assertEqual([s["abs_entry"] for s in sources], [9001, 9002])
        self.assertEqual(sources[1]["label"], "A/R Invoice #4411")
        self.assertEqual(sources[0]["lines"], FILES)
        # Read through the draft, by its entry, in this company.
        args = docs.document_detail.call_args.args
        self.assertEqual((args[0], args[2]), ("JIVO_OIL", 57198))

    def test_downloads_a_file_of_this_credit_note(self, sap, docs):
        sap.return_value.approval_inbox_stage.return_value = inbox_state()
        docs.document_detail.return_value = DRAFT_DETAIL
        docs.fetch_attachment.return_value = {"data": b"%PDF", "file_name": "invoice.pdf", "content_type": "application/pdf"}
        from django.http import HttpResponse

        docs.served_file_response.return_value = HttpResponse(b"%PDF", content_type="application/pdf")
        response = self.client.get(f"{BASE}75424/attachments/9002/1/download/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(docs.fetch_attachment.call_args.args[2:], (9002, 1))

    def test_refuses_an_attachment_of_some_other_document(self, sap, docs):
        """The queue's view right must not become a key to every file in SAP."""
        sap.return_value.approval_inbox_stage.return_value = inbox_state()
        docs.document_detail.return_value = DRAFT_DETAIL
        response = self.client.get(f"{BASE}75424/attachments/1234/1/download/")
        self.assertEqual(response.status_code, 404)
        docs.fetch_attachment.assert_not_called()

    def test_a_family_you_cannot_see_shows_no_files(self, sap, docs):
        from django.contrib.auth.models import Permission

        from accounts.models import User

        for codename in ("can_view_ap_credit_note_approval", "can_approve_ap_credit_note"):
            self.user.user_permissions.remove(
                Permission.objects.get(content_type__app_label="warehouse", codename=codename)
            )
        self.client.force_authenticate(user=User.objects.get(pk=self.user.pk))
        sap.return_value.approval_inbox_stage.return_value = inbox_state(object_type="19")
        self.assertEqual(self.client.get(f"{BASE}75424/attachments/").status_code, 403)
        docs.document_detail.assert_not_called()

    def test_no_draft_means_no_files(self, sap, docs):
        sap.return_value.approval_inbox_stage.return_value = inbox_state(draft_entry=None)
        response = self.client.get(f"{BASE}75424/attachments/")
        self.assertEqual(response.data["sources"], [])
        docs.document_detail.assert_not_called()


@patch("warehouse.views_credit_note_approval.SAPClient")
class ListSearchTests(_CreditNoteExtrasTestCase):
    """Searched in SAP (SAP Portal's filters), not over the rows the page loaded."""

    def test_the_filters_reach_the_reader(self, sap):
        sap.return_value.list_credit_note_approvals.return_value = []
        response = self.client.get(
            BASE,
            {"status": "approved", "party": "ilahi", "doc_num": "6260", "code": "75424",
             "date_from": "2026-09-01", "date_to": "2026-09-30", "limit": "50", "offset": "100"},
        )
        self.assertEqual(response.status_code, 200)
        kwargs = sap.return_value.list_credit_note_approvals.call_args.kwargs
        self.assertEqual(kwargs["status"], "APPROVED")
        self.assertEqual(kwargs["party"], "ilahi")
        self.assertEqual(kwargs["doc_num"], "6260")
        self.assertEqual(kwargs["code"], 75424)
        self.assertEqual(str(kwargs["date_from"]), "2026-09-01")
        self.assertEqual(str(kwargs["date_to"]), "2026-09-30")
        self.assertEqual((kwargs["limit"], kwargs["offset"]), (50, 100))

    def test_nonsense_is_refused_before_sap_is_asked(self, sap):
        for params in ({"doc_num": "12a"}, {"date_from": "2026-09-30", "date_to": "2026-09-01"},
                       {"limit": "5000"}, {"status": "MAYBE"}):
            self.assertEqual(self.client.get(BASE, params).status_code, 400, params)
        sap.return_value.list_credit_note_approvals.assert_not_called()


class ReaderFilterTests(_CreditNoteExtrasTestCase):
    """The reader binds every value; nothing typed is formatted into the SQL."""

    def _sql(self, **filters):
        from sap_client.hana.credit_note_approval_reader import HanaCreditNoteApprovalReader

        reader = HanaCreditNoteApprovalReader.__new__(HanaCreditNoteApprovalReader)
        with patch.object(HanaCreditNoteApprovalReader, "_query", return_value=[]) as query:
            reader.list_approvals(status="PENDING", family="ALL", limit=30, **filters)
        return query.call_args.args

    def test_values_are_bound_in_placeholder_order(self, *_):
        sql, params = self._sql(
            party="o'brien", doc_num="62", code=7, date_from="2026-09-01", date_to="2026-09-30", offset=60,
        )
        self.assertNotIn("o'brien", sql.lower())
        self.assertEqual(params, ("%O'BRIEN%", "%O'BRIEN%", "%62%", 7, "2026-09-01", "2026-09-30"))
        self.assertEqual(sql.count("?"), len(params))
        self.assertIn("OFFSET 60", sql)

    def test_no_filters_reads_as_before(self, *_):
        sql, params = self._sql()
        self.assertEqual(params, ())
        self.assertIn("OFFSET 0", sql)
