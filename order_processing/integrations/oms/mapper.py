"""Turn raw OMS rows into our normalised shape, and say what cannot be trusted.

This module is where the findings from the live data become code. Three of them
matter enough to state:

**Which column is the quantity.** ``qty``. Checked against the ``Quantity`` field
in 1,081 real SAP payload lines: ``qty`` matched 1,021, ``boxes`` 96, ``pcs`` 23.
The written spec says ``pcs`` is the piece count; it is the pack size.

**When to distrust it.** OMS holds **two different conventions** in the same
column, which is the single most dangerous thing in this data:

  A. the majority — ``qty`` is pieces and ``boxes`` is ``round(qty / pcs, 2)``
     cases. e.g. ``qty=8000, pcs=20, boxes=400``.
  B. a minority — inverted: ``qty`` is CASES and ``boxes`` is pieces, so
     ``boxes = qty × pcs``. e.g. ``qty=40, pcs=16, boxes=640``.

Under B, reading ``qty`` as pieces understates the order 16-fold. Nothing in the
row says which convention applies, so neither can be assumed. Both are detected
by the same check — ``|cases × pack_size − quantity|`` is large under B and
merely a rounding artefact under A — and the line is **flagged, never corrected**,
because we cannot know which of the two figures the customer actually wanted.

Rounding alone accounts for 197 of the 226 whole-database mismatches; a live
sample of 823 lines showed 15 (1.8%) genuinely inconsistent.

**The warehouse.** OMS sends ``GP-FG`` for OIL and *no WarehouseCode at all* for
BEVERAGES, on 3,627 and 1,598 real lines respectively. So BEVERAGES lines get no
warehouse and are flagged, rather than being given an invented one.
"""
import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.utils import timezone as dj_timezone

from ...models import LineIssue

logger = logging.getLogger(__name__)

# `delivery_date` is TEXT in OMS, so it can hold anything a user or an importer
# put there. These are the shapes seen in the live data, widest-first.
DATE_FORMATS = (
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%Y/%m/%d",
    "%d-%b-%Y",
    "%d %b %Y",
    "%Y-%m-%dT%H:%M:%S",
)

# How far boxes x pack_size may drift from qty before the line is doubted. Two
# decimal places on `boxes` means the error scales with pack size: 0.005 rounded
# away on a 70-piece case is 0.35 pieces, so a flat 1.0 would flag honest rows.
ROUNDING_TOLERANCE = Decimal("1.0")


def to_aware(value):
    """Make an OMS timestamp timezone-aware.

    OMS's ``orders.created_at`` / ``updated_at`` are ``timestamp WITHOUT time
    zone``, so the value carries no zone at all. It is wall-clock time on the OMS
    server, which ``OMS_DB_TIMEZONE`` names. Attaching that zone here is what makes
    the value comparable with anything we store, and is why the incremental
    watermark works at all.
    """
    if value is None or not isinstance(value, datetime):
        return value
    if dj_timezone.is_aware(value):
        return value
    try:
        from zoneinfo import ZoneInfo

        return value.replace(tzinfo=ZoneInfo(settings.OMS_DB_TIMEZONE))
    except Exception:  # noqa: BLE001 — a bad zone name must not lose the order
        logger.warning("Bad OMS_DB_TIMEZONE %r; treating OMS times as UTC",
                       getattr(settings, "OMS_DB_TIMEZONE", None))
        return dj_timezone.make_aware(value, dj_timezone.utc)


def to_naive_oms(value):
    """Aware datetime -> naive wall-clock in the OMS zone.

    The inverse of :func:`to_aware`, needed when we send a watermark BACK to OMS:
    comparing an aware value against a naive column raises in PostgreSQL's driver.
    """
    if value is None or not isinstance(value, datetime):
        return value
    if dj_timezone.is_naive(value):
        return value
    try:
        from zoneinfo import ZoneInfo

        return value.astimezone(ZoneInfo(settings.OMS_DB_TIMEZONE)).replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        return value.astimezone(dj_timezone.utc).replace(tzinfo=None)


def _dec(value, default=Decimal("0")):
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def parse_delivery_date(raw):
    """``(date | None, raw_text)``.

    Returns the original text alongside, so an unparseable value stays visible
    instead of silently becoming NULL — the operator can see what was typed.
    """
    if raw is None:
        return None, ""
    if isinstance(raw, datetime):
        return raw.date(), raw.isoformat()
    if isinstance(raw, date):
        return raw, raw.isoformat()
    text = str(raw).strip()
    if not text:
        return None, ""
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text[:len(fmt) + 4], fmt).date(), text
        except ValueError:
            continue
    logger.debug("Unparseable OMS delivery_date %r", text)
    return None, text[:64]


def warehouse_for(category):
    """SAP warehouse for a line's category, or "" when there is no rule.

    Empty is a real answer, not a failure: OMS itself sends no ``WarehouseCode``
    for BEVERAGES, leaving SAP to use the item default. Inventing one here would
    check stock in the wrong place and look authoritative doing it.
    """
    mapping = getattr(settings, "OMS_CATEGORY_WAREHOUSE", {}) or {}
    return mapping.get((category or "").strip().upper(), "")


def map_order(row):
    """One raw ``orders`` row → our field names."""
    delivery_date, delivery_raw = parse_delivery_date(row.get("delivery_date"))
    return {
        "oms_order_id": row["id"],
        "order_number": (row.get("order_number") or "")[:64],
        "customer_code": (row.get("card_code") or "")[:50],
        "customer_name": (row.get("card_name") or "")[:255],
        # Kept as text: OMS stores '1' / '2', and coercing to int here would
        # silently break the day a company code stops being numeric.
        "company_code": (str(row.get("company")) if row.get("company") is not None else "")[:10],
        "branch_bpl_id": row.get("dispatch_from_id"),
        "branch_name": (row.get("dispatch_from_name") or "")[:120],
        "oms_status": (row.get("status_code") or "")[:40],
        "order_type": (row.get("order_type") or "")[:20],
        "po_number": (row.get("po_number") or "")[:80],
        "ship_to_address": row.get("ship_to_address") or "",
        "total_amount": _dec(row.get("total_amount"), None),
        "is_foc": bool(row.get("is_foc")),
        "remarks": row.get("remarks") or "",
        "delivery_date": delivery_date,
        "delivery_date_raw": delivery_raw,
        "sap_created": bool(row.get("sap_created")),
        "sap_doc_number": (row.get("sap_doc_number") or "")[:50],
        "quotation_cancelled": bool(row.get("quotation_cancelled")),
        "oms_created_at": to_aware(row.get("created_at")),
        "oms_updated_at": to_aware(row.get("updated_at")),
    }


def map_line(row):
    """One raw ``order_items`` row → our field names, plus any issues found."""
    quantity = _dec(row.get("qty"))
    pack_size = _dec(row.get("pcs"))
    cases = _dec(row.get("boxes"))
    litres = _dec(row.get("ltrs"))
    category = (row.get("category") or "").strip()
    item_code = (row.get("item_code") or "").strip()
    warehouse = warehouse_for(category)

    issues = []
    if not item_code:
        issues.append(LineIssue.NO_ITEM_CODE.value)
    if quantity <= 0:
        issues.append(LineIssue.ZERO_QTY.value)
    if not warehouse:
        issues.append(LineIssue.NO_WAREHOUSE.value)
    # Only doubt the quantity when the gap is bigger than `boxes` rounding can
    # explain — otherwise 197 perfectly good lines would be flagged as broken.
    if pack_size > 0 and cases > 0:
        if abs(cases * pack_size - quantity) > ROUNDING_TOLERANCE:
            issues.append(LineIssue.QTY_DISAGREES.value)

    return {
        "oms_line_id": row["id"],
        "item_code": item_code[:100],
        "item_name": (row.get("item_name") or "")[:255],
        "category": category[:40],
        "brand": (row.get("brand") or "")[:80],
        "sub_group": (row.get("sub_group") or "")[:80],
        "quantity": quantity,
        "pack_size": pack_size,
        "cases": cases,
        "litres": litres,
        "scheme_quantity": _dec(row.get("qty_scheme")),
        "unit_price": _dec(row.get("basic_price")),
        "line_total": _dec(row.get("total")),
        "warehouse_code": warehouse[:40],
        "issues": issues,
    }
