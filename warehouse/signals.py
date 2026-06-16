import logging

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .models import BOMRequest, BOMRequestStatus, FGReceiptStatus, FinishedGoodsReceipt
from .notifications import (
    notify_bom_request_created,
    notify_bom_request_reviewed,
    notify_fg_receipt_result,
)

logger = logging.getLogger(__name__)

_REVIEWED_STATUSES = {
    BOMRequestStatus.APPROVED,
    BOMRequestStatus.PARTIALLY_APPROVED,
    BOMRequestStatus.REJECTED,
}


@receiver(pre_save, sender=BOMRequest)
def capture_bom_request_status(sender, instance, **kwargs):
    if not instance.pk:
        instance._previous_status_for_notification = None
        return

    instance._previous_status_for_notification = (
        sender.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    )


@receiver(post_save, sender=BOMRequest)
def notify_bom_request(sender, instance, created, **kwargs):
    previous_status = getattr(instance, "_previous_status_for_notification", None)
    try:
        if created:
            if instance.status == BOMRequestStatus.PENDING:
                notify_bom_request_created(instance)
            return

        if instance.status in _REVIEWED_STATUSES and previous_status != instance.status:
            notify_bom_request_reviewed(instance)
    except Exception as exc:
        logger.error("Failed to queue BOM request notification: %s", exc)


@receiver(pre_save, sender=FinishedGoodsReceipt)
def capture_fg_receipt_status(sender, instance, **kwargs):
    if not instance.pk:
        instance._previous_status_for_notification = None
        return

    instance._previous_status_for_notification = (
        sender.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    )


@receiver(post_save, sender=FinishedGoodsReceipt)
def notify_fg_receipt(sender, instance, **kwargs):
    previous_status = getattr(instance, "_previous_status_for_notification", None)
    if instance.status == previous_status:
        return
    if instance.status not in (FGReceiptStatus.SAP_POSTED, FGReceiptStatus.FAILED):
        return

    try:
        notify_fg_receipt_result(instance)
    except Exception as exc:
        logger.error("Failed to queue FG receipt notification: %s", exc)
