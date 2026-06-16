import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import DailyNeedGateEntry
from .notifications import notify_daily_need_entry_created

logger = logging.getLogger(__name__)


@receiver(post_save, sender=DailyNeedGateEntry)
def notify_daily_need_gate_entry_created(sender, instance, created, **kwargs):
    if not created:
        return

    try:
        notify_daily_need_entry_created(instance)
    except Exception as exc:
        logger.error("Failed to queue daily-need entry-created notification: %s", exc)
