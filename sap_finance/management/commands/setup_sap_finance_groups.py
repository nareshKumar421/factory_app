"""
Create the SAP Finance groups and set their permissions.

Usage::

    python manage.py setup_sap_finance_groups
    python manage.py setup_sap_finance_groups --list

Mints groups, never members: nobody reaches the module until somebody is put in
one. Re-running resets each group to exactly the table below.

The groups follow SAP Portal's two modules: ``journal-entries`` (journal
entries, general ledger, chart of accounts) and ``budget``.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError

SAP_FINANCE_GROUPS = {
    "SAP Finance - Ledger Viewer": ["sap_finance.can_view_sap_ledgers"],
    "SAP Finance - Budget Viewer": ["sap_finance.can_view_sap_budgets"],
    "SAP Finance - Budget Editor": [
        "sap_finance.can_view_sap_budgets",
        "sap_finance.can_manage_sap_budgets",
    ],
}


class Command(BaseCommand):
    help = "Create / update the SAP Finance groups and their permissions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list", action="store_true", help="Show what each group holds and exit."
        )

    def handle(self, *args, **options):
        if options["list"]:
            for group_name, permissions in SAP_FINANCE_GROUPS.items():
                self.stdout.write(self.style.MIGRATE_HEADING(group_name))
                for permission in permissions:
                    self.stdout.write(f"  {permission}")
            return

        for group_name, permission_codes in SAP_FINANCE_GROUPS.items():
            codenames = [code.split(".", 1)[1] for code in permission_codes]
            permissions = list(
                Permission.objects.filter(
                    content_type__app_label="sap_finance", codename__in=codenames
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
