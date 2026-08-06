"""
Grant Activity Center access.

Two separate things, deliberately kept apart:

* ``can_view_my_activities`` is self-scoped — it can only ever show a user their own
  work — so it is safe to give to everyone. It goes on an "Activity Center" group.
* ``can_view_all_activities`` exposes every user's pending and completed work, so it
  goes on a separate "Activity Supervisor" group that you populate by hand.

Nothing is granted unless you pass ``--apply``; the default is a dry run that prints
exactly what would change.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand
from django.db import transaction

User = get_user_model()

USER_GROUP = "Activity Center"
SUPERVISOR_GROUP = "Activity Supervisor"

USER_PERMS = ["can_view_my_activities"]
SUPERVISOR_PERMS = [
    "can_view_my_activities",
    "can_view_all_activities",
    "can_view_activity_reports",
]


class Command(BaseCommand):
    help = "Create the Activity Center groups and (optionally) enrol all active users."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write the changes. Without this the command only reports.",
        )
        parser.add_argument(
            "--enrol-all-users",
            action="store_true",
            help="Add every active user to the '%s' group." % USER_GROUP,
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        enrol_all = options["enrol_all_users"]

        perms = {
            perm.codename: perm
            for perm in Permission.objects.filter(
                content_type__app_label="activity_center"
            )
        }
        missing = set(SUPERVISOR_PERMS) - set(perms)
        if missing:
            self.stderr.write(
                self.style.ERROR(
                    "Activity Center permissions are missing: %s. "
                    "Run `manage.py migrate activity_center` first."
                    % ", ".join(sorted(missing))
                )
            )
            return

        with transaction.atomic():
            for name, codenames in ((USER_GROUP, USER_PERMS), (SUPERVISOR_GROUP, SUPERVISOR_PERMS)):
                group, created = (
                    Group.objects.get_or_create(name=name)
                    if apply_changes
                    else (Group.objects.filter(name=name).first(), False)
                )
                verb = "Created" if created else ("Updated" if group else "Would create")
                self.stdout.write("%s group %r with: %s" % (verb, name, ", ".join(codenames)))
                if apply_changes and group:
                    group.permissions.set([perms[codename] for codename in codenames])

            if enrol_all:
                group = Group.objects.filter(name=USER_GROUP).first()
                users = User.objects.filter(is_active=True)
                if apply_changes and group:
                    for user in users:
                        user.groups.add(group)
                    self.stdout.write(
                        self.style.SUCCESS(
                            "Enrolled %d active users in %r." % (users.count(), USER_GROUP)
                        )
                    )
                else:
                    self.stdout.write(
                        "Would enrol %d active users in %r." % (users.count(), USER_GROUP)
                    )

            if not apply_changes:
                self.stdout.write(
                    self.style.WARNING("Dry run — nothing written. Re-run with --apply.")
                )
                transaction.set_rollback(True)

        self.stdout.write(
            "Populate %r by hand — it exposes every user's work." % SUPERVISOR_GROUP
        )
