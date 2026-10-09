"""
Create the SAP Approvals groups and set their permissions.

Usage::

    python manage.py setup_sap_approvals_groups
    python manage.py setup_sap_approvals_groups --list

Mints groups, never members: nobody reaches the module until somebody is put in
one. Re-running resets each group to exactly the table below.

SAP Portal had one ``sap-approvals`` module that let anyone mapped to a SAP user
approve, reject and withdraw. Portal users who held it go in *SAP Approvals -
Approver*; people who only raise documents in SAP, in *Requester*.

A group alone lets nobody act. The inbox also needs, per company:

* a ``SapApproverIdentity`` mapping the user to their own SAP account (Admin →
  SAP Identities) — without it the inbox is empty and nothing is decidable;
* for a decision without typing a password, that account's password in
  ``SAP_APPROVER_CREDENTIALS``. With none stored the approver types their own
  SAP password in the decision dialog; it is used once and never saved.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError

SAP_APPROVALS_GROUPS = {
    # Reads the requests that involve them; decides nothing.
    "SAP Approvals - Viewer": ["sap_approvals.can_view_sap_approval_inbox"],
    # Raises documents in SAP and may withdraw their own pending requests.
    "SAP Approvals - Requester": [
        "sap_approvals.can_view_sap_approval_inbox",
        "sap_approvals.can_withdraw_own_sap_approvals",
    ],
    # The portal's sap-approvals module: approve, reject and withdraw.
    "SAP Approvals - Approver": [
        "sap_approvals.can_view_sap_approval_inbox",
        "sap_approvals.can_decide_sap_approvals",
        "sap_approvals.can_withdraw_own_sap_approvals",
    ],
    # Whoever reviews the desk: every rejection, by who raised it.
    "SAP Approvals - Reviewer": [
        "sap_approvals.can_view_sap_approval_inbox",
        "sap_approvals.can_view_sap_rejection_history",
    ],
}


class Command(BaseCommand):
    help = "Create / update the SAP Approvals groups and their permissions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list", action="store_true", help="Show what each group holds and exit."
        )

    def handle(self, *args, **options):
        if options["list"]:
            for group_name, permissions in SAP_APPROVALS_GROUPS.items():
                self.stdout.write(self.style.MIGRATE_HEADING(group_name))
                for permission in permissions:
                    self.stdout.write(f"  {permission}")
            return

        for group_name, permission_codes in SAP_APPROVALS_GROUPS.items():
            codenames = [code.split(".", 1)[1] for code in permission_codes]
            permissions = list(
                Permission.objects.filter(
                    content_type__app_label="sap_approvals", codename__in=codenames
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
