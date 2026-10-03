"""A bill from a warehouse its raiser does not manage waits for that warehouse.

Anyone may bill from any warehouse. What changes with the warehouse is who has
to agree first: a line from a warehouse the raiser does not manage holds the
whole bill in the app — nothing is sent to SAP — until that warehouse's manager
approves it on the Invoice Approval page, beside the OMS and SAP rows. A Sales
Order bill is held for every warehouse on it, whoever raises it.

SAP is mocked at the same boundaries as ``ar_invoice.tests``.
"""
import tempfile
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPValidationError
from warehouse.models_manager import UserWarehouse

from .models import (
    ARInvoiceLine,
    ARInvoicePosting,
    ARInvoiceStatus,
    ARInvoiceWarehouseApproval,
    ARWarehouseApprovalStatus,
)

User = get_user_model()
COMPANY_CODE = "TC001"
AR = "/api/v1/ar-invoices/"
APPROVALS = "/api/v1/invoice-approvals/app-invoices/"
COUNTER_WH = "GP-FG"
PTD = "BH-PTD"
SC = "BH-SC"
ITEM = "FG0000194"

TEMP_MEDIA = tempfile.mkdtemp(prefix="ar_invoice_whs_approval_media_")


def _line(warehouse, item=ITEM, quantity="6"):
    return {
        "item_code": item, "description": "SOYABEAN OIL 1 LTR POUCH 12 PCS",
        "quantity": quantity, "unit_price": "133.3333", "tax_code": "CG+SG@5",
        "warehouse_code": warehouse,
    }


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class WarehouseApprovalTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Co", code=COMPANY_CODE)
        role = UserRole.objects.create(name="Warehouse")

        def user(email, name, warehouses=(), **flags):
            u = User.objects.create_user(
                email=email, password="pass12345", full_name=name,
                employee_code=email.split("@")[0].upper(), **flags,
            )
            UserCompany.objects.create(user=u, company=cls.company, role=role, is_active=True)
            for code in warehouses:
                UserWarehouse.objects.create(user=u, company=cls.company, warehouse_code=code)
            return u

        ar_perms = Permission.objects.filter(
            content_type__app_label="ar_invoice",
            codename__in=[
                "view_ar_invoice_posting", "create_ar_invoice_posting",
                "create_ar_invoice_from_sales_order",
            ],
        )
        approver_group, _ = Group.objects.get_or_create(name="Invoice Approval")
        approver_group.permissions.add(
            *Permission.objects.filter(
                content_type__app_label="invoice_approval",
                codename__in=["view_invoice", "approve_invoice"],
            )
        )
        view_only = Permission.objects.get(
            content_type__app_label="invoice_approval", codename="view_invoice"
        )

        # Runs the counter at GP-FG — and bills, now and then, from elsewhere.
        cls.counter = user("counter@example.com", "Counter Clerk", [COUNTER_WH])
        cls.counter.user_permissions.add(*ar_perms)
        # A creator assigned no warehouse at all.
        cls.drifter = user("drifter@example.com", "No Warehouse")
        cls.drifter.user_permissions.add(*ar_perms)
        cls.admin = user("admin@example.com", "Admin", is_superuser=True)
        cls.admin.user_permissions.add(*ar_perms)

        cls.ptd_manager = user("ptd@example.com", "PTD Manager", [PTD, "BH-LO"])
        cls.ptd_manager.groups.add(approver_group)
        cls.sc_manager = user("sc@example.com", "SC Manager", [SC])
        cls.sc_manager.groups.add(approver_group)
        # Manages BH-PTD but may only look.
        cls.ptd_viewer = user("ptdview@example.com", "PTD Viewer", [PTD])
        cls.ptd_viewer.user_permissions.add(view_only)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.counter)

        patcher = mock.patch("ar_invoice.services.SAPClient")
        self.sap = patcher.start().return_value
        self.addCleanup(patcher.stop)
        views_patcher = mock.patch("ar_invoice.views.SAPClient")
        views_patcher.start()
        self.addCleanup(views_patcher.stop)
        stock_patcher = mock.patch("invoice_approval.app_bills.SAPClient")
        self.stock_sap = stock_patcher.start().return_value
        self.addCleanup(stock_patcher.stop)
        summary_patcher = mock.patch("dispatch_plans.bill_summary_service.BillSummaryService")
        summary_patcher.start()
        self.addCleanup(summary_patcher.stop)

        self.sap.get_customer.return_value = {
            "customer_code": "CUSTA000025", "customer_name": "HARPREET SINGH CASH SALE",
        }
        self.sap.get_warehouse_branches.return_value = {COUNTER_WH: 2, PTD: 2, SC: 2}
        self.sap.return_variety_codes.return_value = {ITEM: "SOYABEAN"}
        self.sap.batch_managed_flags.return_value = {}
        self.sap.create_ar_invoice.return_value = {
            "DocEntry": 80891, "DocNum": 626090568, "DocTotal": 840.0,
        }
        self.stock_sap.get_fg_warehouse_stock.return_value = [
            {"ItemCode": ITEM, "ItemName": "SOYABEAN OIL 1 LTR POUCH 12 PCS", "OnHand": 781},
        ]

    # ── helpers ─────────────────────────────────────────────────────────────
    def _bill(self, *lines, as_user=None, **over):
        if as_user is not None:
            self.client.force_authenticate(user=as_user)
        body = {"customer_code": "CUSTA000025", "direct_lines": list(lines)}
        body.update(over)
        resp = self.client.post(
            f"{AR}invoices/", body, format="json", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.client.force_authenticate(user=self.counter)
        return resp

    def _held(self, *warehouses):
        resp = self._bill(*[_line(w) for w in warehouses])
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        return ARInvoicePosting.objects.get(pk=resp.json()["id"])

    def _so_bill(self, warehouse, as_user=None):
        """A bill against one open Sales Order line from ``warehouse``."""
        self.sap.open_so_lines_for_invoicing.return_value = [{
            "so_doc_entry": 7001, "so_doc_num": 1726097001, "so_doc_date": "2026-09-01",
            "so_customer_ref": "", "so_comments": "", "branch_id": 2,
            "customer_name": "ONENESS TRADERS", "line_num": 0, "item_code": ITEM,
            "description": "SOYABEAN OIL 1 LTR POUCH 12 PCS", "open_qty": 10.0,
            "price": 133.3333, "open_total": 1333.33, "tax_code": "CG+SG@5",
            "warehouse_code": warehouse, "uom": "PCS",
        }]
        return self._bill(
            as_user=as_user, direct_lines=[],
            customer_code="CUSTA000123", lines=[{"so_doc_entry": 7001, "line_num": 0}],
        )

    def _approval(self, posting, warehouse):
        return posting.warehouse_approvals.get(warehouse_code=warehouse)

    def _decide(self, approval, user, decision="APPROVED", **extra):
        self.client.force_authenticate(user=user)
        resp = self.client.patch(
            f"{APPROVALS}{approval.id}/status/",
            {"status": decision, **extra},
            format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.client.force_authenticate(user=self.counter)
        return resp

    def _get(self, url, user):
        self.client.force_authenticate(user=user)
        resp = self.client.get(url, HTTP_COMPANY_CODE=COMPANY_CODE)
        self.client.force_authenticate(user=self.counter)
        return resp

    # ── raising the bill ────────────────────────────────────────────────────
    def test_a_bill_from_their_own_warehouse_goes_straight_to_sap(self):
        resp = self._bill(_line(COUNTER_WH))
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertEqual(resp.json()["status"], "POSTED")
        self.assertEqual(resp.json()["warehouse_approvals"], [])
        self.sap.create_ar_invoice.assert_called_once()

    def test_a_bill_from_a_warehouse_they_do_not_manage_waits_and_nothing_reaches_sap(self):
        resp = self._bill(_line(PTD))
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        data = resp.json()
        self.assertEqual(data["status"], "AWAITING_MANAGER")
        self.assertIsNone(data["sap_doc_num"])
        self.assertEqual(
            [(a["warehouse_code"], a["status"]) for a in data["warehouse_approvals"]],
            [(PTD, "PENDING")],
        )
        # Named so the counter knows whom to call: an approving manager of BH-PTD,
        # not the one who may only look, and not a superuser.
        self.assertEqual(data["warehouse_approvals"][0]["approvers"], ["PTD Manager"])
        self.sap.create_ar_invoice.assert_not_called()
        self.sap.upload_attachment.assert_not_called()

    def test_a_mixed_bill_waits_only_on_the_warehouse_they_do_not_manage(self):
        posting = self._held(COUNTER_WH, PTD)
        self.assertEqual(
            list(posting.warehouse_approvals.values_list("warehouse_code", flat=True)),
            [PTD],
        )
        self.sap.create_ar_invoice.assert_not_called()

    def test_someone_who_manages_nothing_can_still_bill_but_it_all_waits(self):
        resp = self._bill(_line(COUNTER_WH), as_user=self.drifter)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertEqual(resp.json()["status"], "AWAITING_MANAGER")
        self.assertEqual(
            [a["warehouse_code"] for a in resp.json()["warehouse_approvals"]], [COUNTER_WH]
        )
        self.sap.create_ar_invoice.assert_not_called()

    def test_a_superuser_is_not_held(self):
        resp = self._bill(_line(PTD), as_user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertEqual(resp.json()["status"], "POSTED")

    def test_a_held_bill_cannot_be_pushed_to_sap_with_retry(self):
        posting = self._held(PTD)
        resp = self.client.post(
            f"{AR}invoices/{posting.id}/post/", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(PTD, resp.json()["detail"])
        self.sap.create_ar_invoice.assert_not_called()

    def test_the_raiser_can_cancel_a_held_bill_and_it_leaves_the_approval_list(self):
        posting = self._held(PTD)
        resp = self.client.post(
            f"{AR}invoices/{posting.id}/cancel/", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(resp.json()["status"], "CANCELLED")
        resp = self._get(f"{APPROVALS}?whs={PTD}&status=PENDING", self.ptd_manager)
        self.assertEqual(resp.json(), [])

    def test_a_held_sales_order_bill_keeps_its_lines_claimed(self):
        so_line = {
            "so_doc_entry": 7001, "so_doc_num": 1726097001, "so_doc_date": "2026-09-01",
            "so_customer_ref": "", "so_comments": "", "branch_id": 2,
            "customer_name": "ONENESS TRADERS", "line_num": 0, "item_code": ITEM,
            "description": "SOYABEAN OIL 1 LTR POUCH 12 PCS", "open_qty": 10.0,
            "price": 133.3333, "open_total": 1333.33, "tax_code": "CG+SG@5",
            "warehouse_code": PTD, "uom": "PCS",
        }
        self.sap.open_so_lines_for_invoicing.return_value = [so_line]
        resp = self.client.post(
            f"{AR}invoices/",
            {"customer_code": "CUSTA000123", "lines": [{"so_doc_entry": 7001, "line_num": 0}]},
            format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertEqual(resp.json()["status"], "AWAITING_MANAGER")
        resp = self.client.get(
            f"{AR}open-so-lines/?customer_code=CUSTA000123", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.json(), [])

    # ── Sales Order bills: always held ──────────────────────────────────────
    def test_a_sales_order_bill_from_their_own_warehouse_still_waits(self):
        resp = self._so_bill(COUNTER_WH)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertEqual(resp.json()["status"], "AWAITING_MANAGER")
        self.assertEqual(
            [a["warehouse_code"] for a in resp.json()["warehouse_approvals"]], [COUNTER_WH]
        )
        self.sap.create_ar_invoice.assert_not_called()

    def test_a_superusers_sales_order_bill_waits_too(self):
        resp = self._so_bill(PTD, as_user=self.admin)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        self.assertEqual(resp.json()["status"], "AWAITING_MANAGER")
        self.assertEqual(
            [(a["warehouse_code"], a["approvers"]) for a in resp.json()["warehouse_approvals"]],
            [(PTD, ["PTD Manager"])],
        )
        self.sap.create_ar_invoice.assert_not_called()

    def test_a_held_sales_order_bill_cannot_be_pushed_to_sap_by_its_manager(self):
        # The counter clerk runs GP-FG, so only the Sales Order rule holds this.
        resp = self._so_bill(COUNTER_WH)
        posting = ARInvoicePosting.objects.get(pk=resp.json()["id"])
        resp = self.client.post(
            f"{AR}invoices/{posting.id}/post/", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.sap.create_ar_invoice.assert_not_called()

    def test_approving_a_sales_order_bill_creates_it_in_sap_from_the_order(self):
        resp = self._so_bill(PTD, as_user=self.admin)
        posting = ARInvoicePosting.objects.get(pk=resp.json()["id"])
        [row] = self._get(f"{APPROVALS}?whs={PTD}&status=PENDING", self.ptd_manager).json()
        self.assertEqual(row["so_number"], "1726097001")
        self.assertFalse(row["is_counter_sale"])

        resp = self._decide(self._approval(posting, PTD), self.ptd_manager)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(resp.json()["posting_status"], "POSTED")
        [line] = self.sap.create_ar_invoice.call_args[0][0]["DocumentLines"]
        self.assertEqual((line["BaseType"], line["BaseEntry"], line["BaseLine"]), (17, 7001, 0))

    def test_a_failed_sales_order_bill_without_approval_cannot_be_retried(self):
        # From before the rule: FAILED, from the raiser's own warehouse, no approvals.
        posting = ARInvoicePosting.objects.create(
            company=self.company, customer_code="CUSTA000123", branch_id=2,
            status=ARInvoiceStatus.FAILED, created_by=self.counter,
        )
        ARInvoiceLine.objects.create(
            ar_invoice=posting, base_entry=7001, base_line=0, base_doc_num=1726097001,
            item_code=ITEM, quantity="10", price="133.3333", line_total="1333.33",
            tax_code="CG+SG@5", warehouse_code=COUNTER_WH,
        )
        resp = self.client.post(
            f"{AR}invoices/{posting.id}/post/", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Sales Order", resp.json()["detail"])
        self.sap.create_ar_invoice.assert_not_called()

    # ── the approval page ───────────────────────────────────────────────────
    def test_the_warehouse_manager_sees_it_on_the_invoice_approval_page(self):
        posting = self._held(PTD)
        resp = self._get(f"{APPROVALS}?whs={PTD}&status=PENDING", self.ptd_manager)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        [row] = resp.json()
        self.assertEqual(row["source"], "APP")
        self.assertEqual(row["posting_id"], posting.id)
        self.assertEqual(row["warehouse"], PTD)
        self.assertEqual(row["status"], "PENDING")
        self.assertEqual(row["party_name"], "HARPREET SINGH CASH SALE")
        self.assertEqual(row["created_by"], "Counter Clerk")
        self.assertEqual(row["total_amount"], "800.00")
        self.assertTrue(row["can_decide"])
        [line] = row["invoice_payload"]["DocumentLines"]
        self.assertEqual((line["ItemCode"], line["Quantity"], line["WarehouseCode"]), (ITEM, 6.0, PTD))
        # Live stock in the line's own warehouse, to check against the floor.
        self.assertEqual(row["fg_stock"][0]["warehouse_stock"], 781.0)
        self.stock_sap.get_fg_warehouse_stock.assert_called_once_with([ITEM], PTD)

    def test_a_stock_lookup_failure_costs_the_column_not_the_list(self):
        self._held(PTD)
        self.stock_sap.get_fg_warehouse_stock.side_effect = RuntimeError("HANA down")
        resp = self._get(f"{APPROVALS}?whs={PTD}&status=PENDING", self.ptd_manager)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertIsNone(resp.json()[0]["fg_stock"][0]["warehouse_stock"])

    def test_another_warehouses_manager_cannot_see_it(self):
        self._held(PTD)
        resp = self._get(f"{APPROVALS}?whs={PTD}&status=PENDING", self.sc_manager)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_manager_who_may_only_view_sees_it_but_cannot_decide(self):
        posting = self._held(PTD)
        resp = self._get(f"{APPROVALS}?whs={PTD}&status=PENDING", self.ptd_viewer)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.json()[0]["can_decide"])
        resp = self._decide(self._approval(posting, PTD), self.ptd_viewer)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.sap.create_ar_invoice.assert_not_called()

    def test_the_badge_counts_every_warehouse_the_manager_runs(self):
        # Selected on BH-LO, with the bill waiting at BH-PTD: still counted, and
        # the page can say where.
        self._held(PTD)
        resp = self._get(f"{APPROVALS}pending-count/?whs=BH-LO", self.ptd_manager)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(resp.json()["pending"], 0)
        self.assertEqual(resp.json()["all_warehouses"], 1)
        self.assertEqual(resp.json()["by_warehouse"], {PTD: 1})
        resp = self._get(f"{APPROVALS}pending-count/", self.sc_manager)
        self.assertEqual(resp.json()["all_warehouses"], 0)

    # ── deciding ────────────────────────────────────────────────────────────
    def test_approval_creates_the_bill_in_sap_as_its_raiser(self):
        posting = self._held(PTD)
        resp = self._decide(self._approval(posting, PTD), self.ptd_manager)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(resp.json()["posting_status"], "POSTED")
        self.assertEqual(resp.json()["sap_doc_num"], 626090568)
        self.assertNotIn("warning", resp.json())

        self.sap.create_ar_invoice.assert_called_once()
        payload = self.sap.create_ar_invoice.call_args[0][0]
        self.assertIn("User: Counter Clerk", payload["Comments"])
        self.assertEqual(payload["DocumentLines"][0]["WarehouseCode"], PTD)

        posting.refresh_from_db()
        self.assertEqual(posting.status, ARInvoiceStatus.POSTED)
        self.assertEqual(posting.posted_by, self.counter)
        approval = self._approval(posting, PTD)
        self.assertEqual(approval.status, ARWarehouseApprovalStatus.APPROVED)
        self.assertEqual(approval.decided_by, self.ptd_manager)

        resp = self._get(f"{APPROVALS}?whs={PTD}&status=APPROVED", self.ptd_manager)
        self.assertEqual(resp.json()[0]["doc_num"], 626090568)
        resp = self._get(f"{APPROVALS}{approval.id}/history/", self.ptd_manager)
        self.assertEqual(
            [r["status"] for r in resp.json()], ["RAISED", "APPROVED", "POSTED"]
        )
        resp = self._get(f"{APPROVALS}{approval.id}/audit/", self.ptd_manager)
        self.assertEqual(resp.json()[0]["acted_by_name"], "PTD Manager")

    def test_rejection_rejects_the_bill_and_nothing_reaches_sap(self):
        posting = self._held(PTD)
        resp = self._decide(
            self._approval(posting, PTD), self.ptd_manager, "REJECTED",
            rejection_reason="No such pouch stock on the floor",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(resp.json()["posting_status"], "REJECTED")
        posting.refresh_from_db()
        self.assertEqual(posting.status, ARInvoiceStatus.REJECTED)
        self.assertIn("No such pouch stock on the floor", posting.approval_remarks)
        self.assertIn("PTD Manager", posting.approval_remarks)
        self.sap.create_ar_invoice.assert_not_called()

        resp = self._get(f"{APPROVALS}?whs={PTD}&status=REJECTED", self.ptd_manager)
        self.assertEqual(resp.json()[0]["rejection_reason"], "No such pouch stock on the floor")

    def test_a_rejection_needs_a_reason(self):
        posting = self._held(PTD)
        resp = self._decide(self._approval(posting, PTD), self.ptd_manager, "REJECTED")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        posting.refresh_from_db()
        self.assertEqual(posting.status, ARInvoiceStatus.AWAITING_MANAGER)

    def test_another_warehouses_manager_cannot_decide_it(self):
        posting = self._held(PTD)
        resp = self._decide(self._approval(posting, PTD), self.sc_manager)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            self._approval(posting, PTD).status, ARWarehouseApprovalStatus.PENDING
        )
        self.sap.create_ar_invoice.assert_not_called()

    def test_a_bill_across_two_warehouses_needs_both_managers(self):
        posting = self._held(PTD, SC)
        resp = self._decide(self._approval(posting, PTD), self.ptd_manager)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(resp.json()["posting_status"], "AWAITING_MANAGER")
        self.sap.create_ar_invoice.assert_not_called()

        resp = self._decide(self._approval(posting, SC), self.sc_manager)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(resp.json()["posting_status"], "POSTED")
        self.sap.create_ar_invoice.assert_called_once()

    def test_a_decided_row_cannot_be_decided_again(self):
        posting = self._held(PTD)
        approval = self._approval(posting, PTD)
        self._decide(approval, self.ptd_manager)
        resp = self._decide(approval, self.ptd_manager, "REJECTED", rejection_reason="late")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.sap.create_ar_invoice.assert_called_once()

    def test_a_failed_post_after_approval_keeps_the_approval_and_can_be_retried(self):
        posting = self._held(PTD)
        self.sap.create_ar_invoice.side_effect = SAPValidationError("Period locked")
        resp = self._decide(self._approval(posting, PTD), self.ptd_manager)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(resp.json()["posting_status"], "FAILED")
        self.assertIn("Period locked", resp.json()["warning"])
        self.assertEqual(
            self._approval(posting, PTD).status, ARWarehouseApprovalStatus.APPROVED
        )

        # The approval stands, so the raiser's retry is allowed through.
        self.sap.create_ar_invoice.side_effect = None
        resp = self.client.post(
            f"{AR}invoices/{posting.id}/post/", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(resp.json()["status"], "POSTED")

    def test_an_approval_that_comes_a_day_late_dispatches_on_the_day_it_is_raised(self):
        # The form sends no invoice date, so SAP dates the bill the day it is
        # added; a dispatch date before that is refused (1300014).
        yesterday = timezone.localdate() - timedelta(days=1)
        resp = self._bill(_line(PTD), dispatch_date=str(yesterday))
        posting = ARInvoicePosting.objects.get(pk=resp.json()["id"])
        self._decide(self._approval(posting, PTD), self.ptd_manager)
        payload = self.sap.create_ar_invoice.call_args[0][0]
        self.assertEqual(payload["U_Dipatch_Date"], str(timezone.localdate()))

    def test_a_bill_with_its_own_invoice_date_keeps_its_dates(self):
        resp = self._bill(
            _line(PTD), doc_date="2026-09-25", dispatch_date="2026-09-25"
        )
        posting = ARInvoicePosting.objects.get(pk=resp.json()["id"])
        self._decide(self._approval(posting, PTD), self.ptd_manager)
        payload = self.sap.create_ar_invoice.call_args[0][0]
        self.assertEqual(payload["DocDate"], "2026-09-25")
        self.assertEqual(payload["U_Dipatch_Date"], "2026-09-25")

    def test_a_failed_bill_from_before_the_rule_cannot_be_retried_past_it(self):
        # Raised before this rule existed: FAILED, from BH-PTD, no approvals.
        posting = ARInvoicePosting.objects.create(
            company=self.company, customer_code="CUSTA000025", branch_id=2,
            status=ARInvoiceStatus.FAILED, created_by=self.counter,
        )
        ARInvoiceLine.objects.create(
            ar_invoice=posting, item_code=ITEM, quantity="6", price="133.3333",
            line_total="800.00", tax_code="CG+SG@5", warehouse_code=PTD,
        )
        resp = self.client.post(
            f"{AR}invoices/{posting.id}/post/", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(PTD, resp.json()["detail"])
        self.sap.create_ar_invoice.assert_not_called()
        self.assertFalse(ARInvoiceWarehouseApproval.objects.exists())
