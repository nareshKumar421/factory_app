import logging

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .models import GRPOPosting, GRPOStatus, ServiceGRPOPosting
from .notifications import notify_material_grpo_posted, notify_service_grpo_posted

logger = logging.getLogger(__name__)


@receiver(pre_save, sender=GRPOPosting)
@receiver(pre_save, sender=ServiceGRPOPosting)
def capture_grpo_status(sender, instance, **kwargs):
    if not instance.pk:
        instance._previous_status_for_notification = None
        return

    instance._previous_status_for_notification = (
        sender.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    )


@receiver(post_save, sender=GRPOPosting)
def notify_material_grpo_status(sender, instance, **kwargs):
    previous_status = getattr(instance, "_previous_status_for_notification", None)
    if previous_status == instance.status or instance.status != GRPOStatus.POSTED:
        return

    try:
        notify_material_grpo_posted(instance)
    except Exception as exc:
        logger.error("Failed to queue material GRPO notification: %s", exc)


@receiver(post_save, sender=ServiceGRPOPosting)
def notify_service_grpo_status(sender, instance, **kwargs):
    previous_status = getattr(instance, "_previous_status_for_notification", None)
    if previous_status == instance.status or instance.status != GRPOStatus.POSTED:
        return

    try:
        notify_service_grpo_posted(instance)
    except Exception as exc:
        logger.error("Failed to queue service GRPO notification: %s", exc)
