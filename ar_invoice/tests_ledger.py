"""Tests for the customer ledger on the A/R Invoices page.

The reader tests mock the HANA cursor; the endpoint and admin-form tests mock
the readers. Nothing here reaches SAP.
"""
from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import SimpleTestCase, TestCase
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPConnectionError
from sap_client.hana.customer_ledger_reader import HanaCustomerLedgerReader

from .models import UserCustomer

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


def _perm(codename):
    return Permission.objects.get(content_type__app_label="ar_invoice", codename=codename)


class CustomerLedgerEndpointTests(APITestCase):
    """The ledger endpoint and whose ledger each user may open.

    ``accounts`` holds the all-ledgers right; ``counter`` can view A/R invoices
    and is linked to CUSTOMER only; ``unlinked`` can view A/R invoices and is
    linked to nobody; ``stranger`` holds nothing.
    """

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Ledger Co", code=COMPANY_CODE)
        cls.other_company = Company.objects.create(name="Other Co", code="TC002")
        role = UserRole.objects.create(name="Billing")

        def user(email, code):
            u = User.objects.create_user(
                email=email, password="pass12345", full_name=email, employee_code=code
            )
            UserCompany.objects.create(user=u, company=cls.company, role=role, is_active=True)
            return u

        cls.accounts = user("ar-ledger-accounts@example.com", "AR-ACC")
        cls.counter = user("ar-ledger-counter@example.com", "AR-CTR")
        cls.unlinked = user("ar-ledger-unlinked@example.com", "AR-UNL")
        cls.stranger = user("ar-ledger-none@example.com", "AR-NONE")
        for u in (cls.accounts, cls.counter, cls.unlinked):
            u.user_permissions.add(_perm("view_ar_invoice_posting"))
        cls.accounts.user_permissions.add(_perm("view_all_customer_ledgers"))

        UserCustomer.objects.create(
            user=cls.counter, company=cls.company,
            customer_code=CUSTOMER, customer_name="WAL MART INDIA PVT LTD",
        )
        # Neither of these opens anything in this company.
        UserCustomer.objects.create(
            user=cls.counter, company=cls.company,
            customer_code="CUSTA000999", customer_name="Old account", is_active=False,
        )
        UserCustomer.objects.create(
            user=cls.counter, company=cls.other_company,
            customer_code="CUSTA000777", customer_name="Other company's account",
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(user=self.accounts)
        # The test company has no SAP config to build a context from.
        context_patcher = mock.patch("ar_invoice.views_ledger.CompanyContext")
        self.addCleanup(context_patcher.stop)
        self.CompanyContext = context_patcher.start()
        patcher = mock.patch("ar_invoice.views_ledger.HanaCustomerLedgerReader")
        self.addCleanup(patcher.stop)
        self.reader = patcher.start().return_value
        self.reader.ledger.return_value = {"customer_code": CUSTOMER, "lines": []}

    def _get(self, query, url=URL):
        return self.client.get(f"{url}?{query}", HTTP_COMPANY_CODE=COMPANY_CODE)

    def _customers(self, user):
        self.client.force_authenticate(user=user)
        resp = self.client.get(f"{URL}customers/", HTTP_COMPANY_CODE=COMPANY_CODE)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        return resp.json()

    # ── reading a ledger ────────────────────────────────────────────────────
    def test_the_ledger_is_read_for_the_dates_asked(self):
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

    # ── whose ledger ────────────────────────────────────────────────────────
    def test_the_all_ledgers_right_opens_any_customer(self):
        self.assertEqual(self._get("customer_code=CUSTA000001").status_code, status.HTTP_200_OK)

    def test_a_linked_user_reads_their_own_ledger(self):
        self.client.force_authenticate(user=self.counter)
        self.assertEqual(self._get(f"customer_code={CUSTOMER}").status_code, status.HTTP_200_OK)

    def test_a_linked_user_is_refused_any_other_customer_before_sap_is_read(self):
        self.client.force_authenticate(user=self.counter)
        for code in ("CUSTA000001", "CUSTA000999", "CUSTA000777"):  # other, inactive, other company
            with self.subTest(code=code):
                resp = self._get(f"customer_code={code}")
                self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.reader.ledger.assert_not_called()

    def test_no_link_means_no_customer_not_every_customer(self):
        self.client.force_authenticate(user=self.unlinked)
        self.assertEqual(
            self._get(f"customer_code={CUSTOMER}").status_code, status.HTTP_403_FORBIDDEN
        )

    # ── what the picker offers ──────────────────────────────────────────────
    def test_the_picker_offers_a_linked_user_their_active_links_in_this_company(self):
        self.assertEqual(
            self._customers(self.counter),
            {
                "all_customers": False,
                "customers": [
                    {"customer_code": CUSTOMER, "customer_name": "WAL MART INDIA PVT LTD"}
                ],
            },
        )

    def test_the_picker_offers_an_unlinked_user_nothing(self):
        self.assertEqual(
            self._customers(self.unlinked), {"all_customers": False, "customers": []}
        )

    def test_the_picker_offers_any_customer_with_the_right_or_to_a_superuser(self):
        self.assertEqual(self._customers(self.accounts), {"all_customers": True, "customers": []})
        boss = User.objects.create_superuser(
            email="ar-ledger-boss@example.com", password="pass12345",
            full_name="Boss", employee_code="AR-BOSS",
        )
        UserCompany.objects.create(
            user=boss, company=self.company, role=UserRole.objects.first(), is_active=True
        )
        self.assertTrue(self._customers(boss)["all_customers"])


class UserCustomerFormTests(TestCase):
    """The admin form names a customer only once SAP confirms it."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Ledger Co", code=COMPANY_CODE)
        cls.user = User.objects.create_user(
            email="ar-ledger-link@example.com", password="pass12345",
            full_name="Counter", employee_code="AR-LNK",
        )

    def setUp(self):
        patcher = mock.patch("ar_invoice.admin.HanaCustomerReader")
        self.addCleanup(patcher.stop)
        self.reader = patcher.start().return_value
        context_patcher = mock.patch("ar_invoice.admin.CompanyContext")
        self.addCleanup(context_patcher.stop)
        context_patcher.start()

    def _form(self, code):
        from .admin import UserCustomerForm

        return UserCustomerForm(
            data={
                "user": self.user.pk, "company": self.company.pk,
                "customer_code": code, "is_active": True,
            }
        )

    def test_a_customer_sap_knows_is_saved_as_sap_spells_it(self):
        self.reader.get_customer.return_value = {
            "customer_code": CUSTOMER, "customer_name": "WAL MART INDIA PVT LTD",
        }
        form = self._form(" custa000486 ")
        self.assertTrue(form.is_valid(), form.errors)
        link = form.save()
        self.assertEqual(link.customer_code, CUSTOMER)
        self.assertEqual(link.customer_name, "WAL MART INDIA PVT LTD")
        self.reader.get_customer.assert_called_once_with("custa000486")

    def test_a_code_typed_in_lower_case_is_found_in_upper_case(self):
        self.reader.get_customer.side_effect = [
            None, {"customer_code": CUSTOMER, "customer_name": "WAL MART INDIA PVT LTD"},
        ]
        form = self._form("custa000486")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().customer_code, CUSTOMER)
        self.assertEqual(
            [call.args[0] for call in self.reader.get_customer.call_args_list],
            ["custa000486", CUSTOMER],
        )

    def test_a_code_sap_does_not_know_is_refused(self):
        self.reader.get_customer.return_value = None
        form = self._form("CUSTA000000")
        self.assertFalse(form.is_valid())
        self.assertIn("customer_code", form.errors)

    def test_sap_down_refuses_rather_than_link_blind(self):
        self.reader.get_customer.side_effect = SAPConnectionError("down")
        form = self._form(CUSTOMER)
        self.assertFalse(form.is_valid())
        self.assertIn("SAP could not be read", str(form.errors))
