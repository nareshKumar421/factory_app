"""Clear ``linked_vehicle_entry`` on bookings that point at a CONSUMED dispatch
empty-in.

A dispatch empty-in is consumed once its visit ends — the loaded vehicle was
dispatched out, or it left again empty. The entry stays COMPLETED forever, so a
returning fleet vehicle's new booking would auto-link to that stale entry and
appear on the docking board instead of as an expected dispatch vehicle awaiting a
fresh empty-vehicle-in. Clearing the stale link returns the booking to the
expected-dispatch list; a fresh empty-in then re-links it and flows to docking.

Only BOOKED/PENDING active plans are touched (dispatched/cancelled keep their
history). Dry run by default; pass --apply to persist. Optional --company CODE.
"""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from company.models import Company
from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
from gate_core.models import EmptyVehicleGateIn


class Command(BaseCommand):
    help = (
        "Clear linked_vehicle_entry on BOOKED/PENDING plans linked to a consumed "
        "(already dispatched / departed) dispatch empty-in (dry run unless --apply)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--company", default="", help="Limit to one company code.")

    def _stale_entry_ids(self, company):
        # Vehicle entries of *retired* dispatch gate-ins: the visit ended (every
        # bill the gate-in covered dispatched, or the truck left empty), so any
        # booking still linked to one is stale and should drop back to the
        # expected-dispatch list for a fresh empty-vehicle-in.
        return set(
            EmptyVehicleGateIn.objects.filter(
                company=company,
                is_active=True,
                reason="DISPATCH",
                retired_at__isnull=False,
            ).values_list("vehicle_entry_id", flat=True)
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        self.stdout.write("APPLY" if apply_changes else "DRY RUN (nothing written)")

        companies = Company.objects.all()
        if options["company"]:
            companies = companies.filter(code=options["company"])

        targets = []
        for company in companies.order_by("code"):
            stale = self._stale_entry_ids(company)
            if not stale:
                continue
            plans = (
                DispatchPlan.objects.filter(
                    company=company,
                    is_active=True,
                    booking_status__in=[
                        DispatchPlanStatus.BOOKED,
                        DispatchPlanStatus.PENDING,
                    ],
                    linked_vehicle_entry_id__in=stale,
                )
                .select_related("vehicle", "linked_vehicle_entry")
            )
            for plan in plans:
                targets.append(plan)
                self.stdout.write(
                    f"{company.code} plan {plan.id} {plan.sap_invoice_doc_num} "
                    f"{plan.booking_status} veh={plan.vehicle_id} -> clearing stale "
                    f"linked_vehicle_entry {plan.linked_vehicle_entry_id}"
                )

        if apply_changes and targets:
            with transaction.atomic():
                for plan in targets:
                    DispatchPlan.objects.filter(
                        id=plan.id,
                        linked_vehicle_entry_id=plan.linked_vehicle_entry_id,
                    ).update(linked_vehicle_entry=None, updated_at=timezone.now())

        verb = "Cleared" if apply_changes else "Would clear"
        self.stdout.write(self.style.SUCCESS(f"{verb} {len(targets)} stale link(s)."))
