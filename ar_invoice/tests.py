"""Tests for the A/R invoice module.

Endpoint tests mock :class:`sap_client.client.SAPClient` at the service
boundary (no HANA / Service Layer network). Writer tests mock ``requests`` to
check the payload SAP receives and the approval-draft detection.
"""
import json
import tempfile
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from sap_client.hana.ar_invoice_print_reader import HanaARInvoicePrintReader
from sap_client.hana.batch_stock_reader import InsufficientBatchStock
from sap_client.service_layer.ar_invoice_writer import ARInvoiceWriter

from .models import ARInvoiceLine, ARInvoicePosting, ARInvoiceStatus

User = get_user_model()
COMPANY_CODE = "TC001"
CUSTOMER = "CUSTA000123"
BASE = "/api/v1/ar-invoices/"

TEMP_MEDIA = tempfile.mkdtemp(prefix="ar_invoice_test_media_")


class _Context:
    service_layer = {
        "base_url": "https://sap.test:50000",
        "company_db": "TESTDB",
        "username": "sl_user",
        "password": "sl_pass",
    }


def _response(status_code, headers=None, body=None):
    resp = mock.Mock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.json.return_value = body if body is not None else {}
    resp.text = json.dumps(body or {})
    return resp


@mock.patch("sap_client.service_layer.delivery_note_writer.ServiceLayerSession")
class ARInvoiceWriterTests(TestCase):
    def writer(self):
        return ARInvoiceWriter(_Context())

    @mock.patch("sap_client.service_layer.delivery_note_writer.requests.post")
    def test_create_posts_to_invoices(self, post, session):
        post.return_value = _response(201, body={"DocEntry": 91000, "DocNum": 1726090001})
        result = self.writer().create({"CardCode": CUSTOMER, "DocumentLines": []})
        self.assertEqual(result["DocEntry"], 91000)
        self.assertIn("/b1s/v2/Invoices", post.call_args[0][0])

    @mock.patch("sap_client.service_layer.delivery_note_writer.requests.post")
    def test_create_detects_approval_draft(self, post, session):
        post.return_value = _response(
            404,
            headers={"Location": "https://sap.test:50000/b1s/v2/Drafts(61000)"},
            body={"error": {"message": {"value": "approval"}}},
        )
        result = self.writer().create({"CardCode": CUSTOMER, "DocumentLines": []})
        self.assertTrue(result["pending_approval"])
        self.assertEqual(result["draft_entry"], 61000)

    @mock.patch("sap_client.service_layer.delivery_note_writer.requests.post")
    def test_save_draft_uses_oinvoices_object_code(self, post, session):
        post.return_value = _response(204)
        self.writer().save_draft_to_document(61000)
        payload = post.call_args[1]["json"]
        self.assertEqual(
            payload, {"Document": {"DocEntry": 61000, "DocObjectCode": "oInvoices"}}
        )

    @mock.patch("sap_client.service_layer.delivery_note_writer.requests.patch")
    def test_patch_draft_targets_the_draft(self, patch, session):
        patch.return_value = _response(204)
        self.writer().patch_draft(
            61000,
            {"DocumentLines": [{"LineNum": 0, "BatchNumbers": [{"BatchNumber": "B1", "Quantity": 5.0}]}]},
        )
        self.assertIn("/b1s/v2/Drafts(61000)", patch.call_args[0][0])


def _so_line(doc_entry, line_num, branch=2, **over):
    row = {
        "so_doc_entry": doc_entry,
        "so_doc_num": 2026090000 + doc_entry,
        "so_doc_date": "2026-09-01",
        "so_customer_ref": "PO-778",
        "so_comments": "",
        "branch_id": branch,
        "customer_name": "ONENESS TRADERS",
        "line_num": line_num,
        "item_code": "FG00042",
        "description": "JIVO CANOLA 1L (20 PCS)",
        "open_qty": 100.0,
        "price": 120.0,
        "open_total": 12000.0,
        "tax_code": "IGST@5",
        "warehouse_code": "GP-FG",
        "uom": "PCS",
    }
    row.update(over)
    return row


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class ARInvoiceEndpointTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Co", code=COMPANY_CODE)
        cls.role = UserRole.objects.create(name="Billing")

        cls.creator = User.objects.create_user(
            email="ar-creator@example.com", password="pass12345",
            full_name="AR Creator", employee_code="AR-CRT",
        )
        cls.viewer = User.objects.create_user(
            email="ar-viewer@example.com", password="pass12345",
            full_name="AR Viewer", employee_code="AR-VIEW",
        )
        for user in (cls.creator, cls.viewer):
            UserCompany.objects.create(
                user=user, company=cls.company, role=cls.role, is_active=True
            )

        cls.creator.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="ar_invoice",
                codename__in=["view_ar_invoice_posting", "create_ar_invoice_posting"],
            )
        )
        cls.viewer.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="ar_invoice",
                codename="view_ar_invoice_posting",
            )
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.creator)
        patcher = mock.patch("ar_invoice.services.SAPClient")
        self.SAPClient = patcher.start()
        self.addCleanup(patcher.stop)
        # The lookup views (customers/items/line-defaults) build their own
        # client from the views module — share the same mock.
        views_patcher = mock.patch("ar_invoice.views.SAPClient", self.SAPClient)
        views_patcher.start()
        self.addCleanup(views_patcher.stop)
        self.sap = self.SAPClient.return_value
        self.sap.open_so_lines_for_invoicing.return_value = [
            _so_line(7001, 0),
            _so_line(7001, 1),
            _so_line(7002, 0),
        ]
        # Items are not batch-managed unless a test says otherwise.
        self.sap.batch_managed_flags.return_value = {}
        # Direct-sale prerequisites.
        self.sap.get_customer.return_value = {
            "customer_code": "CUSTA000893", "customer_name": "CASH SALE PB",
        }
        self.sap.get_warehouse_branches.return_value = {"GP-FG": 2, "DL-J3": 1}
        self.sap.return_variety_codes.return_value = {"FG0000030": "MUSTARD"}

    def _create_body(self, lines=None, **over):
        body = {
            "customer_code": CUSTOMER,
            "customer_ref": "PO-778",
            "doc_date": "2026-09-03",
            "lines": lines or [
                {"so_doc_entry": 7001, "line_num": 0},
                {"so_doc_entry": 7001, "line_num": 1},
            ],
        }
        body.update(over)
        return body

    def _post_create(self, body=None):
        return self.client.post(
            f"{BASE}invoices/", body or self._create_body(),
            format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )

    # ── open lines ──────────────────────────────────────────────────────────
    def test_open_lines_requires_customer(self):
        resp = self.client.get(f"{BASE}open-so-lines/", HTTP_COMPANY_CODE=COMPANY_CODE)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_open_lines_excludes_locally_claimed(self):
        posting = ARInvoicePosting.objects.create(
            company=self.company, customer_code=CUSTOMER,
            branch_id=2, status=ARInvoiceStatus.PENDING_APPROVAL,
        )
        ARInvoiceLine.objects.create(
            ar_invoice=posting, base_entry=7001, base_line=0, line_total="12000.00"
        )
        resp = self.client.get(
            f"{BASE}open-so-lines/?customer_code={CUSTOMER}",
            HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        keys = {(r["so_doc_entry"], r["line_num"]) for r in resp.json()}
        self.assertNotIn((7001, 0), keys)
        self.assertIn((7001, 1), keys)

    # ── create ──────────────────────────────────────────────────────────────
    def test_create_routed_to_approval(self):
        self.sap.create_ar_invoice.return_value = {
            "DocEntry": None, "DocNum": "", "pending_approval": True,
            "draft_entry": 61100,
        }
        self.sap.ar_draft_state.return_value = {
            "doc_status": "O", "wdd_status": "W", "doc_total": 25200.0,
            "approval_code": 75001, "approval_status": "W", "reject_remarks": None,
        }
        resp = self._post_create()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        data = resp.json()
        self.assertEqual(data["status"], "PENDING_APPROVAL")
        self.assertEqual(data["sap_draft_entry"], 61100)
        self.assertEqual(data["sap_approval_code"], 75001)
        self.assertEqual(len(data["lines"]), 2)

        payload = self.sap.create_ar_invoice.call_args[0][0]
        self.assertEqual(payload["CardCode"], CUSTOMER)
        self.assertEqual(payload["NumAtCard"], "PO-778")
        self.assertEqual(payload["BPL_IDAssignedToInvoice"], 2)
        self.assertEqual(payload["DocType"], "dDocument_Items")
        self.assertEqual(
            payload["DocumentLines"],
            [
                {"BaseType": 17, "BaseEntry": 7001, "BaseLine": 0},
                {"BaseType": 17, "BaseEntry": 7001, "BaseLine": 1},
            ],
        )

    def test_create_allocates_batches_for_managed_items(self):
        # SAP validates batch selection (-4014) BEFORE diverting to a draft, so
        # the create payload itself must carry the FIFO allocation.
        self.sap.batch_managed_flags.return_value = {"FG00042": True}
        self.sap.allocate_batches_fifo.return_value = [
            {"BatchNumber": "B240901", "Quantity": 100.0},
        ]
        self.sap.create_ar_invoice.return_value = {
            "DocEntry": None, "DocNum": "", "pending_approval": True,
            "draft_entry": 61101,
        }
        self.sap.ar_draft_state.return_value = {
            "doc_status": "O", "wdd_status": "W", "doc_total": 12000.0,
            "approval_code": 75002, "approval_status": "W", "reject_remarks": None,
        }
        resp = self._post_create(self._create_body(lines=[
            {"so_doc_entry": 7001, "line_num": 0},
        ]))
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

        self.sap.allocate_batches_fifo.assert_called_once()
        args = self.sap.allocate_batches_fifo.call_args[0]
        self.assertEqual(args[0], "FG00042")
        self.assertEqual(args[1], "GP-FG")
        payload = self.sap.create_ar_invoice.call_args[0][0]
        self.assertEqual(
            payload["DocumentLines"],
            [{
                "BaseType": 17, "BaseEntry": 7001, "BaseLine": 0,
                "BatchNumbers": [{"BatchNumber": "B240901", "Quantity": 100.0}],
            }],
        )

    def test_create_batch_shortfall_is_clean_validation_error(self):
        self.sap.batch_managed_flags.return_value = {"FG00042": True}
        self.sap.allocate_batches_fifo.side_effect = InsufficientBatchStock(
            "FG00042 in GP-FG holds 40, need 100"
        )
        resp = self._post_create(self._create_body(lines=[
            {"so_doc_entry": 7001, "line_num": 0},
        ]))
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.sap.create_ar_invoice.assert_not_called()
        posting = ARInvoicePosting.objects.get()
        self.assertEqual(posting.status, ARInvoiceStatus.FAILED)
        self.assertIn("holds 40", posting.error_message)

    def test_create_posted_directly_when_no_template_matches(self):
        self.sap.create_ar_invoice.return_value = {
            "DocEntry": 91001, "DocNum": 1726090002, "DocTotal": 25200.0,
        }
        resp = self._post_create()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertEqual(resp.json()["status"], "POSTED")

    def test_create_rejects_mixed_branches(self):
        self.sap.open_so_lines_for_invoicing.return_value = [
            _so_line(7001, 0, branch=1),
            _so_line(7002, 0, branch=2),
        ]
        resp = self._post_create(self._create_body(lines=[
            {"so_doc_entry": 7001, "line_num": 0},
            {"so_doc_entry": 7002, "line_num": 0},
        ]))
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.sap.create_ar_invoice.assert_not_called()
        self.assertFalse(ARInvoicePosting.objects.exists())

    def test_create_rejects_line_not_open(self):
        resp = self._post_create(self._create_body(lines=[
            {"so_doc_entry": 9999, "line_num": 0},
        ]))
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.sap.create_ar_invoice.assert_not_called()

    # ── direct (cash) sale ──────────────────────────────────────────────────
    def _direct_body(self, lines=None, **over):
        body = {
            "customer_code": "CUSTA000893",
            "direct_lines": lines or [
                {"item_code": "FG0000030", "description": "MUSTARD KACHI GHANI 1L",
                 "quantity": "9", "unit_price": "133.3333", "tax_code": "CG+SG@5",
                 "warehouse_code": "GP-FG"},
            ],
        }
        body.update(over)
        return body

    def test_direct_sale_builds_free_lines_with_cost_center(self):
        self.sap.create_ar_invoice.return_value = {
            "DocEntry": None, "DocNum": "", "pending_approval": True,
            "draft_entry": 61200,
        }
        self.sap.ar_draft_state.return_value = {
            "doc_status": "O", "wdd_status": "W", "doc_total": 1260.0,
            "approval_code": 75100, "approval_status": "W", "reject_remarks": None,
        }
        resp = self.client.post(
            f"{BASE}invoices/", self._direct_body(),
            format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        data = resp.json()
        self.assertEqual(data["status"], "PENDING_APPROVAL")
        self.assertEqual(data["customer_name"], "CASH SALE PB")
        self.assertEqual(data["branch_id"], 2)
        self.assertEqual(data["selected_total"], "1200.00")

        payload = self.sap.create_ar_invoice.call_args[0][0]
        self.assertEqual(
            payload["DocumentLines"],
            [{
                "ItemCode": "FG0000030",
                "Quantity": Decimal("9.000"),
                "UnitPrice": Decimal("133.3333"),
                "TaxCode": "CG+SG@5",
                "WarehouseCode": "GP-FG",
                "CostingCode": "MUSTARD",
                # SAP's SP demands both the variety and U_SchemeAgst (1310325).
                "U_SchemeAgst": "MUSTARD",
            }],
        )

    def test_direct_sale_rejects_item_without_variety_mapping(self):
        self.sap.return_variety_codes.return_value = {}
        resp = self.client.post(
            f"{BASE}invoices/", self._direct_body(),
            format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("FG0000030", resp.json()["detail"])
        self.sap.create_ar_invoice.assert_not_called()

    def test_direct_sale_allocates_batches_for_managed_items(self):
        self.sap.batch_managed_flags.return_value = {"FG0000030": True}
        self.sap.allocate_batches_fifo.return_value = [
            {"BatchNumber": "B240901", "Quantity": 9.0},
        ]
        self.sap.create_ar_invoice.return_value = {
            "DocEntry": 91500, "DocNum": 1726090500, "DocTotal": 1260.0,
        }
        resp = self.client.post(
            f"{BASE}invoices/", self._direct_body(),
            format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        payload = self.sap.create_ar_invoice.call_args[0][0]
        self.assertEqual(
            payload["DocumentLines"][0]["BatchNumbers"],
            [{"BatchNumber": "B240901", "Quantity": 9.0}],
        )

    def test_direct_sale_rejects_unknown_warehouse(self):
        resp = self.client.post(
            f"{BASE}invoices/",
            self._direct_body(lines=[
                {"item_code": "FG0000030", "quantity": "1", "unit_price": "100",
                 "tax_code": "CG+SG@5", "warehouse_code": "NOPE"},
            ]),
            format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.sap.create_ar_invoice.assert_not_called()

    def test_create_rejects_both_line_kinds(self):
        body = self._direct_body()
        body["lines"] = [{"so_doc_entry": 7001, "line_num": 0}]
        resp = self.client.post(
            f"{BASE}invoices/", body, format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_line_defaults_endpoint(self):
        self.sap.ar_last_sale_defaults.return_value = {
            "FG0000030": {"price": 133.3333, "tax_code": "CG+SG@5"},
        }
        resp = self.client.get(
            f"{BASE}line-defaults/?customer_code=CUSTA000893&item_code=FG0000030",
            HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()["tax_code"], "CG+SG@5")

    def test_viewer_cannot_create(self):
        self.client.force_authenticate(user=self.viewer)
        resp = self._post_create()
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    # ── approval tracking ───────────────────────────────────────────────────
    def _pending_posting(self, status_=ARInvoiceStatus.PENDING_APPROVAL):
        posting = ARInvoicePosting.objects.create(
            company=self.company, customer_code=CUSTOMER,
            customer_name="ONENESS TRADERS", branch_id=2, status=status_,
            sap_draft_entry=61100, sap_approval_code=75001,
            created_by=self.creator,
        )
        ARInvoiceLine.objects.create(
            ar_invoice=posting, base_entry=7001, base_line=0,
            item_code="FG00042", quantity="100.000", line_total="12000.00",
            warehouse_code="GP-FG",
        )
        return posting

    def test_refresh_marks_approved(self):
        posting = self._pending_posting()
        self.sap.ar_invoice_for_draft.return_value = None
        self.sap.ar_draft_state.return_value = {
            "doc_status": "O", "wdd_status": "Y", "doc_total": 12000.0,
            "approval_code": 75001, "approval_status": "Y", "reject_remarks": None,
        }
        resp = self.client.post(
            f"{BASE}invoices/{posting.id}/refresh/", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.json()["status"], "APPROVED")

    def test_refresh_marks_rejected_with_remarks(self):
        posting = self._pending_posting()
        self.sap.ar_invoice_for_draft.return_value = None
        self.sap.ar_draft_state.return_value = {
            "doc_status": "O", "wdd_status": "N", "doc_total": 12000.0,
            "approval_code": 75001, "approval_status": "N",
            "reject_remarks": "Stock mismatch",
        }
        resp = self.client.post(
            f"{BASE}invoices/{posting.id}/refresh/", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.json()["status"], "REJECTED")
        self.assertEqual(resp.json()["approval_remarks"], "Stock mismatch")

    def test_post_draft_allocates_batches_then_posts(self):
        posting = self._pending_posting()
        self.sap.ar_invoice_for_draft.side_effect = [
            None,
            {"doc_entry": 91002, "doc_num": 1726090003, "doc_total": 12600.0},
        ]
        self.sap.ar_draft_state.return_value = {
            "doc_status": "O", "wdd_status": "Y", "doc_total": 12000.0,
            "approval_code": 75001, "approval_status": "Y", "reject_remarks": None,
        }
        self.sap.ar_draft_lines.return_value = [
            {"line_num": 0, "item_code": "FG00042", "quantity": 100.0,
             "warehouse_code": "GP-FG"},
        ]
        self.sap.batch_managed_flags.return_value = {"FG00042": True}
        self.sap.allocate_batches_fifo.return_value = [
            {"BatchNumber": "B240901", "Quantity": 60.0},
            {"BatchNumber": "B240902", "Quantity": 40.0},
        ]

        resp = self.client.post(
            f"{BASE}invoices/{posting.id}/post-draft/", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(resp.json()["status"], "POSTED")
        self.assertEqual(resp.json()["sap_doc_num"], 1726090003)

        self.sap.allocate_batches_fifo.assert_called_once_with(
            "FG00042", "GP-FG", 100.0
        )
        self.sap.update_ar_draft.assert_called_once_with(
            61100,
            {"DocumentLines": [
                {"LineNum": 0, "BatchNumbers": [
                    {"BatchNumber": "B240901", "Quantity": 60.0},
                    {"BatchNumber": "B240902", "Quantity": 40.0},
                ]},
            ]},
        )
        self.sap.save_ar_draft_to_document.assert_called_once_with(61100)

    def test_post_draft_skips_batch_patch_for_unmanaged_items(self):
        posting = self._pending_posting()
        self.sap.ar_invoice_for_draft.side_effect = [
            None,
            {"doc_entry": 91003, "doc_num": 1726090004, "doc_total": 12600.0},
        ]
        self.sap.ar_draft_state.return_value = {
            "doc_status": "O", "wdd_status": "Y", "doc_total": 12000.0,
            "approval_code": 75001, "approval_status": "Y", "reject_remarks": None,
        }
        self.sap.ar_draft_lines.return_value = [
            {"line_num": 0, "item_code": "PM00001", "quantity": 100.0,
             "warehouse_code": "GP-PM"},
        ]
        self.sap.batch_managed_flags.return_value = {"PM00001": False}

        resp = self.client.post(
            f"{BASE}invoices/{posting.id}/post-draft/", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.sap.update_ar_draft.assert_not_called()
        self.sap.save_ar_draft_to_document.assert_called_once_with(61100)

    def test_post_draft_batch_shortfall_keeps_approved(self):
        posting = self._pending_posting()
        self.sap.ar_invoice_for_draft.return_value = None
        self.sap.ar_draft_state.return_value = {
            "doc_status": "O", "wdd_status": "Y", "doc_total": 12000.0,
            "approval_code": 75001, "approval_status": "Y", "reject_remarks": None,
        }
        self.sap.ar_draft_lines.return_value = [
            {"line_num": 0, "item_code": "FG00042", "quantity": 100.0,
             "warehouse_code": "GP-FG"},
        ]
        self.sap.batch_managed_flags.return_value = {"FG00042": True}
        self.sap.allocate_batches_fifo.side_effect = InsufficientBatchStock(
            "FG00042 in GP-FG holds 40, need 100"
        )

        resp = self.client.post(
            f"{BASE}invoices/{posting.id}/post-draft/", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.sap.save_ar_draft_to_document.assert_not_called()
        posting.refresh_from_db()
        self.assertEqual(posting.status, ARInvoiceStatus.APPROVED)
        self.assertIn("holds 40", posting.error_message)

    def test_cancel_failed_record_releases_lines(self):
        posting = self._pending_posting(status_=ARInvoiceStatus.FAILED)
        resp = self.client.post(
            f"{BASE}invoices/{posting.id}/cancel/", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(resp.json()["status"], "CANCELLED")

        # The SO line the record claimed is offered again.
        self.sap.open_so_lines_for_invoicing.return_value = [_so_line(7001, 0)]
        resp = self.client.get(
            f"{BASE}open-so-lines/?customer_code={CUSTOMER}",
            HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        keys = {(r["so_doc_entry"], r["line_num"]) for r in resp.json()}
        self.assertIn((7001, 0), keys)

    def test_cancel_refused_once_in_sap(self):
        posting = self._pending_posting()  # PENDING_APPROVAL — draft exists in SAP
        resp = self.client.post(
            f"{BASE}invoices/{posting.id}/cancel/", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_post_draft_refuses_unapproved(self):
        posting = self._pending_posting()
        self.sap.ar_invoice_for_draft.return_value = None
        self.sap.ar_draft_state.return_value = {
            "doc_status": "O", "wdd_status": "W", "doc_total": 12000.0,
            "approval_code": 75001, "approval_status": "W", "reject_remarks": None,
        }
        resp = self.client.post(
            f"{BASE}invoices/{posting.id}/post-draft/", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.sap.save_ar_draft_to_document.assert_not_called()


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class ARInvoicePrintEndpointTests(APITestCase):
    """GET .../print/ — SAP's TAX INVOICE, as data."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Print Co", code=COMPANY_CODE)
        cls.other_company = Company.objects.create(name="Other Co", code="TC002")
        role = UserRole.objects.create(name="Billing")
        cls.viewer = User.objects.create_user(
            email="ar-print@example.com", password="pass12345",
            full_name="AR Printer", employee_code="AR-PRN",
        )
        UserCompany.objects.create(
            user=cls.viewer, company=cls.company, role=role, is_active=True
        )
        # Deliberately view-only: printing a bill the warehouse already raised
        # must not require the permission to raise one.
        cls.viewer.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="ar_invoice",
                codename="view_ar_invoice_posting",
            )
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.viewer)
        patcher = mock.patch("ar_invoice.services.SAPClient")
        self.addCleanup(patcher.stop)
        self.sap = patcher.start().return_value

    def _posted(self, **over):
        fields = {
            "company": self.company,
            "customer_code": CUSTOMER,
            "branch_id": 2,
            "status": ARInvoiceStatus.POSTED,
            "sap_doc_entry": 79774,
            "sap_doc_num": 626090225,
        }
        fields.update(over)
        return ARInvoicePosting.objects.create(**fields)

    def _print(self, posting):
        return self.client.get(
            f"{BASE}invoices/{posting.id}/print/", HTTP_COMPANY_CODE=COMPANY_CODE
        )

    def test_print_returns_sap_payload(self):
        self.sap.ar_invoice_print.return_value = {"doc_num": 626090225, "lines": []}
        posting = self._posted()

        resp = self._print(posting)

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["doc_num"], 626090225)
        self.assertEqual(resp.data["posting_id"], posting.id)
        self.sap.ar_invoice_print.assert_called_once_with(79774)

    def test_print_refused_before_the_invoice_reaches_sap(self):
        """An approval draft has no number, no tax and no date to print."""
        posting = self._posted(
            status=ARInvoiceStatus.PENDING_APPROVAL, sap_doc_entry=None, sap_doc_num=None
        )

        resp = self._print(posting)

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("not been posted", resp.data["detail"])
        self.sap.ar_invoice_print.assert_not_called()

    def test_print_reports_an_invoice_sap_no_longer_has(self):
        self.sap.ar_invoice_print.return_value = None
        posting = self._posted()

        resp = self._print(posting)

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("626090225", resp.data["detail"])

    def test_print_scoped_to_the_company(self):
        posting = self._posted(company=self.other_company)

        resp = self._print(posting)

        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.sap.ar_invoice_print.assert_not_called()


class ARInvoicePrintReaderRuleTests(TestCase):
    """The SAP-specific arithmetic the printed sheet depends on.

    These are the rules transcribed from ``CRYSTAL_AR_INVOICE_ITEMS``; they are
    tested directly because getting one wrong produces a sheet that looks right
    and disagrees with the customer's copy.
    """

    def split(self, qty, factor2, factor3=1, pack_msr=""):
        return HanaARInvoicePrintReader._split(
            Decimal(str(qty)), Decimal(str(factor2)), Decimal(str(factor3)), pack_msr
        )

    def test_boxed_item_splits_into_whole_boxes_and_a_remainder(self):
        self.assertEqual(self.split(45, 20), (2, Decimal("5")))

    def test_sal_factor2_of_one_means_loose_not_one_box_each(self):
        """500 pieces of an unboxed SKU print as "0 Box 500 PCS", not 500 boxes."""
        self.assertEqual(self.split(500, 1), (0, Decimal("500")))

    def test_exact_boxes_leave_nothing_loose(self):
        self.assertEqual(self.split(100, 20), (5, Decimal("0")))

    def test_sal_factor3_counts_the_line_itself_as_boxes(self):
        self.assertEqual(self.split(7, 1, factor3=12), (7, Decimal("0")))

    def test_drums_are_never_loose(self):
        self.assertEqual(self.split(3, 1, pack_msr="Drum"), (0, Decimal("0")))

    def test_fssai_licence_follows_the_branch(self):
        fssai = HanaARInvoicePrintReader._fssai
        self.assertEqual(fssai(2, "BH-BT"), "10015064000541")
        self.assertEqual(fssai(1, "DL-J3"), "13322999001306")
        self.assertEqual(fssai(3, "PB-JP"), "12123999000082")
        self.assertEqual(fssai(9, "XX"), "10014011001626")

    def test_one_warehouse_overrides_its_branch_licence(self):
        self.assertEqual(HanaARInvoicePrintReader._fssai(2, "BH-LR"), "10824999000237")

    def test_tax_label_keeps_saps_own_blemish(self):
        """SAP prints "CGST@2.5.00 %"; a tidied label would not match the copy
        the customer holds."""
        reader = HanaARInvoicePrintReader.__new__(HanaARInvoicePrintReader)
        summary = reader._tax_summary([
            {"sta_type": "-100", "code": "CGST@2.5", "rate": Decimal("2.5"),
             "amount": Decimal("22.02"), "line_num": 0, "on_item_line": True},
            {"sta_type": "-110", "code": "SGST@2.5", "rate": Decimal("2.5"),
             "amount": Decimal("22.02"), "line_num": 0, "on_item_line": True},
        ])
        self.assertEqual([row["label"] for row in summary],
                         ["CGST@2.5.00 %", "SGST@2.5.00 %"])

    def test_utgst_prints_in_the_sgst_slot(self):
        reader = HanaARInvoicePrintReader.__new__(HanaARInvoicePrintReader)
        summary = reader._tax_summary([
            {"sta_type": "-150", "code": "UTGST@9", "rate": Decimal("9"),
             "amount": Decimal("81"), "line_num": 0, "on_item_line": True},
        ])
        self.assertEqual(summary, [{"label": "UTGST@9.00 %", "amount": "81"}])

    def test_freight_tax_is_totalled_but_not_charged_to_an_item(self):
        """RelateType 3 is tax on a freight line: it belongs in the tax total,
        but attributing it to the item line would overstate that HSN's tax."""
        reader = HanaARInvoicePrintReader.__new__(HanaARInvoicePrintReader)
        taxes = [
            {"sta_type": "-100", "code": "CGST@9", "rate": Decimal("9"),
             "amount": Decimal("90"), "line_num": 0, "on_item_line": True},
            {"sta_type": "-100", "code": "CGST@9", "rate": Decimal("9"),
             "amount": Decimal("18"), "line_num": 0, "on_item_line": False},
        ]
        lines = [{"hsn": "1514.19.20", "line_num": 0, "taxable_value": "1000",
                  "litres": "0", "gross_weight": "0", "category": ""}]

        self.assertEqual(reader._tax_summary(taxes)[0]["amount"], "108")
        self.assertEqual(reader._hsn_summary(lines, taxes)[0]["total_tax"], "90")


class ARApprovalAutoPostTests(APITestCase):
    """Approving OUR A/R draft on the existing warehouse page also posts it."""

    APPROVALS = "/api/v1/invoice-approvals/invoices/"
    WH = "GP-FG"

    @classmethod
    def setUpTestData(cls):
        from django.contrib.auth.models import Group
        from warehouse.models_manager import UserWarehouse

        cls.company = Company.objects.create(name="Test Co", code=COMPANY_CODE)
        cls.role = UserRole.objects.create(name="Approver")
        cls.approver = User.objects.create_user(
            email="ar-approver@example.com", password="pass12345",
            full_name="AR Approver", employee_code="ARA-1",
        )
        UserCompany.objects.create(
            user=cls.approver, company=cls.company, role=cls.role, is_active=True
        )
        group, _ = Group.objects.get_or_create(name="Invoice Approval")
        group.permissions.add(
            *Permission.objects.filter(
                content_type__app_label="invoice_approval",
                codename__in=["view_invoice", "approve_invoice"],
            )
        )
        cls.approver.groups.add(group)
        UserWarehouse.objects.create(
            user=cls.approver, company=cls.company, warehouse_code=cls.WH
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.approver)
        view_patcher = mock.patch("invoice_approval.views.SAPClient")
        self.view_sap = view_patcher.start().return_value
        self.addCleanup(view_patcher.stop)
        self.view_sap.invoice_approval_warehouses.return_value = {self.WH}

        service_patcher = mock.patch("ar_invoice.services.SAPClient")
        self.service_sap = service_patcher.start().return_value
        self.addCleanup(service_patcher.stop)

    def test_approve_decides_and_auto_posts_own_ar_draft(self):
        posting = ARInvoicePosting.objects.create(
            company=self.company, customer_code=CUSTOMER,
            branch_id=2, status=ARInvoiceStatus.PENDING_APPROVAL,
            sap_draft_entry=61200, sap_approval_code=75100,
        )
        self.view_sap.decide_invoice_approval.return_value = {
            "message": "Invoice approved in SAP."
        }
        self.service_sap.ar_invoice_for_draft.side_effect = [
            None,
            {"doc_entry": 91100, "doc_num": 1726090100, "doc_total": 25200.0},
        ]
        self.service_sap.ar_draft_state.return_value = {
            "doc_status": "O", "wdd_status": "Y", "doc_total": 25200.0,
            "approval_code": 75100, "approval_status": "Y", "reject_remarks": None,
        }
        self.service_sap.ar_draft_lines.return_value = []

        resp = self.client.patch(
            f"{self.APPROVALS}75100/status/",
            {"status": "APPROVED", "party_name": "ONENESS TRADERS"},
            format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        data = resp.json()
        self.assertEqual(data["posting_status"], "POSTED")
        self.assertEqual(data["sap_doc_num"], 1726090100)
        self.service_sap.save_ar_draft_to_document.assert_called_once_with(61200)

        posting.refresh_from_db()
        self.assertEqual(posting.status, ARInvoiceStatus.POSTED)

    def test_approve_foreign_draft_unchanged(self):
        self.view_sap.decide_invoice_approval.return_value = {
            "message": "Invoice approved in SAP."
        }
        resp = self.client.patch(
            f"{self.APPROVALS}79999/status/",
            {"status": "APPROVED"}, format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertNotIn("posting_status", resp.json())
        self.service_sap.save_ar_draft_to_document.assert_not_called()
