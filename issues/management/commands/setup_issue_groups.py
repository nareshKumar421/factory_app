"""
Create the issue tracker's role groups and assign their permissions.

Usage::

    python manage.py setup_issue_groups          # create / update the groups
    python manage.py setup_issue_groups --list   # show what each group holds

Three roles, and the split between the first two is the whole point of the
module: **Issue Reporter** is meant for everybody who uses the software -- if
someone can hit a bug they should be able to report it -- while **Issue
Maintainer** is for the handful of people who label, assign and close. A
reporter can still edit and close their own issue without the maintainer right;
that is an object-level rule in ``issues.permissions``, not a group.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

ISSUE_GROUPS = {
    # Everyone. Read the list, file an issue, comment, edit and close your own.
    "Issue Reporter": [
        "issues.can_view_issues",
        "issues.can_create_issues",
    ],
    # The people who own the backlog.
    "Issue Maintainer": [
        "issues.can_view_issues",
        "issues.can_create_issues",
        "issues.can_triage_issues",
    ],
    # Also maintains the label and area masters.
    "Issue Admin": [
        "issues.can_view_issues",
        "issues.can_create_issues",
        "issues.can_triage_issues",
        "issues.can_manage_issue_settings",
    ],
}


class Command(BaseCommand):
    help = "Create the issue tracker's role groups and assign their permissions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list",
            action="store_true",
            help="Show each group and its permissions, without writing.",
        )

    def handle(self, *args, **options):
        if options["list"]:
            for name, permissions in ISSUE_GROUPS.items():
                exists = Group.objects.filter(name=name).exists()
                self.stdout.write(
                    f"{name} ({'exists' if exists else 'missing'})"
                )
                for permission in permissions:
                    self.stdout.write(f"    {permission}")
            return

        for name, permission_strings in ISSUE_GROUPS.items():
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
                # Almost always means migrations have not run yet -- the
                # permissions live on an unmanaged sentinel model.
                self.stdout.write(
                    self.style.WARNING(
                        f"  Not found (run migrate first): {', '.join(missing)}"
                    )
                )
