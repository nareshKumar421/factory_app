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
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from django_apscheduler.jobstores import DjangoJobStore

from maintenance.jobs import expire_lapsed_work_permits
from returnable_items.jobs import run_returnable_checks
from supply_chain.jobs import run_live_trail_digest

logger = logging.getLogger(__name__)

DEFAULT_WORK_PERMIT_INTERVAL_MINUTES = 5
DEFAULT_RETURNABLE_INTERVAL_MINUTES = 60
DEFAULT_LIVE_TRAIL_HOUR = 7
DEFAULT_LIVE_TRAIL_MINUTE = 0


class Command(BaseCommand):
    help = "Runs the APScheduler loop for work permit expiry and returnable item checks."

    def handle(self, *args, **options):
        scheduler = BlockingScheduler(timezone=settings.TIME_ZONE)
        scheduler.add_jobstore(DjangoJobStore(), "default")

        work_permit_interval = getattr(
            settings, "WORK_PERMIT_EXPIRY_INTERVAL_MINUTES", DEFAULT_WORK_PERMIT_INTERVAL_MINUTES
        )
        returnable_interval = getattr(
            settings, "RETURNABLE_CHECK_INTERVAL_MINUTES", DEFAULT_RETURNABLE_INTERVAL_MINUTES
        )
        live_trail_hour = int(
            getattr(settings, "LIVE_TRAIL_DIGEST_HOUR", DEFAULT_LIVE_TRAIL_HOUR)
        )
        live_trail_minute = int(
            getattr(settings, "LIVE_TRAIL_DIGEST_MINUTE", DEFAULT_LIVE_TRAIL_MINUTE)
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

        # Once a day, early, so the buy list and the run plan are waiting before
        # the shift starts rather than being discovered during it. Cron and not
        # an interval: this is a morning routine, and an interval would drift
        # across the working day every time the process restarted.
        scheduler.add_job(
            run_live_trail_digest,
            trigger=CronTrigger(hour=live_trail_hour, minute=live_trail_minute),
            id="supply_chain_live_trail_digest",
            max_instances=1,
            replace_existing=True,
            # A missed 07:00 (a deploy, a reboot) should still run when the
            # process comes back, as long as the morning is not already over.
            misfire_grace_time=3 * 60 * 60,
        )

        logger.info(
            "[Scheduler] Started — work_permit=%smin returnable=%smin live_trail=%02d:%02d",
            work_permit_interval,
            returnable_interval,
            live_trail_hour,
            live_trail_minute,
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Scheduler running: work permit expiry every {work_permit_interval} min, "
                f"returnable checks every {returnable_interval} min, "
                f"supply-chain live trail digest at {live_trail_hour:02d}:{live_trail_minute:02d}. "
                f"Press Ctrl+C to stop."
            )
        )

        try:
            scheduler.start()
        except KeyboardInterrupt:
            scheduler.shutdown()
            self.stdout.write(self.style.WARNING("Scheduler stopped."))
