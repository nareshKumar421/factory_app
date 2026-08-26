"""Remove bill 626080329 from vehicle HR67E3663 (docking DOCK-20260813-0015).

Run from the factory_app directory:

    .venv\\Scripts\\python.exe remove_bill_626080329_from_3663.py            # dry run, rolls back
    .venv\\Scripts\\python.exe remove_bill_626080329_from_3663.py COMMIT     # persists

Bill 626080329 (plan 2361, doc_entry 78488) was added to the HR67E3663 load last
(cover 2015 / document 1995) and **nothing was ever loaded for it -- 0 box scans**,
against 766 scans across the other nine bills on the same docking. It is a
wrongly-added bill, so it comes off.

The console's own "Remove bill" button refuses: ``bill_commit_reason()`` returns
"its docking is already committed" because docking 1078 is PRINT_COMMITTED. That
guard is docking-level, not bill-level -- it fires even though this bill itself has
no scans. Overriding it is an explicit business-owner decision.

Order matters. ``detach_bill_from_gate_in()`` alone is NOT enough here: its
``cancel_unscanned_dockings_for_plan()`` step excludes ``_FINALIZED_DOCKING_STATUSES``,
which includes PRINT_COMMITTED, so the docking would be skipped and document 1995
would stay **active** on the load -- cover gone, bill still on the gatepass. So:

  1. ``remove_document_from_docking(1078, doc 1995)`` -- the app's single sanctioned
     path for pulling one bill off a multi-bill load: deactivates the document, its
     items and its scans (none), and re-aggregates the header from the survivors.
  2. ``detach_bill_from_gate_in(gate_in 1087, 78488)`` -- now unblocked, because
     ``bill_commit_reason`` only counts *active* documents. Deletes cover 2015 and
     resets plan 2361 to PENDING with no vehicle, back into the unbooked pool.

Nothing is invented and no other bill is touched. Two consequences to expect:

  * Gatepass DCK/JIVO_OIL/2026-27/000257 was printed and print-committed at
    15:38 today and lists this bill -- it must be REPRINTED after this runs.
  * The docking header re-aggregates, so ``total_weight`` drops from 14865.320 by
    this bill's share.

Reversible via the console's "Add bill to inside vehicle" while the truck is still
inside (it is: gate-in EVGI-20260813-0012 is live, arrival ARV-20260813-0012).
"""
import os
import sys

import django

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.db import transaction

from accounts.models import User
from dispatch_plans.models import DispatchPlan
from gate_core.models import (
    EmptyVehicleGateIn,
    EmptyVehicleGateInCover,
    SalesDispatchBoxScan,
    SalesDispatchGateOut,
    SalesDispatchGateOutDocument,
)
from gate_core.services.empty_vehicle_dispatch import (
    bill_commit_reason,
    detach_bill_from_gate_in,
)
from gate_core.services.sales_dispatch_docking import remove_document_from_docking

DOCKING_ID = 1078
DOCUMENT_ID = 1995
GATE_IN_ID = 1087
PLAN_ID = 2361
DOC_ENTRY = 78488
DOC_NUM = "626080329"
# The SCM user who booked this bill onto the truck (cover 2015) and committed the
# gatepass print -- the operational owner of this load.
ACTOR_EMAIL = "scm@jivo.in"

COMMIT = len(sys.argv) > 1 and sys.argv[1] == "COMMIT"

docking = SalesDispatchGateOut.objects.get(id=DOCKING_ID)
document = SalesDispatchGateOutDocument.objects.get(id=DOCUMENT_ID)
gate_in = EmptyVehicleGateIn.objects.get(id=GATE_IN_ID)
plan = DispatchPlan.objects.get(id=PLAN_ID)
actor = User.objects.get(email=ACTOR_EMAIL)

print(f"MODE          : {'COMMIT' if COMMIT else 'DRY RUN (rollback)'}")
print(f"docking       : {docking.entry_no} status={docking.status} gatepass={docking.gatepass_no}")
print(f"vehicle       : {docking.vehicle_no}")
print(f"gate-in       : {gate_in.entry_no} retired_at={gate_in.retired_at}")
print(f"bill          : {DOC_NUM} (doc_entry {DOC_ENTRY}, plan {PLAN_ID})")
print(f"attributed to : {actor}")
print(f"blocked by    : {bill_commit_reason(plan)!r}  <-- overridden deliberately")

# --- preconditions -----------------------------------------------------------
if document.sales_dispatch_id != DOCKING_ID or document.dispatch_plan_id != PLAN_ID:
    sys.exit("ABORT: document 1995 is not this bill on this docking.")
if not document.is_active:
    sys.exit("ABORT: document 1995 is already inactive -- bill may already be off.")
if docking.status == "DISPATCHED":
    sys.exit("ABORT: docking already dispatched -- this is no longer a console fix.")

own_scans = SalesDispatchBoxScan.objects.filter(
    sales_dispatch=docking, document=document, is_active=True
).count()
if own_scans:
    sys.exit(f"ABORT: {own_scans} boxes scanned for this bill -- it was physically loaded.")

active_docs = SalesDispatchGateOutDocument.objects.filter(
    sales_dispatch=docking, is_active=True
).count()
if active_docs < 2:
    sys.exit("ABORT: this is the docking's last bill -- cancel the docking instead.")

cover = EmptyVehicleGateInCover.objects.filter(
    empty_vehicle_gate_in=gate_in, sap_doc_entry=DOC_ENTRY, is_active=True
).first()
if cover is None:
    sys.exit("ABORT: no active cover for this bill on gate-in 1087.")
if cover.consumed_at is not None:
    sys.exit("ABORT: cover already consumed -- the bill has been dispatched.")

print(f"\npre-check     : own_scans={own_scans}  active_docs={active_docs}  cover={cover.id}")
print(f"header before : total_weight={docking.total_weight} total_boxes={docking.total_boxes}")

# --- unwind ------------------------------------------------------------------
try:
    with transaction.atomic():
        remove_document_from_docking(docking, document, actor)
        docking.refresh_from_db()
        print(f"\nstep 1 done   : document {DOCUMENT_ID} removed from {docking.entry_no}")
        print(f"  status      : {docking.status}")
        print(f"  header after: total_weight={docking.total_weight} total_boxes={docking.total_boxes}")
        print(f"  active docs : {SalesDispatchGateOutDocument.objects.filter(sales_dispatch=docking, is_active=True).count()}")

        plan.refresh_from_db()
        print(f"  commit_reason now: {bill_commit_reason(plan)!r}")

        ok, detail = detach_bill_from_gate_in(gate_in, DOC_ENTRY, actor)
        print(f"\nstep 2 done   : ok={ok} detail={detail!r}")
        if not ok:
            sys.exit(f"ABORT: detach refused -- {detail}")

        plan.refresh_from_db()
        gate_in.refresh_from_db()
        remaining = EmptyVehicleGateInCover.objects.filter(
            empty_vehicle_gate_in=gate_in, is_active=True, consumed_at__isnull=True
        ).count()
        print(f"\nplan {PLAN_ID}     : status={plan.booking_status} vehicle={plan.vehicle_id} "
              f"linked_entry={plan.linked_vehicle_entry_id}")
        print(f"gate-in 1087  : retired_at={gate_in.retired_at} remaining_open_covers={remaining}")
        print(f"cover gone    : {not EmptyVehicleGateInCover.objects.filter(id=cover.id).exists()}")

        if not COMMIT:
            raise RuntimeError("__DRYRUN__")
except RuntimeError as exc:
    if str(exc) != "__DRYRUN__":
        raise
    print("\nDRY RUN -- rolled back, nothing persisted.")
else:
    print("\nCOMMITTED.")
    print(f"ACTION REQUIRED: reprint gatepass {docking.gatepass_no} -- the committed "
          f"print still lists bill {DOC_NUM}.")
