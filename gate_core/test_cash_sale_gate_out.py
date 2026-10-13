"""Cash sale out: marking counter bills out at the gate, without a docking.

SAP is mocked at ``_cash_sale_book`` — the one place the gate reads the
cash-sale book — so these exercise the gate's own rules: which bills are still
inside, what may be marked out, and that a cancelled entry frees its bills.
"""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from driver_management.models import Driver, VehicleEntry
from gate_core.models import (
    CashSaleGateOut,
    CashSaleGateOutBill,
    CashSaleGateOutStatus,
    SalesDispatchDocumentType,
    SalesDispatchGateOut,
    SalesDispatchGateOutDocument,
    SalesDispatchGateOutStatus,
)
from sap_client.exceptions import SAPConnectionError
from vehicle_management.models import Transporter, Vehicle

BASE_URL = "/api/v1/gate-core/sales-dispatch/cash-sales/"
PENDING_URL = f"{BASE_URL}pending-bills/"
BOOK = "gate_core.services.cash_sale_gate_out._cash_sale_book"


def cash_bill(doc_entry, doc_num, *, cancelled=False, doc_date="2026-10-13"):
    return {
        "doc_entry": doc_entry,
        "doc_num": doc_num,
        "doc_date": doc_date,
        "customer_code": "CUSTA000025",
        "customer_name": "HARPREET SINGH CASH SALE",
        "customer_ref": "RAMESH",
        "comments": "",
        "doc_total": 1250.0,
        "branch_name": "Jivo Oil",
        "is_cancelled": cancelled,
        "lines": [
            {"item_code": "FG001", "description": "JIVO CANOLA 1L", "quantity": 12.0, "uom": "PCS"},
            {"item_code": "FG002", "description": "JIVO OLIVE 5L", "quantity": 2.0, "uom": ""},
        ],
    }


class FakeCashSaleBook:
    """SAP's cash-sale book for one company, answering the reader's two calls."""

    def __init__(self, invoices):
        self.invoices = invoices
        self.ar_cash_sale_invoices = mock.Mock(side_effect=self._read)

    def _read(self, card_codes=None, doc_entries=None, **kwargs):
        if doc_entries:
            return [i for i in self.invoices if i["doc_entry"] in doc_entries]
        return list(self.invoices)


class CashSaleGateOutTests(TestCase):
    def setUp(self):
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.bev = Company.objects.create(name="Jivo Beverages", code="JIVO_BEV")
        self.other = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        role = UserRole.objects.create(name="Gate")
        self.user = get_user_model().objects.create_user(
            email="guard@example.com", password="p", full_name="Guard", employee_code="G1",
        )
        for company, default in ((self.oil, True), (self.bev, False)):
            UserCompany.objects.create(
                user=self.user, company=company, role=role, is_default=default
            )
        self.user.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="gate_core",
                codename__in=[
                    "can_view_sales_dispatch_out",
                    "can_dispatch_sales_dispatch_out",
                    "can_cancel_sales_dispatch_out",
                ],
            )
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.client.credentials(HTTP_COMPANY_CODE=self.oil.code)

        self.books = {
            self.oil.code: FakeCashSaleBook(
                [cash_bill(101, 626100501), cash_bill(102, 626100502),
                 cash_bill(103, 626100503, cancelled=True)]
            ),
            self.bev.code: FakeCashSaleBook([cash_bill(201, 726100001)]),
        }
        patcher = mock.patch(BOOK, side_effect=self._book)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _book(self, company):
        book = self.books[company.code]
        if isinstance(book, Exception):
            raise book
        return book, []

    def _mark_out(self, doc_entries, company=None, **extra):
        payload = {
            "company_id": (company or self.oil).id,
            "doc_entries": doc_entries,
            "taken_by_name": "Ramesh Kumar",
            **extra,
        }
        return self.client.post(BASE_URL, payload, format="json")

    def _dock(self, doc_entry, status=SalesDispatchGateOutStatus.DOCKED):
        transporter = Transporter.objects.create(name="T")
        vehicle = Vehicle.objects.create(vehicle_number="HR67F8513", transporter=transporter)
        driver = Driver.objects.create(name="D", mobile_no="9000000000", license_no="DL-1")
        vehicle_entry = VehicleEntry.objects.create(
            entry_no="DOCKV-1", company=self.oil, vehicle=vehicle, driver=driver,
            entry_type="SALES_DISPATCH", status="IN_PROGRESS",
        )
        docking = SalesDispatchGateOut.objects.create(
            company=self.oil, entry_no="DOCK-1", vehicle_entry=vehicle_entry, vehicle=vehicle,
            driver=driver, document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=doc_entry, sap_doc_num="626100502", status=status,
        )
        SalesDispatchGateOutDocument.objects.create(
            sales_dispatch=docking, company=self.oil,
            document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=doc_entry, sap_doc_num="626100502",
        )
        return docking

    # -- picker ---------------------------------------------------------

    def test_pending_bills_span_the_users_companies_and_skip_cancelled(self):
        response = self.client.get(PENDING_URL)

        self.assertEqual(response.status_code, 200)
        bills = response.json()["bills"]
        self.assertEqual(
            sorted((b["company_code"], b["doc_entry"]) for b in bills),
            [("JIVO_BEV", 201), ("JIVO_OIL", 101), ("JIVO_OIL", 102)],
        )
        bill = next(b for b in bills if b["doc_entry"] == 101)
        self.assertEqual(bill["item_summary"], "JIVO CANOLA 1L x 12 PCS; JIVO OLIVE 5L x 2")
        self.assertEqual(bill["customer_ref"], "RAMESH")

    def test_pending_bills_leave_out_bills_already_out_or_on_a_truck(self):
        self.assertEqual(self._mark_out([101]).status_code, 201)
        self._dock(102)

        bills = self.client.get(PENDING_URL).json()["bills"]

        self.assertEqual([b["doc_entry"] for b in bills], [201])

    def test_one_companys_sap_down_still_lists_the_others(self):
        self.books[self.bev.code] = SAPConnectionError("HANA unreachable")

        body = self.client.get(PENDING_URL).json()

        self.assertEqual(sorted(b["doc_entry"] for b in body["bills"]), [101, 102])
        self.assertEqual([e["company_code"] for e in body["errors"]], ["JIVO_BEV"])

    # -- mark out -------------------------------------------------------

    def test_mark_out_snapshots_the_bills_and_the_person(self):
        response = self._mark_out(
            [102, 101], taken_by_mobile=" 98765 43210 ", vehicle_no="hr 69 e 9959",
            remarks="Own tempo",
        )

        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertTrue(body["entry_no"].startswith("CASH-"))
        self.assertEqual(body["company_code"], "JIVO_OIL")
        self.assertEqual(body["taken_by_name"], "Ramesh Kumar")
        self.assertEqual(body["vehicle_no"], "HR69E9959")
        self.assertEqual(body["status"], CashSaleGateOutStatus.OUT)
        self.assertEqual(body["marked_out_by_name"], "Guard")
        self.assertEqual([b["sap_doc_num"] for b in body["bills"]], [626100501, 626100502])
        self.assertEqual(body["bills"][0]["customer_name"], "HARPREET SINGH CASH SALE")
        # Read back from SAP by DocEntry, not trusted from the request.
        self.books[self.oil.code].ar_cash_sale_invoices.assert_called_with(
            card_codes=[], doc_entries=[101, 102], limit=2
        )

    def test_walking_out_without_a_vehicle_is_fine(self):
        response = self._mark_out([101])

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["vehicle_no"], "")

    def test_a_bill_cannot_go_out_twice(self):
        self._mark_out([101])

        response = self._mark_out([101, 102])

        self.assertEqual(response.status_code, 400)
        self.assertIn("Already marked out: 626100501", response.json()["detail"])
        self.assertEqual(CashSaleGateOut.objects.count(), 1)

    def test_refuses_a_bill_that_is_not_a_cash_sale(self):
        response = self._mark_out([999])

        self.assertEqual(response.status_code, 400)
        self.assertIn("Not a cash-sale bill of Jivo Oil", response.json()["detail"])

    def test_refuses_a_cancelled_bill(self):
        response = self._mark_out([103])

        self.assertEqual(response.status_code, 400)
        self.assertIn("Cancelled in SAP", response.json()["detail"])

    def test_refuses_a_bill_already_on_a_docking(self):
        self._dock(102)

        response = self._mark_out([102])

        self.assertEqual(response.status_code, 400)
        self.assertIn("Already on a truck", response.json()["detail"])

    def test_a_cancelled_docking_does_not_hold_the_bill(self):
        self._dock(102, status=SalesDispatchGateOutStatus.CANCELLED)

        self.assertEqual(self._mark_out([102]).status_code, 201)

    def test_name_is_required(self):
        response = self._mark_out([101], taken_by_name="   ")

        self.assertEqual(response.status_code, 400)
        self.assertIn("taken_by_name", response.json())

    def test_sap_down_on_save_is_a_503(self):
        self.books[self.oil.code] = SAPConnectionError("HANA unreachable")

        self.assertEqual(self._mark_out([101]).status_code, 503)

    def test_cannot_mark_out_for_a_company_the_user_is_not_in(self):
        response = self._mark_out([101], company=self.other)

        self.assertEqual(response.status_code, 403)
        self.assertFalse(CashSaleGateOut.objects.exists())

    def test_marking_out_needs_the_dispatch_right(self):
        self.user.user_permissions.remove(
            Permission.objects.get(codename="can_dispatch_sales_dispatch_out")
        )

        self.assertEqual(self._mark_out([101]).status_code, 403)
        self.assertEqual(self.client.get(PENDING_URL).status_code, 403)
        self.assertEqual(self.client.get(BASE_URL).status_code, 200)

    # -- list + cancel --------------------------------------------------

    def test_list_is_the_date_ranges_entries_across_companies(self):
        self._mark_out([101])
        self._mark_out([201], company=self.bev)
        old = CashSaleGateOut.objects.get(company=self.bev)
        old.out_at = timezone.now() - timedelta(days=10)
        old.save(update_fields=["out_at"])
        today = timezone.localdate().isoformat()

        response = self.client.get(BASE_URL, {"from_date": today, "to_date": today})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [(e["company_code"], e["bills"][0]["sap_doc_entry"]) for e in response.json()],
            [("JIVO_OIL", 101)],
        )

    def test_cancelling_frees_the_bill_to_go_out_again(self):
        entry_id = self._mark_out([101]).json()["id"]

        response = self.client.post(
            f"{BASE_URL}{entry_id}/cancel/", {"reason": "Wrong bill picked"}, format="json"
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], CashSaleGateOutStatus.CANCELLED)
        self.assertEqual(response.json()["cancelled_by_name"], "Guard")
        self.assertFalse(CashSaleGateOutBill.objects.get(sap_doc_entry=101).is_active)
        self.assertIn(101, [b["doc_entry"] for b in self.client.get(PENDING_URL).json()["bills"]])
        self.assertEqual(self._mark_out([101]).status_code, 201)

    def test_cancel_needs_a_reason_and_only_once(self):
        entry_id = self._mark_out([101]).json()["id"]
        url = f"{BASE_URL}{entry_id}/cancel/"

        self.assertEqual(self.client.post(url, {"reason": " "}, format="json").status_code, 400)
        self.assertEqual(self.client.post(url, {"reason": "x"}, format="json").status_code, 200)
        second = self.client.post(url, {"reason": "x"}, format="json")
        self.assertEqual(second.status_code, 400)
        self.assertIn("already cancelled", second.json()["detail"])
