"""
Create the Partner Onboarding groups and set their permissions.

Usage::

    python manage.py setup_partner_onboarding_groups
    python manage.py setup_partner_onboarding_groups --list

Mints groups, never members: nobody reaches the module until somebody is put in
one. Re-running resets each group to exactly the table below. Every right is
checked before any group is touched, so a database that has not been migrated
changes nothing.

The groups follow SAP Portal's two stages — a manager verifies, the SAP adder
creates the partner — once for customers and once for vendors. A verifier may
also edit and reject; an approver may also reject (as the portal's SAP adder
could) and sets the SAP fields as part of approving.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError

APP = "partner_onboarding"

PARTNER_ONBOARDING_GROUPS = {
    "Customer Onboarding - Verifier": [
        f"{APP}.can_view_customer_registrations",
        f"{APP}.can_verify_customer_registrations",
    ],
    "Customer Onboarding - SAP Approver": [
        f"{APP}.can_view_customer_registrations",
        f"{APP}.can_approve_customer_registrations",
    ],
    "Vendor Onboarding - Verifier": [
        f"{APP}.can_view_vendor_registrations",
        f"{APP}.can_verify_vendor_registrations",
    ],
    "Vendor Onboarding - SAP Approver": [
        f"{APP}.can_view_vendor_registrations",
        f"{APP}.can_approve_vendor_registrations",
    ],
    "Partner Onboarding - Viewer": [
        f"{APP}.can_view_customer_registrations",
        f"{APP}.can_view_vendor_registrations",
    ],
}


class Command(BaseCommand):
    help = "Create / update the Partner Onboarding groups and their permissions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list", action="store_true", help="Show what each group holds and exit."
        )

    def handle(self, *args, **options):
        if options["list"]:
            for group_name, permissions in PARTNER_ONBOARDING_GROUPS.items():
                self.stdout.write(self.style.MIGRATE_HEADING(group_name))
                for permission in permissions:
                    self.stdout.write(f"  {permission}")
            return

        wanted = {code.split(".", 1)[1] for codes in PARTNER_ONBOARDING_GROUPS.values() for code in codes}
        found = {
            permission.codename: permission
            for permission in Permission.objects.filter(content_type__app_label=APP, codename__in=wanted)
        }
        missing = wanted - set(found)
        if missing:
            raise CommandError(f"Missing permissions {sorted(missing)} - run migrate first.")

        for group_name, permission_codes in PARTNER_ONBOARDING_GROUPS.items():
            permissions = [found[code.split(".", 1)[1]] for code in permission_codes]
            group, created = Group.objects.get_or_create(name=group_name)
            group.permissions.set(permissions)
            self.stdout.write(
                self.style.SUCCESS(
                    f"{'Created' if created else 'Updated'} {group_name} "
                    f"({len(permissions)} permission(s))."
                )
            )
