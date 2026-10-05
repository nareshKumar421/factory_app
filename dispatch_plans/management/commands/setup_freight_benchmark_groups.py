"""Create/update the Freight Benchmark and Freight Approval permission groups.

Reading the benchmarks and changing them are two rights. The vehicle-linking
approval holds each truck's actual freight against its benchmark, so whoever can
edit a benchmark can move the line a freight has to cross to need approving.
That belongs with a few people, not with everyone who needs to look one up.

Clearing a freight over its benchmark is a third right, and deliberately not a
benchmark manager's: the person who sets the line should not also be the one who
waves a truck across it.

    python manage.py setup_freight_benchmark_groups
    python manage.py setup_freight_benchmark_groups --list

The groups are created with their permissions and no members; who goes in them
is assigned separately.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

FREIGHT_BENCHMARK_GROUPS = {
    "Freight Benchmark Viewer": [
        "dispatch_plans.can_view_freight_benchmarks",
    ],
    # Editing includes reading; the page and its API both treat it that way.
    "Freight Benchmark Manager": [
        "dispatch_plans.can_view_freight_benchmarks",
        "dispatch_plans.can_manage_freight_benchmarks",
    ],
    # Admin > Freight Approvals. Reads the benchmarks too, to see what a
    # freight was held against.
    "Freight Approver": [
        "dispatch_plans.can_view_freight_approvals",
        "dispatch_plans.can_approve_freight_approvals",
        "dispatch_plans.can_view_freight_benchmarks",
    ],
}


class Command(BaseCommand):
    help = "Create/update the freight benchmark and freight approval groups."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list", action="store_true", help="Show the groups and who is in them."
        )

    def handle(self, *args, **options):
        if options["list"]:
            for name in FREIGHT_BENCHMARK_GROUPS:
                group = Group.objects.filter(name=name).first()
                if not group:
                    self.stdout.write(self.style.WARNING(f"{name}: not created yet"))
                    continue
                members = sorted(group.user_set.values_list("email", flat=True))
                self.stdout.write(
                    self.style.MIGRATE_HEADING(f"{name} ({len(members)} users)")
                )
                for perm in sorted(
                    f"{p.content_type.app_label}.{p.codename}"
                    for p in group.permissions.all()
                ):
                    self.stdout.write(f"  {perm}")
                for email in members:
                    self.stdout.write(f"  - {email}")
            return

        for name, perms in FREIGHT_BENCHMARK_GROUPS.items():
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
