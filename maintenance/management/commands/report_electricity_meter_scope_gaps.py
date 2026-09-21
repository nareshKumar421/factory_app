"""Who — and which meter — would be stranded by per-meter scoping.

Run this BEFORE deploying the scoping, and after any change to the electricity
groups. An unassigned user cannot edit a meter or file a reading, so a missing
row here is a person who cannot do their job — and the failure shows up as a
403 mid-shift, not as a warning at deploy time. The other half matters just as
much and is quieter: a meter nobody keeps simply stops being read.

    python manage.py report_electricity_meter_scope_gaps
    python manage.py report_electricity_meter_scope_gaps --assign-from-history
    python manage.py report_electricity_meter_scope_gaps --assign-from-history --commit

``--assign-from-history`` proposes an assignment for each unconfigured user from
what they have actually done: the meters they have filed readings against. It is
a starting point for the admin to confirm, not a substitute for deciding who
keeps which meter — a user who once covered another block would be proposed for
it too, which is why it prints and asks rather than writing by default.
"""

from collections import defaultdict

from django.core.management.base import BaseCommand

from maintenance import meter_scope
from maintenance.models import DailyElectricityReading
from maintenance.models_manager import UserElectricityMeter


class Command(BaseCommand):
    help = "List users who work the electricity register but keep no meter, and meters nobody keeps."

    def add_arguments(self, parser):
        parser.add_argument(
            "--assign-from-history",
            action="store_true",
            help="Propose assignments from the meters each user has actually read.",
        )
        parser.add_argument(
            "--commit",
            action="store_true",
            help="With --assign-from-history, actually write the proposed rows.",
        )

    def handle(self, *args, **options):
        assigned = (
            UserElectricityMeter.objects.filter(is_active=True)
            .select_related("user", "meter")
            .order_by("user__full_name", "meter__name")
        )
        by_user = defaultdict(list)
        for row in assigned:
            by_user[row.user].append(row.meter.name)

        if by_user:
            self.stdout.write(f"\nConfigured managers ({len(by_user)}):")
            for user, meters in sorted(
                by_user.items(), key=lambda kv: kv[0].full_name or ""
            ):
                self.stdout.write(
                    f"  {user.full_name or user.email:<32} {', '.join(sorted(meters))}"
                )
        else:
            self.stdout.write(self.style.WARNING("\nNo managers configured at all."))

        orphans = meter_scope.unmanaged_meters()
        if orphans:
            self.stdout.write(
                self.style.ERROR(
                    f"\n{len(orphans)} active meter(s) have NO manager — nobody can "
                    "edit them or record a reading on them:"
                )
            )
            for meter in orphans:
                self.stdout.write(f"  {meter.name}  ({meter.location or 'no location'})")
        else:
            self.stdout.write(
                self.style.SUCCESS("\nEvery active meter has at least one manager.")
            )

        gaps = meter_scope.users_missing_assignment()
        if not gaps:
            self.stdout.write(
                self.style.SUCCESS(
                    "\nNo gaps: everyone who works the register keeps a meter."
                )
            )
        else:
            self.stdout.write(
                self.style.ERROR(
                    f"\n{len(gaps)} user(s) work the electricity register but keep "
                    "NO meter — they will be refused:"
                )
            )
            proposals = {}
            for user in gaps:
                history = self._read_meters(user)
                proposals[user] = history
                shown = (
                    ", ".join(sorted(name for _, name in history))
                    if history
                    else "no history to go on"
                )
                self.stdout.write(
                    f"  {user.full_name or user.email:<32} "
                    f"({user.employee_code or 'no code'})   read: {shown}"
                )
            if options.get("assign_from_history"):
                self._apply(proposals, commit=options.get("commit", False))

        self.stdout.write("")
        if gaps or orphans:
            self.stdout.write(
                self.style.ERROR(
                    f"{len(gaps)} user gap(s) and {len(orphans)} unkept meter(s). "
                    "Fix them on Admin -> Electricity Meter Managers before this "
                    "ships, or that work stops."
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS("No gaps anywhere."))

    @staticmethod
    def _read_meters(user) -> set:
        """(id, name) of the meters this user has actually filed readings on."""
        return set(
            DailyElectricityReading.objects.filter(created_by=user)
            .values_list("meter_id", "meter__name")
            .distinct()
        )

    def _apply(self, proposals: dict, *, commit: bool) -> None:
        planned = [(u, ms) for u, ms in proposals.items() if ms]
        if not planned:
            self.stdout.write(
                self.style.WARNING(
                    "\nNothing to propose: none of the gap users have any history."
                )
            )
            return

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"\n{'Writing' if commit else 'Would write'} "
                f"{sum(len(m) for _, m in planned)} assignment(s):"
            )
        )
        for user, meters in planned:
            for meter_id, name in sorted(meters, key=lambda m: m[1] or ""):
                self.stdout.write(f"  {user.full_name or user.email} -> {name}")
                if commit:
                    UserElectricityMeter.objects.get_or_create(
                        user=user, meter_id=meter_id
                    )
        if not commit:
            self.stdout.write(
                self.style.WARNING("\nDry run. Re-run with --commit to write these.")
            )
        else:
            self.stdout.write(self.style.SUCCESS("\nWritten."))
