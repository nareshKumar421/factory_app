"""Add Beverages bill 626088031 to arrival ARV-20260811-0004 (vehicle DL01LAN4204).

Run from the factory_app directory:

    .venv\\Scripts\\python.exe add_bev_bill_to_4204.py            # dry run, rolls back
    .venv\\Scripts\\python.exe add_bev_bill_to_4204.py COMMIT     # persists

Creates the missing JIVO_BEVERAGES gate-in under the truck's existing open arrival so
bill 626088031 becomes dockable on this trip. Uses the app's own
_create_company_gate_in() -- the same constructor the sanctioned cross-company path
uses -- with driver, tare, date, in-time and security name all copied from the
arrival. Nothing is invented.

The only precondition skipped is _attach_bill_via_arrival()'s photo-lock guard, which
refuses because the Oil docking on this arrival is PRINT_COMMITTED. That is an
explicit business-owner override. Note the Oil gatepass DCK/JIVO_OIL/2026-27/000241
stays valid for the Oil load (per-company DCK numbers are independent) and the
combined arrival-level gate-exit gatepass has not been issued yet.

Reversible via gate_core.services.empty_vehicle_dispatch.detach_bill_from_gate_in().
"""
import os
import sys

import django

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.db import transaction

from company.models import Company
from dispatch_plans.models import DispatchPlan
from gate_core.models import EmptyVehicleGateInCover, VehicleArrival
from gate_core.services.empty_vehicle_dispatch import _create_company_gate_in

ARRIVAL_ID = 874
PLAN_ID = 2180
DOC_ENTRY = 16799

COMMIT = len(sys.argv) > 1 and sys.argv[1] == "COMMIT"

arrival = VehicleArrival.objects.get(id=ARRIVAL_ID)
bev = Company.objects.get(code="JIVO_BEVERAGES")
plan = DispatchPlan.objects.get(id=PLAN_ID)
actor = arrival.created_by  # the gate user who registered this physical arrival

print(f"MODE          : {'COMMIT' if COMMIT else 'DRY RUN (rollback)'}")
print(f"arrival       : {arrival.arrival_no} status={arrival.status}")
print(f"vehicle       : {arrival.vehicle.vehicle_number}")
print(f"driver        : {arrival.driver}")
print(f"tare (shared) : {arrival.tare_weight}  slip={arrival.weighbridge_slip_no!r}")
print(f"gate_in_date  : {arrival.gate_in_date}  in_time={arrival.in_time}")
print(f"security_name : {arrival.security_name!r}")
print(f"attributed to : {actor}")
print(f"bill          : {plan.sap_invoice_doc_num} (doc_entry {DOC_ENTRY})")

if plan.company_id != bev.id:
    sys.exit(f"ABORT: plan {PLAN_ID} is not a Beverages bill.")
if plan.vehicle_id != arrival.vehicle_id:
    sys.exit("ABORT: bill is not booked to this arrival's vehicle.")
if arrival.gate_ins.filter(company=bev, is_active=True, retired_at__isnull=True).exists():
    sys.exit("ABORT: Beverages already has a live gate-in on this arrival.")

try:
    with transaction.atomic():
        gate_in = _create_company_gate_in(
            arrival,
            bev,
            arrival.vehicle,
            arrival.driver,
            arrival.gate_in_date,
            arrival.in_time,
            actor,
            tare_weight=arrival.tare_weight,
            weighbridge_slip_no=arrival.weighbridge_slip_no,
            security_name=arrival.security_name,
            sap_doc_entries=[DOC_ENTRY],
        )
        plan.refresh_from_db()
        covers = list(EmptyVehicleGateInCover.objects.filter(empty_vehicle_gate_in=gate_in))
        print(f"\ncreated gate_in : {gate_in.entry_no} (id={gate_in.id}) company={gate_in.company.code}")
        print(f"  vehicle_entry : {gate_in.vehicle_entry_id} status={gate_in.vehicle_entry.status}")
        print(f"  arrival       : {gate_in.arrival.arrival_no}")
        print(f"  covers        : {[(c.sap_doc_num, c.sap_doc_entry) for c in covers]}")
        print(f"  plan {PLAN_ID} linked_vehicle_entry = {plan.linked_vehicle_entry_id}")
        print(f"  plan {PLAN_ID} booking_status       = {plan.booking_status}")
        if not COMMIT:
            raise RuntimeError("__DRYRUN__")
except RuntimeError as exc:
    if str(exc) != "__DRYRUN__":
        raise
    print("\nDRY RUN -- rolled back, nothing persisted.")
else:
    print("\nCOMMITTED.")
