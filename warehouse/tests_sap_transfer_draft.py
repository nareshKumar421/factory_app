"""Tests for adding an inventory-transfer draft SAP approved but never posted.

What is risky here is not what the app sends — it sends nothing but a DocEntry —
but *when* it sends it. Adding a draft moves stock, so a draft that is already
added, still pending approval, rejected or cancelled must be refused before the
call, and a timeout must never be reported as a failure while SAP has in fact
committed the move.

SAP is mocked throughout — nothing here reaches HANA or the Service Layer.
"""

from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from company.models import Company, UserCompany, UserRole
from rest_framework.exceptions import PermissionDenied
from sap_client.exceptions import SAPConnectionError, SAPValidationError
from warehouse.models_manager import UserWarehouse
from warehouse.models_sap_draft_post import SapTransferDraftPost
from warehouse.services.sap_transfer_draft_service import (
    SapTransferDraftError,
    SapTransferDraftService,
)

LIST_URL = "/api/v1/warehouse/sap-transfer-drafts/"


def post_url(draft_entry):
    return f"/api/v1/warehouse/sap-transfer-drafts/{draft_entry}/post/"


def _line(line_num=0, item="FG0000323", quantity="94512", **overrides):
    line = {
        "line_num": line_num,
        "item_code": item,
        "item_name": f"{item} NAME",
        "quantity": quantity,
        "uom": "PCS",
        "from_warehouse": "BH-PF",
        "to_warehouse": "BH-FG",
        "source_stock": "282672",
        "short": False,
        "batch_managed": True,
        "batches_allocated": 1,
        "batches_missing": False,
    }
    line.update(overrides)
    return line


def _draft(**overrides):
    draft = {
        "draft_entry": 16130,
        "doc_num": 926678060,
        "doc_date": "2026-09-11",
        "from_warehouse": "BH-PF",
        "to_warehouse": "BH-FG",
        "comments": None,
        "journal_memo": "Inventory Transfers -",
        "branch_id": 2,
        "created_by": "ATUL SHARMA",
        "age_days": 1,
        "approval_status": "Y",
        "approval_label": "approved",
        "doc_status": "O",
        "cancelled": False,
        "obj_type": "67",
        "is_transfer": True,
        "is_open": True,
        "is_approved": True,
        "lines": [_line()],
    }
    draft.update(overrides)
    return draft


class _Harness(TestCase):
    """A service whose SAP client is mocked."""

    def setUp(self):
        self.company = Company.objects.create(code="JIVO_BEVERAGES", name="Jivo Bev")
        role = UserRole.objects.create(name="Store")
        self.user = User.objects.create_user(
            email="atul@example.com", full_name="Atul",
            employee_code="E-34", password="x",
        )
        UserCompany.objects.create(user=self.user, company=self.company, role=role)
        # Adding moves stock out of BH-PF, so that is the assignment that counts.
        UserWarehouse.objects.create(
            user=self.user, company=self.company, warehouse_code="BH-PF"
        )

        patcher = patch(
            "warehouse.services.sap_transfer_draft_service.SAPClient"
        )
        self.SAPClient = patcher.start()
        self.addCleanup(patcher.stop)
        self.sap = self.SAPClient.return_value
        self.sap.get_transfer_draft.return_value = _draft()
        self.sap.list_unposted_transfer_drafts.return_value = [_draft()]
        # Not yet added; the post reads it back afterwards.
        self.sap.stock_transfer_for_draft.side_effect = [
            None, {"doc_entry": 5340, "doc_num": 926678061, "doc_date": "2026-09-12"},
        ]

    def service(self):
        return SapTransferDraftService(self.company.code, self.user)


class AddRulesTests(_Harness):
    def test_an_approved_draft_is_added_and_read_back(self):
        result = self.service().post_draft(16130)
        self.sap.add_stock_transfer_draft.assert_called_once_with(16130)
        self.assertEqual(result["doc_num"], 926678061)
        self.assertEqual(result["lines_moved"], 1)
        self.assertFalse(result["confirmed_by_readback"])

    def test_a_draft_still_waiting_for_approval_is_refused_by_name(self):
        self.sap.get_transfer_draft.return_value = _draft(
            approval_status="W", approval_label="waiting for approval",
            is_approved=False,
        )
        with self.assertRaises(SapTransferDraftError) as ctx:
            self.service().post_draft(16130)
        self.assertIn("waiting for approval", str(ctx.exception))
        self.sap.add_stock_transfer_draft.assert_not_called()

    def test_a_rejected_draft_is_refused(self):
        self.sap.get_transfer_draft.return_value = _draft(
            approval_status="N", approval_label="rejected", is_approved=False,
        )
        with self.assertRaises(SapTransferDraftError):
            self.service().post_draft(16130)
        self.sap.add_stock_transfer_draft.assert_not_called()

    def test_a_cancelled_draft_is_refused(self):
        self.sap.get_transfer_draft.return_value = _draft(cancelled=True)
        with self.assertRaises(SapTransferDraftError) as ctx:
            self.service().post_draft(16130)
        self.assertIn("cancelled", str(ctx.exception))
        self.sap.add_stock_transfer_draft.assert_not_called()

    def test_a_closed_draft_is_refused(self):
        """SAP closes a draft to DocStatus 'C' the moment it is added."""
        self.sap.get_transfer_draft.return_value = _draft(
            doc_status="C", is_open=False
        )
        with self.assertRaises(SapTransferDraftError) as ctx:
            self.service().post_draft(16130)
        self.assertIn("already closed", str(ctx.exception))
        self.sap.add_stock_transfer_draft.assert_not_called()

    def test_a_draft_of_another_document_type_is_refused(self):
        """ODRF holds every document's drafts; only object 67 belongs here."""
        self.sap.get_transfer_draft.return_value = _draft(
            obj_type="13", is_transfer=False
        )
        with self.assertRaises(SapTransferDraftError) as ctx:
            self.service().post_draft(16130)
        self.assertIn("not an inventory transfer", str(ctx.exception))
        self.sap.add_stock_transfer_draft.assert_not_called()

    def test_a_missing_draft_is_refused(self):
        self.sap.get_transfer_draft.return_value = None
        with self.assertRaises(SapTransferDraftError):
            self.service().post_draft(16130)

    def test_an_already_added_draft_names_the_document_instead_of_posting_twice(self):
        self.sap.stock_transfer_for_draft.side_effect = None
        self.sap.stock_transfer_for_draft.return_value = {
            "doc_entry": 5328, "doc_num": 926678060, "doc_date": "2026-09-11",
        }
        with self.assertRaises(SapTransferDraftError) as ctx:
            self.service().post_draft(16130)
        self.assertIn("926678060", str(ctx.exception))
        self.sap.add_stock_transfer_draft.assert_not_called()


class TimeoutTests(_Harness):
    def test_a_timeout_that_SAP_actually_committed_is_reported_as_posted(self):
        """SAP does not roll back on a timeout; retrying would move it twice."""
        self.sap.stock_transfer_for_draft.side_effect = [
            None,  # the pre-check
            {"doc_entry": 5340, "doc_num": 926678061, "doc_date": "2026-09-12"},
        ]
        self.sap.add_stock_transfer_draft.side_effect = SAPConnectionError(
            "SAP did not answer within 180s"
        )
        result = self.service().post_draft(16130)
        self.assertEqual(result["doc_num"], 926678061)
        self.assertTrue(result["confirmed_by_readback"])
        self.assertEqual(
            SapTransferDraftPost.objects.get().result,
            SapTransferDraftPost.RESULT_POSTED,
        )

    def test_a_timeout_with_nothing_posted_still_fails(self):
        self.sap.stock_transfer_for_draft.side_effect = [None, None]
        self.sap.add_stock_transfer_draft.side_effect = SAPConnectionError(
            "SAP did not answer within 180s"
        )
        with self.assertRaises(SAPConnectionError):
            self.service().post_draft(16130)
        self.assertEqual(
            SapTransferDraftPost.objects.get().result,
            SapTransferDraftPost.RESULT_FAILED,
        )


class AuditTests(_Harness):
    def test_a_successful_add_is_recorded_against_the_person_who_clicked(self):
        self.service().post_draft(16130)
        audit = SapTransferDraftPost.objects.get()
        self.assertEqual(audit.created_by, self.user)
        self.assertEqual(audit.draft_entry, 16130)
        self.assertEqual(audit.doc_num, 926678061)
        self.assertEqual(audit.result, SapTransferDraftPost.RESULT_POSTED)

    def test_a_refusal_by_SAP_is_recorded_too(self):
        """The notification procedure runs on the add, never at draft time."""
        self.sap.add_stock_transfer_draft.side_effect = SAPValidationError(
            "(-4014) No batch numbers were allocated"
        )
        with self.assertRaises(SAPValidationError):
            self.service().post_draft(16130)
        audit = SapTransferDraftPost.objects.get()
        self.assertEqual(audit.result, SapTransferDraftPost.RESULT_FAILED)
        self.assertIn("-4014", audit.error_message)


class WarehouseScopeTests(_Harness):
    def test_a_manager_of_the_destination_only_cannot_add_it(self):
        """Adding sends stock out, so it is the source warehouse's call."""
        UserWarehouse.objects.all().delete()
        UserWarehouse.objects.create(
            user=self.user, company=self.company, warehouse_code="BH-FG"
        )
        with self.assertRaises(PermissionDenied):
            self.service().post_draft(16130)
        self.sap.add_stock_transfer_draft.assert_not_called()

    def test_every_source_on_a_multi_source_draft_must_be_managed(self):
        self.sap.get_transfer_draft.return_value = _draft(
            lines=[_line(0), _line(1, item="FG0000324", from_warehouse="BH-VG")]
        )
        with self.assertRaises(PermissionDenied) as ctx:
            self.service().post_draft(16130)
        self.assertIn("BH-VG", str(ctx.exception))

    def test_the_list_marks_rows_this_caller_cannot_add(self):
        self.sap.list_unposted_transfer_drafts.return_value = [
            _draft(),
            _draft(draft_entry=14592, from_warehouse="BH-VG", to_warehouse="DL-INT",
                   lines=[_line(0, from_warehouse="BH-VG", to_warehouse="DL-INT")]),
        ]
        rows = self.service().list_awaiting_add()
        self.assertTrue(rows[0]["can_post"])
        self.assertFalse(rows[1]["can_post"])
        self.assertIn("BH-VG", rows[1]["blocked_reason"])

    def test_a_cross_branch_draft_is_addable(self):
        """Unlike a request, a draft already says how the stock travels."""
        UserWarehouse.objects.create(
            user=self.user, company=self.company, warehouse_code="BH-VG"
        )
        self.sap.get_transfer_draft.return_value = _draft(
            draft_entry=14592, from_warehouse="BH-VG", to_warehouse="DL-INT",
            lines=[_line(0, from_warehouse="BH-VG", to_warehouse="DL-INT")],
        )
        self.service().post_draft(14592)
        self.sap.add_stock_transfer_draft.assert_called_once_with(14592)


class WarningTests(_Harness):
    def test_a_source_warehouse_now_short_of_stock_is_flagged(self):
        """These drafts sit for months; the stock behind them can walk away."""
        self.sap.list_unposted_transfer_drafts.return_value = [
            _draft(lines=[_line(source_stock="12", short=True)])
        ]
        rows = self.service().list_awaiting_add()
        self.assertTrue(rows[0]["can_post"])
        self.assertIn("no longer holds enough stock", rows[0]["warnings"][0])

    def test_a_batch_managed_line_with_no_allocation_is_flagged(self):
        self.sap.list_unposted_transfer_drafts.return_value = [
            _draft(lines=[_line(batches_allocated=0, batches_missing=True)])
        ]
        rows = self.service().list_awaiting_add()
        self.assertIn("batch-managed", rows[0]["warnings"][0])

    def test_a_healthy_draft_carries_no_warnings(self):
        self.assertEqual(self.service().list_awaiting_add()[0]["warnings"], [])


class EndpointTests(_Harness):
    """The two endpoints, including who each is open to."""

    def setUp(self):
        super().setUp()
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.api.credentials(HTTP_COMPANY_CODE=self.company.code)

    def _grant(self, codename):
        self.user.user_permissions.add(
            Permission.objects.get(codename=codename)
        )
        self.user = User.objects.get(pk=self.user.pk)
        self.api.force_authenticate(self.user)

    def test_listing_needs_the_view_permission(self):
        self.assertEqual(self.api.get(LIST_URL).status_code, 403)
        self._grant("can_view_transfer_request")
        response = self.api.get(LIST_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[0]["draft_entry"], 16130)

    def test_adding_needs_the_posting_permission_not_just_the_view_one(self):
        self._grant("can_view_transfer_request")
        self.assertEqual(self.api.post(post_url(16130)).status_code, 403)
        self._grant("can_post_transfer_to_sap")
        response = self.api.post(post_url(16130))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["doc_num"], 926678061)

    def test_a_refused_add_comes_back_as_400_with_SAPs_own_words(self):
        self._grant("can_post_transfer_to_sap")
        self.sap.add_stock_transfer_draft.side_effect = SAPValidationError(
            "Quantity falls into negative inventory (-10)"
        )
        response = self.api.post(post_url(16130))
        self.assertEqual(response.status_code, 400)
        self.assertIn("negative inventory", response.data["error"])

    def test_SAP_being_unreachable_is_a_502_not_a_400(self):
        self._grant("can_post_transfer_to_sap")
        self.sap.stock_transfer_for_draft.side_effect = [None, None]
        self.sap.add_stock_transfer_draft.side_effect = SAPConnectionError(
            "Unable to connect to SAP Service Layer."
        )
        self.assertEqual(self.api.post(post_url(16130)).status_code, 502)
