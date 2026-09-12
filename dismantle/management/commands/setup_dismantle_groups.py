"""
Create the Dismantle role groups and assign their permissions.

Usage::

    python manage.py setup_dismantle_groups           # create / update groups
    python manage.py setup_dismantle_groups --list    # show what each group holds

Three roles. The split that matters is the last one: **posting is separate from
creating**, because posting writes three documents into SAP that nobody here can
withdraw. A storeman prepares the dismantle and says what actually came back off
the floor; whoever holds "Dismantle Poster" is the one who commits it.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

DISMANTLE_GROUPS = {
    "Dismantle Operator": [
        "dismantle.can_view_dismantle",
        "dismantle.can_create_dismantle",
        "dismantle.can_edit_dismantle",
    ],
    "Dismantle Poster": [
        "dismantle.can_view_dismantle",
        "dismantle.can_create_dismantle",
        "dismantle.can_edit_dismantle",
        "dismantle.can_post_dismantle",
    ],
    "Dismantle Viewer": [
        "dismantle.can_view_dismantle",
    ],
}


class Command(BaseCommand):
    help = "Create Dismantle role groups and assign permissions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list", action="store_true", help="List groups and their permissions"
        )

    def handle(self, *args, **options):
        if options["list"]:
            for name in DISMANTLE_GROUPS:
                group = Group.objects.filter(name=name).first()
                if group is None:
                    self.stdout.write(self.style.WARNING(f"{name}: (not created)"))
                    continue
                self.stdout.write(self.style.SUCCESS(f"{name}:"))
                for codename in group.permissions.values_list("codename", flat=True):
                    self.stdout.write(f"    - {codename}")
            return

        for name, permission_codes in DISMANTLE_GROUPS.items():
            group, created = Group.objects.get_or_create(name=name)
            permissions = []
            missing = []
            for code in permission_codes:
                app_label, codename = code.split(".", 1)
                permission = Permission.objects.filter(
                    content_type__app_label=app_label, codename=codename
                ).first()
                if permission is None:
                    missing.append(code)
                else:
                    permissions.append(permission)
            group.permissions.set(permissions)
            verb = "created" if created else "updated"
            self.stdout.write(
                self.style.SUCCESS(f"{verb} {name} ({len(permissions)} permissions)")
            )
            for code in missing:
                self.stdout.write(
                    self.style.WARNING(
                        f"    missing permission {code} — run migrate first"
                    )
                )
