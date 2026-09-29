"""Bills held in this app for a warehouse manager — the page's third source.

A bill raised on the A/R Invoices screen from a warehouse its raiser does not
manage never reaches SAP until that warehouse's manager approves it (see
``ar_invoice.models.ARInvoiceWarehouseApproval``). Those decisions are taken on
the same Invoice Approval page as the OMS and SAP rows, so this module turns the
held bills into the page's existing row shape — the frontend's ``InvoiceLog`` —
rather than asking managers to learn another screen.

``id`` on these rows is the approval row's id: one bill spanning two unmanaged
warehouses is two rows, one on each warehouse's list, each decided by its own
manager. Like the OMS and SAP ids it is its own id-space, so every route and
cache key carries the source (``APP``).
"""
import logging

from sap_client.client import SAPClient

from .fg_stock import _to_number

logger = logging.getLogger(__name__)

SOURCE = "APP"


def _name(user):
    if not user:
        return None
    return getattr(user, "full_name", "") or user.get_username()


def _lines(posting):
    return sorted(posting.lines.all(), key=lambda line: line.id)


def build_stock_map(company_code, approvals):
    """``{(warehouse, item_code): {item_name, warehouse_stock}}`` for these rows.

    Each line is read in its own warehouse — a held bill can span several — with
    one HANA query per warehouse for the whole page. A failure costs the stock
    column, never the list: the manager can still read the bill and decide it.
    """
    wanted = {}
    for approval in approvals:
        for line in _lines(approval.ar_invoice):
            if line.item_code and line.warehouse_code:
                wanted.setdefault(line.warehouse_code, set()).add(line.item_code)

    stock_map = {}
    for warehouse, codes in wanted.items():
        try:
            rows = SAPClient(company_code).get_fg_warehouse_stock(sorted(codes), warehouse)
        except Exception:
            logger.exception(
                "Stock lookup failed for held bills at %s (%d items)", warehouse, len(codes)
            )
            continue
        for row in rows or []:
            if row.get("ItemCode"):
                stock_map[(warehouse, row["ItemCode"])] = {
                    "item_name": row.get("ItemName"),
                    "warehouse_stock": _to_number(row.get("OnHand")),
                }
    return stock_map


def serialize_row(approval, stock_map=None, can_approve=False):
    """One held bill as one warehouse's approval row, in the page's ``InvoiceLog`` shape."""
    posting = approval.ar_invoice
    lines = _lines(posting)
    stock_map = stock_map or {}
    so_numbers = sorted({str(line.base_doc_num) for line in lines if line.base_doc_num})
    pending = (
        approval.status == "PENDING" and posting.status == "AWAITING_MANAGER"
    )
    return {
        "id": approval.id,
        "source": SOURCE,
        "posting_id": posting.id,
        "posting_status": posting.status,
        "posting_status_display": posting.get_status_display(),
        "is_counter_sale": all(line.base_entry is None for line in lines) and bool(lines),
        # Filled once the bill is in SAP, so the Approved tab can say which bill
        # the approval became.
        "doc_entry": posting.sap_doc_entry,
        "doc_num": posting.sap_doc_num,
        # Blank on a counter sale, which has no Sales Order behind it.
        "so_number": ", ".join(so_numbers),
        "card_code": posting.customer_code,
        "party_name": posting.customer_name or posting.customer_code,
        # Before tax: GST is SAP's to work out, and the bill is not in SAP yet.
        "total_amount": (
            str(posting.selected_total) if posting.selected_total is not None else None
        ),
        "amount_is_pre_tax": True,
        "branch": str(posting.branch_id) if posting.branch_id is not None else None,
        "warehouse": approval.warehouse_code,
        "status": approval.status,
        "rejection_reason": approval.remarks or None,
        "error_message": posting.error_message or None,
        "decided_by": _name(approval.decided_by),
        "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
        "invoice_payload": {
            "CardCode": posting.customer_code,
            "DocDate": str(posting.doc_date) if posting.doc_date else None,
            "Comments": posting.comments or None,
            "DocumentLines": [
                {
                    "LineNum": index,
                    "ItemCode": line.item_code,
                    "ItemDescription": line.description or None,
                    "Quantity": _to_number(line.quantity),
                    "WarehouseCode": line.warehouse_code,
                    "TaxCode": line.tax_code,
                    "Price": _to_number(line.price),
                }
                for index, line in enumerate(lines)
            ],
        },
        "fg_stock": [
            {
                "line_num": index,
                "item_code": line.item_code,
                "item_name": (stock_map.get((line.warehouse_code, line.item_code)) or {}).get(
                    "item_name"
                )
                or line.description
                or None,
                "quantity": _to_number(line.quantity),
                "warehouse_code": line.warehouse_code,
                "warehouse_stock": (
                    stock_map.get((line.warehouse_code, line.item_code)) or {}
                ).get("warehouse_stock"),
            }
            for index, line in enumerate(lines)
        ],
        "created_at": posting.created_at.isoformat() if posting.created_at else None,
        "created_by": _name(posting.created_by),
        "can_decide": bool(can_approve and pending),
    }


def history(posting):
    """The bill's trail: raised, each warehouse's answer, and SAP, in order."""
    records = [
        {
            "id": 0,
            "status": "RAISED",
            "created_by_name": _name(posting.created_by),
            "remarks": "Raised in the factory app.",
            "created_at": posting.created_at.isoformat() if posting.created_at else None,
        }
    ]
    for approval in posting.warehouse_approvals.select_related("decided_by").order_by("id"):
        if approval.status == "PENDING":
            records.append({
                "id": approval.id,
                "status": "PENDING",
                "created_by_name": None,
                "remarks": f"Waiting for the manager of {approval.warehouse_code}.",
                "created_at": approval.created_at.isoformat() if approval.created_at else None,
            })
            continue
        remarks = approval.warehouse_code
        if approval.remarks:
            remarks = f"{remarks}: {approval.remarks}"
        records.append({
            "id": approval.id,
            "status": approval.status,
            "created_by_name": _name(approval.decided_by),
            "remarks": remarks,
            "created_at": approval.decided_at.isoformat() if approval.decided_at else None,
        })
    if posting.sap_doc_num:
        records.append({
            "id": -1,
            "status": "POSTED",
            "created_by_name": _name(posting.posted_by),
            "remarks": f"Created in SAP as bill {posting.sap_doc_num}.",
            "created_at": posting.posted_at.isoformat() if posting.posted_at else None,
        })
    elif posting.status == "FAILED" and posting.error_message:
        records.append({
            "id": -1,
            "status": "FAILED",
            "created_by_name": None,
            "remarks": posting.error_message,
            "created_at": posting.updated_at.isoformat() if posting.updated_at else None,
        })
    return records


def audit(approval):
    """The decided row in the page's local-audit shape — the approval row is its own audit."""
    if approval.status == "PENDING":
        return []
    posting = approval.ar_invoice
    return [{
        "id": approval.id,
        "source": SOURCE,
        "approval_code": approval.id,
        "draft_entry": None,
        "so_number": "",
        "party_name": posting.customer_name or posting.customer_code,
        "total_amount": (
            str(posting.selected_total) if posting.selected_total is not None else None
        ),
        "decision": approval.status,
        "rejection_reason": approval.remarks,
        "sap_message": "",
        "company": posting.company_id,
        "acted_by_name": _name(approval.decided_by),
        "created_at": approval.decided_at.isoformat() if approval.decided_at else None,
    }]
