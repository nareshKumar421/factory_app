"""
Create the ETP / STP role groups and assign their permissions.

Usage::

    python manage.py setup_etp_groups           # create / update all groups
    python manage.py setup_etp_groups --list    # show what each group holds

Three roles, matching who signs the paper registers: the plant operator fills
the daily registers, the QA chemist also owns monitoring and calibration and
countersigns, and the QA manager additionally maintains the masters.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

VIEW_ALL = [
    "etp.can_view_etp_module",
    "etp.can_view_etp_daily_log",
    "etp.can_view_etp_monitoring",
    "etp.can_view_etp_chemical",
    "etp.can_view_etp_sludge",
    "etp.can_view_etp_backwash",
    "etp.can_view_etp_calibration",
]

ETP_GROUPS = {
    "ETP Plant Operator": VIEW_ALL
    + [
        "etp.can_manage_etp_daily_log",
        "etp.can_manage_etp_chemical",
        "etp.can_manage_etp_sludge",
        "etp.can_manage_etp_backwash",
        "etp.can_manage_etp_monitoring",
    ],
    "ETP QA Chemist": VIEW_ALL
    + [
        "etp.can_manage_etp_daily_log",
        "etp.can_manage_etp_chemical",
        "etp.can_manage_etp_sludge",
        "etp.can_manage_etp_backwash",
        "etp.can_manage_etp_monitoring",
        "etp.can_verify_etp_monitoring",
        "etp.can_manage_etp_calibration",
    ],
    "ETP QA Manager": VIEW_ALL
    + [
        "etp.can_manage_etp_daily_log",
        "etp.can_manage_etp_chemical",
        "etp.can_manage_etp_sludge",
        "etp.can_manage_etp_backwash",
        "etp.can_manage_etp_monitoring",
        "etp.can_verify_etp_monitoring",
        "etp.can_manage_etp_calibration",
        "etp.can_manage_etp_settings",
    ],
    # Read-only: EHS / audit / management who only look at the registers.
    "ETP Viewer": VIEW_ALL,
}


class Command(BaseCommand):
    help = "Create ETP / STP role groups and assign permissions."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list", action="store_true", help="List groups and their permissions"
        )

    def handle(self, *args, **options):
        if options["list"]:
            for name in ETP_GROUPS:
                group = Group.objects.filter(name=name).first()
                if group is None:
                    self.stdout.write(self.style.WARNING(f"{name}: (not created)"))
                    continue
                self.stdout.write(self.style.SUCCESS(f"{name}:"))
                for codename in group.permissions.values_list("codename", flat=True):
                    self.stdout.write(f"    - {codename}")
            return

        for name, permission_codes in ETP_GROUPS.items():
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
