"""Which vehicles the empty-vehicle-out board offers as able to leave empty.

A finished inward entry is eligible until it is marked out empty, or until a box
is scanned onto a docking for a plan linked to it (the truck is now loading). A
docking's own SALES_DISPATCH entry is never eligible: it is COMPLETED only once
the truck has left loaded. On live, those made up half the list (2,037 trucks).

The exclusions are NOT EXISTS on purpose. As NOT IN they read one row per
scanned box, and past ~250k scans Postgres stopped hashing that list and read it
in full for every entry: 20-65 s a call on live.
"""
from datetime import time

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
from driver_management.models import Driver, VehicleEntry
from gate_core.models import (
    EmptyVehicleGateOut,
    SalesDispatchBoxScan,
    SalesDispatchDocumentType,
    SalesDispatchGateOut,
    SalesDispatchGateOutStatus,
)
from gate_core.models.empty_vehicle_gate_out import EmptyVehicleGateOutStatus
from vehicle_management.models import Transporter, Vehicle

URL = "/api/v1/gate-core/empty-vehicle-outs/eligible-entries/"


class EmptyOutEligibleEntriesTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL_EO")
        self.user = get_user_model().objects.create_user(
            email="emptyout@example.com", password="p",
            full_name="Gate Guard", employee_code="EO1",
        )
        UserCompany.objects.create(
            user=self.user, company=self.company,
            role=UserRole.objects.create(name="Gate"), is_default=True,
        )
        self.transporter = Transporter.objects.create(name="Bhargave Road Carrier")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="PB10EO0001", transporter=self.transporter
        )
        self.driver = Driver.objects.create(
            name="Eligible Driver", mobile_no="9000000201", license_no="DL-EO-1"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    # ----- fixtures -------------------------------------------------------

    def _entry(self, entry_type, status="COMPLETED"):
        self._seq = getattr(self, "_seq", 0) + 1
        return VehicleEntry.objects.create(
            entry_no=f"VE-EO-{self._seq}", company=self.company, vehicle=self.vehicle,
            driver=self.driver, entry_type=entry_type, status=status,
            created_by=self.user, updated_by=self.user,
        )

    def _empty_out(self, vehicle_entry, status=EmptyVehicleGateOutStatus.COMPLETED):
        return EmptyVehicleGateOut.objects.create(
            company=self.company, entry_no=f"EVGO-{vehicle_entry.entry_no}",
            vehicle_entry=vehicle_entry, vehicle=self.vehicle, driver=self.driver,
            gate_out_date=timezone.localdate(), out_time=time(18, 0), status=status,
            created_by=self.user, updated_by=self.user,
        )

    def _docking_for(self, empty_entry, docking_entry_status="IN_PROGRESS"):
        """A plan linked to ``empty_entry`` and its docking, as docking makes them."""
        self._seq = getattr(self, "_seq", 0) + 1
        plan = DispatchPlan.objects.create(
            company=self.company, sap_invoice_doc_entry=90000 + self._seq,
            sap_invoice_doc_num=str(90000 + self._seq),
            booking_status=DispatchPlanStatus.BOOKED,
            dispatch_date=timezone.localdate(), vehicle=self.vehicle,
            linked_vehicle_entry=empty_entry,
        )
        return SalesDispatchGateOut.objects.create(
            company=self.company, entry_no=f"DOCK-EO-{self._seq}",
            vehicle_entry=self._entry("SALES_DISPATCH", status=docking_entry_status),
            dispatch_plan=plan, vehicle=self.vehicle, transporter=self.transporter,
            driver=self.driver, vehicle_no=self.vehicle.vehicle_number,
            document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=plan.sap_invoice_doc_entry,
            sap_doc_num=plan.sap_invoice_doc_num,
            status=SalesDispatchGateOutStatus.DOCKED,
            created_by=self.user, updated_by=self.user,
        )

    def _scan(self, docking, barcode, is_active=True):
        return SalesDispatchBoxScan.objects.create(
            company=self.company, sales_dispatch=docking, box_barcode=barcode,
            is_active=is_active, created_by=self.user, updated_by=self.user,
        )

    def _eligible_ids(self, **params):
        resp = self.client.get(URL, params, HTTP_COMPANY_CODE=self.company.code)
        self.assertEqual(resp.status_code, 200, resp.data)
        return {row["id"] for row in resp.data}

    # ----- tests ----------------------------------------------------------

    def test_lists_finished_entries_that_have_not_left(self):
        raw_material = self._entry("RAW_MATERIAL")
        qc_done = self._entry("RAW_MATERIAL", status="QC_COMPLETED")
        empty = self._entry("EMPTY_VEHICLE")
        self._entry("RAW_MATERIAL", status="IN_PROGRESS")

        self.assertEqual(self._eligible_ids(), {raw_material.id, qc_done.id, empty.id})

    def test_a_completed_empty_out_takes_the_entry_off_but_a_cancelled_one_does_not(self):
        marked_out = self._entry("RAW_MATERIAL")
        cancelled_out = self._entry("RAW_MATERIAL")
        self._empty_out(marked_out)
        self._empty_out(cancelled_out, status=EmptyVehicleGateOutStatus.CANCELLED)

        self.assertEqual(self._eligible_ids(), {cancelled_out.id})

    def test_a_box_scanned_on_the_linked_plan_takes_the_empty_vehicle_off(self):
        empty = self._entry("EMPTY_VEHICLE")
        docking = self._docking_for(empty)
        self.assertIn(empty.id, self._eligible_ids())

        self._scan(docking, "BOX-REMOVED", is_active=False)
        self.assertIn(empty.id, self._eligible_ids())

        self._scan(docking, "BOX-ON")
        self.assertNotIn(empty.id, self._eligible_ids())

    def test_a_dispatched_truck_s_own_entry_is_never_listed(self):
        empty = self._entry("EMPTY_VEHICLE")
        docking = self._docking_for(empty, docking_entry_status="COMPLETED")

        self.assertNotIn(docking.vehicle_entry_id, self._eligible_ids())
        self.assertEqual(self._eligible_ids(entry_type="SALES_DISPATCH"), set())

    def test_the_exclusions_are_not_exists_never_not_in(self):
        self._docking_for(self._entry("EMPTY_VEHICLE"))
        with CaptureQueriesContext(connection) as ctx:
            self._eligible_ids()
        listing = [
            q["sql"] for q in ctx.captured_queries
            if q["sql"].startswith('SELECT "driver_management_vehicleentry"."id"')
        ]
        self.assertEqual(len(listing), 1, [q["sql"] for q in ctx.captured_queries])
        self.assertEqual(listing[0].count("NOT EXISTS"), 2, listing[0])
        self.assertNotIn("IN (SELECT", listing[0])
