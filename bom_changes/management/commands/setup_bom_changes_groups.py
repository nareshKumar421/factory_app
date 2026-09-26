"""
Create the BOM Changes groups and set their permissions.

Usage::

    python manage.py setup_bom_changes_groups
    python manage.py setup_bom_changes_groups --list

Mints groups, never members: nobody reaches the module until somebody is put in
one. Re-running resets each group to exactly the table below.

The groups are SAP Portal's BOM roles (``backend_v1/server.js`` ``LEVEL_ROLES``):
``manager`` → Level 1 Approver, ``sr_manager`` → Level 2 Approver,
``sap_adder`` → SAP Pusher, ``admin`` → Admin. Every portal role could raise a
request, so every role group can too.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError

VIEW = "bom_changes.can_view_bom_changes"
REQUEST = "bom_changes.can_request_bom_changes"
LEVEL_1 = "bom_changes.can_approve_bom_level_1"
LEVEL_2 = "bom_changes.can_approve_bom_level_2"
PUSH = "bom_changes.can_push_bom_to_sap"
DIRECT = "bom_changes.can_push_bom_directly"

BOM_CHANGES_GROUPS = {
    "BOM Changes - Viewer": [VIEW],
    "BOM Changes - Requester": [VIEW, REQUEST],
    "BOM Changes - Level 1 Approver": [VIEW, REQUEST, LEVEL_1],
    "BOM Changes - Level 2 Approver": [VIEW, REQUEST, LEVEL_2],
    "BOM Changes - SAP Pusher": [VIEW, REQUEST, PUSH],
    # The portal admin could do everything. The same-person rule still holds:
    # holding every level right lets one person sign one level of a request.
    "BOM Changes - Admin": [VIEW, REQUEST, LEVEL_1, LEVEL_2, PUSH, DIRECT],
}


class Command(BaseCommand):
    help = "Create / update the BOM Changes groups and their permissions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list", action="store_true", help="Show what each group holds and exit."
        )

    def handle(self, *args, **options):
        if options["list"]:
            for group_name, permissions in BOM_CHANGES_GROUPS.items():
                self.stdout.write(self.style.MIGRATE_HEADING(group_name))
                for permission in permissions:
                    self.stdout.write(f"  {permission}")
            return

        for group_name, permission_codes in BOM_CHANGES_GROUPS.items():
            codenames = [code.split(".", 1)[1] for code in permission_codes]
            permissions = list(
                Permission.objects.filter(
                    content_type__app_label="bom_changes", codename__in=codenames
                )
            )
            missing = set(codenames) - {p.codename for p in permissions}
            if missing:
                raise CommandError(
                    f"Missing permissions {sorted(missing)} - run migrate first."
                )
            group, created = Group.objects.get_or_create(name=group_name)
            group.permissions.set(permissions)
            self.stdout.write(
                self.style.SUCCESS(
                    f"{'Created' if created else 'Updated'} {group_name} "
                    f"({len(permissions)} permission(s))."
                )
            )
