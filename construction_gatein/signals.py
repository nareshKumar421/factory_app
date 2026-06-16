import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import ConstructionGateEntry
from .notifications import notify_construction_entry_created

logger = logging.getLogger(__name__)


@receiver(post_save, sender=ConstructionGateEntry)
def notify_construction_gate_entry_created(sender, instance, created, **kwargs):
    if not created:
        return

    try:
        notify_construction_entry_created(instance)
    except Exception as exc:
        logger.error("Failed to queue construction entry-created notification: %s", exc)
