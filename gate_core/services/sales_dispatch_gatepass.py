import math
import re
from decimal import Decimal, InvalidOperation
from typing import Dict, List

from django.conf import settings

from gate_core.models import (
    SalesDispatchAttachmentType,
    SalesDispatchGateOut,
    SalesDispatchGateOutStatus,
)

EWAY_BILL_AMOUNT_THRESHOLD = Decimal("50000")

# Pack size embedded in an item name, e.g. "... 12 PCS" -> 12. Mirrors the
# frontend regex in salesDispatchBoxCounts.ts so box totals match the scan page.
_PACK_SIZE_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:PCS?|SETS?|TINS?|BOTTLES?)\b", re.IGNORECASE
)


def is_box_scan_optional(entry: SalesDispatchGateOut) -> bool:
    """Box scanning is optional for companies in DOCKING_BOX_SCAN_OPTIONAL_COMPANY_CODES.

    These companies (e.g. Jivo Beverages) don't scan boxes at the factory, so a load
    can proceed to gatepass without any box scan and without an admin scan-skip
    approval. Driven by settings so the set is configurable per environment.
    """
    optional_codes = getattr(settings, "DOCKING_BOX_SCAN_OPTIONAL_COMPANY_CODES", []) or []
    company_code = (getattr(entry.company, "code", "") or "").upper()
    return bool(company_code) and company_code in {str(code).upper() for code in optional_codes}


def scanned_box_count(entry: SalesDispatchGateOut) -> int:
    """Active box scans recorded on the docking.

    Reads ``.all()`` + a Python guard rather than ``.filter(...).count()`` so a
    prefetched ``box_scans`` relation (see ``_sales_dispatch_base_queryset``) is served
    from cache instead of triggering a query per row in the docking list.
    """
    return sum(1 for scan in entry.box_scans.all() if scan.is_active)


def expected_box_count(entry: SalesDispatchGateOut) -> int:
    """Best-known expected box count for a docking: the SAP entry total, else the
    documents' totals, else the line items' totals (mirrors the frontend)."""
    total = decimal_value(entry.total_boxes)
    if total > 0:
        return int(total)
    doc_total = sum((decimal_value(d.total_boxes) for d in entry.documents.all()), Decimal("0"))
    if doc_total > 0:
        return int(doc_total)
    item_total = sum((decimal_value(i.total_boxes) for i in entry.items.all()), Decimal("0"))
    return int(item_total)


def _parse_pack_size(item_name: str) -> Decimal:
    """Last pack size mentioned in an item name (e.g. "OIL 1L 12 PCS" -> 12)."""
    matches = _PACK_SIZE_PATTERN.findall(item_name or "")
    return decimal_value(matches[-1]) if matches else Decimal("0")


def _expected_item_boxes(item) -> int:
    """Expected boxes for one line: its stored total, else quantity / pack size."""
    item_total = decimal_value(item.total_boxes)
    if item_total > 0:
        return int(item_total)
    quantity = decimal_value(item.quantity)
    pack_size = _parse_pack_size(item.item_name)
    if quantity <= 0 or pack_size <= 0:
        return 0
    return int(math.ceil(quantity / pack_size))


def _expected_document_boxes(document) -> int:
    doc_total = decimal_value(document.total_boxes)
    if doc_total > 0:
        return int(doc_total)
    return sum(_expected_item_boxes(i) for i in document.items.all())


def resolved_expected_box_count(entry: SalesDispatchGateOut) -> int:
    """Expected box count that matches the docking scan page exactly.

    Unlike ``expected_box_count`` (kept as-is for gatepass gating), this adds the
    per-item quantity / pack-size fallback the frontend uses, so a total is known
    even when no ``total_boxes`` is stored at entry/document/item level. Use this
    for display (e.g. the partial-dispatch approvals queue).
    """
    total = decimal_value(entry.total_boxes)
    if total > 0:
        return int(total)

    doc_total = sum(_expected_document_boxes(d) for d in entry.documents.all())
    if doc_total > 0:
        return doc_total

    items = list(entry.items.all())
    if not items:
        items = [i for d in entry.documents.all() for i in d.items.all()]
    return sum(_expected_item_boxes(i) for i in items)


def get_gatepass_readiness(entry: SalesDispatchGateOut) -> Dict:
    missing: List[str] = []

    # Iterate the prefetched ``attachments`` (``.filter()`` would re-query per row).
    attachments = list(entry.attachments.all())
    truck_photos = [
        a for a in attachments if a.attachment_type == SalesDispatchAttachmentType.TRUCK_PHOTO
    ]
    photo = max(truck_photos, key=lambda a: a.uploaded_at, default=None)
    has_model_photo = bool(entry.truck_photo and entry.photo_latitude is not None and entry.photo_longitude is not None)
    has_attachment_photo = bool(photo and photo.has_geolocation)

    if not (has_model_photo or has_attachment_photo):
        missing.append("truck_photo_geolocation")

    scanned_boxes = scanned_box_count(entry)
    expected_boxes = expected_box_count(entry)
    has_box_scans = scanned_boxes > 0
    # Admin-approved requests (docking_admin app) let a load proceed: a scan-skip
    # request covers the zero-scan case, a partial-scan request the some-but-not-all
    # case. Queried via the reverse relations to avoid importing docking_admin here
    # (would be a circular import).
    scan_skip_approved = any(r.status == "APPROVED" for r in entry.scan_skip_requests.all())
    partial_scan_approved = any(
        r.status == "APPROVED" for r in entry.partial_scan_requests.all()
    )
    # Companies that don't scan at the factory (e.g. Jivo Beverages) have box scanning
    # turned off entirely — no scan and no approval needed.
    box_scan_optional = is_box_scan_optional(entry)
    # A partial scan = at least one box but fewer than expected (only knowable when
    # the expected count is). A partial scan now needs an approval, just like a skip.
    is_partial_scan = has_box_scans and expected_boxes > 0 and scanned_boxes < expected_boxes

    if box_scan_optional:
        box_scans_ok = True
    elif not has_box_scans:
        box_scans_ok = scan_skip_approved
    elif is_partial_scan:
        box_scans_ok = partial_scan_approved
    else:  # fully scanned, or expected count unknown
        box_scans_ok = True
    if not box_scans_ok:
        missing.append("box_scans")

    if not entry.items.all():
        missing.append("document_items")

    if not (entry.bilty_no or "").strip():
        missing.append("bilty_no")

    if not entry.bilty_date:
        missing.append("bilty_date")

    has_bilty_attachment = any(
        a.attachment_type == SalesDispatchAttachmentType.BILTY for a in attachments
    )
    if not has_bilty_attachment:
        missing.append("bilty_attachment")

    eway_required = requires_eway_bill(entry)
    if eway_required:
        if not (entry.eway_bill or "").strip():
            missing.append("eway_bill")
        has_eway_attachment = any(
            a.attachment_type == SalesDispatchAttachmentType.EWAY_BILL for a in attachments
        )
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
        "partial_scan_approved": partial_scan_approved,
        "is_partial_scan": is_partial_scan,
        "scanned_boxes": scanned_boxes,
        "expected_boxes": expected_boxes,
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
