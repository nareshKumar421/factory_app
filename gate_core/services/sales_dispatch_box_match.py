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
    documents = list(entry.documents.all())
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
    for item in entry.items.all():
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
