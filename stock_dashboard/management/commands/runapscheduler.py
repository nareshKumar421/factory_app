"""
stock_dashboard/management/commands/runapscheduler.py

Starts the APScheduler background scheduler that periodically
checks stock levels and sends notifications for low/critical items.

Usage:
    python manage.py runapscheduler

Configuration (via .env or settings.py):
    STOCK_ALERT_INTERVAL_MINUTES  — How often to check (default: 10)
    STOCK_ALERT_COOLDOWN_MINUTES  — Min time between re-alerting same item (default: 60)
"""

import logging

from django.conf import settings
from django.core.management.base import BaseCommand

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from django_apscheduler.jobstores import DjangoJobStore

from stock_dashboard.jobs import send_stock_alerts

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_MINUTES = 10


class Command(BaseCommand):
    help = "Runs the APScheduler for stock alert notifications."

    def handle(self, *args, **options):
        scheduler = BlockingScheduler(timezone=settings.TIME_ZONE)
        scheduler.add_jobstore(DjangoJobStore(), "default")

        interval_minutes = getattr(
            settings, "STOCK_ALERT_INTERVAL_MINUTES", DEFAULT_INTERVAL_MINUTES
        )

        scheduler.add_job(
            send_stock_alerts,
            trigger=IntervalTrigger(minutes=interval_minutes),
            id="send_stock_alerts",
            max_instances=1,
            replace_existing=True,
        )
        logger.info(
            f"[StockAlert] Scheduler started — "
            f"interval={interval_minutes}min, "
            f"cooldown={getattr(settings, 'STOCK_ALERT_COOLDOWN_MINUTES', 60)}min"
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Stock alert scheduler running (every {interval_minutes} min). Press Ctrl+C to stop."
            )
        )

        try:
            scheduler.start()
        except KeyboardInterrupt:
            scheduler.shutdown()
            self.stdout.write(self.style.WARNING("Scheduler stopped."))
