"""
Create Blowing role groups and assign permissions.

Usage:
    python manage.py setup_blowing_groups          # create/update all groups
    python manage.py setup_blowing_groups --list    # list groups and permissions
"""
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

BLOWING_GROUPS = {
    "Blowing Operator": [
        "blowing.can_view_blowing_run",
        "blowing.can_create_blowing_run",
        "blowing.can_edit_blowing_run",
    ],
    "Blowing Supervisor": [
        "blowing.can_view_blowing_run",
        "blowing.can_create_blowing_run",
        "blowing.can_edit_blowing_run",
        "blowing.can_complete_blowing_run",
        "blowing.can_manage_blowing_machines",
        "blowing.can_manage_preform_specs",
        "blowing.can_view_blowing_reports",
        "blowing.can_create_blowing_breakdown",
        "blowing.can_manage_blowing_breakdown_categories",
        "blowing.can_request_preform",
    ],
    "Blowing HOD": [
        "blowing.can_view_blowing_run",
        "blowing.can_create_blowing_run",
        "blowing.can_edit_blowing_run",
        "blowing.can_complete_blowing_run",
        "blowing.can_manage_blowing_machines",
        "blowing.can_manage_preform_specs",
        "blowing.can_manage_blowing_rate_config",
        "blowing.can_view_blowing_reports",
        "blowing.can_create_blowing_breakdown",
        "blowing.can_manage_blowing_breakdown_categories",
        "blowing.can_request_preform",
        "blowing.can_approve_preform_request",
    ],
    "Blowing Operator+": [
        # Operator who can also run the line and log breakdowns
        "blowing.can_view_blowing_run",
        "blowing.can_create_blowing_run",
        "blowing.can_edit_blowing_run",
        "blowing.can_create_blowing_breakdown",
        "blowing.can_request_preform",
    ],
}


class Command(BaseCommand):
    help = "Create Blowing role groups and assign permissions."

    def add_arguments(self, parser):
        parser.add_argument('--list', action='store_true', help='List groups and their permissions')

    def handle(self, *args, **options):
        if options['list']:
            for name in BLOWING_GROUPS:
                try:
                    group = Group.objects.get(name=name)
                except Group.DoesNotExist:
                    self.stdout.write(self.style.WARNING(f"{name}: (not created)"))
                    continue
                self.stdout.write(self.style.SUCCESS(f"{name}:"))
                for perm in group.permissions.values_list('codename', flat=True):
                    self.stdout.write(f"    - {perm}")
            return

        for name, perm_codes in BLOWING_GROUPS.items():
            group, _ = Group.objects.get_or_create(name=name)
            perms = []
            for code in perm_codes:
                app_label, codename = code.split('.', 1)
                try:
                    perms.append(Permission.objects.get(
                        content_type__app_label=app_label, codename=codename))
                except Permission.DoesNotExist:
                    self.stdout.write(self.style.ERROR(f"  Missing permission: {code}"))
            group.permissions.set(perms)
            self.stdout.write(self.style.SUCCESS(f"{name}: {len(perms)} permissions set"))
