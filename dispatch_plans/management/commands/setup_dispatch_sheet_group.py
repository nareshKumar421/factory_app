"""Create the Dispatch Sheet permission group.

The register is gated on one permission and nothing else:
``dispatch_plans.can_view_dispatch_sheet``. It used to open for anyone holding
``can_view_dispatch_plans`` as well, which meant twenty-four people could read
it without anybody having decided they should — including everyone on a dozen
dashboard groups who have no dispatch role at all.

    python manage.py setup_dispatch_sheet_group
    python manage.py setup_dispatch_sheet_group --list

The group is created EMPTY, deliberately. Who reads the outward register is a
decision for the people who own it, not something a deploy should make by
carrying over whoever happened to see it before.

A user also needs a ``UserCompany`` (company access) to reach the module; that
is assigned separately.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

GROUP_NAME = "Dispatch Sheet Viewer"
PERMISSION = "dispatch_plans.can_view_dispatch_sheet"


class Command(BaseCommand):
    help = "Create/update the Dispatch Sheet Viewer group (read-only register)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list", action="store_true", help="Show the group and who is in it."
        )

    def handle(self, *args, **options):
        if options["list"]:
            group = Group.objects.filter(name=GROUP_NAME).first()
            if not group:
                self.stdout.write(self.style.WARNING(f"{GROUP_NAME}: not created yet"))
                return
            members = sorted(group.user_set.values_list("email", flat=True))
            self.stdout.write(
                self.style.MIGRATE_HEADING(f"{GROUP_NAME} ({len(members)} users)")
            )
            for perm in sorted(
                f"{p.content_type.app_label}.{p.codename}"
                for p in group.permissions.all()
            ):
                self.stdout.write(f"  {perm}")
            for email in members:
                self.stdout.write(f"  - {email}")
            if not members:
                self.stdout.write(
                    "  nobody is in it yet, so nobody can open the register"
                )
            return

        app_label, codename = PERMISSION.split(".", 1)
        permission = Permission.objects.filter(
            content_type__app_label=app_label, codename=codename
        ).first()
        if permission is None:
            # Say so rather than quietly creating a group that grants nothing.
            self.stderr.write(
                self.style.ERROR(f"missing permission {PERMISSION} — run migrate first")
            )
            return

        group, created = Group.objects.get_or_create(name=GROUP_NAME)
        group.permissions.set([permission])
        self.stdout.write(
            self.style.SUCCESS(
                f"{'created' if created else 'updated'} {GROUP_NAME} "
                f"({group.user_set.count()} users)"
            )
        )
        if group.user_set.count() == 0:
            self.stdout.write(
                "Empty on purpose. Add the people who keep the register, in the "
                "admin or by group assignment — until then nobody can open it."
            )
