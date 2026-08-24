from decimal import Decimal, InvalidOperation
from typing import Dict, List

from django.conf import settings

from gate_core.models import (
    SalesDispatchAttachmentType,
    SalesDispatchGateOut,
    SalesDispatchGateOutStatus,
)
from gate_core.services.box_packing import (
    LinePacking,
    box_invoice_units,
    is_full_box,
    pieces_per_box,
    split_line,
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
    doc_total = sum((decimal_value(d.total_boxes) for d in entry.active_documents), Decimal("0"))
    if doc_total > 0:
        return int(doc_total)
    item_total = sum((decimal_value(i.total_boxes) for i in entry.active_items), Decimal("0"))
    return int(item_total)


def item_packing(item) -> LinePacking:
    """Box/loose split for one line, the way SAP's own bill prints it.

    Derived from ``OITM.SalFactor2`` (snapshotted on the row at ingest), never from the
    item name's "N PCS" token -- names lie. ``SalFactor2 = 1`` means the item is not
    transacted in boxes and ships loose, so a 500-piece line of a 10ML bottle is
    ``0 boxes + 500 loose`` exactly as the invoice prints it, instead of the 500 boxes
    the old name parse produced against the 3 cartons that physically exist. CSD stock is
    the exception (see :mod:`gate_core.services.box_packing`): it also carries
    SalFactor2 = 1 but one box IS the billed piece, so it stays box-counted.

    Stored ``total_boxes``/``total_loose`` win when present -- they were computed by this
    same rule in SQL when the bill was read from SAP -- so the split is only recomputed
    for rows written before those columns existed.
    """
    stored_boxes = decimal_value(item.total_boxes)
    stored_loose = decimal_value(item.total_loose)
    factor = decimal_value(getattr(item, "sal_factor2", None))
    if stored_boxes > 0 or stored_loose > 0:
        # Same divisor the stored split was computed with, so ``is_loose`` stays
        # consistent with it: an item with a real box size is never "loose" just because
        # this line happened to be all remainder.
        return LinePacking(
            int(stored_boxes), stored_loose, pieces_per_box(factor, item.item_name)
        )
    return split_line(item.quantity, factor, item.item_name)


def _expected_item_boxes(item) -> int:
    """Full boxes to scan on one line -- 0 for a line that ships loose.

    A loose line is not "no goods": its pieces are counted by
    :func:`_expected_item_loose`, and its completeness is judged on invoiced QUANTITY
    (:func:`has_unscanned_bill_lines`) -- which is what the operator scans against when
    SAP gives no box size to divide by.
    """
    return item_packing(item).boxes


def _expected_item_loose(item) -> Decimal:
    """Pieces on one line that are not in a full box (the whole line when loose)."""
    return item_packing(item).loose


def _expected_document_boxes(document) -> int:
    doc_total = decimal_value(document.total_boxes)
    if doc_total > 0:
        return int(doc_total)
    return sum(_expected_item_boxes(i) for i in document.active_items)


def _expected_document_loose(document) -> Decimal:
    doc_total = decimal_value(document.total_loose)
    if doc_total > 0:
        return doc_total
    return sum((_expected_item_loose(i) for i in document.active_items), Decimal("0"))


def resolved_expected_box_count(entry: SalesDispatchGateOut) -> int:
    """Expected box count that matches the docking scan page exactly.

    Unlike ``expected_box_count`` (raw stored totals only), this recomputes the SAP
    box/loose split per item when no ``total_boxes`` is stored at entry/document/item
    level, so a total is known for rows written before the split existed. This is the
    count used for both gatepass gating (``get_gatepass_readiness``, the partial-scan
    request checks) and display (the partial-dispatch approvals queue), so the
    operator's view, the scan-completeness lock, and the approval flow all agree.

    Counts BOXES only. Loose pieces -- a whole line of them for an item SAP does not
    transact in boxes -- are reported by :func:`resolved_expected_loose_count` and gated
    on quantity instead; see :func:`load_scan_status`.
    """
    total = decimal_value(entry.total_boxes)
    if total > 0:
        return int(total)

    doc_total = sum(_expected_document_boxes(d) for d in entry.active_documents)
    if doc_total > 0:
        return doc_total

    items = list(entry.active_items)
    if not items:
        items = [i for d in entry.active_documents for i in d.active_items]
    return sum(_expected_item_boxes(i) for i in items)


def resolved_expected_loose_count(entry: SalesDispatchGateOut) -> Decimal:
    """Loose pieces on the load -- the goods the box count deliberately does not cover.

    Mirrors :func:`resolved_expected_box_count` (stored total first, then a per-item
    recompute) for the other half of the printed "Box + Loose" pair, so a screen can show
    an all-loose bill as "500 pcs" rather than a bare "0 boxes" that reads as empty.
    """
    total = decimal_value(entry.total_loose)
    if total > 0:
        return total

    doc_total = sum((_expected_document_loose(d) for d in entry.active_documents), Decimal("0"))
    if doc_total > 0:
        return doc_total

    items = list(entry.active_items)
    if not items:
        items = [i for d in entry.active_documents for i in d.active_items]
    return sum((_expected_item_loose(i) for i in items), Decimal("0"))


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


def _invoice_line_index(entry: SalesDispatchGateOut) -> Dict:
    """The invoice line each ``(bill, item_code)`` on the load is written on.

    One line per key is enough: lines of the same item on one bill share the pack size
    every box/loose conversion reads. Built from the prefetched items -- no query."""
    index: Dict = {}
    for item in entry.active_items:
        if item.document_id:
            index.setdefault((item.document_id, _norm_code(item.item_code)), item)
    return index


def scanned_box_split(entry: SalesDispatchGateOut):
    """``(full_boxes, loose_pieces)`` for the load's active scans.

    A scan carrying less than its item's pack size is a PART box: it covers the bill's
    printed loose remainder, not one of its boxes, so it is reported as pieces instead of
    inflating the box count. Without this split, 115 full boxes plus one 4-piece box read
    as "116 / 116 boxes" on a line invoicing 116 boxes + 4 loose -- the count looked
    complete while 16 pieces were still on the floor. A scan whose bill line we can't
    find (unattributed or off-list) counts as a full box: there is no pack size to
    call it short against."""
    lines = _invoice_line_index(entry)
    full_boxes = 0
    loose_pieces = Decimal("0")
    for scan in entry.box_scans.all():
        if not getattr(scan, "is_active", True):
            continue
        line = lines.get((scan.document_id, _norm_code(scan.item_code)))
        quantity = decimal_value(scan.quantity)
        if line is None or is_full_box(
            quantity, getattr(line, "sal_factor2", None), line.item_name or scan.item_name
        ):
            full_boxes += 1
        else:
            loose_pieces += quantity
    return full_boxes, loose_pieces


def scanned_full_box_count(entry: SalesDispatchGateOut) -> int:
    """Active scans that carry a whole pack -- the full boxes loaded on the truck."""
    return scanned_box_split(entry)[0]


def scanned_loose_pieces(entry: SalesDispatchGateOut) -> Decimal:
    """Pieces loaded in part boxes -- the goods covering the bills' loose remainders."""
    return scanned_box_split(entry)[1]


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
    # The line each (bill, item) is invoiced on, so a scan can be converted into the
    # unit that line is written in before the two are compared.
    line_by_key: Dict = {}
    for item in entry.active_items:
        if not item.document_id:
            continue
        qty = decimal_value(item.quantity)
        if qty > 0:
            key = (item.document_id, _norm_code(item.item_code))
            invoiced[key] = invoiced.get(key, Decimal("0")) + qty
            line_by_key.setdefault(key, item)
    if not invoiced:
        return False
    scanned: Dict = {}
    for scan in usable:
        key = (scan.document_id, _norm_code(scan.item_code))
        line = line_by_key.get(key)
        # A CSD bill counts BOXES, so its carton contributes 1 here, not the 20 pieces
        # its label declares — otherwise one carton marks a 4-carton line complete.
        units = box_invoice_units(
            scan.quantity,
            getattr(line, "sal_factor2", None),
            getattr(line, "item_name", "") or scan.item_name,
        )
        scanned[key] = scanned.get(key, Decimal("0")) + units
    return any(scanned.get(key, Decimal("0")) < qty for key, qty in invoiced.items())


def load_scan_status(entry: SalesDispatchGateOut):
    """``(scanned_boxes, expected_boxes, has_scans, is_partial)`` for a docking.

    ``is_partial`` is True when the load has scans but still carries unscanned invoiced
    goods. Completeness is judged on exact QUANTITY — scanned vs invoiced per ``(bill,
    item)`` (:func:`has_unscanned_bill_lines`) — whenever the scans carry a quantity, which
    every barcode box does. Quantity is ground truth on both sides (the invoice line qty
    from SAP; the box's own piece count from our barcode system) and needs no pack size, so
    an item whose box size we cannot derive — SAP transacts it loose (SalFactor2 = 1),
    so it has no box count at all — can no longer manufacture a phantom box-count
    shortfall that locks a fully-loaded truck. The box-COUNT estimate is used only as a
    fallback for legacy or quantity-less scans, where per-line quantity isn't available.

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
        # A load whose lines all ship loose has no box count to be short of (SAP states
        # no box size for them), so it can only be judged on quantity -- the branch
        # above, which every barcode scan takes.
        # Compared in FULL boxes: a part box covers a printed loose remainder, so
        # letting it stand in for a box would hide a missing one.
        aggregate_short = expected_boxes > 0 and scanned_full_box_count(entry) < expected_boxes
        is_partial = has_scans and aggregate_short
    return scanned_boxes, expected_boxes, has_scans, is_partial


# Dockings that have left the gate for good and no longer ride the truck. Mirrors
# ``arrival_gatepass._ACTIVE_DOCKING_STATUSES`` from the other side.
_ARRIVAL_CLOSED_STATUSES = (
    SalesDispatchGateOutStatus.REJECTED,
    SalesDispatchGateOutStatus.CANCELLED,
)


def arrival_scan_dockings(entry: SalesDispatchGateOut) -> List[SalesDispatchGateOut]:
    """Every docking on this entry's physical truck whose boxes have to be scanned.

    One truck trip (``VehicleArrival``) can carry several dockings -- a multi-company load,
    or a same-company split -- and the scan step is walked TRUCK-WIDE: the operator's screen
    sums every scan-required docking's bills, so a shortfall on one docking locks the scan
    page of all of them. Companies with scanning turned off (``box_scan_optional``) ride
    along for context but are never scanned, so they stay out of the gate.

    A lone docking (or one with no arrival) returns ``[entry]``, which keeps every
    single-docking load on exactly the old per-entry behaviour. The caller's own instance is
    always the one returned for its row, so any prefetch it warmed is still used.
    """
    if not entry.arrival_id or is_box_scan_optional(entry):
        return [entry]
    # Memoized on the instance: the status and the full-box count are read one after the
    # other off the same entry, and each sibling carries prefetched scans and lines.
    cached = getattr(entry, "_arrival_scan_dockings", None)
    if cached is not None:
        return cached
    siblings = (
        SalesDispatchGateOut.objects
        .filter(arrival_id=entry.arrival_id, is_active=True)
        .exclude(status__in=_ARRIVAL_CLOSED_STATUSES)
        .select_related("company")
        .prefetch_related("box_scans", "items", "documents__items", "partial_scan_requests",
                          "scan_skip_requests")
    )
    dockings = [entry] + [
        d for d in siblings if d.pk != entry.pk and not is_box_scan_optional(d)
    ]
    entry._arrival_scan_dockings = dockings
    return dockings


def _docking_has_unscanned_goods(docking: SalesDispatchGateOut) -> bool:
    """True when this docking still carries invoiced goods nobody has scanned.

    A docking with no scans at all is short by its whole bill -- both checks in
    :func:`load_scan_status` need a scan to compare against, and an all-loose bill (PM
    cartons: SAP transacts them per piece, so the bill prints 0 boxes) has no expected box
    count to fall short of either. Judge that case on simply having invoiced lines.
    """
    _, expected, has_scans, is_partial = load_scan_status(docking)
    if not has_scans:
        return bool(docking.active_items) or expected > 0
    return is_partial


def arrival_scan_status(entry: SalesDispatchGateOut):
    """:func:`load_scan_status` for the whole truck: ``(scanned, expected, has_scans, is_partial)``.

    The same rule the operator's scan page applies, so the partial-approval endpoint can
    never refuse a request the screen is demanding. That mismatch is a hard deadlock: a
    truck carrying a fully scanned bill plus an unscannable one (PM cartons have no box
    barcodes) locks the scan page load-wide, while the per-docking endpoint answers "all
    boxes are scanned, no approval needed" from the docking the operator is standing on.
    """
    dockings = arrival_scan_dockings(entry)
    scanned = sum(scanned_box_count(d) for d in dockings)
    expected = sum(resolved_expected_box_count(d) for d in dockings)
    has_scans = scanned > 0
    is_partial = has_scans and any(_docking_has_unscanned_goods(d) for d in dockings)
    return scanned, expected, has_scans, is_partial


def arrival_scanned_full_box_count(entry: SalesDispatchGateOut) -> int:
    """Full boxes loaded on the whole truck (part boxes cover printed loose remainders)."""
    return sum(scanned_full_box_count(d) for d in arrival_scan_dockings(entry))


def _sibling_approval_exists(entry: SalesDispatchGateOut, relation: str) -> bool:
    """One EXISTS query: does a sibling docking on this truck carry an approved request?

    Deliberately query-only (no prefetch, no company access): this runs per row on the
    readiness path, and only for dockings whose own box-scan gate already came up short.
    """
    if not entry.arrival_id:
        return False
    return (
        SalesDispatchGateOut.objects
        .filter(arrival_id=entry.arrival_id, is_active=True, **{f"{relation}__status": "APPROVED"})
        .exclude(pk=entry.pk)
        .exclude(status__in=_ARRIVAL_CLOSED_STATUSES)
        .exists()
    )


def arrival_partial_scan_approved(entry: SalesDispatchGateOut) -> bool:
    """True when an admin approved dispatching this TRUCK with a partial box scan.

    The shortfall is judged load-wide but the approval is filed against the one docking the
    operator raised it from, so it has to be honoured across the arrival -- otherwise the
    docking that is short (or the one that is complete) stays locked by an approval sitting
    on its sibling.
    """
    return _sibling_approval_exists(entry, "partial_scan_requests")


def arrival_scan_skip_approved(entry: SalesDispatchGateOut) -> bool:
    """True when an admin approved skipping box scanning for this TRUCK (nothing scanned)."""
    return _sibling_approval_exists(entry, "scan_skip_requests")


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
    scanned_full_boxes, scanned_loose = scanned_box_split(entry)
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

    # An approval is filed against ONE docking, but the shortfall it clears is judged
    # load-wide (the scan page sums every scan-required docking on the truck), so a
    # sibling's approval has to count here too. Without it the two halves of a split load
    # deadlock: the docking that cannot be scanned (PM cartons carry no box barcode) is
    # held for an approval the operator raised from the docking next to it. Checked only
    # after this docking's own approval comes up short, so the common path stays free of
    # the sibling lookup (readiness is serialized per row on the dispatch report boards).
    if box_scan_optional:
        box_scans_ok = True
    elif not has_box_scans:
        # Nothing scanned on this docking: its own skip, or any approval that cleared the
        # truck's scan shortfall. A partial approval counts here too — on a load whose
        # OTHER dockings are scanned, the shortfall this docking represents is exactly what
        # the operator raises a partial request for (there is no separate skip to raise),
        # and it may have been filed from here or from a sibling.
        box_scans_ok = (
            scan_skip_approved
            or partial_scan_approved
            or arrival_partial_scan_approved(entry)
            or arrival_scan_skip_approved(entry)
        )
    elif is_partial_scan:
        # Partly scanned: only a partial approval clears it -- a sibling's scan SKIP says
        # nothing about the goods still missing from this docking.
        box_scans_ok = partial_scan_approved or arrival_partial_scan_approved(entry)
    else:  # fully scanned, or expected count unknown
        box_scans_ok = True
    if not box_scans_ok:
        missing.append("box_scans")

    if not entry.active_items:
        missing.append("document_items")

    # One bilty (LR) is required per distinct customer on the docking, each carrying its
    # own file + number + date. A customer is keyed by customer_code (falling back to
    # customer_name). Each BILTY is tagged with the customer it covers; a blank tag is a
    # legacy/whole-truck bilty whose number/date live on the docking header.
    def _customer_key(code, name):
        return (code or "").strip() or (name or "").strip()

    def _bilty_complete(attachment):
        return (
            bool(attachment.file)
            and bool((attachment.bilty_no or "").strip())
            and attachment.bilty_date is not None
        )

    required_customers = []
    seen_customers = set()
    for document in entry.active_documents:
        key = _customer_key(document.customer_code, document.customer_name)
        if key and key not in seen_customers:
            seen_customers.add(key)
            required_customers.append(key)

    bilty_attachments = [
        a for a in attachments if a.attachment_type == SalesDispatchAttachmentType.BILTY
    ]
    covered_customers = {
        _customer_key(a.customer_code, a.customer_name)
        for a in bilty_attachments
        if _bilty_complete(a)
    }
    covered_customers.discard("")

    if len(required_customers) <= 1:
        # Single (or unknown) customer: one complete bilty satisfies. Back-compat: a
        # legacy untagged bilty file plus header number/date also counts.
        legacy_ok = (
            bool(bilty_attachments)
            and bool((entry.bilty_no or "").strip())
            and entry.bilty_date is not None
        )
        has_bilty_attachment = any(_bilty_complete(a) for a in bilty_attachments) or legacy_ok
    else:
        has_bilty_attachment = all(c in covered_customers for c in required_customers)
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
        # The scan count split the way the bill prints it: full boxes against
        # expected_boxes, and the pieces that arrived in part boxes against
        # expected_loose. A 4-piece box on a 16-PCS line is one of the latter.
        "scanned_full_boxes": scanned_full_boxes,
        "scanned_loose": scanned_loose,
        # Loose pieces the box count deliberately excludes (SAP transacts these items
        # per piece, not per box); shown alongside so "0 boxes" never reads as "nothing".
        "expected_loose": resolved_expected_loose_count(entry),
        "box_scan_optional": box_scan_optional,
        "has_weighment": has_weighment,
        "has_items": "document_items" not in missing,
        # Per-customer bilty now bundles file + number + date into one requirement.
        "has_bilty_details": "bilty_attachment" not in missing,
        "has_bilty_attachment": "bilty_attachment" not in missing,
        "requires_eway_bill": eway_required,
        "has_eway_bill": "eway_bill" not in missing,
        "has_eway_bill_attachment": "eway_bill_attachment" not in missing,
    }


def requires_eway_bill(entry: SalesDispatchGateOut) -> bool:
    documents = list(entry.active_documents)
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
