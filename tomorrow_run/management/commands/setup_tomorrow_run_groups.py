"""Create the Tomorrow's run groups. Idempotent -- safe to re-run.

    .venv/bin/python manage.py setup_tomorrow_run_groups

Run after ``migrate``: Django creates permission rows after every migration in
a run, so a group made inside a migration comes out empty.
"""

from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand

APP_LABEL = "tomorrow_run"

GROUPS = {
    # Everyone who needs to see tomorrow's machines.
    "tomorrow_run_viewer": ["can_view_tomorrow_run"],
    # Gurvinder veerji, Daman: pick what runs first.
    "tomorrow_run_picker": ["can_view_tomorrow_run", "can_pick_tomorrow_run"],
    # The planning team: put the sheet in, read again by hand.
    "tomorrow_run_planner": ["can_view_tomorrow_run", "can_manage_tomorrow_run"],
}


class Command(BaseCommand):
    help = "Create or refresh the Tomorrow's run groups."

    def handle(self, *args, **options):
        available = {p.codename: p for p in Permission.objects.filter(content_type__app_label=APP_LABEL)}
        for name, codes in GROUPS.items():
            group, created = Group.objects.get_or_create(name=name)
            missing = [c for c in codes if c not in available]
            if missing:
                self.stderr.write(self.style.WARNING(f"{name}: not found, skipped: {', '.join(missing)}"))
            group.permissions.set([available[c] for c in codes if c in available])
            self.stdout.write(f"{'created' if created else 'updated'} {name}: {group.permissions.count()} permission(s)")
