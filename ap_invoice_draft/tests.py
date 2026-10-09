"""A/P invoice drafts from GRPOs: the service and the API.

The fixture is real: GRPO 27481 (SSY Containers, three POs merged into one
truck) as JIVO_OIL_HANADB had it on 2026-10-08. SAP and HANA are patched at the
seams ``services`` imports them from.
"""

import copy
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPDataError, SAPOutcomeUnknown, SAPValidationError

from .models import APInvoiceDraft, SapDraftStatus
from .services import APInvoiceDraftService

D = Decimal


def _line(num, item, qty, price, whs="BH-PM"):
    return {
        "line_num": num, "item_code": item, "description": item,
        "quantity": D(qty), "price": D(price), "warehouse": whs, "is_open": True,
    }


SSY_GRPO = {
    "doc_entry": 27481, "doc_num": "2026106506",
    "doc_date": date(2026, 10, 3), "tax_date": date(2026, 10, 2),
    "reference": "26-27/1979", "vendor_code": "VENDA000936", "vendor_name": "SSY CONTAINERS PVT LTD",
    "total": D("284077"), "branch_id": 2,
    "is_open": True, "is_cancelled": False, "is_service": False, "gst_type": "GA",
    "lines": [
        _line(0, "PM0000920", "2000", "34.45"),
        _line(1, "PM0000825", "2334", "37.88"),
        _line(2, "PM0000914", "3240", "34.95"),
    ],
}


def _pdf(name="SSY-1979.pdf"):
    return SimpleUploadedFile(name, b"%PDF-1.7 test bill", content_type="application/pdf")


class FakeReader:
    """GRPOReader with one GRPO and whatever open A/P drafts the test sets."""

    def __init__(self, grpo=SSY_GRPO, drafts=None):
        self.grpo_data = copy.deepcopy(grpo)
        self.drafts = drafts or {}

    def __call__(self, company_code):
        return self

    def grpo(self, doc_entry):
        return copy.deepcopy(self.grpo_data) if doc_entry == self.grpo_data["doc_entry"] else None

    def open_ap_drafts(self, entries):
        return {e: self.drafts[e] for e in entries if e in self.drafts}

    def open_grpos(self, search=""):
        g = self.grpo_data
        return [{
            "doc_entry": g["doc_entry"], "doc_num": g["doc_num"], "doc_date": g["doc_date"],
            "reference": g["reference"], "vendor_code": g["vendor_code"], "vendor_name": g["vendor_name"],
            "total": g["total"], "comments": "", "warehouses": ["BH-PM"],
            "sap_draft_entries": self.drafts.get(g["doc_entry"], []),
        }]

    def ap_invoice_series(self, posting_date, branch_id, gst_type):
        self.series_asked = (posting_date, branch_id, gst_type)
        return None if getattr(self, "no_series", False) else (3686, "HR_G1026")


class FakeSAP:
    def __init__(self):
        self.payloads = []
        self.create_error = None
        self.upload_error = None

    def __call__(self, company_code):
        return self

    def upload_attachment(self, file_path, filename):
        if self.upload_error:
            raise self.upload_error
        return {"AbsoluteEntry": 181900}

    def create_ap_invoice_draft(self, payload):
        self.payloads.append(payload)
        if self.create_error:
            raise self.create_error
        return {"DocEntry": 59001, "DocNum": 59001}


class ServiceTestCase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.user = get_user_model().objects.create(email="store@jivo.in", full_name="Store")
        self.reader = FakeReader()
        self.sap = FakeSAP()
        for patch in (
            mock.patch("ap_invoice_draft.services.GRPOReader", self.reader),
            mock.patch("ap_invoice_draft.services.SAPClient", self.sap),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.service = APInvoiceDraftService(self.company)


class CreateTests(ServiceTestCase):
    def test_makes_the_sap_draft_as_accounts_copy_would(self):
        entry = self.service.create(27481, _pdf(), self.user)

        self.assertEqual(entry.sap_status, SapDraftStatus.CREATED)
        self.assertEqual(entry.sap_draft_entry, 59001)
        self.assertFalse(entry.sap_draft_adopted)
        self.assertEqual(entry.grpo_reference, "26-27/1979")
        self.assertTrue(entry.entry_no.startswith("APD-"))
        payload = self.sap.payloads[0]
        self.assertEqual(payload["CardCode"], "VENDA000936")
        self.assertEqual(payload["NumAtCard"], "26-27/1979")
        self.assertEqual(payload["DocDate"], "2026-10-03")
        self.assertEqual(payload["TaxDate"], "2026-10-02")
        self.assertEqual(payload["BPL_IDAssignedToInvoice"], 2)
        self.assertEqual(payload["Series"], 3686)
        self.assertEqual(payload["GSTTransactionType"], "gsttrantyp_GSTTaxInvoice")
        self.assertEqual(self.reader.series_asked, (date(2026, 10, 3), 2, "GA"))
        self.assertEqual(payload["AttachmentEntry"], 181900)
        self.assertEqual(
            payload["DocumentLines"],
            [{"BaseType": 20, "BaseEntry": 27481, "BaseLine": n} for n in range(3)],
        )
        self.assertIn("Based On Goods Receipt PO 2026106506.", payload["Comments"])

    def test_links_the_draft_sap_already_has(self):
        # On 2026-10-08 accounts had already made draft 58620 for this GRPO by hand.
        self.reader.drafts = {27481: [58620]}
        entry = self.service.create(27481, _pdf(), self.user)
        self.assertEqual(entry.sap_status, SapDraftStatus.CREATED)
        self.assertEqual(entry.sap_draft_entry, 58620)
        self.assertTrue(entry.sap_draft_adopted)
        self.assertEqual(self.sap.payloads, [])

    def test_a_refusal_keeps_the_entry_with_sap_reason(self):
        self.sap.create_error = SAPValidationError("Period is locked")
        entry = self.service.create(27481, _pdf(), self.user)
        self.assertEqual(entry.sap_status, SapDraftStatus.FAILED)
        self.assertIn("Period is locked", entry.sap_error)
        self.assertIsNone(entry.sap_draft_entry)
        self.assertEqual(APInvoiceDraft.objects.count(), 1)

    def test_sap_v2_refusals_read_as_a_sentence(self):
        self.sap.create_error = SAPValidationError(
            '{\n "error" : {\n "code" : "-4002", "details" : [], "message" : '
            '"To generate this document, first define the numbering series in the Administration module"\n }\n}'
        )
        entry = self.service.create(27481, _pdf(), self.user)
        self.assertEqual(
            entry.sap_error,
            "SAP refused the draft: To generate this document, first define the numbering "
            "series in the Administration module",
        )

    def test_a_lost_answer_is_found_on_retry_not_made_twice(self):
        self.sap.create_error = SAPOutcomeUnknown("timeout")
        entry = self.service.create(27481, _pdf(), self.user)
        self.assertEqual(entry.sap_status, SapDraftStatus.FAILED)
        self.assertIn("looks for the draft", entry.sap_error)

        self.reader.drafts = {27481: [59001]}  # SAP had taken it
        self.service.send_to_sap(entry, self.user)
        entry.refresh_from_db()
        self.assertEqual(entry.sap_status, SapDraftStatus.CREATED)
        self.assertEqual(entry.sap_draft_entry, 59001)
        self.assertEqual(len(self.sap.payloads), 1)

    def test_no_open_series_is_said_before_sap_is_asked(self):
        self.reader.no_series = True
        entry = self.service.create(27481, _pdf(), self.user)
        self.assertEqual(entry.sap_status, SapDraftStatus.FAILED)
        self.assertIn("no open A/P invoice series for branch 2", entry.sap_error)
        self.assertEqual(self.sap.payloads, [])

    def test_an_attachment_failure_still_makes_the_draft(self):
        self.sap.upload_error = SAPDataError("Attachments folder not defined")
        entry = self.service.create(27481, _pdf(), self.user)
        self.assertEqual(entry.sap_status, SapDraftStatus.CREATED)
        self.assertNotIn("AttachmentEntry", self.sap.payloads[0])
        self.assertIn("Attachments folder", entry.sap_attachment_error)

    def test_one_entry_per_grpo(self):
        self.service.create(27481, _pdf(), self.user)
        with self.assertRaisesMessage(ValueError, "already has entry"):
            self.service.create(27481, _pdf(), self.user)

    def test_a_simultaneous_second_entry_is_refused_not_a_500(self):
        from django.db import IntegrityError

        with mock.patch.object(APInvoiceDraftService, "_new_entry", side_effect=IntegrityError("dup")):
            with self.assertRaisesMessage(ValueError, "already has an entry"):
                self.service.create(27481, _pdf(), self.user)

    def test_refuses_what_cannot_be_invoiced(self):
        with self.assertRaisesMessage(ValueError, "no GRPO"):
            self.service.create(1, _pdf(), self.user)
        self.reader.grpo_data["is_open"] = False
        with self.assertRaisesMessage(ValueError, "already invoiced"):
            self.service.create(27481, _pdf(), self.user)
        self.reader.grpo_data.update({"is_open": True, "is_service": True})
        with self.assertRaisesMessage(ValueError, "service GRPO"):
            self.service.create(27481, _pdf(), self.user)
        with self.assertRaisesMessage(ValueError, "PDF or a photo"):
            self.service.create(27481, _pdf("bill.docx"), self.user)
        self.assertEqual(APInvoiceDraft.objects.count(), 0)


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------

API = "/api/v1/ap-invoice-drafts/"


class APITests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        role = UserRole.objects.create(name="Warehouse")
        self.maker = get_user_model().objects.create(email="maker@jivo.in", full_name="Maker")
        self.viewer = get_user_model().objects.create(email="viewer@jivo.in", full_name="Viewer")
        self.nobody = get_user_model().objects.create(email="nobody@jivo.in", full_name="Nobody")
        for user in (self.maker, self.viewer, self.nobody):
            UserCompany.objects.create(user=user, company=self.company, role=role, is_active=True)

        def grant(user, *codenames):
            user.user_permissions.add(*Permission.objects.filter(
                content_type__app_label="ap_invoice_draft", codename__in=codenames,
            ))

        grant(self.maker, "can_view_ap_invoice_draft", "can_create_ap_invoice_draft")
        grant(self.viewer, "can_view_ap_invoice_draft")
        self.client = APIClient()

    def _as(self, user):
        self.client.force_authenticate(user)
        return self.client

    def _create(self):
        return self._as(self.maker).post(
            API, {"grpo_doc_entry": 27481, "invoice_file": _pdf()},
            format="multipart", HTTP_COMPANY_CODE="JIVO_OIL",
        )

    def test_maker_creates_and_sees_the_sap_draft(self):
        response = self._create()
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual((body["sap_status"], body["sap_draft_entry"]), ("CREATED", 59001))
        self.assertTrue(body["invoice_file_url"].startswith("http://testserver/"))

        listed = self._as(self.viewer).get(API, HTTP_COMPANY_CODE="JIVO_OIL").json()
        self.assertEqual([row["entry_no"] for row in listed], [body["entry_no"]])

    def test_the_picker_lists_open_grpos(self):
        self.reader.drafts = {27481: [58620]}
        response = self._as(self.maker).get(f"{API}grpos/?search=SSY", HTTP_COMPANY_CODE="JIVO_OIL")
        self.assertEqual(response.status_code, 200, response.content)
        [row] = response.json()
        self.assertEqual((row["doc_num"], row["reference"]), ("2026106506", "26-27/1979"))
        self.assertEqual(row["sap_draft_entries"], [58620])
        self.assertEqual(row["entry_no"], "")

    def test_a_refused_draft_is_tried_again(self):
        self.sap.create_error = SAPValidationError("Period is locked")
        entry_id = self._create().json()["id"]
        self.sap.create_error = None
        response = self._as(self.maker).post(f"{API}{entry_id}/send-to-sap/", HTTP_COMPANY_CODE="JIVO_OIL")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["sap_status"], "CREATED")

    def test_viewer_cannot_create_nobody_cannot_see(self):
        response = self._as(self.viewer).post(
            API, {"grpo_doc_entry": 27481, "invoice_file": _pdf()},
            format="multipart", HTTP_COMPANY_CODE="JIVO_OIL",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self._as(self.nobody).get(API, HTTP_COMPANY_CODE="JIVO_OIL").status_code, 403)

    def test_a_bad_grpo_is_a_400(self):
        response = self._as(self.maker).post(
            API, {"grpo_doc_entry": 1, "invoice_file": _pdf()},
            format="multipart", HTTP_COMPANY_CODE="JIVO_OIL",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("no GRPO", response.json()["detail"])
