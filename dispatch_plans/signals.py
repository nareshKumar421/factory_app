import logging

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .models import DispatchPlan, DispatchPlanStatus
from .notifications import notify_dispatch_plan_status

logger = logging.getLogger(__name__)

_NOTIFIABLE_STATUSES = {DispatchPlanStatus.BOOKED, DispatchPlanStatus.DISPATCHED}


@receiver(pre_save, sender=DispatchPlan)
def capture_dispatch_plan_status(sender, instance, **kwargs):
    if not instance.pk:
        instance._previous_booking_status_for_notification = None
        return

    instance._previous_booking_status_for_notification = (
        sender.objects.filter(pk=instance.pk)
        .values_list("booking_status", flat=True)
        .first()
    )


@receiver(post_save, sender=DispatchPlan)
def notify_dispatch_plan(sender, instance, created, **kwargs):
    previous_status = getattr(instance, "_previous_booking_status_for_notification", None)
    if instance.booking_status == previous_status:
        return
    if instance.booking_status not in _NOTIFIABLE_STATUSES:
        return

    try:
        notify_dispatch_plan_status(instance)
    except Exception as exc:
        logger.error("Failed to queue dispatch plan notification: %s", exc)
