import logging

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from driver_management.models import VehicleEntry
from security_checks.models import SecurityCheck
from weighment.models import Weighment

from .notifications import (
    notify_gate_entry_created,
    notify_security_check_done,
    notify_weighment_recorded,
)

logger = logging.getLogger(__name__)


@receiver(post_save, sender=VehicleEntry)
def notify_raw_material_gate_entry_created(sender, instance, created, **kwargs):
    if not created:
        return

    try:
        notify_gate_entry_created(instance)
    except Exception as exc:
        logger.error("Failed to queue raw material entry-created notification: %s", exc)


@receiver(pre_save, sender=SecurityCheck)
def capture_security_check_submission_state(sender, instance, **kwargs):
    if not instance.pk:
        instance._was_submitted_for_notification = False
        return

    instance._was_submitted_for_notification = (
        sender.objects.filter(pk=instance.pk)
        .values_list("is_submitted", flat=True)
        .first()
        or False
    )


@receiver(post_save, sender=SecurityCheck)
def notify_raw_material_security_check_done(sender, instance, **kwargs):
    was_submitted = getattr(instance, "_was_submitted_for_notification", False)
    if not instance.is_submitted or was_submitted:
        return

    try:
        notify_security_check_done(instance)
    except Exception as exc:
        logger.error("Failed to queue security-check notification: %s", exc)


@receiver(pre_save, sender=Weighment)
def capture_weighment_values(sender, instance, **kwargs):
    if not instance.pk:
        instance._previous_values_for_notification = None
        return

    instance._previous_values_for_notification = (
        sender.objects.filter(pk=instance.pk)
        .values_list(
            "gross_weight",
            "tare_weight",
            "weighbridge_slip_no",
            "first_weighment_time",
            "second_weighment_time",
        )
        .first()
    )


@receiver(post_save, sender=Weighment)
def notify_raw_material_weighment_recorded(sender, instance, created, **kwargs):
    current_values = (
        instance.gross_weight,
        instance.tare_weight,
        instance.weighbridge_slip_no,
        instance.first_weighment_time,
        instance.second_weighment_time,
    )
    previous_values = getattr(instance, "_previous_values_for_notification", None)

    if not created and previous_values == current_values:
        return

    try:
        notify_weighment_recorded(instance)
    except Exception as exc:
        logger.error("Failed to queue weighment notification: %s", exc)
