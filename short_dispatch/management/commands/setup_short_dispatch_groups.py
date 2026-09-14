"""
Create the Short Dispatch role groups and assign their permissions.

Usage::

    python manage.py setup_short_dispatch_groups           # create / update groups
    python manage.py setup_short_dispatch_groups --list    # show what each group holds

Two roles only, because the module has two actions. Recording a short dispatch
*is* posting it -- the form writes an A/R Return into SAP that nobody in this app
can withdraw -- so "Short Dispatch Operator" is a permission to change stock in
SAP, not merely to fill a form in. Everybody else who needs to know what came back
off a bill gets the viewer group.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

SHORT_DISPATCH_GROUPS = {
    "Short Dispatch Operator": [
        "short_dispatch.can_view_short_dispatch",
        "short_dispatch.can_create_short_dispatch",
    ],
    "Short Dispatch Viewer": [
        "short_dispatch.can_view_short_dispatch",
    ],
}


class Command(BaseCommand):
    help = "Create Short Dispatch role groups and assign permissions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list", action="store_true", help="List groups and their permissions"
        )

    def handle(self, *args, **options):
        if options["list"]:
            for name in SHORT_DISPATCH_GROUPS:
                group = Group.objects.filter(name=name).first()
                if group is None:
                    self.stdout.write(self.style.WARNING(f"{name}: (not created)"))
                    continue
                self.stdout.write(self.style.SUCCESS(f"{name}:"))
                for codename in group.permissions.values_list("codename", flat=True):
                    self.stdout.write(f"    - {codename}")
            return

        for name, permission_codes in SHORT_DISPATCH_GROUPS.items():
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
