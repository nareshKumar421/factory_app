"""
Create the leave module's groups and give each one its permissions.

Usage::

    python manage.py setup_leave_groups
    python manage.py setup_leave_groups --list

Four groups, and as with the other modules here the interesting part is what
each one does **not** get:

* **Leave Applicant** -- apply for their own, and see their own. Nothing about
  anybody else. This is most of the workforce.
* **Leave Approver (Manager)** -- their team's requests and the right to decide
  them. Not the right to cancel one after the fact, and not a reach outside
  their own reporting line: the permission is scoped by the tree at the point
  of use, so granting it widely still does not widen anybody's reach.
* **Time Office** -- raises applications on behalf of people who have no login,
  which on the live data is roughly half the workforce. Deliberately *cannot*
  decide: entering somebody's leave and approving it are different jobs, and
  one person doing both unobserved is the thing this split exists to prevent.
* **Leave Administrator (HR)** -- decides for anybody, cancels approved leave,
  and owns the leave types and the holiday calendar.

Like ``setup_employee_hierarchy_groups``, this creates the groups and puts
**nobody** in them. A fresh login therefore gets a 403 on every leave endpoint
until somebody decides which of the four it is -- which is the intended
failure, because the alternative is a default that quietly grants something.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

APP = "leave"

LEAVE_GROUPS = {
    "Leave Applicant": [
        "can_apply_leave",
    ],
    "Leave Approver (Manager)": [
        "can_apply_leave",
        "can_view_team_leave",
        "can_decide_leave",
    ],
    "Time Office": [
        "can_apply_leave",
        "can_apply_leave_for_others",
        "can_view_team_leave",
    ],
    "Leave Administrator (HR)": [
        "can_apply_leave",
        "can_apply_leave_for_others",
        "can_view_team_leave",
        "can_decide_leave",
        "can_decide_any_leave",
        "can_cancel_approved_leave",
        "can_manage_leave_types",
    ],
}


class Command(BaseCommand):
    help = "Create the leave groups and assign their permissions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list",
            action="store_true",
            help="Show what each group would get, and change nothing.",
        )

    def handle(self, *args, **options):
        if options["list"]:
            for name, codenames in LEAVE_GROUPS.items():
                self.stdout.write(self.style.MIGRATE_HEADING(name))
                for codename in codenames:
                    self.stdout.write(f"    {APP}.{codename}")
            return

        for name, codenames in LEAVE_GROUPS.items():
            group, created = Group.objects.get_or_create(name=name)
            permissions = list(
                Permission.objects.filter(
                    content_type__app_label=APP, codename__in=codenames
                )
            )
            missing = set(codenames) - {p.codename for p in permissions}
            if missing:
                self.stdout.write(
                    self.style.WARNING(
                        f"  {name}: missing permission(s) {sorted(missing)} -- "
                        "run migrate first."
                    )
                )
            group.permissions.set(permissions)
            verb = "created" if created else "updated"
            self.stdout.write(
                self.style.SUCCESS(f"  {verb} {name} ({len(permissions)} permission(s))")
            )

        self.stdout.write("")
        self.stdout.write(
            "Groups are empty by design -- add users deliberately, "
            "not by default."
        )
