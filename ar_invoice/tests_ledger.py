"""Tests for the customer ledger on the A/R Invoices page.

The reader tests mock the HANA cursor; the endpoint tests mock the reader.
Nothing here reaches SAP.
"""
from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import SimpleTestCase
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPConnectionError
from sap_client.hana.customer_ledger_reader import HanaCustomerLedgerReader

User = get_user_model()
COMPANY_CODE = "TC001"
CUSTOMER = "CUSTA000486"
URL = "/api/v1/ar-invoices/customer-ledger/"


def _row(trans_id, day, trans_type, debit, credit, *, line_memo="", memo="", due=None, open_=0):
    """One JDT1 line as the reader selects it."""
    return (
        trans_id, 0, day, due or day, trans_type,
        str(trans_id + 600000000), "", line_memo, memo,
        "4110014", "SALES OIL @ 5%",
        debit, credit, max(open_, 0), max(-open_, 0),
    )


class CustomerLedgerReaderTests(SimpleTestCase):
    def setUp(self):
        context = mock.MagicMock()
        context.hana = {"host": "h", "port": 1, "user": "u", "password": "p", "schema": "SCHEMA"}
        self.reader = HanaCustomerLedgerReader(context)
        self.cursor = mock.MagicMock()
        conn = mock.MagicMock()
        conn.cursor.return_value = self.cursor
        patcher = mock.patch.object(self.reader.connection, "connect", return_value=conn)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_balances_run_forward_from_the_opening(self):
        self.cursor.fetchall.side_effect = [
            [(CUSTOMER, "WAL MART INDIA PVT LTD", 1900)],  # OCRD
            [(1000,)],                                      # posted before date_from
            [(3, 1500, 600)],                               # count, debit, credit in range
            [
                _row(1, date(2026, 4, 2), "13", 1500, 0, open_=1500),
                _row(2, date(2026, 4, 9), "24", 0, 400),
                _row(3, date(2026, 4, 20), "14", 0, 200),
            ],
        ]
        ledger = self.reader.ledger(CUSTOMER, date(2026, 4, 1), date(2026, 4, 30))

        self.assertEqual(ledger["opening_balance"], 1000.0)
        self.assertEqual([line["balance"] for line in ledger["lines"]], [2500.0, 2100.0, 1900.0])
        self.assertEqual(ledger["closing_balance"], 1900.0)
        self.assertEqual(ledger["total_debit"], 1500.0)
        self.assertEqual(ledger["total_credit"], 600.0)
        self.assertEqual(ledger["date_from"], "2026-04-01")
        self.assertFalse(ledger["truncated"])
        first = ledger["lines"][0]
        self.assertEqual(first["trans_type_label"], "A/R Invoice")
        self.assertEqual(first["open_amount"], 1500.0)
        self.assertEqual(first["offset_name"], "SALES OIL @ 5%")

    def test_the_closing_balance_survives_a_capped_page(self):
        """Totals come from the aggregate, not from the rows that were shipped."""
        self.cursor.fetchall.side_effect = [
            [(CUSTOMER, "Walmart", 0)],
            [(3, 900, 100)],
            [_row(1, date(2026, 4, 2), "13", 500, 0)],
        ]
        ledger = self.reader.ledger(CUSTOMER, limit=1)
        self.assertEqual(ledger["opening_balance"], 0.0)
        self.assertEqual(ledger["closing_balance"], 800.0)
        self.assertEqual(ledger["total"], 3)
        self.assertTrue(ledger["truncated"])
        self.assertIn("TOP 1", self.cursor.execute.call_args[0][0])

    def test_without_a_start_date_nothing_is_carried_forward(self):
        self.cursor.fetchall.side_effect = [[(CUSTOMER, "Walmart", 0)], [(0, 0, 0)], []]
        self.reader.ledger(CUSTOMER)
        statements = [call.args[0] for call in self.cursor.execute.call_args_list]
        self.assertEqual(len(statements), 3)
        self.assertFalse(any('"RefDate" <' in sql for sql in statements))

    def test_the_bank_narration_beats_sap_s_automatic_memo(self):
        self.cursor.fetchall.side_effect = [
            [(CUSTOMER, "Walmart", 0)],
            [(3, 500, 900)],
            [
                _row(1, date(2026, 4, 2), "24", 0, 400,
                     line_memo=f"Incoming Payments - {CUSTOMER}", memo="BY TRANSFER NEFT/HSBC"),
                _row(2, date(2026, 4, 3), "13", 500, 0,
                     line_memo=f"A/R Invoices - {CUSTOMER}", memo=f"A/R Invoices - {CUSTOMER}"),
                _row(3, date(2026, 4, 4), "30", 0, 500,
                     line_memo="TDS deducted agst Bill No. 726080101", memo="TDS"),
            ],
        ]
        lines = self.reader.ledger(CUSTOMER)["lines"]
        self.assertEqual(
            [line["narration"] for line in lines],
            ["BY TRANSFER NEFT/HSBC", "", "TDS deducted agst Bill No. 726080101"],
        )

    def test_money_rounds_half_up_as_sap_prints_it(self):
        self.cursor.fetchall.side_effect = [
            [(CUSTOMER, "Walmart", "217413.555")],
            [(1, "0.125", 0)],
            [_row(1, date(2026, 4, 2), "13", "0.125", 0)],
        ]
        ledger = self.reader.ledger(CUSTOMER)
        self.assertEqual(ledger["balance_today"], 217413.56)
        self.assertEqual(ledger["lines"][0]["debit"], 0.13)

    def test_the_customer_code_is_bound_not_spliced(self):
        code = "CUST'; DROP"
        self.cursor.fetchall.side_effect = [[(code, "x", 0)], [(0, 0, 0)], []]
        self.reader.ledger(code)
        for call in self.cursor.execute.call_args_list:
            sql, params = call.args
            self.assertNotIn(code, sql)
            self.assertIn(code, params)

    def test_an_unknown_customer_is_none(self):
        self.cursor.fetchall.side_effect = [[]]
        self.assertIsNone(self.reader.ledger("NOPE"))
        self.assertIsNone(self.reader.ledger("  "))


class CustomerLedgerEndpointTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Ledger Co", code=COMPANY_CODE)
        role = UserRole.objects.create(name="Billing")
        cls.viewer = User.objects.create_user(
            email="ar-ledger@example.com", password="pass12345",
            full_name="Ledger Viewer", employee_code="AR-LDG",
        )
        cls.stranger = User.objects.create_user(
            email="ar-ledger-none@example.com", password="pass12345",
            full_name="No Rights", employee_code="AR-NONE",
        )
        for user in (cls.viewer, cls.stranger):
            UserCompany.objects.create(user=user, company=cls.company, role=role, is_active=True)
        cls.viewer.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="ar_invoice", codename="view_ar_invoice_posting"
            )
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.viewer)
        # The test company has no SAP config to build a context from.
        context_patcher = mock.patch("ar_invoice.views_ledger.CompanyContext")
        self.addCleanup(context_patcher.stop)
        self.CompanyContext = context_patcher.start()
        patcher = mock.patch("ar_invoice.views_ledger.HanaCustomerLedgerReader")
        self.addCleanup(patcher.stop)
        self.reader = patcher.start().return_value
        self.reader.ledger.return_value = {"customer_code": CUSTOMER, "lines": []}

    def _get(self, query):
        return self.client.get(f"{URL}?{query}", HTTP_COMPANY_CODE=COMPANY_CODE)

    def test_a_viewer_reads_the_ledger_for_the_dates_asked(self):
        resp = self._get(f"customer_code={CUSTOMER}&date_from=2026-04-01&date_to=2026-10-03")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(resp.json()["customer_code"], CUSTOMER)
        self.reader.ledger.assert_called_once_with(
            CUSTOMER, date_from=date(2026, 4, 1), date_to=date(2026, 10, 3)
        )
        # Read from the SAP company the request is working in.
        self.CompanyContext.assert_called_once_with(COMPANY_CODE)

    def test_the_dates_are_optional(self):
        resp = self._get(f"customer_code={CUSTOMER}")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.reader.ledger.assert_called_once_with(CUSTOMER, date_from=None, date_to=None)

    def test_a_customer_is_required(self):
        self.assertEqual(self._get("date_from=2026-04-01").status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_range_that_ends_before_it_starts_is_refused(self):
        resp = self._get(f"customer_code={CUSTOMER}&date_from=2026-05-01&date_to=2026-04-01")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.reader.ledger.assert_not_called()

    def test_a_customer_sap_does_not_have_is_404(self):
        self.reader.ledger.return_value = None
        self.assertEqual(self._get("customer_code=NOPE").status_code, status.HTTP_404_NOT_FOUND)

    def test_sap_down_is_503(self):
        self.reader.ledger.side_effect = SAPConnectionError("down")
        resp = self._get(f"customer_code={CUSTOMER}")
        self.assertEqual(resp.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_it_needs_the_view_permission(self):
        self.client.force_authenticate(user=self.stranger)
        self.assertEqual(
            self._get(f"customer_code={CUSTOMER}").status_code, status.HTTP_403_FORBIDDEN
        )
