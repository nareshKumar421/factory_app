"""Amazon order-sheet parser — deliberately SEPARATE from the Flipkart parser.

Amazon's tax/shipment report (MTR) has an entirely different layout from Flipkart's
order CSV (84 columns, ASIN instead of FSN, Shipment Id instead of a Tracking ID,
Transaction Type instead of Order State, and it ships as .xlsx). Keeping this in its
own module means a change to Amazon's column mapping can never affect Flipkart, and
vice-versa. It emits the SAME canonical row dicts that ``order_import_service``
consumes, so the rest of the flow (resolve → issue → pack → scan → confirm → DN) is
shared and channel-scoped.

Accepts both ``.xlsx`` (openpyxl) and ``.csv``.
"""
import csv
import io
from datetime import date, datetime

from .errors import MarketplaceError

# Canonical field → Amazon column label. Matching is case/space-insensitive.
# The canonical keys mirror Flipkart's so `ingest()` reads the same shape.
AMAZON_COLUMNS = {
    "order_id": "Order Id",
    "order_item_id": "Shipment Item Id",
    "shipment_id": "Shipment Id",
    "ordered_on": "Order Date",
    "order_state": "Transaction Type",
    "order_type": "Fulfillment Channel",
    "fsn": "Asin",                 # Amazon's product key (used like Flipkart's FSN)
    "sku": "Sku",
    "product": "Item Description",
    "quantity": "Quantity",
    "hsn": "Hsn/sac",
    "unit_price": "Principal Amount",
    "invoice_amount": "Invoice Amount",
    "cgst": "Cgst Tax",
    "igst": "Igst Tax",
    "sgst": "Sgst Tax",
    "buyer": "Ship To City",       # MTR carries no buyer name; ship-to city is the label
    "ship_to": "Ship To City",
    "addr1": "",
    "addr2": "",
    "city": "Ship To City",
    "state": "Ship To State",
    "pin": "Ship To Postal Code",
    "dispatch_by": "Shipment Date",
    "tracking": "Shipment Id",      # the scannable per-shipment id (Amazon's "Tracking ID")
}

# Transaction Types that mean the order was cancelled/returned (not to be fulfilled).
_CANCEL_TYPES = {"cancel", "refund", "return"}


def _norm(s):
    return (s or "").strip().lower().replace("_", " ").replace("  ", " ")


def _cell(value):
    """Stringify one cell. Dates are rendered in Flipkart's date style so the shared
    ``_parse_dt`` reads them without touching its format list."""
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.strftime("%b %d, %Y %H:%M:%S") if isinstance(value, datetime) else value.strftime("%b %d, %Y")
    return str(value).strip()


def _is_xlsx(content, filename, content_type):
    name = (filename or "").lower()
    if name.endswith(".xlsx"):
        return True
    if name.endswith(".csv"):
        return False
    if "sheet" in (content_type or "").lower() or "excel" in (content_type or "").lower():
        return True
    # xlsx files are ZIP archives → magic bytes "PK".
    return bool(content) and content[:2] == b"PK"


def _raw_rows(*, text, content, filename, content_type):
    """Return a list of raw rows (list of stringified cells); row 0 is the header."""
    if _is_xlsx(content, filename, content_type):
        if content is None:
            raise MarketplaceError("Excel upload is empty.", code="BAD_SHEET", status_code=400)
        try:
            import openpyxl
        except Exception:  # pragma: no cover - env specific
            raise MarketplaceError(
                "Excel (.xlsx) support is not available on the server; upload a CSV instead.",
                code="BAD_SHEET", status_code=400,
            )
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = wb[wb.sheetnames[0]]
        return [[_cell(c) for c in row] for row in ws.iter_rows(values_only=True)]
    # CSV
    if text is None and content is not None:
        text = content.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text or ""))
    return [[_cell(c) for c in row] for row in reader]


def parse_amazon_rows(*, text=None, content=None, filename="", content_type=""):
    """Parse an Amazon sheet into canonical row dicts (same keys as the Flipkart
    parser). Raises ``BAD_SHEET`` if the required columns are absent."""
    raw = _raw_rows(text=text, content=content, filename=filename, content_type=content_type)
    if not raw:
        raise MarketplaceError("The sheet is empty.", code="BAD_SHEET", status_code=400)

    header = raw[0]
    index = {_norm(h): i for i, h in enumerate(header)}
    col = {key: index.get(_norm(label)) for key, label in AMAZON_COLUMNS.items() if label}
    if col.get("order_id") is None or col.get("sku") is None or col.get("quantity") is None:
        raise MarketplaceError(
            "Amazon sheet is missing required columns (Order Id, Sku, Quantity).",
            code="BAD_SHEET", status_code=400,
        )

    rows = []
    for raw_row in raw[1:]:
        if not any((c or "").strip() for c in raw_row):
            continue

        def get(key):
            i = col.get(key)
            return raw_row[i].strip() if i is not None and i < len(raw_row) and raw_row[i] else ""

        row = {key: get(key) for key in AMAZON_COLUMNS}
        # Normalise the state so the shared cancellation check (``"cancel" in state``)
        # works without changing Flipkart's logic.
        if _norm(row["order_state"]) in _CANCEL_TYPES:
            row["order_state"] = "Cancelled"
        rows.append(row)
    return rows
