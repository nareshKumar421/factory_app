"""
Create the cash book -> SAP groups and give them their permissions.

Usage::

    python manage.py setup_cash_sap_groups           # create / top up
    python manage.py setup_cash_sap_groups --list    # show what each holds

* **Cash Book SAP Poster**   -- posts bunches to SAP and uses the escape
  hatches. Accounts, usually the person who keys imprest GRPOs today.
* **Cash Book SAP Settings** -- edits the branch routing and the G/L item map.

Both also hold the cash book's view right: posting or configuring a book you
cannot read is useless. Migration ``0002_seed_and_groups`` creates the same two
groups; this command is the cash book's own convention and is safe to re-run.

Permissions are only ever **added** (``permissions.add``), never ``set``: a
group somebody topped up by hand keeps what they gave it.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

VIEW = "cash_book.can_view_cash_book"
POST = "cash_sap.can_post_cash_to_sap"
SETTINGS = "cash_sap.can_manage_cash_sap_settings"

CASH_SAP_GROUPS = {
    "Cash Book SAP Poster": [VIEW, POST],
    "Cash Book SAP Settings": [VIEW, SETTINGS],
}


class Command(BaseCommand):
    help = "Create the cash book -> SAP groups (Poster, Settings) and add their permissions."

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

        for name, codes in CASH_SAP_GROUPS.items():
            permissions, missing = self._resolve(codes)
            if missing:
                self.stdout.write(
                    self.style.ERROR(
                        f"{name}: permission(s) not found: {', '.join(missing)}. Run `migrate` "
                        f"first -- the permissions are created with the cash_sap tables."
                    )
                )
                continue
            group, created = Group.objects.get_or_create(name=name)
            group.permissions.add(*permissions)
            verb = "Created" if created else "Updated"
            self.stdout.write(self.style.SUCCESS(f"{verb} {name} ({len(permissions)} permission(s))"))

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
        for name in CASH_SAP_GROUPS:
            group = Group.objects.filter(name=name).first()
            if group is None:
                self.stdout.write(self.style.WARNING(f"{name}: (not created)"))
                continue
            self.stdout.write(self.style.SUCCESS(f"{name}:"))
            for perm in group.permissions.select_related("content_type").order_by("content_type__app_label", "codename"):
                self.stdout.write(f"    - {perm.content_type.app_label}.{perm.codename}")
