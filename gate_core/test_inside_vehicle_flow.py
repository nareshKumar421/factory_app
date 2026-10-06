from decimal import Decimal

from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
from driver_management.models import Driver
from gate_core.models import (
    EmptyVehicleGateInCover,
    EmptyVehicleGateOut,
)
from gate_core.services.empty_vehicle_dispatch import create_vehicle_arrival
from vehicle_management.models import Transporter, Vehicle, VehicleType
from weighment.models import Weighment


class InsideVehicleFlowTests(TestCase):
    """Add-bill-to-inside-vehicle (Part B) and cross-company empty-out (Part C)."""

    def setUp(self):
        self.beverages = Company.objects.create(name="Jivo Beverages", code="JIVO_BEVERAGES")
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        role = UserRole.objects.create(name="Gate")
        self.user = get_user_model().objects.create_user(
            email="inside@example.com",
            password="testpass123",
            full_name="Inside User",
            employee_code="INS001",
        )
        UserCompany.objects.create(
            user=self.user, company=self.beverages, role=role, is_active=True
        )
        UserCompany.objects.create(user=self.user, company=self.oil, role=role, is_active=True)
        self.user.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="dispatch_plans",
                codename__in=[
                    "can_add_bill_inside_vehicle",
                    "can_view_inside_vehicle_manager",
                ],
            )
        )
        vt = VehicleType.objects.create(name="TRUCK-INS")
        self.transporter = Transporter.objects.create(name="Bhargave Road Carrier")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="DL01INS0001", vehicle_type=vt, transporter=self.transporter
        )
        self.driver = Driver.objects.create(
            name="Inside Driver", mobile_no="9000000001", license_no="DL-INS-0001"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _booked(self, company, doc_entry, **kwargs):
        return DispatchPlan.objects.create(
            company=company,
            sap_invoice_doc_entry=doc_entry,
            sap_invoice_doc_num=str(doc_entry),
            booking_status=DispatchPlanStatus.BOOKED,
            dispatch_date=timezone.localdate(),
            vehicle=self.vehicle,
            **kwargs,
        )

    def _arrival(self, tare=Decimal("1500.000"), companies=None):
        companies = companies or [self.beverages, self.oil]
        return create_vehicle_arrival(
            vehicle=self.vehicle,
            driver=self.driver,
            company_ids=[company.id for company in companies],
            gate_in_date=timezone.localdate(),
            in_time=timezone.now().time(),
            tare_weight=tare,
            user=self.user,
        )

    # ---- Part B: add a bill to a vehicle that is already inside --------------
    def test_add_bill_to_inside_vehicle_creates_cover_and_links(self):
        self._booked(self.beverages, 80001)
        arrival = self._arrival()
        gate_in = arrival.gate_ins.get(company=self.beverages)
        new_plan = self._booked(self.beverages, 80009)  # a late second bill

        resp = self._add_bill(gate_in, 80009, bilty_no="2124", bilty_date="2026-10-06")

        self.assertEqual(resp.status_code, 200, resp.data)
        new_plan.refresh_from_db()
        self.assertEqual(new_plan.linked_vehicle_entry_id, gate_in.vehicle_entry_id)
        self.assertTrue(
            EmptyVehicleGateInCover.objects.filter(
                empty_vehicle_gate_in=gate_in, sap_doc_entry=80009, is_active=True
            ).exists()
        )

    def _add_bill(self, gate_in, doc_entry, **bilty):
        return self.client.post(
            "/api/v1/gate-core/inside-dispatch-vehicles/add-bill/",
            {"vehicle_entry_id": gate_in.vehicle_entry_id, "sap_doc_entry": doc_entry, **bilty},
            format="json",
            HTTP_COMPANY_CODE=gate_in.company.code,
        )

    def _add_bill_to_truck(self, company, doc_entry, **bilty):
        return self.client.post(
            "/api/v1/gate-core/inside-dispatch-vehicles/add-bill-to-truck/",
            {
                "vehicle_id": self.vehicle.id,
                "company_code": company.code,
                "sap_doc_entry": doc_entry,
                **bilty,
            },
            format="json",
            HTTP_COMPANY_CODE=company.code,
        )

    def test_add_bill_without_a_bilty_is_refused(self):
        """Linking will not take a truck without each consignee's bilty, and a
        bill added at the gate used to slip past that: its bill summary went to
        the warehouse with no bilty and came straight back."""
        self._booked(self.beverages, 80001)
        gate_in = self._arrival().gate_ins.get(company=self.beverages)
        late = self._booked(self.beverages, 80009)

        no_number = self._add_bill(gate_in, 80009)
        no_date = self._add_bill(gate_in, 80009, bilty_no="2124")

        self.assertEqual(no_number.status_code, 400, no_number.data)
        self.assertIn("bilty number", no_number.data["detail"])
        self.assertEqual(no_date.status_code, 400, no_date.data)
        self.assertIn("bilty date", no_date.data["detail"])
        late.refresh_from_db()
        self.assertIsNone(late.linked_vehicle_entry_id)

    def test_add_bill_records_the_bilty_and_the_truck_s_transporter(self):
        """What linking would have written: without the transporter the bill's
        sheet went over blank there too."""
        self._booked(self.beverages, 80001)
        gate_in = self._arrival().gate_ins.get(company=self.beverages)
        late = self._booked(self.beverages, 80009)

        resp = self._add_bill(gate_in, 80009, bilty_no=" 2124 ", bilty_date="2026-10-06")

        self.assertEqual(resp.status_code, 200, resp.data)
        late.refresh_from_db()
        self.assertEqual(late.bilty_no, "2124")
        self.assertEqual(late.bilty_date, date(2026, 10, 6))
        self.assertEqual(late.transporter, self.transporter)

    def test_add_bill_keeps_a_transporter_planning_chose(self):
        self._booked(self.beverages, 80001)
        gate_in = self._arrival().gate_ins.get(company=self.beverages)
        planned = Transporter.objects.create(name="Abhiman Express")
        late = self._booked(self.beverages, 80009, transporter=planned)

        self._add_bill(gate_in, 80009, bilty_no="2124", bilty_date="2026-10-06")

        late.refresh_from_db()
        self.assertEqual(late.transporter, planned)

    def test_add_bill_keeps_a_bilty_the_plan_already_holds(self):
        """A booked bill being attached was linked through the form, which took
        its bilty then. It is not asked again."""
        self._booked(self.beverages, 80001)
        gate_in = self._arrival().gate_ins.get(company=self.beverages)
        late = self._booked(
            self.beverages, 80009, bilty_no="NCR-4494", bilty_date=date(2026, 10, 5)
        )

        resp = self._add_bill(gate_in, 80009)

        self.assertEqual(resp.status_code, 200, resp.data)
        late.refresh_from_db()
        self.assertEqual(late.bilty_no, "NCR-4494")
        self.assertEqual(late.bilty_date, date(2026, 10, 5))

    def test_a_sent_bilty_comes_whole_never_with_the_plan_s_old_date(self):
        self._booked(self.beverages, 80001)
        gate_in = self._arrival().gate_ins.get(company=self.beverages)
        self._booked(self.beverages, 80009, bilty_no="OLD-1", bilty_date=date(2026, 9, 1))

        resp = self._add_bill(gate_in, 80009, bilty_no="2124")

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("bilty date", resp.data["detail"])

    def test_add_bill_refuses_a_bilty_date_that_is_not_a_date(self):
        self._booked(self.beverages, 80001)
        gate_in = self._arrival().gate_ins.get(company=self.beverages)
        self._booked(self.beverages, 80009)

        resp = self._add_bill(gate_in, 80009, bilty_no="2124", bilty_date="06/10/2026")

        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("not a date", resp.data["detail"])

    def test_add_other_company_s_bill_needs_a_bilty_too(self):
        """The DL01LAN0395 case: the truck was in under Beverages, and Oil's bills
        came on through "Add other bill" with no bilty and no transporter."""
        self._booked(self.beverages, 80001)
        self._arrival(companies=[self.beverages])
        oil_bill = self._booked(self.oil, 80101)

        refused = self._add_bill_to_truck(self.oil, 80101)
        added = self._add_bill_to_truck(
            self.oil, 80101, bilty_no="2125", bilty_date="2026-10-06"
        )

        self.assertEqual(refused.status_code, 400, refused.data)
        self.assertEqual(added.status_code, 200, added.data)
        oil_bill.refresh_from_db()
        self.assertIsNotNone(oil_bill.linked_vehicle_entry_id)
        self.assertEqual(oil_bill.bilty_no, "2125")
        self.assertEqual(oil_bill.transporter, self.transporter)

    def test_the_inside_feed_says_whose_bill_and_which_bilty(self):
        """So a bill added for a consignee already on the truck can be offered
        that consignee's LR instead of having it typed again."""
        self._booked(
            self.beverages,
            80001,
            customer_code="CUSTA000844",
            customer_name="ILAHI CO.",
            bilty_no="2124",
            bilty_date=date(2026, 10, 6),
        )
        self._arrival(companies=[self.beverages])

        resp = self.client.get(
            "/api/v1/gate-core/inside-dispatch-vehicles/",
            HTTP_COMPANY_CODE=self.beverages.code,
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        bill = resp.data[0]["bills"][0]
        self.assertEqual(bill["customer_code"], "CUSTA000844")
        self.assertEqual(bill["customer_name"], "ILAHI CO.")
        self.assertEqual(bill["bilty_no"], "2124")
        self.assertEqual(bill["bilty_date"], "2026-10-06")

    def test_add_bill_rejects_vehicle_not_inside(self):
        resp = self.client.post(
            "/api/v1/gate-core/inside-dispatch-vehicles/add-bill/",
            {"vehicle_entry_id": 999999, "sap_doc_entry": 80001},
            format="json",
            HTTP_COMPANY_CODE=self.beverages.code,
        )
        self.assertEqual(resp.status_code, 404, resp.data)

    # ---- Part C: empty-out cascades across arrival siblings ------------------
    def test_empty_out_cascades_to_sibling_company(self):
        bev_plan = self._booked(self.beverages, 81001)
        oil_plan = self._booked(self.oil, 81002)
        arrival = self._arrival()
        bev_gate_in = arrival.gate_ins.get(company=self.beverages)
        oil_gate_in = arrival.gate_ins.get(company=self.oil)

        # The empty-out POST requires a full gross+tare weighment on the acting
        # (Beverages) entry; give it one (the physical exit weighment).
        weighment = Weighment.objects.get(vehicle_entry=bev_gate_in.vehicle_entry)
        weighment.gross_weight = Decimal("3000.000")
        weighment.save(update_fields=["gross_weight"])

        resp = self.client.post(
            "/api/v1/gate-core/empty-vehicle-outs/",
            {
                "vehicle_entry_id": bev_gate_in.vehicle_entry_id,
                "gate_out_date": timezone.localdate().isoformat(),
                "out_time": "17:00:00",
            },
            format="json",
            HTTP_COMPANY_CODE=self.beverages.code,
        )

        self.assertEqual(resp.status_code, 201, resp.data)
        # Both companies' entries are marked out empty...
        self.assertTrue(
            EmptyVehicleGateOut.objects.filter(
                vehicle_entry=bev_gate_in.vehicle_entry, status="COMPLETED"
            ).exists()
        )
        self.assertTrue(
            EmptyVehicleGateOut.objects.filter(
                vehicle_entry=oil_gate_in.vehicle_entry, status="COMPLETED"
            ).exists()
        )
        # ...and both companies' bills are released + gate-ins retired.
        bev_plan.refresh_from_db()
        oil_plan.refresh_from_db()
        self.assertIsNone(bev_plan.linked_vehicle_entry_id)
        self.assertIsNone(oil_plan.linked_vehicle_entry_id)
        bev_gate_in.refresh_from_db()
        oil_gate_in.refresh_from_db()
        self.assertIsNotNone(bev_gate_in.retired_at)
        self.assertIsNotNone(oil_gate_in.retired_at)

    def test_empty_out_eligible_entries_supports_all_companies(self):
        self._booked(self.beverages, 82001)
        self._booked(self.oil, 82002)
        self._arrival()
        # A gross+tare weighment keeps the entries eligible-list-able; not required
        # for listing, but mirrors real data.
        resp = self.client.get(
            "/api/v1/gate-core/empty-vehicle-outs/eligible-entries/?all_companies=1",
            HTTP_COMPANY_CODE=self.beverages.code,
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        companies = {row.get("company_code") for row in resp.data}
        # Both companies' inside vehicles show up in the aggregated board.
        self.assertIn(self.beverages.code, companies)
        self.assertIn(self.oil.code, companies)
