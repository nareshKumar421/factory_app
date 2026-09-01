"""Single APScheduler process for every time-based job in the system.

Replaces ``run_work_permit_scheduler``: running one scheduler avoids a second
process, a second job store and a second thing to forget to start. The old
command still works and is left in place for back-compat, but do not run both —
each job is registered once here.

Usage:
    python manage.py run_scheduler

Configuration (settings.py / .env):
    WORK_PERMIT_EXPIRY_INTERVAL_MINUTES  — work permit expiry check (default: 5)
    RETURNABLE_CHECK_INTERVAL_MINUTES    — returnable due/overdue check (default: 60)
"""

import logging

from django.conf import settings
from django.core.management.base import BaseCommand

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from django_apscheduler.jobstores import DjangoJobStore

from maintenance.jobs import expire_lapsed_work_permits
from returnable_items.jobs import run_returnable_checks

logger = logging.getLogger(__name__)

DEFAULT_WORK_PERMIT_INTERVAL_MINUTES = 5
DEFAULT_RETURNABLE_INTERVAL_MINUTES = 60


class Command(BaseCommand):
    help = "Runs the APScheduler loop for work permit expiry and returnable item checks."

    def handle(self, *args, **options):
        scheduler = BlockingScheduler(timezone=settings.TIME_ZONE)
        scheduler.add_jobstore(DjangoJobStore(), "default")

        # The supply-chain app (and its live-trail digest job) was removed, but the
        # job survives in the persistent job store and would fail to import on
        # every trigger. Drop it if this store still carries one.
        try:
            scheduler.remove_job("supply_chain_live_trail_digest")
        except Exception:
            pass

        work_permit_interval = getattr(
            settings, "WORK_PERMIT_EXPIRY_INTERVAL_MINUTES", DEFAULT_WORK_PERMIT_INTERVAL_MINUTES
        )
        returnable_interval = getattr(
            settings, "RETURNABLE_CHECK_INTERVAL_MINUTES", DEFAULT_RETURNABLE_INTERVAL_MINUTES
        )

        scheduler.add_job(
            expire_lapsed_work_permits,
            trigger=IntervalTrigger(minutes=work_permit_interval),
            id="expire_work_permits",
            max_instances=1,
            replace_existing=True,
        )
        scheduler.add_job(
            run_returnable_checks,
            trigger=IntervalTrigger(minutes=returnable_interval),
            id="check_returnable_items",
            max_instances=1,
            replace_existing=True,
        )

        logger.info(
            "[Scheduler] Started — work_permit=%smin returnable=%smin",
            work_permit_interval,
            returnable_interval,
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Scheduler running: work permit expiry every {work_permit_interval} min, "
                f"returnable checks every {returnable_interval} min. "
                f"Press Ctrl+C to stop."
            )
        )

        try:
            scheduler.start()
        except KeyboardInterrupt:
            scheduler.shutdown()
            self.stdout.write(self.style.WARNING("Scheduler stopped."))
