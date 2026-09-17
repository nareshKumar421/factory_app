"""
Create the issue tracker's role groups and assign their permissions.

Usage::

    python manage.py setup_issue_groups                    # create / update the groups
    python manage.py setup_issue_groups --list             # show what each group holds
    python manage.py setup_issue_groups --assign-everyone  # + put every user in Issue Reporter

Three roles, and the split between the first two is the whole point of the
module: **Issue Reporter** is meant for everybody who uses the software -- if
someone can hit a bug they should be able to report it -- while **Issue
Maintainer** is for the handful of people who label, assign and close. A
reporter can still edit and close their own issue without the maintainer right;
that is an object-level rule in ``issues.permissions``, not a group.

``--assign-everyone`` acts on that first sentence: it drops every active
account into **Issue Reporter**. It only ever adds, so running it twice is
harmless, and it skips anyone who already triages -- those groups carry the
right to file on their own. New accounts do not need the flag; they pick the
group up at creation (see :mod:`issues.signals`).
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

from issues.constants import REPORTER_GROUP, TRIAGE_GROUPS

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
    # Also maintains the labels and the support number.
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
        parser.add_argument(
            "--assign-everyone",
            action="store_true",
            help=(
                "Also put every active user in 'Issue Reporter'. Adds only, "
                "skips anyone who already triages."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="With --assign-everyone, report who would be added without writing.",
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

        if options["assign_everyone"]:
            self._assign_everyone(dry_run=options["dry_run"])

    def _assign_everyone(self, *, dry_run):
        """Backfill: every active account that cannot yet file gets to."""
        reporter = Group.objects.filter(name=REPORTER_GROUP).first()
        if reporter is None:
            self.stdout.write(
                self.style.ERROR(f"'{REPORTER_GROUP}' does not exist -- nothing assigned.")
            )
            return

        User = get_user_model()
        # Someone who triages already holds can_create_issues through that
        # group, so adding the reporter group to them would be noise.
        candidates = (
            User.objects.filter(is_active=True)
            .exclude(groups=reporter)
            .exclude(groups__name__in=TRIAGE_GROUPS)
            .order_by("email")
        )

        emails = list(candidates.values_list("email", flat=True))
        if not emails:
            self.stdout.write(f"Every active user is already covered by '{REPORTER_GROUP}'.")
            return

        if dry_run:
            self.stdout.write(f"Would add {len(emails)} user(s) to '{REPORTER_GROUP}':")
            for email in emails:
                self.stdout.write(f"    {email}")
            return

        reporter.user_set.add(*candidates)
        self.stdout.write(
            self.style.SUCCESS(f"Added {len(emails)} user(s) to '{REPORTER_GROUP}'")
        )
