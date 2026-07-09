"""Grant the Work Permit approval permission (Fire Department Head) to a group.

Adds ``maintenance.can_approve_work_permit`` to the named auth Group so its members
(e.g. the Fire Department Head) can approve submitted work permits. Idempotent.

Usage:
    python manage.py grant_permit_approval "Fire Department"

The group must already exist (create it in admin and assign users first). Pass
``--create`` to create it if missing.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Grant maintenance.can_approve_work_permit to a group (Fire Department Head)."

    def add_arguments(self, parser):
        parser.add_argument("group", help="Exact auth Group name, e.g. 'Fire Department'")
        parser.add_argument(
            "--create",
            action="store_true",
            help="Create the group if it does not exist.",
        )

    def handle(self, *args, **options):
        name = options["group"]
        try:
            perm = Permission.objects.get(
                content_type__app_label="maintenance",
                codename="can_approve_work_permit",
            )
        except Permission.DoesNotExist:
            raise CommandError(
                "Permission maintenance.can_approve_work_permit not found — run migrations first."
            )

        if options["create"]:
            group, created = Group.objects.get_or_create(name=name)
            if created:
                self.stdout.write(self.style.WARNING(f"Created group '{name}'."))
        else:
            try:
                group = Group.objects.get(name=name)
            except Group.DoesNotExist:
                available = ", ".join(Group.objects.values_list("name", flat=True))
                raise CommandError(
                    f"Group '{name}' not found. Existing groups: {available}"
                )

        group.permissions.add(perm)
        members = list(group.user_set.values_list("email", flat=True))
        self.stdout.write(
            self.style.SUCCESS(
                f"Granted 'Can approve Work permits' to group '{name}'. "
                f"Members ({len(members)}): {', '.join(members) or 'none'}"
            )
        )
