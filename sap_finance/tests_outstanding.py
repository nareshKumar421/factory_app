"""The outstanding reports (from EXIM): party balances, open bills, open GRPOs,
customer aging.

The SAP reads are mocked; what is pinned down is what the reports make of them:
  - a balance's sign reads as who owes whom, and the totals split by it;
  - open bills page, filter and bucket on the server, overdue by the due date;
  - a GRPO is one row, and "raw material only" narrows to those with an RM line;
  - aging takes open documents only, nets credit notes, and ages by due date
    unless asked to age by bill date;
  - the right to the outstanding reports, or EXIM's right for that one report,
    opens it; nothing else does; SAP down is a 503.
"""

from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPConnectionError

from . import outstanding

D = Decimal
BASE = "/api/v1/sap-finance/outstanding/"
TODAY = timezone.localdate()


def bill(num, card="VENDA1", due_in=0, total="100", paid="0", group="PURCHASE OIL"):
    return {
        "doc_entry": int(num), "doc_num": str(num), "doc_date": TODAY - timedelta(days=40),
        "due_date": TODAY + timedelta(days=due_in), "party_ref": f"INV/{num}", "card_code": card,
        "card_name": f"Party {card}", "group": group, "sales_employee": "", "currency": "INR",
        "total": D(total), "paid": D(paid), "due": D(total) - D(paid), "total_fc": D("0"), "paid_fc": D("0"),
        "remarks": "", "vehicle_number": "HR55A1",
    }


class PartyTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")

    def test_a_balance_reads_as_who_owes_whom(self):
        rows = [
            {"card_code": "V1", "card_name": "A", "group": "PURCHASE OIL", "sales_employee": "", "balance": D("-500"),
             "currency": "INR", "last_bill": {"number": "1", "date": TODAY - timedelta(days=3), "total": D("5")},
             "last_payment": None},
            {"card_code": "V2", "card_name": "B", "group": "PURCHASE", "sales_employee": "", "balance": D("200"),
             "currency": "INR", "last_bill": None, "last_payment": None},
        ]
        with mock.patch("sap_finance.outstanding_reader.party_balances", return_value=rows) as read:
            data = outstanding.party_outstanding(self.company, "vendor", oil_suppliers=True)
        read.assert_called_once_with("JIVO_OIL", "vendor", oil_suppliers=True)
        self.assertEqual(data["totals"], {"parties": 2, "debit": D("200"), "credit": D("-500"), "net": D("-300")})
        self.assertEqual(data["rows"][0]["days_since_bill"], 3)
        self.assertIsNone(data["rows"][0]["days_since_payment"])
        self.assertEqual(data["groups"], ["PURCHASE", "PURCHASE OIL"])


class OpenBillTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.rows = [bill(1, due_in=5), bill(2, due_in=-10), bill(3, card="VENDA2", due_in=-100, total="300"),
                     bill(4, due_in=-200, paid="40")]

    def read(self, **filters):
        with mock.patch("sap_finance.outstanding_reader.open_bills", return_value=self.rows):
            return outstanding.open_bills(self.company, "vendor", **filters)

    def test_overdue_is_counted_from_the_due_date_into_buckets(self):
        data = self.read()
        buckets = {b["key"]: (b["count"], b["due"]) for b in data["buckets"]}
        self.assertEqual(buckets["not_due"], (1, D("100")))
        self.assertEqual(buckets["d0_30"], (1, D("100")))
        self.assertEqual(buckets["d91_180"], (1, D("300")))
        self.assertEqual(buckets["d180"], (1, D("60")))
        self.assertEqual(data["totals"]["due"], D("560"))
        self.assertEqual(data["totals"]["overdue"], D("460"))

    def test_filters_and_paging_happen_on_the_server(self):
        data = self.read(bucket="d0_30")
        self.assertEqual([r["doc_num"] for r in data["results"]], ["2"])
        # The other buckets still say what they hold.
        self.assertEqual({b["key"]: b["count"] for b in data["buckets"]}["d91_180"], 1)
        data = self.read(card_code="VENDA2")
        self.assertEqual(data["totals"]["count"], 1)
        data = self.read(q="inv/4")
        self.assertEqual([r["doc_num"] for r in data["results"]], ["4"])
        data = self.read(page_size=2, page=2, sort="due", descending=True)
        self.assertEqual((data["count"], data["pages"], data["page"]), (4, 2, 2))
        self.assertEqual([r["doc_num"] for r in data["results"]], ["2", "4"])
        self.assertEqual(data["top_parties"][0]["card_code"], "VENDA2")


class GrpoTests(TestCase):
    def test_raw_material_only_narrows_and_days_are_counted(self):
        company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        rows = [
            {"doc_entry": 1, "doc_num": "1", "doc_date": TODAY - timedelta(days=9), "party_ref": "", "card_code": "V",
             "card_name": "V", "total": D("10"), "currency": "INR", "user": "U", "warehouses": ["BH-GJ"], "lines": 2,
             "raw_material": True},
            {"doc_entry": 2, "doc_num": "2", "doc_date": TODAY - timedelta(days=1), "party_ref": "", "card_code": "W",
             "card_name": "W", "total": D("5"), "currency": "INR", "user": "U", "warehouses": ["BH-PM"], "lines": 1,
             "raw_material": False},
        ]
        with mock.patch("sap_finance.outstanding_reader.open_grpos", return_value=rows):
            every = outstanding.open_grpos(company)
            raw = outstanding.open_grpos(company, raw_material_only=True)
        self.assertEqual(every["totals"]["count"], 2)
        self.assertEqual(every["totals"]["oldest_days"], 9)
        self.assertEqual([r["doc_num"] for r in raw["rows"]], ["1"])
        self.assertEqual(raw["warehouses"], ["BH-GJ"])


class AgingTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.rows = [
            {"kind": "INVOICE", "doc_num": "1", "doc_date": TODAY - timedelta(days=70),
             "due_date": TODAY - timedelta(days=40), "card_code": "C1", "card_name": "Cust", "sales_employee": "S",
             "group": "G", "due": D("1000")},
            {"kind": "CREDIT_NOTE", "doc_num": "9", "doc_date": TODAY - timedelta(days=5),
             "due_date": TODAY - timedelta(days=5), "card_code": "C1", "card_name": "Cust", "sales_employee": "S",
             "group": "G", "due": D("-100")},
            {"kind": "INVOICE", "doc_num": "2", "doc_date": TODAY, "due_date": TODAY + timedelta(days=30),
             "card_code": "C2", "card_name": "Other", "sales_employee": "T", "group": "H", "due": D("50")},
        ]

    def read(self, **kw):
        with mock.patch("sap_finance.outstanding_reader.open_receivables", return_value=self.rows):
            return outstanding.customer_aging(self.company, **kw)

    def test_aging_by_due_date_nets_credit_notes(self):
        data = self.read()
        c1 = next(r for r in data["rows"] if r["card_code"] == "C1")
        self.assertEqual((c1["total"], c1["d31_60"], c1["d0_30"]), (D("900"), D("1000"), D("-100")))
        self.assertEqual(data["totals"]["not_due"], D("50"))

    def test_aging_by_bill_date_and_one_customers_documents(self):
        data = self.read(basis="bill", card_code="C2")
        c2 = next(r for r in data["rows"] if r["card_code"] == "C2")
        self.assertEqual(c2["d0_30"], D("50"))  # billed today: the first bucket, not "not due"
        self.assertEqual([d["doc_num"] for d in data["documents"]], ["2"])
        self.assertEqual([r["card_code"] for r in self.read(sales_employee="T")["rows"]], ["C2"])


class OutstandingAPITests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.role = UserRole.objects.create(name="Accounts")
        self.headers = {"HTTP_COMPANY_CODE": "JIVO_OIL"}

    def client_for(self, *rights):
        n = get_user_model().objects.count() + 1
        user = get_user_model().objects.create_user(email=f"o{n}@example.com", password="x", full_name="O",
                                                    employee_code=f"O{n}")
        UserCompany.objects.create(user=user, company=self.company, role=self.role, is_default=True)
        for right in rights:
            label, codename = right.split(".")
            user.user_permissions.add(Permission.objects.get(content_type__app_label=label, codename=codename))
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_the_outstanding_right_or_exims_own_opens_each_report(self):
        with mock.patch("sap_finance.outstanding_reader.open_bills", return_value=[bill(1)]):
            native = self.client_for("sap_finance.can_view_sap_outstanding")
            self.assertEqual(native.get(f"{BASE}bills/", {"side": "customer"}, **self.headers).status_code, 200)
            exim_ap = self.client_for("exim.view_open_aps")
            self.assertEqual(exim_ap.get(f"{BASE}bills/", {"side": "vendor"}, **self.headers).status_code, 200)
            # EXIM's open A/P right is not its open A/R right.
            self.assertEqual(exim_ap.get(f"{BASE}bills/", {"side": "customer"}, **self.headers).status_code, 403)
            ledgers_only = self.client_for("sap_finance.can_view_sap_ledgers")
            self.assertEqual(ledgers_only.get(f"{BASE}bills/", **self.headers).status_code, 403)
        with mock.patch("sap_finance.outstanding_reader.open_receivables", return_value=[]):
            self.assertEqual(self.client_for("exim.view_customer_aging").get(f"{BASE}aging/", **self.headers)
                             .status_code, 200)
        self.assertEqual(self.client_for().get(f"{BASE}aging/", **self.headers).status_code, 403)

    def test_sap_down_is_a_503(self):
        with mock.patch("sap_finance.outstanding_reader.open_grpos", side_effect=SAPConnectionError("down")):
            response = self.client_for("exim.sync_open_grpos").get(f"{BASE}grpos/", **self.headers)
        self.assertEqual(response.status_code, 503)

    def test_party_balances_by_side(self):
        with mock.patch("sap_finance.outstanding_reader.party_balances", return_value=[]) as read:
            client = self.client_for("exim.view_customer_outstanding")
            self.assertEqual(client.get(f"{BASE}parties/", {"side": "customer"}, **self.headers).status_code, 200)
            self.assertEqual(client.get(f"{BASE}parties/", {"side": "vendor"}, **self.headers).status_code, 403)
        read.assert_called_once_with("JIVO_OIL", "customer", oil_suppliers=False)
