"""Create/update the Warehouse Transfer Request permission groups.

Four roles. The split between raising and approving is the point of the flow —
the *receiving* warehouse decides, so one person must not hold both for their own
requests. Grant "Transfer Requester" to the sending side and "Transfer Approver"
to the receiving side; anyone who needs to do both gets both, deliberately.

    python manage.py setup_transfer_request_groups          # create/update
    python manage.py setup_transfer_request_groups --list    # show what they hold

Note: a user also needs a `UserCompany` (company access) to use the module — that
is assigned separately, not by these groups.

Note also: since per-user warehouse scoping landed, holding one of these groups is
necessary but NOT sufficient. A user with no warehouse assigned manages nothing
and is refused every raise and approve. Run `report_warehouse_scope_gaps` after
granting a group to see who still needs assigning.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

TRANSFER_GROUPS = {
    # The sending warehouse: raises requests and posts approved ones to SAP.
    # Posting is what actually moves stock, so it travels with the sender.
    "Transfer Requester": [
        "warehouse.can_view_transfer_request",
        "warehouse.can_create_transfer_request",
        "warehouse.can_post_transfer_to_sap",
    ],
    # The receiving warehouse: decides. Deliberately cannot raise or post, so a
    # request is always approved by someone other than whoever asked.
    "Transfer Approver": [
        "warehouse.can_view_transfer_request",
        "warehouse.can_approve_transfer_request",
    ],
    # Read-only oversight — supervisors and the reconciliation report.
    "Transfer Viewer": [
        "warehouse.can_view_transfer_request",
    ],
    # Decides WHO manages which warehouse (Admin -> Warehouse Managers). Kept
    # apart from the movement groups on purpose: a warehouse manager holding this
    # could widen their own scope, which would defeat the restriction.
    "Warehouse Scope Admin": [
        "warehouse.can_manage_user_warehouses",
    ],
}


class Command(BaseCommand):
    help = (
        "Create/update the warehouse transfer-request groups "
        "(Transfer Requester, Transfer Approver, Transfer Viewer)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--list", action="store_true", help="List the groups and their permissions."
        )

    def handle(self, *args, **options):
        if options["list"]:
            for name in TRANSFER_GROUPS:
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

        for name, codenames in TRANSFER_GROUPS.items():
            group, created = Group.objects.get_or_create(name=name)
            perms = []
            for dotted in codenames:
                app_label, codename = dotted.split(".", 1)
                perm = Permission.objects.filter(
                    content_type__app_label=app_label, codename=codename
                ).first()
                if perm is None:
                    # Almost always means migrations have not been applied yet —
                    # the permissions are created by migration 0016.
                    self.stderr.write(
                        self.style.WARNING(
                            f"  ! permission not found, skipped: {dotted} "
                            f"(run migrate first)"
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
