"""Tests for the supporting documents an A/R invoice is raised with.

The create endpoint takes files alongside the invoice (multipart: a ``data``
JSON part plus one ``attachments`` part per file). Each file becomes an
``ARInvoiceAttachment`` row, is pushed to SAP's Attachments2 collection BEFORE
the invoice is posted, and the resulting ``AbsoluteEntry`` rides on the
document as ``AttachmentEntry`` — an attachment cannot be hung off an invoice
after the fact, so the upload has to happen first or not at all.

Kept in its own module rather than in ``tests.py``: the file it would join is
being edited for an unrelated feature.
"""
import json
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPValidationError

from .models import ARInvoiceAttachment, ARInvoicePosting, ARInvoiceStatus

User = get_user_model()
COMPANY_CODE = "TC001"
CUSTOMER = "CUSTA000123"
BASE = "/api/v1/ar-invoices/"

TEMP_MEDIA = tempfile.mkdtemp(prefix="ar_invoice_attachment_test_media_")

SO_LINE = {
    "so_doc_entry": 7001,
    "so_doc_num": 2026097001,
    "so_doc_date": "2026-09-01",
    "so_customer_ref": "PO-778",
    "so_comments": "",
    "branch_id": 2,
    "customer_name": "ONENESS TRADERS",
    "line_num": 0,
    "item_code": "FG00042",
    "description": "JIVO CANOLA 1L (20 PCS)",
    "open_qty": 100.0,
    "price": 120.0,
    "open_total": 12000.0,
    "tax_code": "IGST@5",
    "warehouse_code": "GP-FG",
    "uom": "PCS",
}


def _pdf(name="po-778.pdf", body=b"%PDF-1.4 signed order"):
    return SimpleUploadedFile(name, body, content_type="application/pdf")


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class ARInvoiceAttachmentTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Co", code=COMPANY_CODE)
        cls.role = UserRole.objects.create(name="Billing")
        cls.creator = User.objects.create_user(
            email="ar-attach@example.com", password="pass12345",
            full_name="AR Creator", employee_code="AR-ATT",
        )
        UserCompany.objects.create(
            user=cls.creator, company=cls.company, role=cls.role, is_active=True
        )
        cls.creator.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="ar_invoice",
                codename__in=["view_ar_invoice_posting", "create_ar_invoice_posting"],
            )
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.creator)
        patcher = mock.patch("ar_invoice.services.SAPClient")
        self.SAPClient = patcher.start()
        self.addCleanup(patcher.stop)
        self.sap = self.SAPClient.return_value
        self.sap.open_so_lines_for_invoicing.return_value = [dict(SO_LINE)]
        self.sap.batch_managed_flags.return_value = {}
        self.sap.upload_attachment.return_value = {"AbsoluteEntry": 4210}
        self.sap.create_ar_invoice.return_value = {
            "DocEntry": 91001, "DocNum": 1726090002, "DocTotal": 12600.0,
        }

    def _post(self, files, **over):
        body = {
            "customer_code": CUSTOMER,
            "doc_date": "2026-09-03",
            "lines": [{"so_doc_entry": 7001, "line_num": 0}],
        }
        body.update(over)
        return self.client.post(
            f"{BASE}invoices/",
            {"data": json.dumps(body), "attachments": files},
            format="multipart",
            HTTP_COMPANY_CODE=COMPANY_CODE,
        )

    def test_file_is_uploaded_to_sap_and_carried_on_the_invoice(self):
        resp = self._post([_pdf()])
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

        self.sap.upload_attachment.assert_called_once()
        self.assertEqual(
            self.sap.upload_attachment.call_args[1]["filename"], "po-778.pdf"
        )
        # The document has to be born with the attachment entry: SAP has no way
        # to bolt one onto a posted invoice afterwards.
        payload = self.sap.create_ar_invoice.call_args[0][0]
        self.assertEqual(payload["AttachmentEntry"], 4210)

        attachment = ARInvoiceAttachment.objects.get()
        self.assertEqual(attachment.original_filename, "po-778.pdf")
        self.assertEqual(attachment.sap_absolute_entry, 4210)
        self.assertEqual(attachment.sap_attachment_status, "LINKED")
        self.assertEqual(attachment.uploaded_by, self.creator)
        # And the response the screen renders lists it.
        listed = resp.json()["attachments"]
        self.assertEqual([a["original_filename"] for a in listed], ["po-778.pdf"])
        self.assertTrue(listed[0]["file_url"])

    def test_several_files_ride_on_one_attachment_entry(self):
        # SAP holds one attachment record per document with a line per file, so
        # the second file is added to the first's AbsoluteEntry, not uploaded
        # as a second record the invoice could not reference.
        resp = self._post([_pdf(), _pdf("weighment.pdf", b"%PDF-1.4 slip")])
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

        self.sap.upload_attachment.assert_called_once()
        self.sap.add_line_to_existing_attachment.assert_called_once()
        self.assertEqual(
            self.sap.add_line_to_existing_attachment.call_args[1]["absolute_entry"],
            4210,
        )
        payload = self.sap.create_ar_invoice.call_args[0][0]
        self.assertEqual(payload["AttachmentEntry"], 4210)
        self.assertEqual(
            list(
                ARInvoiceAttachment.objects.order_by("id").values_list(
                    "sap_absolute_entry", flat=True
                )
            ),
            [4210, 4210],
        )

    def test_invoice_raised_without_files_carries_no_attachment_entry(self):
        resp = self.client.post(
            f"{BASE}invoices/",
            {
                "customer_code": CUSTOMER,
                "lines": [{"so_doc_entry": 7001, "line_num": 0}],
            },
            format="json",
            HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.sap.upload_attachment.assert_not_called()
        self.assertNotIn("AttachmentEntry", self.sap.create_ar_invoice.call_args[0][0])

    def test_approval_draft_keeps_the_file_marked_uploaded(self):
        self.sap.create_ar_invoice.return_value = {
            "DocEntry": None, "DocNum": "", "pending_approval": True,
            "draft_entry": 61100,
        }
        self.sap.ar_draft_state.return_value = {
            "doc_status": "O", "wdd_status": "W", "doc_total": 12600.0,
            "approval_code": 75001, "approval_status": "W", "reject_remarks": None,
        }
        resp = self._post([_pdf()])
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertEqual(resp.json()["status"], "PENDING_APPROVAL")
        # Uploaded, not LINKED: the draft is not an OINV document yet.
        self.assertEqual(
            ARInvoiceAttachment.objects.get().sap_attachment_status, "UPLOADED"
        )

    def test_a_refused_upload_fails_the_invoice_before_it_reaches_sap(self):
        self.sap.upload_attachment.side_effect = SAPValidationError(
            "Attachment folder is not defined"
        )
        resp = self._post([_pdf()])
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        # No invoice posted without the paper it was supposed to carry.
        self.sap.create_ar_invoice.assert_not_called()
        posting = ARInvoicePosting.objects.get()
        self.assertEqual(posting.status, ARInvoiceStatus.FAILED)
        attachment = ARInvoiceAttachment.objects.get()
        self.assertEqual(attachment.sap_attachment_status, "FAILED")
        self.assertIn("Attachment folder", attachment.sap_error_message)

    def test_retry_reuses_the_file_already_in_sap(self):
        self.sap.upload_attachment.side_effect = SAPValidationError("SAP hiccup")
        self.assertEqual(
            self._post([_pdf()]).status_code, status.HTTP_400_BAD_REQUEST
        )
        posting = ARInvoicePosting.objects.get()
        attachment = ARInvoiceAttachment.objects.get()
        # Pretend the first attempt got the file in before the invoice failed.
        ARInvoiceAttachment.objects.filter(pk=attachment.pk).update(
            sap_absolute_entry=4210, sap_attachment_status="UPLOADED"
        )
        self.sap.upload_attachment.side_effect = None
        self.sap.upload_attachment.reset_mock()

        resp = self.client.post(
            f"{BASE}invoices/{posting.id}/post/", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        # Uploading it a second time would leave an orphan file in SAP.
        self.sap.upload_attachment.assert_not_called()
        self.assertEqual(
            self.sap.create_ar_invoice.call_args[0][0]["AttachmentEntry"], 4210
        )
        self.assertEqual(
            ARInvoiceAttachment.objects.get().sap_attachment_status, "LINKED"
        )
