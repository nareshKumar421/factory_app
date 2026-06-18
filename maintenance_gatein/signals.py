import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import MaintenanceGateEntry
from .notifications import notify_maintenance_entry_created

logger = logging.getLogger(__name__)


@receiver(post_save, sender=MaintenanceGateEntry)
def notify_maintenance_gate_entry_created(sender, instance, created, **kwargs):
    if not created:
        return

    try:
        notify_maintenance_entry_created(instance)
    except Exception as exc:
        logger.error("Failed to queue maintenance entry-created notification: %s", exc)
