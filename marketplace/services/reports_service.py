"""Marketplace Reports — downloadable CSVs filtered by channel + a date range.

Each report is a small function returning ``(filename, csv_text)``; the public
:func:`build_report_csv` dispatches by report type. All reports are HANA-free (built
from the marketplace DB). Date filters are optional — an empty range exports
everything for that report/channel.

Report types (slug → what it covers):
  * ``orders``          — one row per order item (order/upload date, status)
  * ``invoices``        — internal JI billing docs (invoice date)
  * ``delivery-notes``  — posted SAP delivery notes, one row per DN (post date)
  * ``returns``         — returns (return/submitted date)
  * ``reconciliation``  — per-order-item deviation (portal vs outward vs inward)
"""
import csv
import io
from decimal import Decimal

from .errors import MarketplaceError


def _csv(header, rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for r in rows:
        writer.writerow(r)
    return buf.getvalue()


def _span(date_from, date_to):
    return f"{date_from or 'all'}_{date_to or 'all'}"


# ── orders / dispatch ────────────────────────────────────────────────────────
_ORDERS_HEADER = [
    "Order ID", "Order item ID", "Buyer", "Ship-to name", "Order type", "Order date",
    "Dispatch-by", "City", "State", "PIN", "Status", "Dispatch status", "SAP post status",
    "Tracking scanned", "Tracking total", "Tracking ID", "Item scanned", "Scanned at",
    "Scanned by", "SKU", "Marketplace SKU", "FSN/ASIN", "HSN", "Quantity", "Unit price",
    "Invoice amount", "Tax amount", "Order state", "Invoice number", "Invoice date",
    "DN number", "GI number", "Confirmed at", "Confirmed by",
]


def orders_report(company, channel, *, date_from=None, date_to=None, date_field="order", status=None, **_):
    from .dispatch_board_service import orders_in_range
    views = orders_in_range(company, channel, date_from, date_to, date_field=date_field)["orders"]
    if status:
        views = [o for o in views if o["status"] == status]
    rows = []
    for o in views:
        items = o["items"] or [None]
        for it in items:
            rows.append([
                o["order_id"], (it or {}).get("order_item_id", ""), o["buyer_name"],
                o.get("ship_to_name", ""), o.get("order_type", ""), o.get("order_date", "") or "",
                o.get("dispatch_by", "") or "", o.get("city", ""), o.get("state", ""),
                o.get("pin_code", ""), o["status"], o.get("dispatch_status", "") or "",
                o.get("sap_post_status", "") or "", o["tracking_scanned"], o["tracking_total"],
                (it or {}).get("tracking_id", ""),
                ("yes" if (it or {}).get("scanned") else "no") if it else "",
                (it or {}).get("scanned_at", "") or "", (it or {}).get("scanned_by", ""),
                (it or {}).get("sku_name", ""), (it or {}).get("marketplace_sku", ""),
                (it or {}).get("fsn", ""), (it or {}).get("hsn", ""),
                (it or {}).get("quantity", ""), (it or {}).get("unit_price", ""),
                (it or {}).get("invoice_amount", ""), (it or {}).get("tax_amount", ""),
                (it or {}).get("order_state", ""), o.get("invoice_number", ""),
                o.get("invoice_date", "") or "", o.get("dn_number", ""), o.get("gi_number", ""),
                o.get("confirmed_at", "") or "", o.get("confirmed_by", ""),
            ])
    return f"orders_{channel}_{_span(date_from, date_to)}.csv", _csv(_ORDERS_HEADER, rows)


# ── invoices (internal JI billing) ───────────────────────────────────────────
def invoices_report(company, channel, *, date_from=None, date_to=None, **_):
    from ..models import MarketplaceOrderBilling
    qs = MarketplaceOrderBilling.objects.filter(company=company, channel=channel)
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)
    header = ["Invoice No", "Invoice Date", "Order ID", "Buyer", "DN Number", "Amount", "Status"]
    rows = [
        [b.invoice_number, b.created_at.date().isoformat(), b.order_id, b.buyer_name,
         b.sap_delivery_note_num, str(b.total_amount), b.status]
        for b in qs.order_by("-created_at")
    ]
    return f"invoices_{channel}_{_span(date_from, date_to)}.csv", _csv(header, rows)


# ── posted SAP delivery notes (one row per DN) ───────────────────────────────
def delivery_notes_report(company, channel, *, date_from=None, date_to=None, **_):
    from ..models import MarketplaceDispatch
    qs = MarketplaceDispatch.objects.filter(
        company=company, channel=channel, sap_delivery_note_doc_entry__isnull=False,
    ).select_related("order", "internal_billing")
    if date_from:
        qs = qs.filter(confirmed_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(confirmed_at__date__lte=date_to)
    groups = {}
    for d in qs:
        g = groups.setdefault(d.sap_delivery_note_doc_entry, {
            "num": d.sap_delivery_note_num, "date": d.confirmed_at,
            "orders": set(), "amount": Decimal("0"),
        })
        g["orders"].add(d.order.order_id)
        if d.internal_billing_id:
            g["amount"] += Decimal(d.internal_billing.total_amount)
    header = ["DN Number", "DN Doc Entry", "DN Date", "Channel", "Orders", "Total Amount"]
    rows = [
        [g["num"], de, g["date"].date().isoformat() if g["date"] else "", channel,
         len(g["orders"]), str(g["amount"])]
        for de, g in sorted(groups.items())
    ]
    return f"delivery-notes_{channel}_{_span(date_from, date_to)}.csv", _csv(header, rows)


# ── returns ──────────────────────────────────────────────────────────────────
def returns_report(company, channel, *, date_from=None, date_to=None, **_):
    from ..models import MarketplaceReturn
    qs = MarketplaceReturn.objects.filter(company=company, channel=channel).select_related(
        "order", "submitted_by")
    if date_from:
        qs = qs.filter(submitted_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(submitted_at__date__lte=date_to)
    header = ["Order ID", "Return Status", "Credit Doc", "Submitted At", "Submitted By"]
    rows = [
        [r.order.order_id if r.order_id else "", r.status, r.internal_credit_doc_num,
         r.submitted_at.isoformat() if r.submitted_at else "",
         r.submitted_by.full_name if r.submitted_by_id else ""]
        for r in qs.order_by("-submitted_at")
    ]
    return f"returns_{channel}_{_span(date_from, date_to)}.csv", _csv(header, rows)


# ── reconciliation (deviation) ───────────────────────────────────────────────
def reconciliation_report(company, channel, *, date_from=None, date_to=None, **_):
    from .reconciliation_service import build_report
    data = build_report(company, channel=channel, from_date=date_from, to_date=date_to)
    header = [
        "Order ID", "Channel", "Item Code", "Item Name", "Portal Qty", "Outward Qty",
        "Inward Qty", "Physical Qty", "Outward-vs-Inward Deviation",
        "Portal-vs-Physical Deviation", "Has Deviation",
    ]
    rows = [
        [r["order_id"], r["channel"], r["item_code"], r["item_name"], r["portal_quantity"],
         r["outward_quantity"], r["inward_quantity"], r["physical_quantity"],
         r["outward_vs_inward_deviation"], r["portal_vs_physical_deviation"],
         "yes" if r["has_deviation"] else "no"]
        for r in data["rows"]
    ]
    return f"reconciliation_{channel}_{_span(date_from, date_to)}.csv", _csv(header, rows)


REPORTS = {
    "orders": orders_report,
    "invoices": invoices_report,
    "delivery-notes": delivery_notes_report,
    "returns": returns_report,
    "reconciliation": reconciliation_report,
}


def build_report_csv(report_type, company, channel, params):
    """Dispatch to the report builder; returns ``(filename, csv_text)``."""
    fn = REPORTS.get(report_type)
    if fn is None:
        raise MarketplaceError(
            f"Unknown report type {report_type!r}.", code="NOT_FOUND", status_code=404)
    return fn(company, channel, **params)
