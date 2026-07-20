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
    """Raw stored-total box count for a docking: the SAP entry total, else the
    documents' totals, else the line items' stored totals. Does NOT fall back to the
    per-item pack-size estimate, so it reports 0 when no total is stored. Gating and
    display use ``resolved_expected_box_count`` instead; kept for callers that only
    want the stored figure."""
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
    """Expected boxes for one line: its stored total, else quantity / pack size, else
    (a weight/loose line with no PCS-style pack size, e.g. "SOYABEAN OIL 12 KGS") one box
    per invoiced unit.

    Returning 0 for that last case made weight-priced lines invisible to the box total:
    a load's scanned boxes then over-counted against a too-small expected, so a genuinely
    short load read as "fully scanned" and slipped past the completeness gate. One box per
    unit is the count operators actually scan for these lines (1 scan per tin/unit)."""
    item_total = decimal_value(item.total_boxes)
    if item_total > 0:
        return int(item_total)
    quantity = decimal_value(item.quantity)
    if quantity <= 0:
        return 0
    pack_size = _parse_pack_size(item.item_name)
    if pack_size <= 0:
        return int(math.ceil(quantity))
    return int(math.ceil(quantity / pack_size))


def _expected_document_boxes(document) -> int:
    doc_total = decimal_value(document.total_boxes)
    if doc_total > 0:
        return int(doc_total)
    return sum(_expected_item_boxes(i) for i in document.items.all())


def resolved_expected_box_count(entry: SalesDispatchGateOut) -> int:
    """Expected box count that matches the docking scan page exactly.

    Unlike ``expected_box_count`` (raw stored totals only), this adds the per-item
    quantity / pack-size fallback the frontend uses, so a total is known even when no
    ``total_boxes`` is stored at entry/document/item level. This is the count used for
    both gatepass gating (``get_gatepass_readiness``, the partial-scan request checks)
    and display (the partial-dispatch approvals queue), so the operator's view, the
    scan-completeness lock, and the approval flow all agree.
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


def _norm_code(value) -> str:
    return str(value or "").strip().upper()


def usable_quantity_scans(entry: SalesDispatchGateOut) -> List:
    """Active box scans that carry BOTH a bill attribution and a scanned quantity.

    These are the scans the exact per-``(bill, item)`` quantity check can trust. Every
    barcode-scanned box has both — ``quantity`` is copied from the box's own known piece
    count at scan time — so any normally-scanned load qualifies; only legacy or
    quantity-less scans fall out. Reads prefetched ``box_scans`` — no per-row query."""
    return [
        s
        for s in entry.box_scans.all()
        if getattr(s, "is_active", True) and s.document_id and s.quantity is not None
    ]


def has_trustworthy_scan_quantities(entry: SalesDispatchGateOut) -> bool:
    """True when at least one scan carries a ``(bill, quantity)`` pair, so completeness
    can be judged on exact scanned-vs-invoiced QUANTITY rather than the box-count estimate
    (which needs a pack size SAP frequently does not store)."""
    return bool(usable_quantity_scans(entry))


def has_unscanned_bill_lines(entry: SalesDispatchGateOut) -> bool:
    """True if any bill line on the load still has invoiced quantity not yet scanned.

    The load-wide box-count check nets the whole truck together, so a surplus on one bill
    or line — an over-scan, or a weight line the box estimate can't see — can hide a real
    shortfall on another (this is how a load with two unscanned items still read as 100%).
    This compares scanned vs invoiced QUANTITY per ``(bill, item)`` and never lets a
    surplus offset a deficit, so a partial load is caught even when the aggregate box total
    looks complete.

    Conservative by design: only trusts scans attributed to a bill and carrying a scanned
    quantity (the per-bill scanning path). A load whose scans lack that data falls back to
    the count-only signal (returns False here), so we never invent a shortfall from absent
    quantity data. Reads only prefetched ``box_scans`` / ``items`` — no per-row queries."""
    usable = usable_quantity_scans(entry)
    if not usable:
        return False
    invoiced: Dict = {}
    for item in entry.items.all():
        if not item.document_id:
            continue
        qty = decimal_value(item.quantity)
        if qty > 0:
            key = (item.document_id, _norm_code(item.item_code))
            invoiced[key] = invoiced.get(key, Decimal("0")) + qty
    if not invoiced:
        return False
    scanned: Dict = {}
    for scan in usable:
        key = (scan.document_id, _norm_code(scan.item_code))
        scanned[key] = scanned.get(key, Decimal("0")) + decimal_value(scan.quantity)
    return any(scanned.get(key, Decimal("0")) < qty for key, qty in invoiced.items())


def load_scan_status(entry: SalesDispatchGateOut):
    """``(scanned_boxes, expected_boxes, has_scans, is_partial)`` for a docking.

    ``is_partial`` is True when the load has scans but still carries unscanned invoiced
    goods. Completeness is judged on exact QUANTITY — scanned vs invoiced per ``(bill,
    item)`` (:func:`has_unscanned_bill_lines`) — whenever the scans carry a quantity, which
    every barcode box does. Quantity is ground truth on both sides (the invoice line qty
    from SAP; the box's own piece count from our barcode system) and needs no pack size, so
    an item whose box size we cannot derive — SAP stores none and the name may lack an
    "N PCS" token — can no longer manufacture a phantom box-count shortfall that locks a
    fully-loaded truck. The box-COUNT estimate is used only as a fallback for legacy or
    quantity-less scans, where per-line quantity isn't available.

    Shared by the gatepass readiness gate and the partial-scan-approval endpoint so the two
    can never disagree, which would deadlock the operator (the gate demands an approval the
    endpoint refuses to create)."""
    scanned_boxes = scanned_box_count(entry)
    expected_boxes = resolved_expected_box_count(entry)
    has_scans = scanned_boxes > 0
    if has_trustworthy_scan_quantities(entry):
        # Exact path: a box-granularity over-scan (a 20-pc box covering an 18-pc line)
        # reads complete, as it should; a genuine per-line shortfall still flags partial.
        is_partial = has_scans and has_unscanned_bill_lines(entry)
    else:
        # Fallback: no scan carries a quantity, so fall back to the box-count estimate.
        aggregate_short = expected_boxes > 0 and scanned_boxes < expected_boxes
        is_partial = has_scans and aggregate_short
    return scanned_boxes, expected_boxes, has_scans, is_partial


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

    # Completeness is judged per bill/line, not only on the load-wide box total: a surplus
    # on one bill (an over-scan, or a weight line the box estimate misses) must not net
    # against a shortfall on another. ``load_scan_status`` combines the aggregate box count
    # with the per-(bill, item) invoiced-quantity check (same rule the partial-scan-approval
    # endpoint uses, so the two can't deadlock).
    scanned_boxes, expected_boxes, has_box_scans, is_partial_scan = load_scan_status(entry)
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
