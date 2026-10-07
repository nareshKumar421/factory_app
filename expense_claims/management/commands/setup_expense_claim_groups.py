"""
Create the expense claim groups and assign their permissions.

Usage::

    python manage.py setup_expense_claim_groups                    # create / update
    python manage.py setup_expense_claim_groups --list             # show what each holds
    python manage.py setup_expense_claim_groups --assign-everyone  # + every user may submit

Two groups (see :mod:`expense_claims.constants`): **Expense Submitter** for
everybody, and **Expense Approver**, which also approves and is filled from
Admin.

``--assign-everyone`` drops every active account into **Expense Submitter**.
It only ever adds, so running it twice is harmless. New accounts do not need
it; they pick the group up at creation (see :mod:`expense_claims.signals`).

Who approves is NOT set here: put them in **Expense Approver** from Admin.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

from expense_claims.constants import (
    APPROVE_PERMISSION,
    APPROVER_GROUP,
    SUBMIT_PERMISSION,
    SUBMITTER_GROUP,
)

EXPENSE_CLAIM_GROUPS = {
    SUBMITTER_GROUP: [SUBMIT_PERMISSION],
    # Approvers spend money too.
    APPROVER_GROUP: [SUBMIT_PERMISSION, APPROVE_PERMISSION],
}


class Command(BaseCommand):
    help = "Create the expense claim groups and assign their permissions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list",
            action="store_true",
            help="Show each group and its permissions, without writing.",
        )
        parser.add_argument(
            "--assign-everyone",
            action="store_true",
            help=f"Also put every active user in '{SUBMITTER_GROUP}'. Adds only.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="With --assign-everyone, report who would be added without writing.",
        )

    def handle(self, *args, **options):
        if options["list"]:
            for name, permissions in EXPENSE_CLAIM_GROUPS.items():
                exists = Group.objects.filter(name=name).exists()
                self.stdout.write(f"{name} ({'exists' if exists else 'missing'})")
                for permission in permissions:
                    self.stdout.write(f"    {permission}")
            return

        for name, permission_strings in EXPENSE_CLAIM_GROUPS.items():
            group, created = Group.objects.get_or_create(name=name)
            resolved, missing = [], []
            for permission_string in permission_strings:
                app_label, codename = permission_string.split(".", 1)
                permission = Permission.objects.filter(
                    content_type__app_label=app_label, codename=codename
                ).first()
                if permission is None:
                    missing.append(permission_string)
                else:
                    resolved.append(permission)
            group.permissions.set(resolved)
            verb = "Created" if created else "Updated"
            self.stdout.write(
                self.style.SUCCESS(f"{verb} '{name}' with {len(resolved)} permissions")
            )
            if missing:
                self.stdout.write(
                    self.style.WARNING(
                        f"  Not found (run migrate first): {', '.join(missing)}"
                    )
                )

        if options["assign_everyone"]:
            self._assign_everyone(dry_run=options["dry_run"])

    def _assign_everyone(self, *, dry_run):
        submitters = Group.objects.filter(name=SUBMITTER_GROUP).first()
        if submitters is None:
            self.stdout.write(
                self.style.ERROR(f"'{SUBMITTER_GROUP}' does not exist -- nothing assigned.")
            )
            return

        candidates = (
            get_user_model()
            .objects.filter(is_active=True)
            .exclude(groups=submitters)
            .order_by("email")
        )
        emails = list(candidates.values_list("email", flat=True))
        if not emails:
            self.stdout.write(f"Every active user is already in '{SUBMITTER_GROUP}'.")
            return

        if dry_run:
            self.stdout.write(f"Would add {len(emails)} user(s) to '{SUBMITTER_GROUP}':")
            for email in emails:
                self.stdout.write(f"    {email}")
            return

        submitters.user_set.add(*candidates)
        self.stdout.write(
            self.style.SUCCESS(f"Added {len(emails)} user(s) to '{SUBMITTER_GROUP}'")
        )
