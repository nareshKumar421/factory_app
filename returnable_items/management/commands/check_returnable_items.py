"""Send due-today reminders and flag overdue returnable gate passes.

Idempotent — safe to run repeatedly; each pass is notified at most once for each
of the two events.

Run once (manual / external cron / Windows Task Scheduler):

    python manage.py check_returnable_items

For fully automatic checks, use the APScheduler loop instead, which also runs
the work-permit expiry job in the same process:

    python manage.py run_scheduler
"""

from django.core.management.base import BaseCommand

from returnable_items.jobs import run_returnable_checks


class Command(BaseCommand):
    help = "Notify due-today returnable gate passes and flag the overdue ones."

    def handle(self, *args, **options):
        result = run_returnable_checks()
        self.stdout.write(
            self.style.SUCCESS(
                f"Due-today notifications: {result['due_notified']}. "
                f"Flagged overdue: {result['overdue_flagged']}."
            )
        )
