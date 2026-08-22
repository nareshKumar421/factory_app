"""Resolve which bill (SAP document) a scanned docking box belongs to.

A docking (``SalesDispatchGateOut``) is one physical truck that can carry several
bills (``SalesDispatchGateOutDocument``). Boxes are scanned against the *truck*,
not a bill, so when two bills on the load invoice the same item a scan must be
attributed to exactly one of them — otherwise the same box shows as dispatched
against every bill that contains that item.

Attribution rule (first match wins):

1. **Origin bill.** If the box was picked against a specific bill in the barcode
   dispatch module (``Box.dispatch_session``), and that bill is on this load, the
   box belongs to it — this is the physical truth, even past the invoiced qty.
2. **Greedy fill.** Otherwise, among the bills that invoice this item, fill the
   first bill (document order) that still has un-scanned invoiced quantity, then
   overflow to the next. This makes N scanned boxes land on the bills that need
   them instead of all bills at once.
3. **Overflow.** If every such bill is already fully scanned, attribute to the
   first bill that invoices the item (the over-scan lands on one bill, not all).
4. **Unplanned.** If no bill on the load invoices the item, return ``None`` — the
   scan stays unattributed (the UI flags it as "outside list").
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation


def _normalize_code(value) -> str:
    return str(value or "").strip().upper()


def _to_decimal(value) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def match_document_for_bill(documents, *, sap_doc_entry=None, sap_doc_num="", bill_number=""):
    """Return the document on the load that identifies the given bill, else None.

    Matches on the stable SAP DocEntry first, then the human doc number / bill
    number, so it works regardless of which identifier the other side captured.
    """
    entry_key = sap_doc_entry
    if entry_key not in (None, "", 0, "0"):
        entry_key = str(entry_key).strip()
        for document in documents:
            if str(document.sap_doc_entry).strip() == entry_key:
                return document

    for candidate in (_normalize_code(sap_doc_num), _normalize_code(bill_number)):
        if not candidate:
            continue
        for document in documents:
            if _normalize_code(document.sap_doc_num) == candidate:
                return document
    return None


def document_for_dispatch_session(documents, session):
    """The load document matching a barcode-module dispatch session's bill."""
    if session is None:
        return None
    return match_document_for_bill(
        documents,
        sap_doc_entry=getattr(session, "sap_doc_entry", None),
        sap_doc_num=getattr(session, "sap_doc_num", ""),
        bill_number=getattr(session, "bill_number", ""),
    )


def resolve_scan_document(entry, *, item_code, box=None, exclude_scan_id=None):
    """Pick the one bill (document) a scanned box should be attributed to.

    ``entry`` is the docking; its ``documents`` and ``items`` are assumed prefetched.
    Pass ``box`` to honour the box's origin bill. ``exclude_scan_id`` skips a scan
    when re-resolving an existing one. Returns a ``SalesDispatchGateOutDocument``
    or ``None``. See module docstring for the rule.
    """
    documents = list(entry.active_documents)
    if not documents:
        return None

    # 1) Origin bill from the barcode dispatch session.
    origin = document_for_dispatch_session(
        documents, getattr(box, "dispatch_session", None) if box is not None else None
    )
    if origin is not None:
        return origin

    norm_code = _normalize_code(item_code)
    if not norm_code:
        return None

    # Invoiced quantity per document for this item (prefetched items, no query).
    invoiced = defaultdict(Decimal)
    for item in entry.active_items:
        if item.document_id and _normalize_code(item.item_code) == norm_code:
            invoiced[item.document_id] += _to_decimal(item.quantity)

    candidate_docs = [doc for doc in documents if invoiced.get(doc.id, Decimal("0")) > 0]
    if not candidate_docs:
        return None

    # Quantity already attributed to each document for this item (committed state).
    scanned = defaultdict(Decimal)
    scan_qs = entry.box_scans.filter(
        is_active=True,
        document__isnull=False,
        item_code__iexact=item_code,
    )
    if exclude_scan_id is not None:
        scan_qs = scan_qs.exclude(id=exclude_scan_id)
    for document_id, quantity in scan_qs.values_list("document_id", "quantity"):
        scanned[document_id] += _to_decimal(quantity)

    for doc in candidate_docs:
        if scanned.get(doc.id, Decimal("0")) < invoiced[doc.id]:
            return doc

    # Every invoicing bill is already fully scanned — land the over-scan on one.
    return candidate_docs[0]


def document_invoices_item(entry, document_id, item_code) -> bool:
    """True if the given bill (document) on the load has a line for this item."""
    norm = _normalize_code(item_code)
    return any(
        item.document_id == document_id and _normalize_code(item.item_code) == norm
        for item in entry.active_items
    )


def invoice_units_per_box(entry, document_id, item_code, box_pieces) -> Decimal:
    """How much of (bill, item)'s invoiced quantity one physical box covers.

    Delegates to ``box_packing.box_invoice_units`` using the bill line's own pack
    factor, so a CSD carton counts as 1 (its bill counts boxes) while every other box
    counts its pieces. Kept here so every over-scan guard converts identically.
    """
    from gate_core.services.box_packing import box_invoice_units

    line = _bill_item_line(entry, document_id, item_code)
    return box_invoice_units(
        box_pieces,
        getattr(line, "sal_factor2", None),
        getattr(line, "item_name", "") or "",
    )


def invoice_unit_label(entry, document_id, item_code, amount) -> str:
    """The unit (bill, item) is invoiced in: "box"/"boxes" for CSD stock, else "PCS".

    Only for wording messages, so a rejection on a CSD line talks about the cartons the
    bill actually counts instead of pieces it never did.
    """
    if invoice_units_per_box(entry, document_id, item_code, 20) == 1:
        return "box" if amount == 1 else "boxes"
    return "PCS"


def remaining_invoiced_qty(entry, document_id, item_code, exclude_scan_id=None) -> Decimal:
    """Invoiced qty for (bill, item) minus what's already scanned against it.

    >0 means the bill still needs more of this item; <=0 means it is fully scanned
    (used to block over-scanning past the invoice). ``entry.items`` is assumed
    prefetched; scan totals are read from the committed state.

    Both sides are counted in the INVOICE's unit. That unit is a piece for most items
    but a BOX for CSD stock, so each CSD scan contributes 1 here rather than the 20
    pieces its label declares — otherwise a 4-carton line reads as 20 of 4 scanned.
    """
    norm = _normalize_code(item_code)
    invoiced = sum(
        (
            _to_decimal(item.quantity)
            for item in entry.active_items
            if item.document_id == document_id and _normalize_code(item.item_code) == norm
        ),
        Decimal("0"),
    )
    scan_qs = entry.box_scans.filter(
        is_active=True,
        document_id=document_id,
        item_code__iexact=item_code,
    )
    if exclude_scan_id is not None:
        scan_qs = scan_qs.exclude(id=exclude_scan_id)
    scanned = sum(
        (
            invoice_units_per_box(entry, document_id, item_code, qty)
            for qty in scan_qs.values_list("quantity", flat=True)
        ),
        Decimal("0"),
    )
    return invoiced - scanned


def expected_boxes_for_bill_item(entry, document_id, item_code) -> int:
    """Expected physical box count for (bill, item): the sum of the per-line box
    estimates for lines on this bill matching the item.

    Uses the same ``_expected_item_boxes`` estimate the docking scan page and the
    gatepass completeness gate display, so the hard cap below can never disagree
    with the "N / M boxes" figure the operator sees. Imported lazily to keep this
    module free of a service-layer import cycle."""
    from gate_core.services.sales_dispatch_gatepass import _expected_item_boxes

    return sum(
        _expected_item_boxes(item)
        for item in _bill_item_lines(entry, document_id, item_code)
    )


def expected_containers_for_bill_item(entry, document_id, item_code) -> int:
    """Physical boxes (bill, item) can legitimately arrive in: its printed box count PLUS
    one for the loose remainder -- i.e. ``ceil(qty / pack)``.

    The printed box count (``expected_boxes_for_bill_item``) is ``floor(qty / pack)``: the
    remainder is invoiced as loose pieces, and those pieces physically arrive in a short
    box of their own. So a 1,860-PCS line of a 16-PCS item prints "116 boxes + 4 loose"
    but is loaded as 117 boxes. Capping the box COUNT at 116 deadlocked exactly that bill
    after 115 full boxes and one 4-piece box -- the count read full while 16 pieces were
    still on the floor and the box that would finish the bill was refused.

    Used only to cap the count. What the operator is shown stays the printed split, with
    part boxes reported as loose pieces (see ``sales_dispatch_gatepass.scanned_box_split``).
    """
    from gate_core.services.sales_dispatch_gatepass import item_packing

    total = 0
    for item in _bill_item_lines(entry, document_id, item_code):
        packing = item_packing(item)
        total += packing.boxes + (1 if packing.loose > 0 else 0)
    return total


def _bill_item_line(entry, document_id, item_code):
    """The first invoice line of (bill, item) -- the one whose pack factor/name every
    conversion below reads. Lines of the same item on one bill share a pack size."""
    lines = _bill_item_lines(entry, document_id, item_code)
    return lines[0] if lines else None


def _bill_item_lines(entry, document_id, item_code) -> list:
    norm = _normalize_code(item_code)
    return [
        item
        for item in entry.active_items
        if item.document_id == document_id and _normalize_code(item.item_code) == norm
    ]


def bill_item_ships_loose(entry, document_id, item_code) -> bool:
    """True when every line of (bill, item) ships loose, so no box count exists.

    SAP states no box size for these items (SalFactor2 = 1, non-CSD) -- its own bill
    prints "0 Box / N PCS" -- so there is nothing to cap a box COUNT against. The
    quantity cap (``remaining_invoiced_qty``) is the guard that applies instead.
    """
    from gate_core.services.sales_dispatch_gatepass import item_packing

    lines = _bill_item_lines(entry, document_id, item_code)
    return bool(lines) and all(item_packing(item).is_loose for item in lines)


def loose_box_scan_error(entry, document, box):
    """Reject a dismantled ('loose') box when the bill only calls for full boxes.

    A box that had pieces pulled out as loose stock keeps the same barcode and
    looks identical to a full box at the dock, so the operator scanning it has
    no physical cue that it is short. When the bill's invoiced quantity for the
    item is an exact number of full boxes, accepting such a box silently ships
    fewer pieces than invoiced — so it is blocked here. When the invoiced
    quantity itself has a loose remainder (not a whole number of boxes), a
    partial box is genuinely expected and allowed.

    A box counts as dismantled only when ``LooseStock`` records name it as
    their source — the audit trail the dismantle flow writes — and its full
    size is reconstructed as current qty + total pulled. Item names are NOT
    parsed for a pack size here: names lie (e.g. CSD items say "20 PCS" but
    ship 1 pc/box), and a box with no dismantle trail is simply allowed.
    Returns a human-readable error string, or ``None`` when the scan is
    allowed. ``entry.active_items`` is assumed prefetched.

    Skipped entirely for a bill that counts BOXES (CSD): the test below divides an
    invoiced quantity by a box's piece count, which only means something when both are
    pieces. On a CSD line "4" is four cartons, so ``4 % 20`` is arithmetic on two
    different units and would reject or allow at random.
    """
    if invoice_units_per_box(entry, document.id, box.item_code, box.qty) == 1:
        return None

    pulled = sum(
        (_to_decimal(qty) for qty in box.loose_stocks.values_list("original_qty", flat=True)),
        Decimal("0"),
    )
    if pulled <= 0:
        return None

    box_qty = _to_decimal(box.qty)
    full_size = box_qty + pulled
    if full_size <= 0:
        return None

    norm = _normalize_code(box.item_code)
    invoiced = sum(
        (
            _to_decimal(item.quantity)
            for item in entry.active_items
            if item.document_id == document.id and _normalize_code(item.item_code) == norm
        ),
        Decimal("0"),
    )
    if invoiced <= 0 or invoiced % full_size != 0:
        return None

    return (
        f"Box {box.box_barcode} is a loose/partial box — {pulled} of its "
        f"{full_size} PCS were removed as loose stock and it now holds {box_qty}. "
        f"Bill {document.sap_doc_num} invoices {invoiced} PCS of {box.item_code}, "
        f"an exact number of full boxes, so a loose box cannot be scanned on it. "
        f"Scan a full box instead."
    )


def remaining_expected_boxes(entry, document_id, item_code, exclude_scan_id=None) -> int | None:
    """Box-count headroom for (bill, item): the boxes it can arrive in, minus those
    already scanned against it.

    >0 means more boxes may still be scanned; <=0 means the count is reached (the hard cap
    on the physical box COUNT, so an extra box is blocked even when its pieces would still
    fit inside the invoiced quantity -- the 581-vs-580 case this cap exists to prevent).

    The cap is ``ceil(qty / pack)`` (:func:`expected_containers_for_bill_item`), not the
    bill's printed box count: a line invoicing 116 boxes + 4 loose is loaded as 117 boxes,
    the last one holding just the 4 loose pieces. Capping at the printed 116 refused that
    117th box, leaving the bill 16 pieces short with no way to finish it.

    Returns None when the item ships loose and there is no box count to cap against.
    ``entry.active_items`` / ``box_scans`` are assumed prefetched."""
    if bill_item_ships_loose(entry, document_id, item_code):
        # SAP gives these items no box size, so however many cartons the packers made is
        # legitimate: capping on their (zero) box count would reject every scan. Returns
        # None = uncapped; the quantity cap is what bounds the scan.
        return None
    scan_qs = entry.box_scans.filter(
        is_active=True,
        document_id=document_id,
        item_code__iexact=item_code,
    )
    if exclude_scan_id is not None:
        scan_qs = scan_qs.exclude(id=exclude_scan_id)
    return expected_containers_for_bill_item(entry, document_id, item_code) - scan_qs.count()
