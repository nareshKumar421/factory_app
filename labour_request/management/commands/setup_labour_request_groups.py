"""
Create the Request Labour groups and assign their permissions.

Usage::

    python manage.py setup_labour_request_groups
    python manage.py setup_labour_request_groups --list

Three groups, one per role on the board. They are deliberately not a ladder:
a department head who raises the ask is not the person who signs off the labour
bill, and plenty of people (planning, the gate, the plant head) need to read
tomorrow's headcount without touching either side of it.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

LABOUR_REQUEST_GROUPS = {
    "Labour Request Viewer": ["labour_request.can_view_labour_request"],
    "Labour Request Requester": [
        "labour_request.can_view_labour_request",
        "labour_request.can_raise_labour_request",
    ],
    "Labour Request Approver": [
        "labour_request.can_view_labour_request",
        "labour_request.can_decide_labour_request",
    ],
}


class Command(BaseCommand):
    help = "Create / update the Request Labour groups and their permissions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list", action="store_true", help="Show what each group holds and exit."
        )

    def handle(self, *args, **options):
        if options["list"]:
            for group_name, permissions in LABOUR_REQUEST_GROUPS.items():
                self.stdout.write(self.style.MIGRATE_HEADING(group_name))
                for permission in permissions:
                    self.stdout.write(f"  {permission}")
            return

        for group_name, permission_codes in LABOUR_REQUEST_GROUPS.items():
            group, created = Group.objects.get_or_create(name=group_name)
            codenames = [code.split(".", 1)[1] for code in permission_codes]
            permissions = list(
                Permission.objects.filter(
                    content_type__app_label="labour_request", codename__in=codenames
                )
            )
            missing = set(codenames) - {p.codename for p in permissions}
            if missing:
                raise SystemExit(
                    f"Missing permissions {sorted(missing)} - run migrate first."
                )
            group.permissions.set(permissions)
            self.stdout.write(
                self.style.SUCCESS(
                    f"{'Created' if created else 'Updated'} {group_name} "
                    f"({len(permissions)} permission(s))."
                )
            )
