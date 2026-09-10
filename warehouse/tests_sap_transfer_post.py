"""Tests for posting a transfer against a SAP-raised transfer request.

The risky part is not the HTTP plumbing, it is the quantities: SAP will happily
post a movement larger than the request reserved, or a second movement against a
line another transfer already closed, and either leaves the register wrong in a
way nobody notices for weeks. So every quantity is checked against the line's
live ``OpenQty`` and every line is tied back to its request line.

SAP is mocked throughout — nothing here reaches HANA or the Service Layer.
"""

from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from company.models import Company, UserCompany, UserRole
from warehouse.models_manager import UserWarehouse
from warehouse.services.sap_transfer_post_service import (
    SapTransferPostError,
    SapTransferPostService,
)

AWAITING_URL = "/api/v1/warehouse/sap-transfer-requests/awaiting/"


def post_url(doc_entry):
    return f"/api/v1/warehouse/sap-transfer-requests/{doc_entry}/post/"


def _line(line_num, item, quantity, open_qty, status="O", **overrides):
    line = {
        "line_num": line_num,
        "item_code": item,
        "item_name": f"{item} NAME",
        "quantity": Decimal(str(quantity)),
        "open_quantity": Decimal(str(open_qty)),
        "served_quantity": Decimal(str(quantity)) - Decimal(str(open_qty)),
        "line_status": status,
        "from_warehouse": "BH-LO",
        "to_warehouse": "BH-PC",
        "uom": "KGS",
    }
    line.update(overrides)
    return line


def _request(**overrides):
    request = {
        "doc_entry": 2736,
        "doc_num": 926656513,
        "doc_date": None,
        "from_warehouse": "BH-LO",
        "to_warehouse": "BH-PC",
        "doc_status": "O",
        "cancelled": False,
        "comments": "",
        "branch_id": 2,
        "is_open": True,
        "lines": [
            _line(0, "RM0000002", 50000, 50000),
            _line(1, "RM0000025", 50000, 12000),
        ],
    }
    request.update(overrides)
    return request


class _ServiceHarness(TestCase):
    """A service whose SAP client and HANA reader are both mocked."""

    def setUp(self):
        self.company = Company.objects.create(code="JIVO_OIL", name="Jivo Oil")
        role = UserRole.objects.create(name="Store")
        self.user = User.objects.create_user(
            email="honey@example.com", full_name="Honey",
            employee_code="E-37", password="x",
        )
        UserCompany.objects.create(user=self.user, company=self.company, role=role)
        # Posting moves stock out of BH-LO, so that is the assignment that counts.
        UserWarehouse.objects.create(
            user=self.user, company=self.company, warehouse_code="BH-LO"
        )

        patcher = patch(
            "warehouse.services.sap_transfer_post_service.SAPClient"
        )
        self.SAPClient = patcher.start()
        self.addCleanup(patcher.stop)
        self.client_mock = self.SAPClient.return_value
        # Both warehouses in branch 2 — the same-branch case.
        self.client_mock.get_warehouse_branches.return_value = {
            "BH-LO": 2, "BH-PC": 2, "PB-PS": 3,
        }
        self.client_mock.batch_managed_flags.return_value = {}
        self.client_mock.create_stock_transfer.return_value = {
            "DocEntry": 9001, "DocNum": 926676700,
        }

        reader_patcher = patch(
            "warehouse.services.sap_transfer_post_service.HanaTransferRequestReader"
        )
        self.Reader = reader_patcher.start()
        self.addCleanup(reader_patcher.stop)
        self.reader = self.Reader.return_value
        self.reader.get_request.return_value = _request()
        self.reader.list_open_requests.return_value = [{
            "doc_entry": 2736, "doc_num": 926656513, "doc_date": None,
            "from_warehouse": "BH-LO", "to_warehouse": "BH-PC",
            "comments": "", "age_days": 3, "open_lines": 2,
            "open_quantity": Decimal("62000"),
        }]

        series_patcher = patch(
            "warehouse.services.sap_transfer_post_service.HanaSeriesReader"
        )
        self.Series = series_patcher.start()
        self.addCleanup(series_patcher.stop)
        self.Series.return_value.resolve_stock_transfer.return_value = 7

    def service(self):
        return SapTransferPostService(self.company.code, self.user)


class QuantityRulesTests(_ServiceHarness):
    def test_a_quantity_over_the_open_amount_is_refused_by_name(self):
        with self.assertRaises(SapTransferPostError) as ctx:
            self.service().post_transfer(2736, {"1": "20000"})
        message = str(ctx.exception)
        self.assertIn("RM0000025", message)
        self.assertIn("12000", message)
        self.client_mock.create_stock_transfer.assert_not_called()

    def test_a_closed_line_is_refused(self):
        """Another transfer may have taken it while the page sat open."""
        self.reader.get_request.return_value = _request(
            lines=[_line(0, "RM0000002", 50000, 0, status="C")]
        )
        with self.assertRaises(SapTransferPostError) as ctx:
            self.service().post_transfer(2736, {"0": "10"})
        self.assertIn("already closed", str(ctx.exception))
        self.client_mock.create_stock_transfer.assert_not_called()

    def test_an_unknown_line_is_refused(self):
        with self.assertRaises(SapTransferPostError) as ctx:
            self.service().post_transfer(2736, {"9": "10"})
        self.assertIn("Line 9", str(ctx.exception))

    def test_a_zero_quantity_is_skipped_not_an_error(self):
        """Leaving a line for later is normal — SAP allows partial service."""
        self.service().post_transfer(2736, {"0": "1000", "1": "0"})
        payload = self.client_mock.create_stock_transfer.call_args[0][0]
        self.assertEqual(len(payload["StockTransferLines"]), 1)
        self.assertEqual(payload["StockTransferLines"][0]["ItemCode"], "RM0000002")

    def test_all_zero_is_refused_rather_than_posting_an_empty_transfer(self):
        with self.assertRaises(SapTransferPostError) as ctx:
            self.service().post_transfer(2736, {"0": "0", "1": "0"})
        self.assertIn("at least one line", str(ctx.exception))
        self.client_mock.create_stock_transfer.assert_not_called()

    def test_a_negative_quantity_is_refused(self):
        with self.assertRaises(SapTransferPostError):
            self.service().post_transfer(2736, {"0": "-5"})

    def test_a_non_numeric_quantity_is_refused(self):
        with self.assertRaises(SapTransferPostError):
            self.service().post_transfer(2736, {"0": "lots"})

    def test_a_fractional_quantity_survives_as_a_decimal(self):
        """Loose oil moves in fractions; 143.846 must not become 143 or 143.85."""
        self.reader.get_request.return_value = _request(
            lines=[_line(0, "RM0000002", 200, "143.846")]
        )
        self.service().post_transfer(2736, {"0": "143.846"})
        payload = self.client_mock.create_stock_transfer.call_args[0][0]
        self.assertEqual(
            Decimal(str(payload["StockTransferLines"][0]["Quantity"])),
            Decimal("143.846"),
        )


class BaseDocumentLinkTests(_ServiceHarness):
    def test_every_line_is_tied_back_to_its_request_line(self):
        """Without the base link SAP leaves the reservation open alongside."""
        self.service().post_transfer(2736, {"0": "1000"})
        line = self.client_mock.create_stock_transfer.call_args[0][0][
            "StockTransferLines"
        ][0]
        self.assertEqual(line["BaseType"], 1250000001)
        self.assertEqual(line["BaseEntry"], 2736)
        self.assertEqual(line["BaseLine"], 0)

    def test_batches_are_allocated_only_for_batch_managed_items(self):
        self.client_mock.batch_managed_flags.return_value = {"RM0000002": True}
        self.client_mock.allocate_batches_fifo.return_value = [
            {"BatchNumber": "B1", "Quantity": 1000}
        ]
        self.service().post_transfer(2736, {"0": "1000", "1": "500"})
        lines = self.client_mock.create_stock_transfer.call_args[0][0][
            "StockTransferLines"
        ]
        batched = [line for line in lines if "BatchNumbers" in line]
        self.assertEqual(len(batched), 1)
        self.assertEqual(batched[0]["ItemCode"], "RM0000002")
        self.client_mock.allocate_batches_fifo.assert_called_once_with(
            "RM0000002", "BH-LO", Decimal("1000")
        )

    def test_the_result_reports_what_sap_still_owes(self):
        self.reader.get_request.side_effect = [
            _request(),
            _request(is_open=True, lines=[_line(1, "RM0000025", 50000, 11000)]),
        ]
        result = self.service().post_transfer(2736, {"0": "1000"})
        self.assertEqual(result["doc_num"], 926676700)
        self.assertEqual(result["remaining_quantity"], "11000")
        self.assertFalse(result["request_closed"])


class RequestStateTests(_ServiceHarness):
    def test_a_cross_branch_request_is_refused_and_says_why(self):
        self.reader.get_request.return_value = _request(to_warehouse="PB-PS")
        with self.assertRaises(SapTransferPostError) as ctx:
            self.service().post_transfer(2736, {"0": "10"})
        message = str(ctx.exception)
        self.assertIn("different SAP branches", message)
        self.assertIn("two legs", message)
        self.client_mock.create_stock_transfer.assert_not_called()

    def test_a_cancelled_request_is_refused(self):
        self.reader.get_request.return_value = _request(cancelled=True)
        with self.assertRaises(SapTransferPostError):
            self.service().post_transfer(2736, {"0": "10"})

    def test_a_fully_served_request_is_refused(self):
        self.reader.get_request.return_value = _request(is_open=False)
        with self.assertRaises(SapTransferPostError) as ctx:
            self.service().post_transfer(2736, {"0": "10"})
        self.assertIn("nothing left", str(ctx.exception))

    def test_a_missing_request_is_refused(self):
        self.reader.get_request.return_value = None
        with self.assertRaises(SapTransferPostError):
            self.service().post_transfer(2736, {"0": "10"})

    def test_posting_out_of_an_unmanaged_warehouse_is_refused(self):
        """Moving stock out is the source warehouse manager's call."""
        UserWarehouse.objects.filter(user=self.user).update(warehouse_code="BH-PC")
        from rest_framework.exceptions import PermissionDenied

        with self.assertRaises(PermissionDenied):
            self.service().post_transfer(2736, {"0": "10"})
        self.client_mock.create_stock_transfer.assert_not_called()

    def test_the_awaiting_list_names_why_a_row_is_blocked(self):
        self.reader.list_open_requests.return_value = [{
            "doc_entry": 2736, "doc_num": 926656513, "doc_date": None,
            "from_warehouse": "PB-PS", "to_warehouse": "BH-PC",
            "comments": "", "age_days": 3, "open_lines": 1,
            "open_quantity": Decimal("10"),
        }]
        self.reader.get_request.return_value = _request(from_warehouse="PB-PS")
        rows = self.service().list_awaiting_transfer()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["cross_branch"])
        self.assertFalse(rows[0]["can_post"])
        self.assertIn("two legs", rows[0]["blocked_reason"])

    def test_the_awaiting_list_drops_requests_with_no_open_lines(self):
        self.reader.get_request.return_value = _request(
            lines=[_line(0, "RM0000002", 50000, 0, status="C")]
        )
        self.assertEqual(self.service().list_awaiting_transfer(), [])


class SapTransferPostAPITests(TestCase):
    """The endpoints, and which permission each one needs."""

    def setUp(self):
        self.company = Company.objects.create(code="JIVO_OIL", name="Jivo Oil")
        UserRole.objects.create(name="Store")
        self.poster = self._user("poster@example.com", "Poster", "E-P")
        self.viewer = self._user("viewer@example.com", "Viewer", "E-V")
        for codename in (
            "can_view_transfer_request", "can_post_transfer_to_sap",
        ):
            self.poster.user_permissions.add(
                Permission.objects.get(
                    content_type__app_label="warehouse", codename=codename
                )
            )
        self.viewer.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="warehouse",
                codename="can_view_transfer_request",
            )
        )
        patcher = patch("warehouse.views_sap_transfer_post.SapTransferPostService")
        self.Service = patcher.start()
        self.addCleanup(patcher.stop)
        self.service = self.Service.return_value

    def _user(self, email, name, code):
        user = User.objects.create_user(
            email=email, full_name=name, employee_code=code, password="x"
        )
        UserCompany.objects.create(
            user=user, company=self.company, role=UserRole.objects.first()
        )
        return user

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        client.credentials(HTTP_COMPANY_CODE=self.company.code)
        return client

    def test_the_backlog_is_readable_with_the_view_permission(self):
        """A viewer should see what is owed even if they cannot move it."""
        self.service.list_awaiting_transfer.return_value = []
        response = self._client(self.viewer).get(AWAITING_URL)
        self.assertEqual(response.status_code, 200)

    def test_posting_needs_the_post_permission(self):
        response = self._client(self.viewer).post(
            post_url(2736), {"quantities": {"0": "10"}}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        self.service.post_transfer.assert_not_called()

    def test_posting_passes_the_quantities_through(self):
        self.service.post_transfer.return_value = {
            "doc_entry": 9001, "doc_num": 926676700, "request_doc_entry": 2736,
            "request_closed": False, "remaining_quantity": "11000", "lines_moved": 1,
        }
        response = self._client(self.poster).post(
            post_url(2736), {"quantities": {"0": "1000"}}, format="json"
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["doc_num"], 926676700)
        self.service.post_transfer.assert_called_once_with(2736, {"0": "1000"})

    def test_an_empty_body_is_refused(self):
        response = self._client(self.poster).post(
            post_url(2736), {"quantities": {}}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.service.post_transfer.assert_not_called()

    def test_a_service_refusal_becomes_a_400_the_operator_can_read(self):
        self.service.post_transfer.side_effect = SapTransferPostError(
            "Line 1 (RM0000025) has only 12000 left to transfer, not 20000."
        )
        response = self._client(self.poster).post(
            post_url(2736), {"quantities": {"1": "20000"}}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("12000", response.data["error"])
