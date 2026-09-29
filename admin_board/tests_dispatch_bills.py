"""
The bill list under the Total dispatch panel.

The property that matters is that it ADDS UP to the company row that opened it:
the same trucks, the same tonnes, and the same bill count as the tile. The
tests below build a month with every shape that can break that — a truck with
several bills, a bill on two trucks, a removed bill, an intercompany truck, a
truck still at the dock — and check the list and the tile against each other.
"""

from datetime import date, time
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from driver_management.models import Driver, VehicleEntry
from gate_core.models import (
    SalesDispatchDocumentType,
    SalesDispatchGateOut,
    SalesDispatchGateOutDocument,
    SalesDispatchGateOutStatus,
)
from vehicle_management.models import Transporter, Vehicle

from .dispatch_bills import company_bills
from .services import AdminBoardService

User = get_user_model()

TODAY = date(2026, 9, 29)
MONTH_FIRST = date(2026, 9, 1)


class DispatchBillsFixture(TestCase):
    def setUp(self):
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.mart = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        self.user = User.objects.create_user(
            email="bills@example.com", full_name="Bills", password="x"
        )
        self.transporter = Transporter.objects.create(name="Arnav")
        self.vehicle = Vehicle.objects.create(vehicle_number="DL01AB0001", transporter=self.transporter)
        self.driver = Driver.objects.create(name="Ravi", mobile_no="9000000000", license_no="L1")
        self._n = 0

        # A truck carrying two bills, one of them raised last month.
        truck = self._gate_out(self.oil, "DL01AB0001", date(2026, 9, 28), time(17, 5), 1500)
        self._bill(truck, 101, "626090101", date(2026, 8, 30), 1000, amount=200000)
        self._bill(truck, 102, "626090102", date(2026, 9, 26), 500, amount=100000)
        # A bill the dock removed from the truck. Not a bill that left.
        self._bill(truck, 103, "626090103", date(2026, 9, 26), 0, active=False)

        # One bill split across two trucks.
        first = self._gate_out(self.oil, "HR67C1036", date(2026, 9, 10), time(9, 0), 900)
        second = self._gate_out(self.oil, "HR67C2336", date(2026, 9, 11), time(10, 30), 12100)
        self._bill(first, 104, "626080721", date(2026, 9, 9), 900)
        self._bill(second, 104, "626080721", date(2026, 9, 9), 12100)

        # Tins with no weight on the item master: a real dispatch at 0 kg.
        tins = self._gate_out(self.oil, "DL01AB0009", date(2026, 9, 5), time(12, 0), 0)
        self._bill(tins, 105, "626090446", date(2026, 9, 5), 0, amount=2370000)

        # A truck from before documents were recorded: it is its own bill.
        self._gate_out(self.oil, "DL01AB0010", date(2026, 9, 3), time(8, 0), 700, doc_entry=106)

        # None of these are in the month's dispatch.
        group = self._gate_out(self.oil, "DL01AB0002", date(2026, 9, 20), time(11, 0), 5000, customer="CUSTA000606")
        self._bill(group, 201, "626090201", date(2026, 9, 20), 5000)
        docked = self._gate_out(
            self.oil, "DL01AB0003", date(2026, 9, 21), time(11, 0), 4000,
            status=SalesDispatchGateOutStatus.DOCKED,
        )
        self._bill(docked, 202, "626090202", date(2026, 9, 21), 4000)
        august = self._gate_out(self.oil, "DL01AB0004", date(2026, 8, 31), time(11, 0), 3000)
        self._bill(august, 203, "626090203", date(2026, 8, 29), 3000)

        # Mart, with an invoice number that collides with Oil's: two bills.
        mart = self._gate_out(self.mart, "HR67C8170", date(2026, 9, 15), time(12, 47), 800)
        self._bill(mart, 101, "609260101", date(2026, 9, 14), 800)

    def _gate_out(
        self, company, vehicle_no, out_date, out_time, weight_kg,
        customer="CUSTA000844", status=SalesDispatchGateOutStatus.DISPATCHED, doc_entry=None,
    ):
        self._n += 1
        entry = VehicleEntry.objects.create(
            entry_no=f"VE-{self._n}", company=company, vehicle=self.vehicle, driver=self.driver,
            entry_type="SALES_DISPATCH", status="COMPLETED",
        )
        return SalesDispatchGateOut.objects.create(
            company=company, entry_no=f"DK-{self._n}", vehicle_entry=entry,
            vehicle=self.vehicle, transporter=self.transporter, driver=self.driver,
            document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=doc_entry or 9000 + self._n, sap_doc_num=str(doc_entry or ""),
            sap_doc_date=out_date, customer_code=customer, customer_name="ILAHI CO.",
            total_weight=Decimal(weight_kg), vehicle_no=vehicle_no, transporter_name="Arnav",
            driver_name="Ravi", gatepass_no=f"DCK/{company.code}/{self._n}",
            gate_out_date=out_date, out_time=out_time, status=status,
        )

    def _bill(self, gate_out, entry, number, bill_date, weight_kg, amount=None, active=True):
        return SalesDispatchGateOutDocument.objects.create(
            sales_dispatch=gate_out, company=gate_out.company,
            document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=entry,
            sap_doc_num=number, sap_doc_date=bill_date, sap_doc_total=amount,
            customer_code=gate_out.customer_code, customer_name="ILAHI CO.",
            total_weight=Decimal(weight_kg), total_boxes=Decimal(10), is_active=active,
        )


class CompanyBillsTests(DispatchBillsFixture):
    def bills(self):
        return company_bills(self.oil.id, "JIVO_OIL", MONTH_FIRST, TODAY)

    def test_one_row_per_bill_per_truck(self):
        rows = self.bills()["rows"]
        self.assertEqual(
            [row["bill_no"] for row in rows],
            ["626090101", "626090102", "626080721", "626080721", "626090446", "106"],
        )

    def test_it_adds_up_to_the_trucks_that_left(self):
        payload = self.bills()
        self.assertEqual(payload["trucks"], 5)
        # 101, 102, 104 (once, on two trucks), 105, and the undocumented 106.
        self.assertEqual(payload["bills"], 5)
        self.assertEqual(payload["tons"], 15.2)
        self.assertAlmostEqual(sum(row["tons"] for row in payload["rows"]), 15.2)

    def test_the_tile_and_the_list_agree(self):
        service = AdminBoardService(company_code="JIVO_OIL", today=TODAY)
        with mock.patch.object(AdminBoardService, "_invoiced_tons", return_value=None):
            tile = service._dispatch()
        oil_row = next(c for c in tile["companies"] if c["company_code"] == "JIVO_OIL")
        payload = self.bills()
        self.assertEqual(
            (oil_row["tons"], oil_row["trucks"], oil_row["bills"]),
            (payload["tons"], payload["trucks"], payload["bills"]),
        )
        # Mart's 101 is not Oil's 101.
        self.assertEqual(tile["bills"], 6)
        self.assertEqual(tile["trucks"], 6)

    def test_a_row_carries_both_dates_and_the_truck(self):
        row = self.bills()["rows"][0]
        self.assertEqual(row["bill_date"], "2026-08-30")
        self.assertEqual(row["dispatch_date"], "2026-09-28")
        self.assertEqual(row["out_time"], "17:05")
        self.assertEqual(row["days_to_dispatch"], 29)
        self.assertTrue(row["billed_before_month"])
        self.assertEqual(row["vehicle_no"], "DL01AB0001")
        self.assertEqual(row["bills_on_truck"], 2)
        self.assertEqual(row["tons"], 1.0)
        self.assertNotIn("_bill", row)

    def test_a_split_bill_is_flagged_on_both_trucks(self):
        payload = self.bills()
        split = [row for row in payload["rows"] if row["bill_no"] == "626080721"]
        self.assertEqual([row["trucks_for_bill"] for row in split], [2, 2])
        self.assertEqual(payload["split_bills"], 1)

    def test_bills_raised_last_month_are_counted_apart(self):
        self.assertEqual(self.bills()["earlier_bills"], {"bills": 1, "tons": 1.0})

    def test_an_unweighed_bill_is_named_not_hidden(self):
        payload = self.bills()
        tins = next(row for row in payload["rows"] if row["bill_no"] == "626090446")
        self.assertFalse(tins["weighed"])
        self.assertEqual(tins["amount"], 2370000.0)
        self.assertEqual(payload["unweighed_bills"], 1)


class DispatchBillsAPITests(DispatchBillsFixture):
    url = reverse("admin_board:admin-board-dispatch-bills")

    def setUp(self):
        super().setUp()
        role, _ = UserRole.objects.get_or_create(name="Admin")
        UserCompany.objects.create(
            user=self.user, company=self.oil, role=role, is_default=True, is_active=True
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.client.credentials(HTTP_COMPANY_CODE="JIVO_OIL")

    def grant(self, app_label, codename):
        self.user.user_permissions.add(
            Permission.objects.get(content_type__app_label=app_label, codename=codename)
        )

    def test_a_dispatch_reader_gets_their_company_bills(self):
        self.grant("dispatch_plans", "can_view_dispatch_plans")
        # Pinned, so the fixture's September stays "this month" in October.
        with mock.patch("admin_board.views.timezone.localdate", return_value=TODAY):
            response = self.client.get(self.url, {"company": "jivo_oil"})
        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.data["bills"], 5)
        self.assertEqual((response.data["from"], response.data["to"]), ("2026-09-01", "2026-09-29"))

    def test_a_company_the_reader_does_not_belong_to_is_refused(self):
        self.grant("dispatch_plans", "can_view_dispatch_plans")
        response = self.client.get(self.url, {"company": "JIVO_MART"})
        self.assertEqual(response.status_code, 403)

    def test_an_unknown_company_is_a_bad_request(self):
        self.grant("dispatch_plans", "can_view_dispatch_plans")
        self.assertEqual(self.client.get(self.url, {"company": "JIVO_BEVERAGES"}).status_code, 400)
        self.assertEqual(self.client.get(self.url).status_code, 400)

    def test_a_board_right_other_than_dispatch_does_not_open_it(self):
        # A stock reader opens the board but never sees the dispatch tile, so
        # the bills under it are not theirs either.
        self.grant("stock_dashboard", "can_view_stock_dashboard")
        self.assertEqual(self.client.get(self.url, {"company": "JIVO_OIL"}).status_code, 403)
