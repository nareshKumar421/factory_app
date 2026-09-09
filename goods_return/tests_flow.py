"""The booking flow: the gate sees a return from its first page onwards.

The vehicle is the first thing the clerk fills in, and saving that page puts the
return in the gate's arrival queue while the items are still being keyed in.
These tests pin that hand-off -- and the editing it implies, since the truck can
pull up before the clerk has finished.

Uses the DEBIT_NOTE basis throughout: an invoice-basis return would call SAP.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from driver_management.models import Driver
from vehicle_management.models import Vehicle

from .models import (
    GoodsReturn,
    GoodsReturnInvoiceRef,
    GoodsReturnItem,
    GoodsReturnStatus,
)
from .serializers import GoodsReturnListSerializer
from .services import (
    GoodsReturnService,
    list_expected_returns,
    list_gate_history,
    mark_return_in,
)


class GoodsReturnFlowTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="OIL")
        self.user = get_user_model().objects.create(
            email="clerk@example.com", full_name="Return Clerk"
        )
        self.vehicle = Vehicle.objects.create(vehicle_number="PB01AB1234")
        self.driver = Driver.objects.create(
            name="Ranjit", mobile_no="9990001111", license_no="DL-1"
        )
        self.service = GoodsReturnService(self.company)
        self.allowed = [self.company.id]

    def create(self, **overrides):
        data = {
            "basis": "DEBIT_NOTE",
            "customer_name": "Sharma Traders",
            "vehicle_id": self.vehicle.id,
            "driver_id": self.driver.id,
        }
        data.update(overrides)
        return self.service.create_return(data, self.user)

    # -- creation ------------------------------------------------------------

    def test_a_new_return_is_awaiting_arrival_not_a_draft(self):
        gr = self.create()
        self.assertEqual(gr.status, GoodsReturnStatus.AWAITING_ARRIVAL)
        self.assertEqual(gr.vehicle_id, self.vehicle.id)
        self.assertEqual(gr.driver_id, self.driver.id)

    def test_the_gate_can_see_it_before_a_single_item_is_entered(self):
        gr = self.create()
        self.assertEqual(gr.lines.count(), 0)
        self.assertIn(gr, list(list_expected_returns(self.allowed)))

    def test_no_vehicle_is_refused(self):
        with self.assertRaises(ValueError):
            self.create(vehicle_id=None)

    def test_no_driver_is_refused(self):
        with self.assertRaises(ValueError):
            self.create(driver_id=None)

    def test_an_unknown_vehicle_is_refused(self):
        with self.assertRaises(ValueError):
            self.create(vehicle_id=self.vehicle.id + 999)

    # -- editing while the gate waits ----------------------------------------

    def test_items_can_still_be_saved_once_it_is_awaiting_arrival(self):
        gr = self.create()
        gr = self.service.save_items(
            gr.id,
            [{"item_code": "FG1", "return_quantity": 2}],
            self.user,
            self.allowed,
        )
        self.assertEqual(len(gr.active_lines), 1)

    def test_items_can_still_be_saved_after_the_truck_is_inside(self):
        gr = self.create()
        mark_return_in(gr.id, self.user, {}, self.allowed)
        gr = self.service.save_items(
            gr.id,
            [{"item_code": "FG1", "return_quantity": 2}],
            self.user,
            self.allowed,
        )
        self.assertEqual(gr.status, GoodsReturnStatus.ARRIVED)
        self.assertEqual(len(gr.active_lines), 1)

    # -- the truck itself -----------------------------------------------------

    def test_the_vehicle_can_be_corrected_but_not_cleared(self):
        gr = self.create()
        other = Vehicle.objects.create(vehicle_number="PB02CD5678")
        gr = self.service.set_vehicle(
            gr.id, {"vehicle_id": other.id}, self.user, self.allowed
        )
        self.assertEqual(gr.vehicle_id, other.id)
        with self.assertRaises(ValueError):
            self.service.set_vehicle(
                gr.id, {"vehicle_id": None}, self.user, self.allowed
            )

    def test_the_vehicle_cannot_be_swapped_once_it_is_inside(self):
        gr = self.create()
        mark_return_in(gr.id, self.user, {}, self.allowed)
        other = Vehicle.objects.create(vehicle_number="PB03EF9012")
        with self.assertRaises(ValueError):
            self.service.set_vehicle(
                gr.id, {"vehicle_id": other.id}, self.user, self.allowed
            )

    def test_the_gate_marks_in_the_vehicle_booked_on_the_first_page(self):
        gr = self.create()
        gr = mark_return_in(gr.id, self.user, {}, self.allowed)
        self.assertEqual(gr.status, GoodsReturnStatus.ARRIVED)
        self.assertEqual(gr.vehicle_entry.vehicle_id, self.vehicle.id)
        self.assertEqual(gr.vehicle_entry.entry_type, "GOODS_RETURN")
        # Off the gate's queue once it is in.
        self.assertNotIn(gr, list(list_expected_returns(self.allowed)))

    # -- submit ---------------------------------------------------------------

    def test_submit_records_the_clerk_without_moving_an_arrived_return_back(self):
        gr = self.create()
        self.service.save_items(
            gr.id,
            [{"item_code": "FG1", "return_quantity": 2}],
            self.user,
            self.allowed,
        )
        self.service.upload_attachment(
            gr.id, _a_file(), "DEBIT_NOTE", "", self.user, self.allowed
        )
        mark_return_in(gr.id, self.user, {}, self.allowed)

        gr = self.service.submit(gr.id, self.user, self.allowed)
        self.assertEqual(gr.status, GoodsReturnStatus.ARRIVED)
        self.assertIsNotNone(gr.submitted_at)

    def test_submit_still_needs_items_and_a_document(self):
        gr = self.create()
        with self.assertRaises(ValueError):
            self.service.submit(gr.id, self.user, self.allowed)


class GateHistoryTests(TestCase):
    """The queue drops a return the moment it is marked in; the history tab is
    where the gate goes to see what it let in earlier."""

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="OIL")
        self.other_company = Company.objects.create(name="Jivo Mart", code="MART")
        self.user = get_user_model().objects.create(
            email="guard@example.com", full_name="Gate Guard"
        )
        self.vehicle = Vehicle.objects.create(vehicle_number="PB01AB1234")
        self.driver = Driver.objects.create(
            name="Ranjit", mobile_no="9990001111", license_no="DL-1"
        )
        self.service = GoodsReturnService(self.company)
        self.allowed = [self.company.id]

    def create(self, **overrides):
        data = {
            "basis": "DEBIT_NOTE",
            "customer_name": "Sharma Traders",
            "vehicle_id": self.vehicle.id,
            "driver_id": self.driver.id,
        }
        data.update(overrides)
        return self.service.create_return(data, self.user)

    def gate_in(self, gr, *, at=None):
        gr = mark_return_in(gr.id, self.user, {}, self.allowed)
        if at is not None:
            GoodsReturn.objects.filter(pk=gr.pk).update(gated_in_at=at)
            gr.refresh_from_db()
        return gr

    def test_a_return_leaves_the_queue_and_lands_in_the_history(self):
        gr = self.create()
        self.assertIn(gr, list(list_expected_returns(self.allowed)))

        self.gate_in(gr)
        self.assertNotIn(gr, list(list_expected_returns(self.allowed)))
        self.assertIn(gr, list(list_gate_history(self.allowed)))

    def test_a_return_still_waiting_is_not_history(self):
        gr = self.create()
        self.assertNotIn(gr, list(list_gate_history(self.allowed)))

    def test_the_default_window_covers_the_last_week_and_no_further(self):
        recent = self.gate_in(self.create(), at=timezone.now() - timedelta(days=3))
        old = self.gate_in(self.create(), at=timezone.now() - timedelta(days=30))

        rows = list(list_gate_history(self.allowed))
        self.assertIn(recent, rows)
        self.assertNotIn(old, rows)

    def test_an_explicit_window_reaches_older_arrivals(self):
        old = self.gate_in(self.create(), at=timezone.now() - timedelta(days=30))

        rows = list(
            list_gate_history(
                self.allowed,
                from_date=timezone.localdate() - timedelta(days=45),
                to_date=timezone.localdate(),
            )
        )
        self.assertIn(old, rows)

    def test_newest_first(self):
        older = self.gate_in(self.create(), at=timezone.now() - timedelta(days=2))
        newer = self.gate_in(self.create(), at=timezone.now() - timedelta(hours=1))

        rows = list(list_gate_history(self.allowed))
        self.assertEqual([row.id for row in rows], [newer.id, older.id])

    def test_search_matches_the_vehicle_and_the_customer(self):
        gr = self.gate_in(self.create())

        self.assertIn(gr, list(list_gate_history(self.allowed, search="PB01AB")))
        self.assertIn(gr, list(list_gate_history(self.allowed, search="sharma")))
        self.assertEqual(list(list_gate_history(self.allowed, search="nobody")), [])

    def test_another_company_s_arrival_is_not_listed(self):
        gr = self.gate_in(self.create())
        self.assertNotIn(gr, list(list_gate_history([self.other_company.id])))


class GateHistoryEndpointTests(TestCase):
    """The route the history tab calls: same GATE_IN permission as the queue, so
    a gate-only user (who cannot open the Returns module) can still read it."""

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="OIL")
        self.user = get_user_model().objects.create(
            email="guard2@example.com", full_name="Gate Guard"
        )
        UserCompany.objects.create(
            user=self.user,
            company=self.company,
            role=UserRole.objects.create(name="Gate"),
            is_active=True,
        )
        self.vehicle = Vehicle.objects.create(vehicle_number="PB07XY9999")
        self.driver = Driver.objects.create(
            name="Sukhdev", mobile_no="9990002222", license_no="DL-2"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def grant(self, codename):
        self.user.user_permissions.add(
            Permission.objects.get(
                codename=codename, content_type=ContentType.objects.get_for_model(GoodsReturn)
            )
        )
        self.user = get_user_model().objects.get(pk=self.user.pk)  # drop the perm cache
        self.client.force_authenticate(self.user)

    def get(self, **params):
        return self.client.get(
            "/api/v1/goods-return/gate/history/", params, HTTP_COMPANY_CODE="OIL"
        )

    def test_gate_in_permission_is_enough_to_read_the_history(self):
        self.assertEqual(self.get().status_code, 403)

        self.grant("can_gate_in_goods_return")
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])

    def test_a_marked_in_return_is_returned_with_the_person_who_let_it_in(self):
        self.grant("can_gate_in_goods_return")
        gr = GoodsReturnService(self.company).create_return(
            {
                "basis": "DEBIT_NOTE",
                "customer_name": "Sharma Traders",
                "vehicle_id": self.vehicle.id,
                "driver_id": self.driver.id,
            },
            self.user,
        )
        mark_return_in(gr.id, self.user, {}, [self.company.id])

        row = self.get().data[0]
        self.assertEqual(row["entry_no"], gr.entry_no)
        self.assertEqual(row["vehicle_no"], "PB07XY9999")
        self.assertEqual(row["gated_in_by_name"], "Gate Guard")
        self.assertIsNotNone(row["gated_in_at"])

    def test_an_unparseable_date_falls_back_to_the_default_window(self):
        self.grant("can_gate_in_goods_return")
        self.assertEqual(self.get(from_date="not-a-date").status_code, 200)


class InvoiceVisibilityTests(TestCase):
    """Which bill a line came off has to be visible, and findable.

    Every screen that lists a return's lines names the invoice behind them, because
    the invoice decides which of the return's SAP documents the line lands on --
    one A/R Return is posted per invoice. No SAP: nothing here posts.
    """

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="OIL")
        self.user = get_user_model().objects.create(
            email="clerk@example.com", full_name="Return Clerk"
        )
        self.vehicle = Vehicle.objects.create(vehicle_number="PB01AB1234")
        self.driver = Driver.objects.create(
            name="Ranjit", mobile_no="9990001111", license_no="DL-1"
        )
        self.service = GoodsReturnService(self.company)
        self.allowed = [self.company.id]

        self.gr = GoodsReturn.objects.create(
            company=self.company,
            entry_no=GoodsReturn.generate_entry_no(),
            basis="INVOICE",
            status=GoodsReturnStatus.AWAITING_ARRIVAL,
            customer_code="CUST001",
            customer_name="Sharma Traders",
            vehicle=self.vehicle,
            driver=self.driver,
        )
        self.first = GoodsReturnInvoiceRef.objects.create(
            goods_return=self.gr, sap_invoice_doc_entry=5001, sap_invoice_doc_num="1500"
        )
        self.second = GoodsReturnInvoiceRef.objects.create(
            goods_return=self.gr, sap_invoice_doc_entry=5002, sap_invoice_doc_num="1501"
        )

    def line(self, ref, item, source_line_num):
        return GoodsReturnItem.objects.create(
            goods_return=self.gr,
            invoice_ref=ref,
            source_line_num=source_line_num,
            item_code=item,
            return_quantity=1,
        )

    def test_lines_come_back_grouped_by_invoice_not_interleaved(self):
        # Created interleaved by line number, which is how the invoices are read.
        self.line(self.second, "FG-B0", 0)
        self.line(self.first, "FG-A0", 0)
        self.line(self.second, "FG-B1", 1)
        self.line(self.first, "FG-A1", 1)

        self.assertEqual(
            [line.item_code for line in self.gr.lines.all()],
            ["FG-A0", "FG-A1", "FG-B0", "FG-B1"],
        )

    def test_a_hand_keyed_line_sorts_last_not_first(self):
        # SQLite and PostgreSQL disagree on where a null sorts, so this is pinned.
        self.line(None, "FG-MANUAL", None)
        self.line(self.first, "FG-A0", 0)

        self.assertEqual(
            [line.item_code for line in self.gr.lines.all()], ["FG-A0", "FG-MANUAL"]
        )

    def test_the_list_row_names_the_invoices(self):
        row = GoodsReturnListSerializer(
            self.service.list_returns(self.allowed).first()
        ).data
        self.assertEqual(row["invoice_doc_nums"], ["1500", "1501"])

    def test_a_return_can_be_found_by_its_invoice_number(self):
        found = self.service.list_returns(self.allowed, search="1501")
        self.assertEqual([gr.id for gr in found], [self.gr.id])

    def test_searching_an_invoice_on_two_bills_returns_the_return_once(self):
        # The invoice match joins the ref rows; without `distinct` the return
        # would come back once per matching bill.
        found = self.service.list_returns(self.allowed, search="150")
        self.assertEqual([gr.id for gr in found], [self.gr.id])

    def test_a_search_matching_nothing_still_matches_nothing(self):
        self.assertEqual(list(self.service.list_returns(self.allowed, search="9999")), [])


def _a_file():
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile("debit-note.pdf", b"%PDF-1.4", content_type="application/pdf")
