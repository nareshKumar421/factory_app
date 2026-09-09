"""Create/update the raw-material stock register permission groups.

Two roles, deliberately split:

- "RM Stock Keeper" — the store keeper who states what is on their floor. Can
  read the register and set quantities.
- "RM Stock Viewer" — read-only. Production planning and supervisors read the
  register but must not be able to rewrite the floor's figures.

    python manage.py setup_rm_stock_groups           # create/update
    python manage.py setup_rm_stock_groups --list     # show what they hold

Two things a group does NOT grant, both of which have to be arranged separately:

* a `UserCompany` (company access), without which no warehouse screen loads;
* a `UserWarehouse` assignment. Setting a quantity also requires the user to
  manage that warehouse, so a keeper with the group and no assignment is
  refused every save. Run `report_warehouse_scope_gaps` after granting this.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

RM_STOCK_GROUPS = {
    "RM Stock Keeper": [
        "warehouse.can_view_rm_stock",
        "warehouse.can_set_rm_stock",
    ],
    "RM Stock Viewer": [
        "warehouse.can_view_rm_stock",
    ],
}


class Command(BaseCommand):
    help = (
        "Create/update the raw-material stock register groups "
        "(RM Stock Keeper, RM Stock Viewer)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--list", action="store_true", help="List the groups and their permissions."
        )

    def handle(self, *args, **options):
        if options["list"]:
            for name in RM_STOCK_GROUPS:
                group = Group.objects.filter(name=name).first()
                if not group:
                    self.stdout.write(f"{name}: (not created)")
                    continue
                self.stdout.write(f"{name}:")
                for perm in group.permissions.all().order_by("codename"):
                    self.stdout.write(
                        f"  - {perm.content_type.app_label}.{perm.codename}"
                    )
            return

        for name, codenames in RM_STOCK_GROUPS.items():
            group, created = Group.objects.get_or_create(name=name)
            perms = []
            for dotted in codenames:
                app_label, codename = dotted.split(".", 1)
                perm = Permission.objects.filter(
                    content_type__app_label=app_label, codename=codename
                ).first()
                if perm is None:
                    self.stderr.write(
                        self.style.WARNING(
                            f"  ! permission not found, skipped: {dotted} "
                            "(has the warehouse migration been applied?)"
                        )
                    )
                    continue
                perms.append(perm)
            group.permissions.set(perms)
            verb = "Created" if created else "Updated"
            self.stdout.write(
                self.style.SUCCESS(
                    f"{verb} group '{name}' with {len(perms)} permissions."
                )
            )
