"""
Create the artwork register's role groups and assign their permissions.

Usage::

    python manage.py setup_artwork_groups           # create / update the groups
    python manage.py setup_artwork_groups --list    # show what each group holds

Two roles, which is what the register needs:

* **Artwork Viewer** -- anybody who has to look up what is printed on a label:
  QA, production, dispatch, purchase. Read and download, nothing else.
* **Artwork Editor** -- the people who hold the artwork files and file them:
  packaging development / QA documentation. Capture, revise and retire.

Editor holds the view right explicitly as well as the manage right. It is
implied at the endpoints, but a group that lists both is one somebody can read
off the admin screen and understand.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

VIEW = "artwork.can_view_artwork"
MANAGE = "artwork.can_manage_artwork"

ARTWORK_GROUPS = {
    "Artwork Viewer": [VIEW],
    "Artwork Editor": [VIEW, MANAGE],
}


class Command(BaseCommand):
    help = "Create the Artwork Viewer / Artwork Editor groups and assign permissions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list",
            action="store_true",
            help="List the groups and their permissions without changing anything.",
        )

    def handle(self, *args, **options):
        if options["list"]:
            self._list()
            return

        for name, codes in ARTWORK_GROUPS.items():
            group, created = Group.objects.get_or_create(name=name)
            permissions, missing = self._resolve(codes)

            if missing:
                self.stdout.write(
                    self.style.ERROR(
                        f"{name}: permission(s) not found: {', '.join(missing)}. "
                        f"Run `migrate` first -- the permissions are created with "
                        f"the artwork tables."
                    )
                )
                continue

            group.permissions.set(permissions)
            verb = "Created" if created else "Updated"
            self.stdout.write(
                self.style.SUCCESS(f"{verb} {name} ({len(permissions)} permission(s))")
            )

    def _resolve(self, codes):
        permissions, missing = [], []
        for code in codes:
            app_label, codename = code.split(".", 1)
            permission = Permission.objects.filter(
                content_type__app_label=app_label, codename=codename
            ).first()
            if permission is None:
                missing.append(code)
            else:
                permissions.append(permission)
        return permissions, missing

    def _list(self):
        for name in ARTWORK_GROUPS:
            group = Group.objects.filter(name=name).first()
            if group is None:
                self.stdout.write(self.style.WARNING(f"{name}: (not created)"))
                continue
            self.stdout.write(self.style.SUCCESS(f"{name}:"))
            for codename in group.permissions.values_list("codename", flat=True):
                self.stdout.write(f"    - {codename}")
