"""
Create the A/P Invoice Draft role groups and assign their permissions.

Usage::

    python manage.py setup_ap_invoice_draft_groups           # create / update groups
    python manage.py setup_ap_invoice_draft_groups --list    # show what each group holds

The store makes the entries (and with them the SAP drafts); whoever audits the
bills marks the checks the app cannot settle by itself -- a signature, a QC
record kept on paper. Everybody else who needs to see where a bill stands gets
the viewer group.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

AP_INVOICE_DRAFT_GROUPS = {
    "AP Invoice Draft Maker": [
        "ap_invoice_draft.can_view_ap_invoice_draft",
        "ap_invoice_draft.can_create_ap_invoice_draft",
    ],
    "AP Invoice Draft Auditor": [
        "ap_invoice_draft.can_view_ap_invoice_draft",
        "ap_invoice_draft.can_review_ap_invoice_draft",
    ],
    "AP Invoice Draft Viewer": [
        "ap_invoice_draft.can_view_ap_invoice_draft",
    ],
}


class Command(BaseCommand):
    help = "Create A/P Invoice Draft role groups and assign permissions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list", action="store_true", help="List groups and their permissions"
        )

    def handle(self, *args, **options):
        if options["list"]:
            for name in AP_INVOICE_DRAFT_GROUPS:
                group = Group.objects.filter(name=name).first()
                if group is None:
                    self.stdout.write(self.style.WARNING(f"{name}: (not created)"))
                    continue
                self.stdout.write(self.style.SUCCESS(f"{name}:"))
                for codename in group.permissions.values_list("codename", flat=True):
                    self.stdout.write(f"    - {codename}")
            return

        for name, permission_codes in AP_INVOICE_DRAFT_GROUPS.items():
            group, created = Group.objects.get_or_create(name=name)
            permissions = []
            missing = []
            for code in permission_codes:
                app_label, codename = code.split(".", 1)
                permission = Permission.objects.filter(
                    content_type__app_label=app_label, codename=codename
                ).first()
                if permission is None:
                    missing.append(code)
                else:
                    permissions.append(permission)
            group.permissions.set(permissions)
            verb = "created" if created else "updated"
            self.stdout.write(
                self.style.SUCCESS(f"{verb} {name} ({len(permissions)} permissions)")
            )
            for code in missing:
                self.stdout.write(
                    self.style.WARNING(
                        f"    missing permission {code} — run migrate first"
                    )
                )
