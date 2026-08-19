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


def remaining_invoiced_qty(entry, document_id, item_code, exclude_scan_id=None) -> Decimal:
    """Invoiced qty for (bill, item) minus what's already scanned against it.

    >0 means the bill still needs more of this item; <=0 means it is fully scanned
    (used to block over-scanning past the invoice). ``entry.items`` is assumed
    prefetched; scan totals are read from the committed state.
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
        (_to_decimal(qty) for qty in scan_qs.values_list("quantity", flat=True)),
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
    """
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
    """Expected boxes for (bill, item) minus boxes already scanned against it.

    >0 means more boxes may still be scanned; <=0 means the expected box count is
    already reached (used to hard-cap the physical box COUNT). This mirrors
    ``remaining_invoiced_qty`` but on box count rather than quantity: the qty cap
    stops shipping more PIECES than invoiced, while this stops scanning more
    physical BOXES than the item's estimated pack-out — so an extra box is blocked
    even when its pieces would still fit inside the invoiced quantity (a partial
    box). ``entry.active_items`` / ``box_scans`` are assumed prefetched. Returns None
    when the item ships loose and no box count exists to cap against."""
    if bill_item_ships_loose(entry, document_id, item_code):
        # SAP gives these items no box size, so however many cartons the packers made is
        # legitimate: capping on their (zero) box count would reject every scan. Returns
        # None = uncapped; the quantity cap is what bounds the scan.
        return None
    expected = expected_boxes_for_bill_item(entry, document_id, item_code)
    scan_qs = entry.box_scans.filter(
        is_active=True,
        document_id=document_id,
        item_code__iexact=item_code,
    )
    if exclude_scan_id is not None:
        scan_qs = scan_qs.exclude(id=exclude_scan_id)
    return expected - scan_qs.count()
