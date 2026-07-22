"""Whole-truck (arrival-level) truck-photo + box-scan orchestration.

A physical truck (``VehicleArrival``) carries one ``SalesDispatchGateOut`` docking
per company. Scanning and the truck photo are per-docking in the data model; these
helpers fan a single operator action out across every company's docking so the
truck is scanned + photographed once, as one entity -- the arrival-aware loading
flow. They wrap the existing per-docking logic so behaviour stays identical.
"""
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from gate_core.models import (
    SalesDispatchAttachment,
    SalesDispatchAttachmentType,
    SalesDispatchGateOutStatus as Status,
)

# A docking can still take (or replace) its truck photo only before the gatepass is
# printed. Beyond that the load is committed and must not be re-photographed.
_PHOTO_EDITABLE_STATUSES = (
    Status.DOCKED,
    Status.PHOTO_ATTACHED,
    Status.READY_FOR_GATEPASS,
)


class PartialLoadLockError(Exception):
    """A docking still has booked bills not loaded on it and the caller did not
    pass ``allow_partial``. Carries ``undocked`` -> [{sap_doc_entry, sap_doc_num}]
    merged across the truck's dockings, so the caller can prompt the same override
    the per-docking photo upload uses."""

    def __init__(self, undocked):
        self.undocked = undocked
        super().__init__("Truck has booked bills not yet loaded.")


def apply_truck_photo_to_docking(entry, attachment, *, latitude, longitude, user):
    """Mirror a just-created truck-photo attachment onto its docking + lock the load.

    The single source of truth for the ``TRUCK_PHOTO`` side effects (mirror the
    photo + geo onto the docking, move ``DOCKED -> PHOTO_ATTACHED``), shared by the
    per-docking upload view and the arrival fan-out so they can never diverge.
    """
    entry.truck_photo = attachment.file
    entry.photo_latitude = latitude
    entry.photo_longitude = longitude
    entry.photo_uploaded_by = user
    entry.photo_uploaded_at = timezone.now()
    if entry.status == Status.DOCKED:
        entry.status = Status.PHOTO_ATTACHED
    entry.updated_by = user
    entry.save(
        update_fields=[
            "truck_photo",
            "photo_latitude",
            "photo_longitude",
            "photo_uploaded_by",
            "photo_uploaded_at",
            "status",
            "updated_by",
            "updated_at",
        ]
    )


def attach_truck_photo_to_docking(entry, *, file, latitude, longitude, notes, user):
    """Create + attach one truck photo to a single docking and lock its load.

    Create the attachment, sync bilty side effects, then apply the shared
    ``TRUCK_PHOTO`` mirror/transition. The caller owns the load-lock and status
    guards. Returns the created ``SalesDispatchAttachment``.
    """
    # Imported lazily: the view module imports services, so a top-level import here
    # would be circular.
    from gate_core.views_sales_dispatch import (
        sync_sales_dispatch_bilty_attachment_to_plans,
    )

    attachment = SalesDispatchAttachment.objects.create(
        sales_dispatch=entry,
        attachment_type=SalesDispatchAttachmentType.TRUCK_PHOTO,
        file=file,
        original_filename=getattr(file, "name", ""),
        latitude=latitude,
        longitude=longitude,
        notes=notes or "",
        uploaded_by=user,
    )
    sync_sales_dispatch_bilty_attachment_to_plans(entry, attachment, user)
    apply_truck_photo_to_docking(entry, attachment, latitude=latitude, longitude=longitude, user=user)
    return attachment


def attach_truck_photo_to_arrival(
    arrival, *, file, latitude, longitude, notes, user, allow_partial=False
):
    """One truck photo -> every open docking on the arrival, atomically.

    Runs the same one-docking-per-truck load-lock as the per-docking upload (per
    docking, merged) unless ``allow_partial``. The uploaded file is saved once per
    docking -- each keeps its own attachment, since every downstream gate reads the
    photo off the individual docking. Raises ``PartialLoadLockError`` (booked bills
    still unloaded) or ``ValueError`` (nothing to photograph). Returns the list of
    created attachments.
    """
    from gate_core.services import sales_dispatch_docking as docking_builder
    from gate_core.services.arrival_gatepass import arrival_dockings

    dockings = [d for d in arrival_dockings(arrival) if d.status in _PHOTO_EDITABLE_STATUSES]
    if not dockings:
        raise ValueError("No open dockings on this arrival to photograph.")

    if not allow_partial:
        # Merge each docking's still-un-loaded booked bills; any one blocks the lock.
        undocked = {}
        for docking in dockings:
            for bill in docking_builder.undocked_booked_bills(docking):
                undocked[bill["sap_doc_entry"]] = bill
        if undocked:
            raise PartialLoadLockError(sorted(undocked.values(), key=lambda b: b["sap_doc_entry"]))

    # Read the upload once; hand each docking an independent copy (a single
    # UploadedFile can't be saved to N FileFields).
    file.seek(0)
    content = file.read()
    filename = getattr(file, "name", "truck.jpg")

    attachments = []
    with transaction.atomic():
        for docking in dockings:
            attachments.append(
                attach_truck_photo_to_docking(
                    docking,
                    file=ContentFile(content, name=filename),
                    latitude=latitude,
                    longitude=longitude,
                    notes=notes,
                    user=user,
                )
            )
    return attachments
