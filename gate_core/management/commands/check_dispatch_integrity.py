"""Health check for the two docking data-drift classes a bill removal can leave.

A bill leaving a docking must fully unwind (header re-aggregated, box scans
deactivated). Paths that don't -- older code, a direct DB edit, a future
regression -- leave one of two footprints this command finds:

  A. HEADER DRIFT -- the docking's denormalized header (invoice list, totals,
     item summary, primary sap_doc_entry) no longer matches its active documents,
     so the load is over/misstated on the board, gatepass and reports.
  B. ORPHANED SCANS -- box scans still ACTIVE on a document that was removed
     (deactivated); on a still-dispatchable docking they would settle at dispatch
     as if the removed bill's boxes shipped.

Read-only by default (exits non-zero when anything is found, for cron/CI). With
``--fix`` it heals: re-aggregates each drifted header (``resync_docking_header``)
and deactivates the orphaned scans, in one transaction.

Examples::

    python manage.py check_dispatch_integrity
    python manage.py check_dispatch_integrity --company JIVO_OIL
    python manage.py check_dispatch_integrity --fix
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from gate_core.models import (
    SalesDispatchBoxScan,
    SalesDispatchGateOut,
    SalesDispatchGateOutStatus,
)
from gate_core.services.sales_dispatch_docking import (
    _sum_decimals,
    join_unique,
    resync_docking_header,
)


def _d(value):
    return Decimal(str(value)) if value not in (None, "") else None


def find_header_drift(qs):
    """Active dockings whose header no longer matches their active documents."""
    drifted = []
    for go in qs.prefetch_related("documents"):
        docs = [d for d in go.documents.all() if d.is_active]
        if not docs:
            continue
        exp_num = join_unique(d.sap_doc_num for d in docs)
        exp_qty = _sum_decimals([_d(d.total_quantity) for d in docs])
        primary_orphan = go.sap_doc_entry not in {d.sap_doc_entry for d in docs}
        if join_unique([go.sap_doc_num]) != exp_num or _d(go.total_quantity) != exp_qty or primary_orphan:
            drifted.append((go, exp_num, exp_qty, primary_orphan))
    return drifted


class Command(BaseCommand):
    help = "Find (and optionally heal) docking header drift and orphaned box scans."

    def add_arguments(self, parser):
        parser.add_argument("--company", default=None, help="Limit to one company code.")
        parser.add_argument("--entry", type=int, default=None, help="Limit to one docking id.")
        parser.add_argument(
            "--fix", action="store_true",
            help="Heal the issues (re-aggregate headers, deactivate orphaned scans).",
        )

    def handle(self, *args, **options):
        base = SalesDispatchGateOut.objects.filter(is_active=True).select_related("company")
        if options["company"]:
            base = base.filter(company__code=options["company"])
        if options["entry"]:
            base = base.filter(id=options["entry"])

        fix = options["fix"]
        now = timezone.now()

        # ---- A. header drift ------------------------------------------------
        drifted = find_header_drift(base.order_by("id"))
        self.stdout.write(self.style.MIGRATE_HEADING(f"A. Header drift: {len(drifted)} docking(s)"))
        for go, exp_num, exp_qty, primary_orphan in drifted:
            self.stdout.write(
                f"  #{go.id} {go.entry_no} [{go.status}] {go.company.code}\n"
                f"     header : {go.sap_doc_num!r} qty={go.total_quantity}\n"
                f"     docs   : {exp_num!r} qty={exp_qty}"
                + ("  (primary sap_doc_entry orphaned -> will re-point)" if primary_orphan else "")
            )

        # ---- B. orphaned active scans on inactive documents -----------------
        orphan_qs = SalesDispatchBoxScan.objects.filter(
            is_active=True, document__is_active=False, sales_dispatch__in=base
        ).select_related("sales_dispatch")
        by_dock = {}
        for scan in orphan_qs:
            by_dock.setdefault(scan.sales_dispatch, []).append(scan.id)
        orphan_total = sum(len(v) for v in by_dock.values())
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"B. Orphaned active scans on removed documents: {orphan_total} scan(s) "
            f"across {len(by_dock)} docking(s)"
        ))
        for dock, ids in by_dock.items():
            at_risk = dock.status != SalesDispatchGateOutStatus.DISPATCHED
            note = "would mis-settle at dispatch" if at_risk else "docking dispatched -- moot"
            self.stdout.write(f"  #{dock.id} {dock.entry_no} [{dock.status}]: {len(ids)} scan(s) -- {note}")

        if not drifted and not orphan_total:
            self.stdout.write(self.style.SUCCESS("Dispatch data is clean -- no drift or orphaned scans."))
            return

        if not fix:
            self.stdout.write(self.style.WARNING(
                f"\nFound {len(drifted)} drifted header(s) and {orphan_total} orphaned scan(s). "
                f"Re-run with --fix to heal."
            ))
            # Non-zero exit for cron/CI health checks.
            raise SystemExit(1)

        # ---- heal -----------------------------------------------------------
        healed_headers = healed_scans = 0
        with transaction.atomic():
            for go, *_ in drifted:
                resync_docking_header(go, user=None)
                healed_headers += 1
            for dock, ids in by_dock.items():
                from barcode.services.vehicle_load import (
                    resolve_scan_boxes,
                    unload_boxes_from_vehicle,
                )

                # Boxes of orphaned scans come back off the truck (no-op for
                # boxes already settled DISPATCHED).
                scans = list(
                    SalesDispatchBoxScan.objects.filter(id__in=ids, is_active=True)
                    .select_related("box", "box__pallet")
                )
                unload_boxes_from_vehicle(
                    dock.company,
                    resolve_scan_boxes(dock.company, scans),
                    None,
                    reference=f"Orphaned scan healed — Docking {dock.entry_no}",
                )
                healed_scans += SalesDispatchBoxScan.objects.filter(id__in=ids, is_active=True).update(
                    is_active=False, updated_at=now
                )
        self.stdout.write(self.style.SUCCESS(
            f"\nHealed {healed_headers} header(s) and deactivated {healed_scans} orphaned scan(s)."
        ))
