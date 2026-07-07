"""Building a docking (``SalesDispatchGateOut``) from SAP documents.

The per-document snapshot / header-aggregate / line-item helpers used when a
docking is first created live here so they can be reused to *grow* an existing
docking. A truck that is gated in for several bills should end up as **one**
docking carrying every bill; when a bill is booked late (after the docking was
already created) ``merge_bill_into_open_docking`` adds it to that same docking
instead of leaving it as a separate pending row — as long as the load has not
been photo-locked yet.

Imports of models/SAP are lazy to avoid app-load circular imports; this module
is only imported at runtime by the docking view and the linking flow.
"""

from decimal import Decimal

from django.db import transaction
from django.db.models import Max

from gate_core.models.sales_dispatch import decimal_or_none

# A docking can still take another bill only before its load photo is attached
# (same cut-off the cover-level ``attach_bill_to_inside_vehicle`` enforces).
OPEN_DOCKING_STATUSES = ("DOCKED",)
ACTIVE_DOCKING_STATUSES = (
    "DOCKED",
    "PHOTO_ATTACHED",
    "READY_FOR_GATEPASS",
    "GATEPASS_PRINTED",
    "PRINT_COMMITTED",
    "DISPATCHED",
)


def join_unique(values):
    """Join distinct, stripped, non-empty values with ", " (header aggregate)."""
    result = []
    for value in values:
        value = str(value or "").strip()
        if value and value not in result:
            result.append(value)
    return ", ".join(result)


def sum_documents(documents, key, places="0.001"):
    """Sum a numeric SAP-document key across documents; None when all are blank."""
    values = [decimal_or_none(document.get(key), places) for document in documents]
    values = [value for value in values if value is not None]
    if not values:
        return None
    return sum(values, Decimal("0"))


def _sum_decimals(values):
    """Sum already-parsed Decimal/None values; None when all are blank."""
    values = [value for value in values if value is not None]
    if not values:
        return None
    return sum(values, Decimal("0"))


def document_snapshot(document):
    """Map one SAP document dict to the stored per-document snapshot fields."""
    return {
        "document_type": document["document_type"],
        "sap_doc_entry": document["doc_entry"],
        "sap_doc_num": document.get("doc_num", ""),
        "sap_doc_date": document.get("doc_date"),
        "sap_doc_total": decimal_or_none(document.get("doc_total"), "0.01"),
        "sap_branch_id": document.get("branch_id"),
        "sap_branch_name": document.get("branch_name", ""),
        "sap_reference": document.get("sap_reference") or document.get("base_refs", ""),
        "sap_comments": document.get("sap_comments", ""),
        "customer_code": document.get("card_code", ""),
        "customer_name": document.get("card_name", ""),
        "ship_to_code": document.get("ship_to_code", ""),
        "ship_to_address": document.get("ship_to_address", ""),
        "place_of_supply": document.get("place_of_supply", ""),
        "bp_gstin": document.get("bp_gstin", ""),
        "eway_bill": document.get("eway_bill", ""),
        "from_warehouse": document.get("from_warehouse", ""),
        "to_warehouse": document.get("to_warehouse", ""),
        "warehouses": document.get("warehouses", ""),
        "item_summary": document.get("item_summary", ""),
        "base_refs": document.get("base_refs", ""),
        "total_quantity": decimal_or_none(document.get("total_quantity")),
        "total_litres": decimal_or_none(document.get("total_litres")),
        "total_boxes": decimal_or_none(document.get("total_boxes")),
        "total_weight": decimal_or_none(document.get("total_weight")),
    }


# Header fields recomputed by aggregating across *all* of a docking's documents
# (the rest of the header stays anchored to the primary document).
_AGGREGATE_HEADER_FIELDS = (
    "sap_doc_num",
    "sap_doc_total",
    "sap_reference",
    "customer_code",
    "customer_name",
    "eway_bill",
    "warehouses",
    "base_refs",
    "item_summary",
    "total_quantity",
    "total_litres",
    "total_boxes",
    "total_weight",
)


def header_snapshot(documents):
    """Header field values for a docking built from ``documents`` (primary + aggregates)."""
    primary = documents[0]
    snapshot = document_snapshot(primary)
    snapshot["sap_doc_num"] = join_unique(document.get("doc_num", "") for document in documents)
    snapshot["sap_doc_total"] = sum_documents(documents, "doc_total", "0.01")
    snapshot["sap_reference"] = join_unique(
        document.get("sap_reference") or document.get("base_refs", "") for document in documents
    )
    snapshot["customer_code"] = join_unique(document.get("card_code", "") for document in documents)
    snapshot["customer_name"] = join_unique(document.get("card_name", "") for document in documents)
    snapshot["eway_bill"] = join_unique(document.get("eway_bill", "") for document in documents)
    snapshot["warehouses"] = join_unique(document.get("warehouses", "") for document in documents)
    snapshot["item_summary"] = " | ".join(
        document.get("item_summary", "") for document in documents if document.get("item_summary", "")
    )
    snapshot["base_refs"] = join_unique(document.get("base_refs", "") for document in documents)
    snapshot["total_quantity"] = sum_documents(documents, "total_quantity")
    snapshot["total_litres"] = sum_documents(documents, "total_litres")
    snapshot["total_boxes"] = sum_documents(documents, "total_boxes")
    snapshot["total_weight"] = sum_documents(documents, "total_weight")
    return snapshot


def create_items(entry, document_row, document, user, start_line_num=0):
    """Create the docking's line items for one document; returns the next line number."""
    from gate_core.models import SalesDispatchGateOutItem
    from gate_core.services.sales_dispatch_documents import SalesDispatchDocumentService

    items = []
    for index, item in enumerate(SalesDispatchDocumentService.iter_items(document)):
        item["line_num"] = start_line_num + index
        items.append(
            SalesDispatchGateOutItem(
                sales_dispatch=entry,
                document=document_row,
                created_by=user,
                updated_by=user,
                **item,
            )
        )
    SalesDispatchGateOutItem.objects.bulk_create(items)
    return start_line_num + len(items)


def recompute_header_from_document_rows(docking, user):
    """Re-aggregate the docking header from its stored document rows.

    Called after a document is added/removed; rebuilds only the joined/summed
    header fields (doc numbers, totals, customers, ...) from every active
    document row, leaving the primary-document-derived header fields untouched.
    """
    from gate_core.models import SalesDispatchGateOutDocument

    docs = list(SalesDispatchGateOutDocument.objects.filter(sales_dispatch=docking, is_active=True))
    docking.sap_doc_num = join_unique(doc.sap_doc_num for doc in docs)
    docking.sap_doc_total = _sum_decimals([doc.sap_doc_total for doc in docs])
    docking.sap_reference = join_unique(doc.sap_reference for doc in docs)
    docking.customer_code = join_unique(doc.customer_code for doc in docs)
    docking.customer_name = join_unique(doc.customer_name for doc in docs)
    docking.eway_bill = join_unique(doc.eway_bill for doc in docs)
    docking.warehouses = join_unique(doc.warehouses for doc in docs)
    docking.base_refs = join_unique(doc.base_refs for doc in docs)
    docking.item_summary = " | ".join(doc.item_summary for doc in docs if doc.item_summary)
    docking.total_quantity = _sum_decimals([doc.total_quantity for doc in docs])
    docking.total_litres = _sum_decimals([doc.total_litres for doc in docs])
    docking.total_boxes = _sum_decimals([doc.total_boxes for doc in docs])
    docking.total_weight = _sum_decimals([doc.total_weight for doc in docs])
    docking.updated_by = user
    docking.save(update_fields=[*_AGGREGATE_HEADER_FIELDS, "updated_by", "updated_at"])


def find_open_docking_for_plan(plan):
    """The truck's open (pre-photo-lock) docking this bill could join, or None.

    A plan's ``linked_vehicle_entry`` is its gate-in's vehicle entry; every bill
    on the same physical truck shares it, so the truck's docking is the one whose
    primary plan points at the same gate-in entry and is still open.
    """
    from gate_core.models import SalesDispatchGateOut

    vehicle_entry_id = getattr(plan, "linked_vehicle_entry_id", None)
    if not vehicle_entry_id:
        return None
    return (
        SalesDispatchGateOut.objects.filter(
            company_id=plan.company_id,
            is_active=True,
            status__in=OPEN_DOCKING_STATUSES,
            dispatch_plan__linked_vehicle_entry_id=vehicle_entry_id,
        )
        .order_by("-created_at")
        .first()
    )


def _bill_already_docked(company_id, doc_entry):
    from gate_core.models import (
        SalesDispatchDocumentType,
        SalesDispatchGateOutDocument,
    )

    return SalesDispatchGateOutDocument.objects.filter(
        company_id=company_id,
        is_active=True,
        document_type=SalesDispatchDocumentType.INVOICE,
        sap_doc_entry=doc_entry,
        sales_dispatch__is_active=True,
        sales_dispatch__status__in=ACTIVE_DOCKING_STATUSES,
    ).exists()


def add_plan_to_open_docking(docking, plan, user, document_service=None, document=None):
    """Append ``plan``'s invoice to ``docking`` as another document. Returns the docking.

    Returns None (no-op) when the docking is a stock-transfer, the docking is no
    longer open, the bill is already docked, or SAP cannot supply the invoice —
    leaving the bill as a normal pending dockable row. Raises only on unexpected
    programming errors; SAP failures are swallowed by ``merge_bill_into_open_docking``.

    Pass ``document`` (an already-fetched SAP document dict) to skip the SAP call
    entirely -- the docking-create reuse path already has the fetched documents, so
    this avoids a second round-trip (and the SAP-in-transaction failure window).
    """
    from gate_core.models import (
        SalesDispatchDocumentType,
        SalesDispatchGateOutDocument,
    )
    from gate_core.services.sales_dispatch_documents import SalesDispatchDocumentService

    doc_entry = getattr(plan, "sap_invoice_doc_entry", None)
    if doc_entry is None:
        return None
    # This late-merge path is invoice-only; stock-transfer dockings stay single-document.
    if docking.document_type != SalesDispatchDocumentType.INVOICE:
        return None
    if docking.status not in OPEN_DOCKING_STATUSES:
        return None
    if _bill_already_docked(docking.company_id, doc_entry):
        return None

    if document is None:
        if document_service is None:
            document_service = SalesDispatchDocumentService(docking.company)
        document = document_service.get_document(SalesDispatchDocumentType.INVOICE, doc_entry)
    if not document:
        return None
    # Never merge a different SAP branch into the same docking.
    branch_id = document.get("branch_id")
    if branch_id is not None and docking.sap_branch_id is not None and branch_id != docking.sap_branch_id:
        return None

    with transaction.atomic():
        next_line_num = _next_line_num(docking)
        document_row = SalesDispatchGateOutDocument.objects.create(
            sales_dispatch=docking,
            company=docking.company,
            dispatch_plan=plan,
            created_by=user,
            updated_by=user,
            **document_snapshot(document),
        )
        create_items(docking, document_row, document, user, next_line_num)
        recompute_header_from_document_rows(docking, user)
    return docking


def _next_line_num(docking):
    """Next free ``line_num`` for the docking's items (0 when it has none yet)."""
    from gate_core.models import SalesDispatchGateOutItem

    current_max = SalesDispatchGateOutItem.objects.filter(sales_dispatch=docking).aggregate(
        value=Max("line_num")
    )["value"]
    return 0 if current_max is None else current_max + 1


def merge_bill_into_open_docking(plan, user, document_service=None):
    """Try to fold a late-booked bill into its truck's open docking.

    Returns the updated docking on success, or None when there is no open docking
    or the merge can't proceed (SAP down, branch mismatch, already docked, load
    photo-locked) — in which case the bill stays a pending dockable row.
    """
    from sap_client.exceptions import SAPConnectionError, SAPDataError

    docking = find_open_docking_for_plan(plan)
    if docking is None:
        return None
    try:
        return add_plan_to_open_docking(docking, plan, user, document_service=document_service)
    except (SAPConnectionError, SAPDataError, ValueError):
        return None
