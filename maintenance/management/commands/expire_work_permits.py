"""Expire work permits whose validity window has passed before completion.

Marks lapsed live permits (Submitted / Approved / In Progress) EXPIRED and notifies
the Fire Department Head(s) and the submitter. Idempotent — safe to run repeatedly.

Run once (manual / external cron / Windows Task Scheduler):

    python manage.py expire_work_permits

For fully automatic expiry, use the APScheduler loop instead:

    python manage.py run_work_permit_scheduler
"""

from django.core.management.base import BaseCommand

from maintenance.jobs import expire_lapsed_work_permits


class Command(BaseCommand):
    help = "Mark lapsed work permits EXPIRED and notify the Fire Department Head."

    def handle(self, *args, **options):
        count = expire_lapsed_work_permits()
        self.stdout.write(self.style.SUCCESS(f"Expired {count} work permit(s)."))
