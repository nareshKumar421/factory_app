from decimal import Decimal, InvalidOperation
from typing import Dict, List

from django.conf import settings

from gate_core.models import (
    SalesDispatchAttachmentType,
    SalesDispatchGateOut,
    SalesDispatchGateOutStatus,
)

EWAY_BILL_AMOUNT_THRESHOLD = Decimal("50000")


def is_box_scan_optional(entry: SalesDispatchGateOut) -> bool:
    """Box scanning is optional for companies in DOCKING_BOX_SCAN_OPTIONAL_COMPANY_CODES.

    These companies (e.g. Jivo Beverages) don't scan boxes at the factory, so a load
    can proceed to gatepass without any box scan and without an admin scan-skip
    approval. Driven by settings so the set is configurable per environment.
    """
    optional_codes = getattr(settings, "DOCKING_BOX_SCAN_OPTIONAL_COMPANY_CODES", []) or []
    company_code = (getattr(entry.company, "code", "") or "").upper()
    return bool(company_code) and company_code in {str(code).upper() for code in optional_codes}


def get_gatepass_readiness(entry: SalesDispatchGateOut) -> Dict:
    missing: List[str] = []

    photo = (
        entry.attachments
        .filter(attachment_type=SalesDispatchAttachmentType.TRUCK_PHOTO)
        .order_by("-uploaded_at")
        .first()
    )
    has_model_photo = bool(entry.truck_photo and entry.photo_latitude is not None and entry.photo_longitude is not None)
    has_attachment_photo = bool(photo and photo.has_geolocation)

    if not (has_model_photo or has_attachment_photo):
        missing.append("truck_photo_geolocation")

    has_box_scans = entry.box_scans.filter(is_active=True).exists()
    # An admin-approved scan-skip request (docking_admin app) satisfies the box-scan
    # requirement so a non-scannable load can still proceed to gatepass. Queried via the
    # reverse relation to avoid importing docking_admin here (would be a circular import).
    scan_skip_approved = entry.scan_skip_requests.filter(status="APPROVED").exists()
    # Companies that don't scan at the factory (e.g. Jivo Beverages) have box scanning
    # turned off entirely — no scan and no approval needed.
    box_scan_optional = is_box_scan_optional(entry)
    if not (has_box_scans or scan_skip_approved or box_scan_optional):
        missing.append("box_scans")

    if not entry.items.exists():
        missing.append("document_items")

    if not (entry.bilty_no or "").strip():
        missing.append("bilty_no")

    if not entry.bilty_date:
        missing.append("bilty_date")

    has_bilty_attachment = entry.attachments.filter(
        attachment_type=SalesDispatchAttachmentType.BILTY,
    ).exists()
    if not has_bilty_attachment:
        missing.append("bilty_attachment")

    eway_required = requires_eway_bill(entry)
    if eway_required:
        if not (entry.eway_bill or "").strip():
            missing.append("eway_bill")
        has_eway_attachment = entry.attachments.filter(
            attachment_type=SalesDispatchAttachmentType.EWAY_BILL,
        ).exists()
        if not has_eway_attachment:
            missing.append("eway_bill_attachment")

    weighment = getattr(entry.vehicle_entry, "weighment", None)
    has_weighment = bool(
        weighment
        and weighment.gross_weight is not None
        and weighment.gross_weight > 0
        and weighment.tare_weight is not None
        and weighment.tare_weight >= 0
        and weighment.tare_weight <= weighment.gross_weight
    )

    return {
        "ready": not missing,
        "missing": missing,
        "has_truck_photo_geolocation": "truck_photo_geolocation" not in missing,
        "has_box_scans": "box_scans" not in missing,
        "scan_skip_approved": scan_skip_approved,
        "box_scan_optional": box_scan_optional,
        "has_weighment": has_weighment,
        "has_items": "document_items" not in missing,
        "has_bilty_details": "bilty_no" not in missing and "bilty_date" not in missing,
        "has_bilty_attachment": "bilty_attachment" not in missing,
        "requires_eway_bill": eway_required,
        "has_eway_bill": "eway_bill" not in missing,
        "has_eway_bill_attachment": "eway_bill_attachment" not in missing,
    }


def requires_eway_bill(entry: SalesDispatchGateOut) -> bool:
    documents = list(entry.documents.all())
    if not documents:
        return (
            entry.document_type == "INVOICE"
            and decimal_value(entry.sap_doc_total) > EWAY_BILL_AMOUNT_THRESHOLD
        )
    return any(
        document.document_type == "INVOICE"
        and decimal_value(document.sap_doc_total) > EWAY_BILL_AMOUNT_THRESHOLD
        for document in documents
    )


def decimal_value(value) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def ensure_gatepass_ready(entry: SalesDispatchGateOut) -> Dict:
    readiness = get_gatepass_readiness(entry)
    if not readiness["ready"]:
        missing = ", ".join(readiness["missing"])
        raise ValueError(f"Docking entry is not ready for gatepass: {missing}.")
    return readiness


def can_edit(entry: SalesDispatchGateOut) -> bool:
    return entry.status in (
        SalesDispatchGateOutStatus.DOCKED,
        SalesDispatchGateOutStatus.PHOTO_ATTACHED,
        SalesDispatchGateOutStatus.READY_FOR_GATEPASS,
    )
