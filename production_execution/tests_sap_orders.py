"""SAP production-order screens (ported from SAP Portal).

    python manage.py test production_execution.tests_sap_orders --settings=config.sqlite_test_settings

The reader runs against a mocked HANA cursor; the endpoints against a mocked
SAPClient, patched where each module looks it up (views and the service).
Nothing here reaches SAP.
"""

from datetime import date
from io import StringIO
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPValidationError, SAPConnectionError
from sap_client.hana.production_order_reader import HanaProductionOrderReader

from .models_sap_orders import SapProductionOrderAction

BASE = "/api/v1/production-execution/sap-orders/"
RIGHTS = [
    "can_view_sap_production_orders",
    "can_create_sap_production_orders",
    "can_release_close_sap_production_orders",
    "can_issue_for_sap_production_orders",
    "can_receive_from_sap_production_orders",
]

RELEASED_ORDER = {
    "doc_entry": 77, "doc_num": 1077, "item_code": "FG-OIL-1L", "item_name": "Oil 1L",
    "planned_quantity": 100.0, "issued_quantity": 0.0, "received_quantity": 0.0,
    "status": "R", "status_label": "Released", "warehouse": "BH-FG", "branch_id": 3,
    "lines": [
        {"line_num": 0, "item_code": "RM-OIL", "item_name": "Crude", "is_resource": False,
         "planned_quantity": 100.0, "issued_quantity": 0.0, "warehouse": "BH-RM", "uom": "KG",
         "batch_managed": True},
        {"line_num": 1, "item_code": "PM-CAP", "item_name": "Cap", "is_resource": False,
         "planned_quantity": 100.0, "issued_quantity": 0.0, "warehouse": "BH-PM", "uom": "PCS",
         "batch_managed": False},
    ],
    "issues": [], "receipts": [],
}


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class ProductionOrderReaderTests(SimpleTestCase):
    def setUp(self):
        context = MagicMock()
        context.hana = {"host": "h", "port": 1, "user": "u", "password": "p", "schema": "SCHEMA"}
        self.reader = HanaProductionOrderReader(context)
        self.cursor = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value = self.cursor
        patcher = patch.object(self.reader.connection, "connect", return_value=conn)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_list_merges_issued_and_received_totals_in_one_extra_query(self):
        self.cursor.fetchall.side_effect = [
            [(2,)],
            [
                (78, 1078, "FG-B", "B", 50, 0, 0, "P", None, date(2026, 10, 1), "BH-FG", "PCS"),
                (77, 1077, "FG-A", "A", 100, 40, 0, "R", None, date(2026, 9, 30), "BH-FG", "PCS"),
            ],
            [(77, "I", 60), (77, "R", 40)],
        ]
        page = self.reader.list_orders(status="R", search="fg", limit=10)
        self.assertEqual(page["count"], 2)
        by_entry = {row["doc_entry"]: row for row in page["results"]}
        self.assertEqual((by_entry[77]["issued_quantity"], by_entry[77]["received_quantity"]), (60.0, 40.0))
        self.assertEqual((by_entry[78]["issued_quantity"], by_entry[78]["received_quantity"]), (0.0, 0.0))
        self.assertEqual(by_entry[78]["status_label"], "Planned")
        # The status and the search are bound, never formatted into the SQL.
        count_sql, count_params = self.cursor.execute.call_args_list[0][0]
        self.assertEqual(count_params, ("R", "%FG%", "%FG%", "%FG%"))
        self.assertNotIn("'R'", count_sql)
        movement_sql, movement_params = self.cursor.execute.call_args_list[2][0]
        self.assertEqual(movement_params, (78, 77, 78, 77))
        self.assertIn('"IGE1"', movement_sql)
        self.assertIn('"IGN1"', movement_sql)

    def test_an_unknown_status_is_refused(self):
        with self.assertRaises(SAPValidationError):
            self.reader.list_orders(status="X")

    def test_detail_flags_resources_and_batch_managed_components(self):
        self.cursor.fetchall.side_effect = [
            [(77, 1077, "FG-A", "A", 100, 0, 0, "R", None, date(2026, 9, 30), "BH-FG", "PCS", 3)],
            [
                (0, "RM-OIL", "Crude", 4, 100, 10, "BH-RM", "KG", "Y"),
                (1, "LAB-01", "Filling crew", 290, 8, 0, "", "HR", "N"),
            ],
            [(501, 9001, date(2026, 9, 20), 10, "first issue")],
            [],
            [(77, "I", 10)],
        ]
        order = self.reader.order_detail(77)
        self.assertEqual(order["branch_id"], 3)
        self.assertTrue(order["lines"][0]["batch_managed"])
        self.assertTrue(order["lines"][1]["is_resource"])
        self.assertEqual(order["issues"][0]["doc_num"], 9001)
        self.assertEqual(order["issued_quantity"], 10.0)

    def test_a_missing_order_is_none(self):
        self.cursor.fetchall.return_value = []
        self.assertIsNone(self.reader.order_detail(1))


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class SapOrderApiTestCase(APITestCase):
    rights = RIGHTS

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="planner@example.com", password="x", full_name="Planner", employee_code="PLN1"
        )
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        UserCompany.objects.create(
            user=self.user, company=self.company, role=UserRole.objects.create(name="Staff"), is_default=True
        )
        self.headers = {"HTTP_COMPANY_CODE": "JIVO_OIL"}
        self.grant(*self.rights)
        patcher = patch("production_execution.services.sap_order_service.SAPClient")
        self.service_sap = patcher.start().return_value
        self.addCleanup(patcher.stop)
        patcher = patch("production_execution.views_sap_orders.SAPClient")
        self.view_sap = patcher.start().return_value
        self.addCleanup(patcher.stop)
        self.service_sap.sap_production_order.return_value = dict(RELEASED_ORDER)

    def grant(self, *codenames):
        if codenames:
            self.user.user_permissions.add(
                *Permission.objects.filter(content_type__app_label="production_execution", codename__in=codenames)
            )
        self.user = get_user_model().objects.get(pk=self.user.pk)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def post(self, path, body):
        return self.client.post(f"{BASE}{path}", body, format="json", **self.headers)


class CreateAndStatusTests(SapOrderApiTestCase):
    BODY = {"item_code": "FG-OIL-1L", "planned_quantity": "100", "due_date": "2026-10-01", "release": True}

    def test_create_posts_the_order_released_and_records_who(self):
        self.service_sap.create_production_order.return_value = {"DocEntry": 90, "DocNum": 1090}
        response = self.post("", self.BODY)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        payload = self.service_sap.create_production_order.call_args[0][0]
        self.assertEqual(payload["ItemNo"], "FG-OIL-1L")
        self.assertEqual(payload["ProductionOrderStatus"], "boposReleased")
        self.assertNotIn("ProductionOrderLines", payload)
        row = SapProductionOrderAction.objects.get()
        self.assertEqual((row.action, row.order_doc_entry, row.sap_doc_num, row.created_by), ("CREATE", 90, 1090, self.user))

    def test_the_same_create_twice_in_a_row_is_refused_until_confirmed(self):
        self.service_sap.create_production_order.return_value = {"DocEntry": 90, "DocNum": 1090}
        self.assertEqual(self.post("", self.BODY).status_code, status.HTTP_201_CREATED)
        again = self.post("", self.BODY)
        self.assertEqual(again.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(again.data["code"], "REPEAT_POST")
        self.assertEqual(self.service_sap.create_production_order.call_count, 1)
        confirmed = self.post("", dict(self.BODY, confirm_repeat=True))
        self.assertEqual(confirmed.status_code, status.HTTP_201_CREATED)

    def test_a_start_after_the_due_date_is_refused(self):
        response = self.post("", dict(self.BODY, start_date="2026-10-05"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_release_needs_a_planned_order_and_close_a_released_one(self):
        self.assertEqual(self.post("77/release/", {}).status_code, status.HTTP_400_BAD_REQUEST)
        self.service_sap.release_production_order.assert_not_called()
        self.assertEqual(self.post("77/close/", {}).status_code, status.HTTP_200_OK)
        self.service_sap.close_production_order.assert_called_once_with(77)
        self.assertEqual(SapProductionOrderAction.objects.get().action, "CLOSE")

    def test_a_missing_order_is_404(self):
        self.service_sap.sap_production_order.return_value = None
        self.assertEqual(self.post("5/close/", {}).status_code, status.HTTP_404_NOT_FOUND)

    def test_sap_refusing_leaves_no_audit_row(self):
        self.service_sap.close_production_order.side_effect = SAPValidationError("Cannot close: open issues")
        response = self.post("77/close/", {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("open issues", response.data["detail"])
        self.assertFalse(SapProductionOrderAction.objects.exists())


class IssueAndReceiptTests(SapOrderApiTestCase):
    def test_an_issue_links_each_line_to_the_order_with_its_batches(self):
        self.service_sap.issue_for_production.return_value = {"DocEntry": 601, "DocNum": 9601}
        body = {
            "lines": [
                {"line_num": 0, "quantity": "30", "batches": [
                    {"batch_number": "B1", "quantity": "20"}, {"batch_number": "B2", "quantity": "10"}]},
                {"line_num": 1, "quantity": "100"},
            ],
            "posting_date": "2026-09-26",
        }
        response = self.post("77/issue/", body)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data, {"pending_approval": False, "doc_entry": 601, "doc_num": 9601})
        payload = self.service_sap.issue_for_production.call_args[0][0]
        self.assertEqual(payload["BPL_IDAssignedToInvoice"], 3)
        first, second = payload["DocumentLines"]
        self.assertEqual((first["BaseType"], first["BaseEntry"], first["BaseLine"]), (202, 77, 0))
        self.assertEqual(first["WarehouseCode"], "BH-RM")
        self.assertEqual(len(first["BatchNumbers"]), 2)
        self.assertNotIn("BatchNumbers", second)
        self.assertEqual(SapProductionOrderAction.objects.get().quantity, 130)

    def test_batches_must_add_up_to_the_issued_quantity(self):
        body = {"lines": [{"line_num": 0, "quantity": "30", "batches": [{"batch_number": "B1", "quantity": "20"}]}]}
        response = self.post("77/issue/", body)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("batch managed", response.data["detail"])
        self.service_sap.issue_for_production.assert_not_called()

    def test_a_line_not_on_the_order_is_refused(self):
        response = self.post("77/issue/", {"lines": [{"line_num": 9, "quantity": "1"}]})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_issue_held_for_approval_says_so(self):
        self.service_sap.issue_for_production.return_value = {
            "DocEntry": None, "DocNum": "", "pending_approval": True, "draft_entry": 44}
        response = self.post("77/issue/", {"lines": [{"line_num": 1, "quantity": "5"}]})
        self.assertEqual(response.data, {"pending_approval": True, "draft_entry": 44})
        self.assertEqual(SapProductionOrderAction.objects.get().pending_approval_draft, 44)

    def test_a_receipt_of_a_batch_managed_product_needs_a_batch(self):
        self.service_sap.batch_managed_flags.return_value = {"FG-OIL-1L": True}
        self.assertEqual(self.post("77/receipt/", {"quantity": "40"}).status_code, status.HTTP_400_BAD_REQUEST)
        self.service_sap.receipt_from_production.return_value = {"DocEntry": 701, "DocNum": 9701}
        response = self.post("77/receipt/", {"quantity": "40", "batch_number": "FG260926"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        line = self.service_sap.receipt_from_production.call_args[0][0]["DocumentLines"][0]
        self.assertEqual((line["BaseType"], line["BaseEntry"]), (202, 77))
        self.assertNotIn("BaseLine", line)
        self.assertEqual(line["BatchNumbers"], [{"BatchNumber": "FG260926", "Quantity": 40.0}])

    def test_nothing_is_issued_to_an_order_that_is_not_released(self):
        self.service_sap.sap_production_order.return_value = dict(RELEASED_ORDER, status="P", status_label="Planned")
        self.assertEqual(
            self.post("77/issue/", {"lines": [{"line_num": 1, "quantity": "1"}]}).status_code,
            status.HTTP_400_BAD_REQUEST,
        )



class DoublePostTests(SapOrderApiTestCase):
    """SAP has no idempotency key: a posting is claimed before SAP is asked, and
    one SAP may have taken without answering is not sent again unconfirmed."""

    ISSUE = {"lines": [{"line_num": 1, "quantity": "40"}]}

    def issue(self, **extra):
        return self.post("77/issue/", dict(self.ISSUE, **extra))

    def test_a_posting_ends_done_with_what_sap_created(self):
        self.service_sap.issue_for_production.return_value = {"DocEntry": 601, "DocNum": 9601}
        self.assertEqual(self.issue().status_code, status.HTTP_201_CREATED)
        row = SapProductionOrderAction.objects.get()
        self.assertEqual((row.outcome, row.sap_doc_entry, row.sap_doc_num), ("DONE", 601, 9601))
        self.assertEqual(len(row.fingerprint), 64)

    def test_a_timed_out_posting_is_not_sent_again_until_confirmed(self):
        self.service_sap.issue_for_production.side_effect = SAPConnectionError(
            "SAP took too long to answer; it may have posted."
        )
        self.assertEqual(self.issue().status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(SapProductionOrderAction.objects.get().outcome, "UNKNOWN")

        self.service_sap.issue_for_production.side_effect = None
        self.service_sap.issue_for_production.return_value = {"DocEntry": 602, "DocNum": 9602}
        again = self.issue()
        self.assertEqual(again.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(again.data["code"], "UNCERTAIN_POST")
        self.assertEqual(self.service_sap.issue_for_production.call_count, 1)

        confirmed = self.issue(confirm_repeat=True)
        self.assertEqual(confirmed.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.service_sap.issue_for_production.call_count, 2)

    def test_the_uncertain_guard_covers_every_operator(self):
        """Somebody else retrying the same posting after a timeout is just as risky."""
        SapProductionOrderAction.objects.create(
            company=self.company, action="ISSUE", order_doc_entry=77, outcome="UNKNOWN",
            fingerprint=self._fingerprint_of_issue(),
        )
        self.assertEqual(self.issue().data["code"], "UNCERTAIN_POST")
        self.service_sap.issue_for_production.assert_not_called()

    def test_a_refused_posting_can_be_fixed_and_sent_again(self):
        self.service_sap.issue_for_production.side_effect = SAPValidationError("Batch B1 has no stock")
        self.assertEqual(self.issue().status_code, status.HTTP_400_BAD_REQUEST)
        row = SapProductionOrderAction.objects.get()
        self.assertEqual(row.outcome, "FAILED")
        self.assertIn("no stock", row.error)
        self.service_sap.issue_for_production.side_effect = None
        self.service_sap.issue_for_production.return_value = {"DocEntry": 603, "DocNum": 9603}
        # Nothing was posted, so this is not a repeat.
        self.assertEqual(self.issue().status_code, status.HTTP_201_CREATED)

    def test_the_same_posting_in_flight_is_refused_without_asking_sap(self):
        """The second tab, or the second operator, while the first is still waiting on SAP."""
        other = get_user_model().objects.create_user(
            email="second@example.com", password="x", full_name="Second", employee_code="PLN2"
        )
        SapProductionOrderAction.objects.create(
            company=self.company, action="ISSUE", order_doc_entry=77, outcome="POSTING",
            fingerprint=self._fingerprint_of_issue(), created_by=other,
        )
        response = self.issue(confirm_repeat=True)
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["code"], "POSTING_IN_PROGRESS")
        self.service_sap.issue_for_production.assert_not_called()

    def test_a_posting_left_by_a_dead_process_is_retired_not_blocking_for_ever(self):
        from datetime import timedelta

        from django.utils import timezone

        stuck = SapProductionOrderAction.objects.create(
            company=self.company, action="ISSUE", order_doc_entry=77, outcome="POSTING",
            fingerprint=self._fingerprint_of_issue(),
        )
        SapProductionOrderAction.objects.filter(pk=stuck.pk).update(
            created_at=timezone.now() - timedelta(minutes=6)
        )
        response = self.issue()
        # SAP may have posted it, so it is uncertain — not "in progress" any more.
        self.assertEqual(response.data["code"], "UNCERTAIN_POST")
        stuck.refresh_from_db()
        self.assertEqual(stuck.outcome, "UNKNOWN")

    def test_the_database_holds_one_posting_per_payload(self):
        from django.db import IntegrityError, transaction

        SapProductionOrderAction.objects.create(
            company=self.company, action="ISSUE", outcome="POSTING", fingerprint="f" * 64
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            SapProductionOrderAction.objects.create(
                company=self.company, action="ISSUE", outcome="POSTING", fingerprint="f" * 64
            )
        # Finished rows with the same payload are fine: that is the log.
        SapProductionOrderAction.objects.create(
            company=self.company, action="ISSUE", outcome="DONE", fingerprint="f" * 64
        )

    def test_the_detail_shows_how_each_posting_ended(self):
        SapProductionOrderAction.objects.create(
            company=self.company, action="ISSUE", order_doc_entry=77, outcome="UNKNOWN", error="timed out"
        )
        self.view_sap.sap_production_order.return_value = dict(RELEASED_ORDER)
        data = self.client.get(f"{BASE}77/", **self.headers).data
        self.assertEqual(data["actions"][0]["outcome"], "UNKNOWN")
        self.assertEqual(data["actions"][0]["outcome_label"], "SAP did not answer")
        self.assertEqual(data["actions"][0]["error"], "timed out")

    def _fingerprint_of_issue(self) -> str:
        """The fingerprint the service computes for ISSUE, by posting it once against a stub."""
        from production_execution.services import sap_order_service

        captured = {}
        with patch.object(sap_order_service, "_post_once", side_effect=lambda *a, **k: captured.update(payload=a[3]) or {}):
            self.issue()
        return sap_order_service._fingerprint(captured["payload"])

class ReadTests(SapOrderApiTestCase):
    def test_the_list_passes_filters_and_sap_outage_is_503(self):
        self.view_sap.list_sap_production_orders.return_value = {"count": 0, "results": []}
        response = self.client.get(f"{BASE}?status=r&search=oil", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        kwargs = self.view_sap.list_sap_production_orders.call_args.kwargs
        self.assertEqual((kwargs["status"], kwargs["search"]), ("R", "oil"))
        self.view_sap.list_sap_production_orders.side_effect = SAPConnectionError("down")
        self.assertEqual(self.client.get(BASE, **self.headers).status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_the_detail_carries_this_companys_actions_only(self):
        self.view_sap.sap_production_order.return_value = dict(RELEASED_ORDER)
        other = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        SapProductionOrderAction.objects.create(company=other, action="CLOSE", order_doc_entry=77)
        mine = SapProductionOrderAction.objects.create(company=self.company, action="RELEASE", order_doc_entry=77)
        response = self.client.get(f"{BASE}77/", **self.headers)
        self.assertEqual([row["id"] for row in response.data["actions"]], [mine.id])


class PermissionTests(SapOrderApiTestCase):
    rights = []

    def test_the_company_header_is_required(self):
        self.grant(*RIGHTS)
        self.assertEqual(self.client.get(BASE).status_code, status.HTTP_403_FORBIDDEN)

    def test_no_rights_is_refused_everywhere(self):
        self.assertEqual(self.client.get(BASE, **self.headers).status_code, status.HTTP_403_FORBIDDEN)
        for path in ("", "77/release/", "77/close/", "77/issue/", "77/receipt/"):
            with self.subTest(path=path):
                self.assertEqual(self.post(path, {}).status_code, status.HTTP_403_FORBIDDEN)

    def test_any_action_right_lets_you_see_the_orders(self):
        self.view_sap.list_sap_production_orders.return_value = {"count": 0, "results": []}
        self.grant("can_issue_for_sap_production_orders")
        self.assertEqual(self.client.get(BASE, **self.headers).status_code, status.HTTP_200_OK)
        self.assertEqual(self.post("77/close/", {}).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.post("77/receipt/", {"quantity": "1"}).status_code, status.HTTP_403_FORBIDDEN)


class GroupAndSurfaceTests(TestCase):
    def test_the_new_rights_exist_and_the_group_hands_them_out(self):
        declared = set(
            Permission.objects.filter(
                content_type__app_label="production_execution", content_type__model="sapproductionorderaction"
            ).values_list("codename", flat=True)
        )
        self.assertEqual(declared, set(RIGHTS))
        call_command("setup_production_groups", stdout=StringIO())
        held = set(Group.objects.get(name="Production SAP Orders").permissions.values_list("codename", flat=True))
        self.assertEqual(held, set(RIGHTS))
        viewer = set(Group.objects.get(name="Production SAP Orders Viewer").permissions.values_list("codename", flat=True))
        self.assertEqual(viewer, {"can_view_sap_production_orders"})
