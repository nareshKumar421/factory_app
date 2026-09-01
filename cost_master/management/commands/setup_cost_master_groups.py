"""
Create the Cost Master role groups and assign their permissions.

Usage::

    python manage.py setup_cost_master_groups           # create / update groups
    python manage.py setup_cost_master_groups --list    # show what each group holds

Two roles: the admin who defines cost types and sets rates, and read-only
viewers (costing/reporting users who consume the master).
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

COST_MASTER_GROUPS = {
    "Cost Master Admin": [
        "cost_master.can_view_cost_master",
        "cost_master.can_manage_cost_master",
    ],
    "Cost Master Viewer": [
        "cost_master.can_view_cost_master",
    ],
}


class Command(BaseCommand):
    help = "Create Cost Master role groups and assign permissions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list", action="store_true", help="List groups and their permissions"
        )

    def handle(self, *args, **options):
        if options["list"]:
            for name in COST_MASTER_GROUPS:
                group = Group.objects.filter(name=name).first()
                if group is None:
                    self.stdout.write(self.style.WARNING(f"{name}: (not created)"))
                    continue
                self.stdout.write(self.style.SUCCESS(f"{name}:"))
                for codename in group.permissions.values_list("codename", flat=True):
                    self.stdout.write(f"    - {codename}")
            return

        for name, permission_codes in COST_MASTER_GROUPS.items():
            group, created = Group.objects.get_or_create(name=name)
            permissions = []
            missing = []
            for code in permission_codes:
                app_label, codename = code.split(".", 1)
                permission = Permission.objects.filter(
                    content_type__app_label=app_label, codename=codename
                ).first()
                if permission is None:
                    missing.append(code)
                else:
                    permissions.append(permission)
            group.permissions.set(permissions)
            verb = "created" if created else "updated"
            self.stdout.write(
                self.style.SUCCESS(f"{verb} {name} ({len(permissions)} permissions)")
            )
            for code in missing:
                self.stdout.write(
                    self.style.WARNING(
                        f"    missing permission {code} — run migrate first"
                    )
                )
