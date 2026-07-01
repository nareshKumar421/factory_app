"""Create/update the Branch Stock Transfer (BST) permission group.

A "BST Operator" only uses the warehouse BST module — creating, scanning,
approving and receiving branch stock transfers. The group is granted exactly the
dedicated `warehouse` BST permissions (plus the model's `view` permission) and
nothing else, so a user placed in it sees only "Warehouse → Branch Transfer" in
the app.

    python manage.py setup_bst_group           # create/update the group
    python manage.py setup_bst_group --list     # show the group's permissions

Note: the user also needs a `UserCompany` (company access) to use BST — that is
assigned separately, not by this group.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

BST_GROUPS = {
    "BST Operator": [
        "warehouse.view_bsttransfer",   # list / detail / incoming / dashboard
        "warehouse.can_create_bst",     # create + scan + review/approve
        "warehouse.can_scan_bst",
        "warehouse.can_dispatch_bst",
        "warehouse.can_receive_bst",    # receive incoming transfers
    ],
}


class Command(BaseCommand):
    help = "Create/update the BST Operator permission group."

    def add_arguments(self, parser):
        parser.add_argument("--list", action="store_true", help="List the group and its permissions.")

    def handle(self, *args, **options):
        if options["list"]:
            for name in BST_GROUPS:
                group = Group.objects.filter(name=name).first()
                if not group:
                    self.stdout.write(f"{name}: (not created)")
                    continue
                self.stdout.write(f"{name}:")
                for perm in group.permissions.all().order_by("codename"):
                    self.stdout.write(f"  - {perm.content_type.app_label}.{perm.codename}")
            return

        for name, codenames in BST_GROUPS.items():
            group, created = Group.objects.get_or_create(name=name)
            perms = []
            for dotted in codenames:
                app_label, codename = dotted.split(".", 1)
                perm = Permission.objects.filter(
                    content_type__app_label=app_label, codename=codename
                ).first()
                if perm is None:
                    self.stderr.write(
                        self.style.WARNING(f"  ! permission not found, skipped: {dotted}")
                    )
                    continue
                perms.append(perm)
            group.permissions.set(perms)
            verb = "Created" if created else "Updated"
            self.stdout.write(
                self.style.SUCCESS(f"{verb} group '{name}' with {len(perms)} permissions.")
            )
