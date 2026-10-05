"""
A truck's freight against its benchmark: worked out at linking, cleared or
refused in Admin, and read by the gate.

The benchmark arithmetic is tested where each mistake would pass for a real
figure -- a per-kg rate multiplied by the wrong weight, a slab the capacity does
not fall in, a split that loses a paisa -- and the gate where a mistake would let
an uncleared truck in, or keep a cleared one out.
"""

import datetime as dt
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from driver_management.models import Driver
from gate_core.models import EmptyVehicleGateIn
from vehicle_management.models import Transporter, Vehicle

from .freight_approval_service import (
    FreightApprovalError,
    gate_refusal,
    record_truck_freight,
    review,
    truck_plans,
)
from .models import DispatchPlan, DispatchPlanStatus
from .models_freight_approval import DispatchFreightApproval, FreightApprovalStatus
from .models_freight_benchmark import (
    FreightBenchmark,
    FreightDestination,
    FreightRateBasis,
    FreightSlab,
)

GATE_IN_URL = "/api/v1/gate-core/empty-vehicle-ins/"
ARRIVALS_URL = "/api/v1/gate-core/arrivals/"
TRUCK_URL = "/api/v1/dispatch/freight-approvals/truck/"
TRUCKS_URL = "/api/v1/dispatch/freight-approvals/trucks/"
QUEUE_URL = "/api/v1/dispatch/freight-approvals/"
NEVER_LATE_CUTOFF = dt.time(23, 59, 59)

User = get_user_model()


class FreightApprovalBase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.role = UserRole.objects.create(name="Dispatch")
        self.dispatcher = self._user("desk@example.com", "can_link_dispatch_vehicle")
        self.transporter = Transporter.objects.create(name="Om Logistics")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="HR55AB1234",
            transporter=self.transporter,
            capacity_ton=Decimal("9.00"),
        )
        self.five = FreightSlab.objects.create(label="5 MT", above_kg=0, up_to_kg=5000, sort_order=5)
        self.ten = FreightSlab.objects.create(
            label="10 MT", above_kg=5000, up_to_kg=10000, sort_order=10
        )
        self.fifteen = FreightSlab.objects.create(
            label="15 MT", above_kg=10000, up_to_kg=15000, sort_order=15
        )
        self.kg8 = FreightSlab.objects.create(
            label="5,001-8,000 kg", above_kg=5000, up_to_kg=8000, sort_order=1080
        )
        self.khanna = FreightDestination.objects.create(state="PUNJAB", name="KHANNA")
        FreightBenchmark.objects.create(destination=self.khanna, slab=self.ten, amount=Decimal("13500"))
        FreightBenchmark.objects.create(
            destination=self.khanna, slab=self.fifteen, amount=Decimal("20250")
        )
        self.delhi = FreightDestination.objects.create(state="DELHI NCR", name="DELHI")
        FreightBenchmark.objects.create(
            destination=self.delhi,
            slab=self.kg8,
            basis=FreightRateBasis.PER_KG,
            amount=Decimal("1.20"),
        )
        self.today = timezone.localdate()

    def _user(self, email, *codenames):
        user = User.objects.create_user(
            email=email, password="x", full_name=email.split("@")[0].title(),
            employee_code=email.split("@")[0].upper(),
        )
        UserCompany.objects.create(user=user, company=self.company, role=self.role, is_default=True)
        for codename in codenames:
            user.user_permissions.add(Permission.objects.get(codename=codename))
        return User.objects.get(pk=user.pk)

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def _book(self, doc_entry, *, litres="1000", weight="1000", customer="Acme"):
        return DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=doc_entry,
            sap_invoice_doc_num=str(626090000 + doc_entry),
            customer_name=customer,
            vehicle=self.vehicle,
            transporter=self.transporter,
            booking_status=DispatchPlanStatus.BOOKED,
            dispatch_date=self.today,
            total_litres=Decimal(litres),
            invoice_weight=Decimal(weight),
        )

    def _freight(self, amount, *, destination=None, slab=None, reason="", plans=None, extend=False):
        """The freight over `plans` (default: every bill booked on the truck)."""
        if plans is None:
            plans = DispatchPlan.objects.filter(vehicle=self.vehicle, booking_status="BOOKED")
        return record_truck_freight(
            vehicle=self.vehicle,
            company_ids=[self.company.pk],
            bills=[(self.company.code, plan.sap_invoice_doc_entry) for plan in plans],
            extend=extend,
            destination=destination or self.khanna,
            slab=slab or self.ten,
            actual_freight=Decimal(amount),
            reason=reason,
            user=self.dispatcher,
        )["approval"]


class BenchmarkTests(FreightApprovalBase):
    def test_freight_at_or_under_the_benchmark_needs_nothing_and_is_still_recorded(self):
        self._book(1)
        approval = self._freight("13500")

        self.assertEqual(approval.status, FreightApprovalStatus.WITHIN_BENCHMARK)
        self.assertEqual(approval.benchmark_freight, Decimal("13500.00"))
        self.assertTrue(gate_refusal(self.vehicle, truck_plans(self.vehicle, [self.company.pk])) is None)

    def test_freight_over_the_benchmark_waits_for_an_approver(self):
        self._book(1)
        with patch(
            "notifications.services.NotificationService.send_notification_by_permission"
        ) as notify, self.captureOnCommitCallbacks(execute=True):
            approval = self._freight("15000", reason="Diwali rush")

        self.assertEqual(approval.status, FreightApprovalStatus.PENDING)
        self.assertEqual(approval.excess, Decimal("1500.00"))
        notify.assert_called_once()
        self.assertEqual(
            notify.call_args.kwargs["permission_codename"], "can_approve_freight_approvals"
        )

    def test_a_slab_with_no_benchmark_has_nothing_to_be_under(self):
        self._book(1)
        approval = self._freight("9000", slab=self.five, reason="Small load")

        self.assertIsNone(approval.benchmark_freight)
        self.assertEqual(approval.status, FreightApprovalStatus.PENDING)

    def test_the_capacity_suggests_the_slab_and_a_different_pick_is_kept_for_the_approver(self):
        self._book(1)
        approval = self._freight("20000", slab=self.fifteen)

        # 9 T sits in 10 MT; the desk ran it as a 15 MT load.
        self.assertEqual(approval.vehicle_capacity_kg, 9000)
        self.assertEqual(approval.suggested_slab, self.ten)
        self.assertEqual(approval.slab, self.fifteen)
        self.assertEqual(approval.status, FreightApprovalStatus.WITHIN_BENCHMARK)

    def test_a_per_kg_rate_is_multiplied_by_the_bills_weight_not_the_capacity(self):
        self._book(1, weight="4000")
        self._book(2, weight="2400")
        approval = self._freight("7000", destination=self.delhi, slab=self.kg8, reason="x")

        # ₹1.20 × 6,400 kg of bills, not × the 9,000 kg the truck could carry.
        self.assertEqual(approval.load_kg, Decimal("6400"))
        self.assertEqual(approval.benchmark_freight, Decimal("7680.00"))
        self.assertEqual(approval.status, FreightApprovalStatus.WITHIN_BENCHMARK)

    def test_the_truck_freight_is_split_over_its_bills_by_litres_to_the_paisa(self):
        a = self._book(1, litres="1000")
        b = self._book(2, litres="2000")
        self._freight("10000")

        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.freight, b.freight), (Decimal("3333.33"), Decimal("6666.67")))
        self.assertEqual(a.total_freight + b.total_freight, Decimal("10000.00"))

    def test_a_truck_with_no_booked_bills_has_nothing_to_hold_the_freight_against(self):
        with self.assertRaises(FreightApprovalError):
            self._freight("13500")

    def test_one_basis_for_every_bill_so_a_bill_with_no_litres_does_not_take_it_all(self):
        # The second bill carries no litres. Splitting it by invoice value while
        # the first goes by litres would hand it nearly the whole freight.
        a = self._book(1, litres="3000", weight="3100")
        b = self._book(2, litres="0", weight="3100")
        DispatchPlan.objects.filter(pk=b.pk).update(invoice_amount=Decimal("250000"))
        self._freight("9200")

        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.freight, b.freight), (Decimal("4600.00"), Decimal("4600.00")))

    def test_a_bill_not_booked_on_the_truck_is_refused_by_number(self):
        self._book(1)
        with self.assertRaisesMessage(FreightApprovalError, "99"):
            record_truck_freight(
                vehicle=self.vehicle,
                company_ids=[self.company.pk],
                bills=[(self.company.code, 99)],
                destination=self.khanna,
                slab=self.ten,
                actual_freight=Decimal("13000"),
            )


class CoverageTests(FreightApprovalBase):
    """Which of the truck's bookings the freight is for."""

    def test_an_old_booking_left_on_the_truck_is_not_pulled_into_the_freight(self):
        # Booked onto this truck in May and never dispatched: still BOOKED, so
        # the gate would carry it, but the desk is freighting today's two bills.
        stale = self._book(1, litres="5000")
        today = [self._book(2, litres="3000"), self._book(3, litres="3000")]
        approval = self._freight("9200", plans=today, reason="Transporter rates")

        stale.refresh_from_db()
        self.assertIsNone(stale.freight)
        self.assertIsNone(stale.freight_approval_id)
        self.assertEqual(approval.bill_count, 2)
        self.assertEqual(
            sorted(DispatchPlan.objects.filter(freight_approval=approval).values_list("freight", flat=True)),
            [Decimal("4600.00"), Decimal("4600.00")],
        )

    def test_adding_a_bill_from_the_linking_sheet_keeps_the_ones_already_freighted(self):
        first = self._book(1)
        self._freight("13000", plans=[first])
        added = self._book(2)

        approval = self._freight("13000", plans=[added], extend=True)

        self.assertEqual(
            set(DispatchPlan.objects.filter(freight_approval=approval).values_list("pk", flat=True)),
            {first.pk, added.pk},
        )

    def test_a_bill_dropped_from_the_freight_comes_off_it_with_its_share(self):
        a = self._book(1)
        b = self._book(2)
        self._freight("13000", plans=[a, b])

        self._freight("13000", plans=[a])

        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.freight, Decimal("13000.00"))
        self.assertIsNone(b.freight)
        self.assertIsNone(b.freight_approval_id)

    def test_a_truck_has_one_live_freight_whichever_bills_it_names(self):
        a = self._book(1)
        b = self._book(2)
        first = self._freight("15000", plans=[a], reason="Rush")

        second = self._freight("12000", plans=[b])

        first.refresh_from_db()
        a.refresh_from_db()
        self.assertEqual(first.status, FreightApprovalStatus.SUPERSEDED)
        self.assertIsNone(a.freight_approval_id)
        self.assertEqual(second.status, FreightApprovalStatus.WITHIN_BENCHMARK)


class RelinkTests(FreightApprovalBase):
    def test_the_same_price_again_keeps_the_approval_even_with_a_bill_added(self):
        self._book(1)
        first = self._freight("15000", reason="Rush")
        review(first, approve=True, reviewer=self.dispatcher)
        self._book(2)

        again = self._freight("15000")

        self.assertEqual(again.pk, first.pk)
        self.assertEqual(again.status, FreightApprovalStatus.APPROVED)
        self.assertEqual(again.bill_count, 2)
        self.assertEqual(DispatchPlan.objects.filter(freight_approval=first).count(), 2)

    def test_a_new_price_supersedes_the_old_row_and_asks_again(self):
        self._book(1)
        first = self._freight("15000", reason="Rush")
        review(first, approve=True, reviewer=self.dispatcher)

        second = self._freight("16000", reason="Rush, and a toll")

        first.refresh_from_db()
        self.assertEqual(first.status, FreightApprovalStatus.SUPERSEDED)
        self.assertEqual(second.status, FreightApprovalStatus.PENDING)

    def test_relinking_a_refused_truck_under_the_benchmark_clears_it(self):
        self._book(1)
        refused = self._freight("15000", reason="Rush")
        review(refused, approve=False, reviewer=self.dispatcher, notes="Too high")

        self._freight("13000")

        plans = truck_plans(self.vehicle, [self.company.pk])
        self.assertIsNone(gate_refusal(self.vehicle, plans))


class ReviewTests(FreightApprovalBase):
    def test_a_refusal_needs_a_reason(self):
        self._book(1)
        approval = self._freight("15000", reason="Rush")
        with self.assertRaises(FreightApprovalError):
            review(approval, approve=False, reviewer=self.dispatcher, notes="")

    def test_a_decision_is_not_taken_twice(self):
        self._book(1)
        approval = self._freight("15000", reason="Rush")
        review(approval, approve=True, reviewer=self.dispatcher)
        with self.assertRaises(FreightApprovalError):
            review(approval, approve=False, reviewer=self.dispatcher, notes="Changed my mind")

    def test_deciding_needs_the_approve_right_and_the_queue_the_view_right(self):
        self._book(1)
        approval = self._freight("15000", reason="Rush")
        viewer = self._user("viewer@example.com", "can_view_freight_approvals")
        approver = self._user("boss@example.com", "can_approve_freight_approvals")
        headers = {"HTTP_COMPANY_CODE": self.company.code}

        self.assertEqual(self._client(self.dispatcher).get(QUEUE_URL, **headers).status_code, 403)
        listed = self._client(viewer).get(QUEUE_URL, **headers)
        self.assertEqual([row["id"] for row in listed.json()], [approval.pk])
        self.assertEqual(listed.json()[0]["excess"], 1500.0)
        self.assertEqual(
            self._client(viewer)
            .post(f"{QUEUE_URL}{approval.pk}/approve/", {}, format="json", **headers)
            .status_code,
            403,
        )
        # The patch outlives the captured callbacks, which run as that block exits.
        with patch(
            "notifications.services.NotificationService.send_notification_to_user"
        ) as notify, self.captureOnCommitCallbacks(execute=True):
            response = self._client(approver).post(
                f"{QUEUE_URL}{approval.pk}/approve/", {"notes": "Festival rates"},
                format="json", **headers,
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["status"], "APPROVED")
        self.assertEqual(notify.call_args.kwargs["user"], self.dispatcher)

    def test_reject_without_notes_is_a_400_with_a_sentence(self):
        self._book(1)
        approval = self._freight("15000", reason="Rush")
        approver = self._user("boss@example.com", "can_approve_freight_approvals")
        response = self._client(approver).post(
            f"{QUEUE_URL}{approval.pk}/reject/", {}, format="json",
            HTTP_COMPANY_CODE=self.company.code,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Say why", response.json()["detail"])


class TruckEndpointTests(FreightApprovalBase):
    def test_the_desk_enters_a_trucks_freight_and_the_board_reads_it_back(self):
        self._book(1)
        self._book(2)
        headers = {"HTTP_COMPANY_CODE": self.company.code}
        desk = self._client(self.dispatcher)

        response = desk.post(
            TRUCK_URL,
            {
                "vehicle_id": self.vehicle.pk,
                "destination_id": self.khanna.pk,
                "slab_id": self.ten.pk,
                "actual_freight": "12000",
                "bills": [
                    {"company_code": self.company.code, "doc_entry": 1},
                    {"company_code": self.company.code, "doc_entry": 2},
                ],
            },
            format="json",
            **headers,
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["approval"]["status"], "WITHIN_BENCHMARK")
        self.assertEqual(sum(share["freight"] for share in body["shares"]), 12000.0)

        (truck,) = desk.get(TRUCKS_URL, **headers).json()
        self.assertEqual(truck["vehicle_id"], self.vehicle.pk)
        self.assertEqual(len(truck["covered_bills"]), 2)
        self.assertEqual(truck["approval"]["actual_freight"], 12000.0)

    def test_the_board_names_the_bills_the_freight_covers(self):
        # A bill added since is not among them; the board, which knows which
        # bills it shows, badges it as not covered.
        first = self._book(1)
        self._freight("12000", plans=[first])
        self._book(2)
        (truck,) = self._client(self.dispatcher).get(
            TRUCKS_URL, HTTP_COMPANY_CODE=self.company.code
        ).json()
        self.assertEqual(
            truck["covered_bills"], [{"company_code": self.company.code, "doc_entry": 1}]
        )

    def test_the_bills_are_required(self):
        self._book(1)
        response = self._client(self.dispatcher).post(
            TRUCK_URL,
            {
                "vehicle_id": self.vehicle.pk,
                "destination_id": self.khanna.pk,
                "slab_id": self.ten.pk,
                "actual_freight": "12000",
                "bills": [],
            },
            format="json",
            HTTP_COMPANY_CODE=self.company.code,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("bills", response.json())

    def test_entering_freight_needs_the_linking_right(self):
        self._book(1)
        nobody = self._user("nobody@example.com")
        response = self._client(nobody).post(
            TRUCK_URL,
            {
                "vehicle_id": self.vehicle.pk,
                "destination_id": self.khanna.pk,
                "slab_id": self.ten.pk,
                "actual_freight": "12000",
            },
            format="json",
            HTTP_COMPANY_CODE=self.company.code,
        )
        self.assertEqual(response.status_code, 403)


@override_settings(LATE_DISPATCH_GATE_IN_CUTOFF=NEVER_LATE_CUTOFF)
class GateTests(FreightApprovalBase):
    def setUp(self):
        super().setUp()
        self.gate = self._user("gate@example.com")
        self.driver = Driver.objects.create(
            name="Test Driver", mobile_no="9000000000", license_no="DL-FREIGHT-1"
        )
        self.headers = {"HTTP_COMPANY_CODE": self.company.code}

    def _gate_in(self):
        return self._client(self.gate).post(
            GATE_IN_URL,
            {
                "vehicle_id": self.vehicle.id,
                "driver_id": self.driver.id,
                "reason": "DISPATCH",
                "gate_in_date": self.today.isoformat(),
                "in_time": "10:00",
            },
            format="json",
            **self.headers,
        )

    def _arrival(self):
        return self._client(self.gate).post(
            ARRIVALS_URL,
            {
                "vehicle_id": self.vehicle.id,
                "driver_id": self.driver.id,
                "gate_in_date": self.today.isoformat(),
                "in_time": "10:00",
            },
            format="json",
            **self.headers,
        )

    def test_a_truck_waiting_on_its_freight_is_refused_at_both_doors(self):
        self._book(1)
        self._freight("15000", reason="Rush")

        for response in (self._gate_in(), self._arrival()):
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.data["code"], "FREIGHT_APPROVAL_REQUIRED")
            self.assertEqual(response.data["approval_status"], "PENDING")
        self.assertFalse(EmptyVehicleGateIn.objects.exists())

    def test_a_refused_freight_is_refused_with_the_approvers_reason(self):
        self._book(1)
        approval = self._freight("15000", reason="Rush")
        review(approval, approve=False, reviewer=self.dispatcher, notes="Use Om at 13,500")

        response = self._gate_in()

        self.assertEqual(response.status_code, 400)
        self.assertIn("Use Om at 13,500", response.data["detail"])

    def test_an_approved_freight_lets_the_truck_in(self):
        self._book(1)
        approval = self._freight("15000", reason="Rush")
        review(approval, approve=True, reviewer=self.dispatcher)

        self.assertEqual(self._gate_in().status_code, 201)

    def test_a_truck_linked_before_freight_was_asked_for_is_let_in(self):
        self._book(1)
        self.assertEqual(self._arrival().status_code, 201)

    def test_an_approval_taken_for_another_truck_does_not_hold_this_one(self):
        plan = self._book(1)
        approval = self._freight("15000", reason="Rush")
        other = Vehicle.objects.create(vehicle_number="PB10ZZ9999", transporter=self.transporter)
        DispatchPlan.objects.filter(pk=plan.pk).update(vehicle=other)
        self.assertEqual(approval.status, FreightApprovalStatus.PENDING)

        self.assertIsNone(gate_refusal(other, truck_plans(other, [self.company.pk])))
