"""
Create the ownership-chart groups and assign their permissions.

Usage::

    python manage.py setup_org_chart_groups
    python manage.py setup_org_chart_groups --list

Two groups only: everyone who needs to know whom to ask gets the viewer group,
and whoever maintains the chart (HR / the plant head) gets the editor group.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

ORG_CHART_GROUPS = {
    "Org Chart Viewer": ["org_chart.can_view_org_chart"],
    "Org Chart Manager": [
        "org_chart.can_view_org_chart",
        "org_chart.can_manage_org_chart",
    ],
}


class Command(BaseCommand):
    help = "Create / update the org chart groups and their permissions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list", action="store_true", help="Show what each group holds and exit."
        )

    def handle(self, *args, **options):
        if options["list"]:
            for group_name, permissions in ORG_CHART_GROUPS.items():
                self.stdout.write(self.style.MIGRATE_HEADING(group_name))
                for permission in permissions:
                    self.stdout.write(f"  {permission}")
            return

        for group_name, permission_codes in ORG_CHART_GROUPS.items():
            group, created = Group.objects.get_or_create(name=group_name)
            codenames = [code.split(".", 1)[1] for code in permission_codes]
            permissions = list(
                Permission.objects.filter(
                    content_type__app_label="org_chart", codename__in=codenames
                )
            )
            missing = set(codenames) - {p.codename for p in permissions}
            if missing:
                raise SystemExit(
                    f"Missing permissions {sorted(missing)} — run migrate first."
                )
            group.permissions.set(permissions)
            self.stdout.write(
                self.style.SUCCESS(
                    f"{'Created' if created else 'Updated'} {group_name} "
                    f"({len(permissions)} permission(s))."
                )
            )
