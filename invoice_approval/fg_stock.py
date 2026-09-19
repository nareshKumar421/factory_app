"""Per-line warehouse stock for OMS invoice logs, read from our own SAP client.

While the approval page read OMS over HTTP, this field arrived already computed:
OMS's serializer built it from its own HANA connection. Reading OMS's database
directly gets the invoice rows but not that, so it is rebuilt here — same query,
same shape, same meaning — through ``sap_client``.

The structure mirrors what OMS returned exactly, because the frontend's
``FgStock`` type is the contract and it is shared with the SAP-source rows.

A HANA failure must not take the invoice list down with it. The approver can
still read the bill and act on it; they simply lose the stock column. So every
lookup failure is logged and skipped, and the affected lines serialize with a
null ``warehouse_stock`` — which already means "no OITW row here" and so needs no
new handling in the UI.
"""
import logging

from sap_client.client import SAPClient

logger = logging.getLogger(__name__)

# OMS records which company database an invoice belongs to in `branch`. Live data
# carries exactly two values (verified 2026-09-19: BEVERAGE 183, OIL 145), and
# OMS's own query treats anything that is not BEVERAGE as oil — mirrored here so
# a value we have not seen degrades the same way it does there.
BEVERAGE_BRANCH = "BEVERAGE"
BEVERAGE_COMPANY = "JIVO_BEVERAGES"
DEFAULT_COMPANY = "JIVO_OIL"

# Logs written before `branch` was populated carry NULL; every one of them is
# from the oil company database.
DEFAULT_BRANCH = "OIL"


def _company_for_branch(branch):
    """The SAP company code whose schema holds this invoice's stock."""
    if (branch or DEFAULT_BRANCH).strip().upper() == BEVERAGE_BRANCH:
        return BEVERAGE_COMPANY
    return DEFAULT_COMPANY


def extract_fg_item_codes(payload):
    """FG item codes on an invoice payload, in the order the lines appear."""
    lines = (payload or {}).get("DocumentLines") or []
    codes = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        code = line.get("ItemCode")
        if not code:
            continue
        code = str(code)
        if code.upper().startswith("FG") and code not in codes:
            codes.append(code)
    return codes


def _to_number(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _key_of(invoice):
    """(company, warehouse) this invoice's stock should be read from, or None."""
    warehouse = invoice.get("warehouse")
    if not warehouse:
        return None
    return (_company_for_branch(invoice.get("branch")), warehouse)


def build_fg_stock_map(invoices):
    """``{(company, warehouse): {item_code: {item_name, warehouse_stock}}}``

    One HANA query per company/warehouse pair for the whole page, not one per
    invoice line — the list endpoint hands the result to every row it is about to
    serialize.
    """
    wanted = {}
    for invoice in invoices:
        key = _key_of(invoice)
        codes = extract_fg_item_codes(invoice.get("invoice_payload"))
        if key and codes:
            wanted.setdefault(key, set()).update(codes)

    stock_map = {}
    for (company, warehouse), codes in wanted.items():
        try:
            rows = SAPClient(company).get_fg_warehouse_stock(sorted(codes), warehouse)
        except Exception:
            logger.exception(
                "FG stock lookup failed for company %s warehouse %s (%d items)",
                company,
                warehouse,
                len(codes),
            )
            continue

        stock_map[(company, warehouse)] = {
            row["ItemCode"]: {
                "item_name": row.get("ItemName"),
                "warehouse_stock": _to_number(row.get("OnHand")),
            }
            for row in (rows or [])
            if row.get("ItemCode")
        }

    return stock_map


def fg_stock_for_invoice(invoice, stock_map=None):
    """Per-line warehouse stock for one invoice, ready to serialize.

    Pass ``stock_map`` from :func:`build_fg_stock_map` on list endpoints; without
    one the lookup is done for this invoice alone.
    """
    payload = invoice.get("invoice_payload")
    if not extract_fg_item_codes(payload):
        return []

    key = _key_of(invoice)
    if stock_map is None:
        stock_map = build_fg_stock_map([invoice])
    by_item = (stock_map or {}).get(key) or {}
    warehouse = key[1] if key else None

    result = []
    for line in (payload or {}).get("DocumentLines") or []:
        if not isinstance(line, dict):
            continue
        item_code = line.get("ItemCode")
        if not item_code or not str(item_code).upper().startswith("FG"):
            continue
        item_code = str(item_code)
        stock = by_item.get(item_code) or {}
        result.append({
            "line_num": line.get("LineNum"),
            "item_code": item_code,
            "item_name": stock.get("item_name"),
            "quantity": _to_number(line.get("Quantity")),
            "warehouse_code": warehouse,
            # None (not 0) when the item has no OITW row for this warehouse, so
            # "not stocked here" stays distinguishable from "stocked, empty".
            "warehouse_stock": stock.get("warehouse_stock"),
        })
    return result
