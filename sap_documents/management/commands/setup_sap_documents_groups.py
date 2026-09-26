"""
Create the SAP Documents groups and set their permissions.

Usage::

    python manage.py setup_sap_documents_groups
    python manage.py setup_sap_documents_groups --list

Mints groups, never members: nobody reaches the module until somebody is put in
one. Re-running resets each group to exactly the table below.

Two audiences: people who look documents up, and people who also open the
scanned bills and challans attached to them in SAP.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError

SAP_DOCUMENTS_GROUPS = {
    "SAP Documents - Viewer": ["sap_documents.can_view_sap_documents"],
    "SAP Documents - Viewer with attachments": [
        "sap_documents.can_view_sap_documents",
        "sap_documents.can_download_sap_attachments",
    ],
}


class Command(BaseCommand):
    help = "Create / update the SAP Documents groups and their permissions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list", action="store_true", help="Show what each group holds and exit."
        )

    def handle(self, *args, **options):
        if options["list"]:
            for group_name, permissions in SAP_DOCUMENTS_GROUPS.items():
                self.stdout.write(self.style.MIGRATE_HEADING(group_name))
                for permission in permissions:
                    self.stdout.write(f"  {permission}")
            return

        for group_name, permission_codes in SAP_DOCUMENTS_GROUPS.items():
            codenames = [code.split(".", 1)[1] for code in permission_codes]
            permissions = list(
                Permission.objects.filter(
                    content_type__app_label="sap_documents", codename__in=codenames
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
