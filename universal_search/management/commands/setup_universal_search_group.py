"""
Create the Universal Search group and put the module's permission in it.

Usage::

    python manage.py setup_universal_search_group          # create / update
    python manage.py setup_universal_search_group --list   # show who is in it

One group, one permission. The right only opens the modal: every result is
filtered again against the view permission of the module that owns it, so a
user added here sees exactly the records they could already have opened --
found by number instead of by navigating to them.

That is why this group is safe to hand out widely, and why it is worth doing:
a permission nobody holds is a feature nobody has.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

GROUP_NAME = "Universal Search"
PERMISSIONS = ["universal_search.can_use_universal_search"]


class Command(BaseCommand):
    help = "Create the Universal Search group and assign its permission."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list",
            action="store_true",
            help="Show the group, its permission and its members",
        )

    def handle(self, *args, **options):
        if options["list"]:
            self._list()
            return

        group, created = Group.objects.get_or_create(name=GROUP_NAME)
        permissions = []
        for code in PERMISSIONS:
            app_label, codename = code.split(".", 1)
            permission = Permission.objects.filter(
                content_type__app_label=app_label, codename=codename
            ).first()
            if permission is None:
                self.stdout.write(
                    self.style.WARNING(
                        f"missing permission {code} -- run migrate first"
                    )
                )
                continue
            permissions.append(permission)

        group.permissions.set(permissions)
        verb = "created" if created else "updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} {GROUP_NAME} ({len(permissions)} permission(s), "
                f"{group.user_set.count()} member(s))"
            )
        )
        if created:
            self.stdout.write(
                "Nobody is in it yet -- add users in the admin, or the modal "
                "stays closed for everyone."
            )

    def _list(self):
        group = Group.objects.filter(name=GROUP_NAME).first()
        if group is None:
            self.stdout.write(self.style.WARNING(f"{GROUP_NAME}: (not created)"))
            return
        self.stdout.write(self.style.SUCCESS(f"{GROUP_NAME}:"))
        for codename in group.permissions.values_list("codename", flat=True):
            self.stdout.write(f"    - {codename}")
        members = group.user_set.count()
        self.stdout.write(f"    {members} member(s)")
