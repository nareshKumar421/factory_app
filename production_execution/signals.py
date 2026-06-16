import logging

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .models import ProductionRun
from .notifications import notify_production_run_sap_result

logger = logging.getLogger(__name__)

_TERMINAL_SYNC_STATUSES = {"SUCCESS", "FAILED"}


@receiver(pre_save, sender=ProductionRun)
def capture_production_run_sync_status(sender, instance, **kwargs):
    if not instance.pk:
        instance._previous_sync_status_for_notification = None
        return

    instance._previous_sync_status_for_notification = (
        sender.objects.filter(pk=instance.pk)
        .values_list("sap_sync_status", flat=True)
        .first()
    )


@receiver(post_save, sender=ProductionRun)
def notify_production_run_sap_sync(sender, instance, **kwargs):
    previous_status = getattr(instance, "_previous_sync_status_for_notification", None)
    if instance.sap_sync_status == previous_status:
        return
    if instance.sap_sync_status not in _TERMINAL_SYNC_STATUSES:
        return

    try:
        notify_production_run_sap_result(instance)
    except Exception as exc:
        logger.error("Failed to queue production run SAP notification: %s", exc)
