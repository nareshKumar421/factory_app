import logging

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .models import EntryLog
from .notifications import notify_person_entry_created, notify_person_entry_exited

logger = logging.getLogger(__name__)


@receiver(pre_save, sender=EntryLog)
def capture_entry_log_status(sender, instance, **kwargs):
    if not instance.pk:
        instance._previous_status_for_notification = None
        return

    instance._previous_status_for_notification = (
        sender.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    )


@receiver(post_save, sender=EntryLog)
def notify_person_gate_entry(sender, instance, created, **kwargs):
    previous_status = getattr(instance, "_previous_status_for_notification", None)

    try:
        if created:
            if instance.status == "IN":
                notify_person_entry_created(instance)
            return

        if instance.status == "OUT" and previous_status != "OUT":
            notify_person_entry_exited(instance)
    except Exception as exc:
        logger.error("Failed to queue person gate notification: %s", exc)
