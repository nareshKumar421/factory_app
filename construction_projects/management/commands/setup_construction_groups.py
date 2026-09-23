"""Create the module's three role groups. Idempotent -- safe to re-run after
adding a permission.

    .venv/bin/python manage.py setup_construction_groups
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

APP_LABEL = "construction_projects"

#: The approver group deliberately holds nothing but viewing and approving:
#: the person who sanctions the money does not also record the spend.
GROUPS = {
    "construction_site": [
        "can_view_project",
        "can_log_daily_work",
        "can_record_expense",
    ],
    "construction_manager": [
        # The project manager checks the day's payments. Sanctioning a budget is
        # a different job and stays with construction_approver.
        "can_approve_expense",
        "can_view_project",
        "can_view_all_projects",
        "can_create_project",
        "can_edit_project",
        "can_close_project",
        "can_log_daily_work",
        "can_record_expense",
    ],
    "construction_approver": [
        "can_view_project",
        "can_view_all_projects",
        "can_approve_project",
    ],
}


class Command(BaseCommand):
    help = "Create or refresh the construction module's role groups."

    def handle(self, *args, **options):
        available = {
            perm.codename: perm
            for perm in Permission.objects.filter(content_type__app_label=APP_LABEL)
        }

        for group_name, codenames in GROUPS.items():
            group, created = Group.objects.get_or_create(name=group_name)
            missing = [code for code in codenames if code not in available]
            if missing:
                self.stderr.write(
                    self.style.WARNING(
                        f"{group_name}: permission(s) not found, skipped: "
                        f"{', '.join(missing)}"
                    )
                )
            group.permissions.set(
                [available[code] for code in codenames if code in available]
            )
            verb = "created" if created else "updated"
            self.stdout.write(
                self.style.SUCCESS(f"{verb} {group_name} ({len(codenames)} perms)")
            )
