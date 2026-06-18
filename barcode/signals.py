import logging

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .models import IntercompanyTransfer, IntercompanyTransferStatus
from .notifications import (
    notify_intercompany_transfer_completed,
    notify_intercompany_transfer_failed,
)

logger = logging.getLogger(__name__)


@receiver(pre_save, sender=IntercompanyTransfer)
def capture_intercompany_transfer_status(sender, instance, **kwargs):
    if not instance.pk:
        instance._previous_status_for_notification = None
        return

    instance._previous_status_for_notification = (
        sender.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    )


@receiver(post_save, sender=IntercompanyTransfer)
def notify_intercompany_transfer(sender, instance, created, **kwargs):
    previous_status = getattr(instance, "_previous_status_for_notification", None)
    if instance.status == previous_status:
        return

    try:
        if instance.status == IntercompanyTransferStatus.COMPLETED:
            notify_intercompany_transfer_completed(instance)
        elif instance.status == IntercompanyTransferStatus.SAP_SYNC_FAILED:
            notify_intercompany_transfer_failed(instance)
    except Exception as exc:
        logger.error("Failed to queue intercompany transfer notification: %s", exc)
