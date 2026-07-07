"""
maintenance/management/commands/run_work_permit_scheduler.py

Starts an APScheduler loop that automatically expires work permits the moment
their validity window passes and notifies the Fire Department Head.

Usage:
    python manage.py run_work_permit_scheduler

Configuration (settings.py / .env):
    WORK_PERMIT_EXPIRY_INTERVAL_MINUTES — how often to check (default: 5)
"""

import logging

from django.conf import settings
from django.core.management.base import BaseCommand

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from django_apscheduler.jobstores import DjangoJobStore

from maintenance.jobs import expire_lapsed_work_permits

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_MINUTES = 5


class Command(BaseCommand):
    help = "Runs the APScheduler that auto-expires lapsed work permits."

    def handle(self, *args, **options):
        scheduler = BlockingScheduler(timezone=settings.TIME_ZONE)
        scheduler.add_jobstore(DjangoJobStore(), "default")

        interval_minutes = getattr(
            settings, "WORK_PERMIT_EXPIRY_INTERVAL_MINUTES", DEFAULT_INTERVAL_MINUTES
        )

        scheduler.add_job(
            expire_lapsed_work_permits,
            trigger=IntervalTrigger(minutes=interval_minutes),
            id="expire_work_permits",
            max_instances=1,
            replace_existing=True,
        )
        logger.info(
            "[WorkPermit] Expiry scheduler started — interval=%smin", interval_minutes
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Work permit expiry scheduler running (every {interval_minutes} min). "
                "Press Ctrl+C to stop."
            )
        )

        try:
            scheduler.start()
        except KeyboardInterrupt:
            scheduler.shutdown()
            self.stdout.write(self.style.WARNING("Scheduler stopped."))
