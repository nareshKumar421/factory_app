"""Detect and repair dispatch plans whose linked empty-vehicle-in entry points
to a DIFFERENT vehicle than the plan's current vehicle.

This orphan state arose before vehicle linking was locked after empty-in: a
plan's vehicle was changed after its empty-in completed, leaving a stale
``linked_vehicle_entry`` for the old vehicle. Such a plan shows as "Locked" in
vehicle linking yet is invisible in Empty-Vehicle-In / Docking / Empty-Vehicle-Out
(those key off the plan's CURRENT vehicle). Clearing the stale link unlocks the
plan and lets its real vehicle flow through a fresh empty-in.

Only BOOKED/PENDING active plans are touched (dispatched/cancelled plans keep
their history). Dry run by default; pass --apply to persist.
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from dispatch_plans.models import DispatchPlan, DispatchPlanStatus


class Command(BaseCommand):
    help = (
        "Clear stale linked_vehicle_entry on plans whose vehicle no longer "
        "matches the linked empty-in entry (dry run unless --apply)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist changes. Without this flag the command only reports.",
        )

    def _find_orphans(self):
        candidates = (
            DispatchPlan.objects.filter(
                is_active=True,
                linked_vehicle_entry__isnull=False,
            )
            .exclude(linked_vehicle_entry__vehicle_id=None)
            .exclude(
                booking_status__in=[
                    DispatchPlanStatus.DISPATCHED,
                    DispatchPlanStatus.CANCELLED,
                ]
            )
            .select_related("linked_vehicle_entry")
        )
        return [
            plan
            for plan in candidates
            if plan.vehicle_id != plan.linked_vehicle_entry.vehicle_id
        ]

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        self.stdout.write("APPLY" if apply_changes else "DRY RUN (nothing written)")

        orphans = self._find_orphans()
        for plan in orphans:
            entry = plan.linked_vehicle_entry
            self.stdout.write(
                f"plan {plan.id} co{plan.company_id} {plan.sap_invoice_doc_num} "
                f"{plan.booking_status} | plan.vehicle {plan.vehicle_id} != "
                f"entry.vehicle {entry.vehicle_id} | clearing linked_vehicle_entry "
                f"{entry.id} ({entry.entry_no})"
            )

        if apply_changes and orphans:
            with transaction.atomic():
                for plan in orphans:
                    # Guard on the current value so a concurrent change is not
                    # silently overwritten.
                    DispatchPlan.objects.filter(
                        id=plan.id,
                        linked_vehicle_entry_id=plan.linked_vehicle_entry_id,
                    ).update(linked_vehicle_entry=None, updated_at=timezone.now())

        verb = "Cleared" if apply_changes else "Would clear"
        self.stdout.write(
            self.style.SUCCESS(f"{verb} {len(orphans)} orphaned link(s).")
        )
