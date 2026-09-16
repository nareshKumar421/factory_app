"""Create/update the SAP credit-note approval permission groups.

Four groups: an approver and a viewer for each family.

- "Credit Note A/R Approver" / "… A/P Approver" — decides that family. Can read
  its queue and approve or reject.
- "Credit Note A/R Viewer" / "… A/P Viewer" — read-only. Finance and sales read
  the queue to find where a credit note has got stuck; deciding is the
  authorizer's job, not theirs.

A/R (a customer is credited) and A/P (a vendor is debited) are separate because
they are separate jobs — in SAP the two queues' authorizers do not overlap by a
single account. The split is enforced, not cosmetic: the list, the badge and the
decision endpoint all narrow to the families the caller holds, so an A/R-only
user never sees an A/P row, let alone decides one. Grant both pairs to anyone
who genuinely works both.

    python manage.py setup_credit_note_approval_groups           # create/update
    python manage.py setup_credit_note_approval_groups --list    # show holdings

Three things a group does NOT grant, all of which must be arranged separately —
and without all three the Approve button never appears:

* a `UserCompany` (company access), without which no warehouse screen loads;
* a `SapApproverIdentity` mapping the user to their own SAP account in that
  company (Admin → SAP Identities). SAP accepts a decision only from the one
  authorizer it named on the request's current stage, so the app has to know
  which SAP user this person IS before it can offer them anything;
* that SAP account's password in ``SAP_APPROVER_CREDENTIALS``, or the app
  cannot authenticate as them to record the decision at all.

Granting the group alone is therefore safe: a user with no identity mapping can
read the queue and see who each row is stuck on, and can decide nothing.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

# Per family, not one pair for both: A/R credit notes credit a customer (sales)
# and A/P ones debit a vendor (purchasing). A viewer of one sees nothing of the
# other — the list endpoint narrows to the families the caller holds, so an A/R
# viewer's page and badge simply do not contain A/P rows.
CREDIT_NOTE_GROUPS = {
    "Credit Note A/R Approver": [
        "warehouse.can_view_ar_credit_note_approval",
        "warehouse.can_approve_ar_credit_note",
    ],
    "Credit Note A/R Viewer": [
        "warehouse.can_view_ar_credit_note_approval",
    ],
    "Credit Note A/P Approver": [
        "warehouse.can_view_ap_credit_note_approval",
        "warehouse.can_approve_ap_credit_note",
    ],
    "Credit Note A/P Viewer": [
        "warehouse.can_view_ap_credit_note_approval",
    ],
}


class Command(BaseCommand):
    help = (
        "Create/update the SAP credit-note approval groups "
        "(Credit Note A/R + A/P, Approver and Viewer each)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--list", action="store_true", help="List the groups and their permissions."
        )

    def handle(self, *args, **options):
        if options["list"]:
            for name in CREDIT_NOTE_GROUPS:
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

        for name, codenames in CREDIT_NOTE_GROUPS.items():
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
