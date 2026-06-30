import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from django.conf import settings
from django.core.management.base import BaseCommand
from django_apscheduler.jobstores import DjangoJobStore

from labour_count.jobs import auto_submit_labour_sheets

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Runs the labour-count auto-submit scheduler (locks unsubmitted sheets at the shift lock times)."

    def handle(self, *args, **options):
        scheduler = BlockingScheduler(timezone=settings.TIME_ZONE)
        scheduler.add_jobstore(DjangoJobStore(), "default")

        hours = getattr(settings, "LABOUR_AUTO_SUBMIT_HOURS", "6,18")
        minute = getattr(settings, "LABOUR_AUTO_SUBMIT_MINUTE", 30)

        scheduler.add_job(
            auto_submit_labour_sheets,
            trigger=CronTrigger(hour=hours, minute=minute),
            id="labour_auto_submit",
            max_instances=1,
            replace_existing=True,
        )

        logger.info("Labour-count scheduler started: hours=%s minute=%s", hours, minute)
        self.stdout.write(
            self.style.SUCCESS(
                f"Labour-count scheduler running (daily at {hours} :{minute:02d}). Press Ctrl+C to stop."
            )
        )

        try:
            scheduler.start()
        except KeyboardInterrupt:
            scheduler.shutdown()
            self.stdout.write(self.style.WARNING("Scheduler stopped."))
