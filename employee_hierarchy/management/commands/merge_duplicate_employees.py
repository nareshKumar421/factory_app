"""
Fold one employee record into another when both are the same person.

The factory's directory carries a handful of people twice: once as a
supervisor row created from the org chart (``MGR-007``, no department, no SAP
segment, holding a team) and once as the staff row the workbook gave a real
code (``JWPL2884``, a department, a segment, and the punches to go with it).
Neither row is complete, which is why neither can simply be deleted.

**The code side is kept.** It is the one the punching machines know, so it
carries the attendance; the supervisor row carries only the team. So the team
moves across and the supervisor row goes.

The removed row's attendance is deliberately let go. A code no machine has
cannot have produced a punch, and it has not: those rows are ``ABSENT`` for
every working day, which is a person who was at work appearing absent beside
themselves. Keeping them would keep the double-count they cause.

The move goes through :func:`employee_hierarchy.services.change_manager`, one
report at a time, so every re-pointing lands in the audit trail with a reason
rather than being a silent ``UPDATE``.

Dry run by default::

    python manage.py merge_duplicate_employees --keep JWPL2884 --remove MGR-007
    python manage.py merge_duplicate_employees --keep JWPL2884 --remove MGR-007 --commit
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from ... import hierarchy, services
from ...models import Employee


class Command(BaseCommand):
    help = "Merge two records for one person, keeping the coded one."

    def add_arguments(self, parser):
        parser.add_argument("--keep", required=True, help="Employee code to keep.")
        parser.add_argument("--remove", required=True, help="Employee code to delete.")
        parser.add_argument("--company", default="JIVO_OIL")
        parser.add_argument("--commit", action="store_true")

    def _get(self, company, code, role):
        employee = Employee.objects.filter(
            company__code=company, employee_code__iexact=code
        ).first()
        if employee is None:
            raise CommandError(f"No {role} employee {code!r} in {company}.")
        return employee

    def handle(self, *args, **options):
        keeper = self._get(options["company"], options["keep"], "keeper")
        goner = self._get(options["company"], options["remove"], "duplicate")
        if keeper.pk == goner.pk:
            raise CommandError("--keep and --remove name the same record.")

        # Not enforced as equality -- the two rows often spell a name slightly
        # differently -- but a mismatch is worth stopping for, because merging
        # two different people is unrecoverable.
        if keeper.full_name.strip().upper() != goner.full_name.strip().upper():
            raise CommandError(
                f"{keeper.employee_code} is {keeper.full_name!r} but "
                f"{goner.employee_code} is {goner.full_name!r}. Refusing to merge "
                "two different names."
            )

        reports = list(Employee.objects.filter(reporting_manager=goner))
        inherit = keeper.reporting_manager is None and goner.reporting_manager is not None

        self.stdout.write(f"Keep   : {keeper.employee_code:<12} {keeper.full_name}")
        self.stdout.write(
            f"         dept={keeper.department} segment={keeper.sap_segment!r} "
            f"attendance={keeper.daily_attendance.count()}"
        )
        self.stdout.write(f"Remove : {goner.employee_code:<12} {goner.full_name}")
        self.stdout.write(
            f"         dept={goner.department} segment={goner.sap_segment!r} "
            f"attendance={goner.daily_attendance.count()} (dropped)"
        )
        self.stdout.write(f"Moving : {len(reports)} direct report(s) onto {keeper.employee_code}")
        for report in reports[:10]:
            self.stdout.write(f"   {report.employee_code:<12} {report.full_name}")
        if len(reports) > 10:
            self.stdout.write(f"   … +{len(reports) - 10} more")
        if inherit:
            self.stdout.write(
                f"Also   : {keeper.employee_code} takes {goner.employee_code}'s own manager "
                f"({goner.reporting_manager.employee_code}) -- it currently reports to nobody"
            )

        if not options["commit"]:
            self.stdout.write(self.style.WARNING("\nDry run. Re-run with --commit to write."))
            return

        with transaction.atomic():
            # The keeper's own place first: a report cannot be moved under
            # somebody whose position in the tree is about to change.
            if inherit:
                services.change_manager(
                    keeper,
                    goner.reporting_manager,
                    reason=f"Merged with duplicate record {goner.employee_code}",
                )
            keeper.is_manager = True
            keeper.save(update_fields=["is_manager"])

            for report in reports:
                services.change_manager(
                    report,
                    keeper,
                    reason=f"{goner.employee_code} merged into {keeper.employee_code}",
                )

            goner.delete()
            # The paths are a cache of the parent links, and a delete plus a
            # batch of moves is exactly the case they exist to be rebuilt after.
            hierarchy.rebuild_paths()

        keeper.refresh_from_db()
        self.stdout.write(
            self.style.SUCCESS(
                f"\nMerged. {keeper.employee_code} now holds "
                f"{Employee.objects.filter(reporting_manager=keeper).count()} report(s)."
            )
        )
