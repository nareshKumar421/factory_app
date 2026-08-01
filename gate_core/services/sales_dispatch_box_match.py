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

    norm = _normalize_code(item_code)
    return sum(
        _expected_item_boxes(item)
        for item in entry.active_items
        if item.document_id == document_id and _normalize_code(item.item_code) == norm
    )


def remaining_expected_boxes(entry, document_id, item_code, exclude_scan_id=None) -> int:
    """Expected boxes for (bill, item) minus boxes already scanned against it.

    >0 means more boxes may still be scanned; <=0 means the expected box count is
    already reached (used to hard-cap the physical box COUNT). This mirrors
    ``remaining_invoiced_qty`` but on box count rather than quantity: the qty cap
    stops shipping more PIECES than invoiced, while this stops scanning more
    physical BOXES than the item's estimated pack-out — so an extra box is blocked
    even when its pieces would still fit inside the invoiced quantity (a partial
    box). ``entry.active_items`` / ``box_scans`` are assumed prefetched."""
    expected = expected_boxes_for_bill_item(entry, document_id, item_code)
    scan_qs = entry.box_scans.filter(
        is_active=True,
        document_id=document_id,
        item_code__iexact=item_code,
    )
    if exclude_scan_id is not None:
        scan_qs = scan_qs.exclude(id=exclude_scan_id)
    return expected - scan_qs.count()
