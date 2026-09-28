"""The request's draft in full, and its attachments (SAP Portal's Document,
TDS, GL and Attachments tabs), for someone on the request.

    python manage.py test sap_approvals.tests_document --settings=config.sqlite_test_settings
"""

from unittest.mock import patch

from django.http import HttpResponse
from rest_framework import status

from sap_approvals.tests import BASE, SapApprovalsTestCase, _row

STAGES = [{"step_code": 20, "user_code": "USER37", "status": "PENDING", "is_current": True}]
DOCUMENT = {
    "kind": "marketing",
    "attachment_entry": 9001,
    "base_documents": [{"type_label": "A/R Invoice", "doc_num": 4411, "base_entry": 812, "attachment_entry": 9002}],
    "lines": [{"line_num": 0, "item_code": "FG1"}],
}


@patch("sap_approvals.views.document_services")
@patch("sap_approvals.views.SAPClient")
class DocumentApiTests(SapApprovalsTestCase):
    def _on_request(self, sap, **overrides):
        sap.return_value.approval_inbox_detail.return_value = _row(stages=STAGES, lines=[], **overrides)

    def test_the_draft_is_read_through_the_document_browser(self, sap, docs):
        self._on_request(sap)
        docs.document_detail.return_value = DOCUMENT
        docs.attachment_sources.return_value = [{"label": "A/R Credit Note", "abs_entry": 9001}]
        response = self.client.get(f"{BASE}requests/75424/document/", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["type"]["key"], "Drafts")
        self.assertEqual(response.data["document"], DOCUMENT)
        self.assertEqual(docs.document_detail.call_args.args[2], 57198)

    def test_a_payment_draft_is_read_from_opdf(self, sap, docs):
        self._on_request(sap, object_type="46", object_type_label="Outgoing Payment")
        docs.document_detail.return_value = {"kind": "payment_draft", "base_documents": []}
        docs.attachment_sources.return_value = []
        response = self.client.get(f"{BASE}requests/75424/document/", **self.headers)
        self.assertEqual(response.data["type"]["key"], "PaymentDrafts")

    def test_someone_not_on_the_request_cannot_read_its_draft(self, sap, docs):
        sap.return_value.approval_inbox_detail.return_value = _row(
            stages=[{"step_code": 20, "user_code": "USER26", "status": "PENDING", "is_current": True}],
            lines=[],
        )
        response = self.client.get(f"{BASE}requests/75424/document/", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        docs.document_detail.assert_not_called()

    def test_a_draft_sap_no_longer_holds_is_404(self, sap, docs):
        self._on_request(sap)
        docs.document_detail.return_value = None
        self.assertEqual(
            self.client.get(f"{BASE}requests/75424/document/", **self.headers).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_attachment_files_are_served_only_for_this_requests_entries(self, sap, docs):
        self._on_request(sap)
        docs.document_detail.return_value = DOCUMENT
        docs.attachment_sources.return_value = [
            {"label": "A/R Credit Note", "abs_entry": 9001},
            {"label": "A/R Invoice #4411", "abs_entry": 9002},
        ]
        docs.attachment_lines.return_value = [{"line": 1, "file_name": "scan.pdf"}]
        docs.fetch_attachment.return_value = {"data": b"%PDF", "file_name": "scan.pdf", "content_type": "application/pdf"}
        docs.served_file_response.return_value = HttpResponse(b"%PDF", content_type="application/pdf")

        lines = self.client.get(f"{BASE}requests/75424/attachments/9002/", **self.headers)
        self.assertEqual(lines.data["lines"], [{"line": 1, "file_name": "scan.pdf"}])
        file = self.client.get(f"{BASE}requests/75424/attachments/9002/1/download/", **self.headers)
        self.assertEqual(file.status_code, status.HTTP_200_OK)
        self.assertEqual(docs.fetch_attachment.call_args.args[2:], (9002, 1))

        # Another document's entry: the inbox right is not a key to every file in SAP.
        for url in (f"{BASE}requests/75424/attachments/1234/", f"{BASE}requests/75424/attachments/1234/1/download/"):
            self.assertEqual(self.client.get(url, **self.headers).status_code, status.HTTP_404_NOT_FOUND, url)
        self.assertEqual(docs.fetch_attachment.call_count, 1)

    def test_the_inbox_right_is_needed(self, sap, docs):
        self.user.user_permissions.clear()
        from django.contrib.auth import get_user_model

        self.client.force_authenticate(get_user_model().objects.get(pk=self.user.pk))
        self.assertEqual(
            self.client.get(f"{BASE}requests/75424/document/", **self.headers).status_code,
            status.HTTP_403_FORBIDDEN,
        )


class AttachmentSourcesTests(SapApprovalsTestCase):
    def test_own_entry_first_then_each_base_documents_once(self):
        from sap_documents.services import attachment_sources

        doc = {
            "attachment_entry": 9001,
            "base_documents": [
                {"type_label": "A/R Invoice", "doc_num": 4411, "attachment_entry": 9002},
                {"type_label": "A/R Invoice", "doc_num": 4411, "attachment_entry": 9002},
                {"type_label": "Delivery", "doc_num": 77, "attachment_entry": None},
                {"type_label": "Return", "base_entry": 5, "attachment_entry": 9001},
            ],
        }
        self.assertEqual(
            attachment_sources(doc, "A/R Credit Note"),
            [{"label": "A/R Credit Note", "abs_entry": 9001}, {"label": "A/R Invoice #4411", "abs_entry": 9002}],
        )
