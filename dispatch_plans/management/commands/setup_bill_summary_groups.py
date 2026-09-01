"""Create/update the Bill Summary permission groups.

Issuing and picking are separate roles on purpose. The manager issues the sheet;
the floor confirms what actually came off it. One person holding both can record
a pick nobody performed, which is the thing the paper trail existed to prevent —
so anyone who genuinely needs both gets both, deliberately and visibly.

    python manage.py setup_bill_summary_groups
    python manage.py setup_bill_summary_groups --list

A user also needs a `UserCompany` (company access) to reach the module; that is
assigned separately.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

BILL_SUMMARY_GROUPS = {
    # Plans the day and hands the sheet to the floor.
    "Bill Summary Issuer": [
        "dispatch_plans.can_view_bill_summary",
        "dispatch_plans.can_create_bill_summary",
        "dispatch_plans.can_cancel_bill_summary",
    ],
    # The floor: records what was actually picked, which is also what gets
    # written back to SAP as the dispatched quantity.
    "Bill Summary Picker": [
        "dispatch_plans.can_view_bill_summary",
        "dispatch_plans.can_pick_bill_summary",
    ],
    "Bill Summary Viewer": [
        "dispatch_plans.can_view_bill_summary",
    ],
}


class Command(BaseCommand):
    help = "Create/update the bill-summary groups (Issuer, Picker, Viewer)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list", action="store_true", help="Show the groups and what they hold."
        )

    def handle(self, *args, **options):
        if options["list"]:
            for name in BILL_SUMMARY_GROUPS:
                group = Group.objects.filter(name=name).first()
                if not group:
                    self.stdout.write(self.style.WARNING(f"{name}: not created yet"))
                    continue
                perms = sorted(
                    f"{p.content_type.app_label}.{p.codename}"
                    for p in group.permissions.all()
                )
                self.stdout.write(self.style.MIGRATE_HEADING(f"{name} ({group.user_set.count()} users)"))
                for perm in perms:
                    self.stdout.write(f"  {perm}")
            return

        for name, perms in BILL_SUMMARY_GROUPS.items():
            group, created = Group.objects.get_or_create(name=name)
            resolved = []
            for perm in perms:
                app_label, codename = perm.split(".", 1)
                found = Permission.objects.filter(
                    content_type__app_label=app_label, codename=codename
                ).first()
                if found is None:
                    # A missing permission means migrations have not run. Say so
                    # rather than quietly creating a group that grants nothing.
                    self.stderr.write(
                        self.style.ERROR(f"  missing permission {perm} — run migrate first")
                    )
                    continue
                resolved.append(found)
            group.permissions.set(resolved)
            self.stdout.write(
                self.style.SUCCESS(
                    f"{'created' if created else 'updated'} {name} "
                    f"({len(resolved)} permission(s))"
                )
            )
