"""Create/update the godown outward-movement permission groups.

Two roles, deliberately split:

- "Godown Movement Keeper" — the keeper who declares what is leaving his floor.
  Can read the register and file movements onto it.
- "Godown Movement Viewer" — read-only. Dispatch, planning and management read
  the register (and the dashboard built on it) but must not be able to rewrite
  what a keeper declared.

    python manage.py setup_pf_movement_groups           # create/update
    python manage.py setup_pf_movement_groups --list    # show what they hold

Two things a group does NOT grant, both of which have to be arranged separately:

* a `UserCompany` (company access), without which no warehouse screen loads;
* a `UserWarehouse` assignment. Filing a movement also requires the user to
  manage the source warehouse, so a keeper with the group and no assignment for
  BH-PF is refused every save. Run `report_warehouse_scope_gaps` after granting
  this.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

PF_MOVEMENT_GROUPS = {
    "Godown Movement Keeper": [
        "warehouse.can_view_pf_movement",
        "warehouse.can_record_pf_movement",
    ],
    "Godown Movement Viewer": [
        "warehouse.can_view_pf_movement",
    ],
}


class Command(BaseCommand):
    help = (
        "Create/update the godown outward-movement groups "
        "(Godown Movement Keeper, Godown Movement Viewer)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--list", action="store_true", help="List the groups and their permissions."
        )

    def handle(self, *args, **options):
        if options["list"]:
            for name in PF_MOVEMENT_GROUPS:
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

        for name, codenames in PF_MOVEMENT_GROUPS.items():
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
