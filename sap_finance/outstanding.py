"""The outstanding reports: party balances, open bills, open GRPOs, aging.

Each report is one read of SAP per company (``outstanding_reader``), kept for a
couple of minutes in the shared cache so paging, sorting and filtering a list
of thirteen thousand open invoices does not read SAP again each time. A
``refresh`` reads SAP afresh.

DAYS are counted to today: a bill's overdue days from its due date (negative =
not yet due), a GRPO's from the day it was received, a party's from its last
bill and last payment.
"""

import logging
from collections import defaultdict
from decimal import Decimal
from typing import Callable, Optional

from django.core.cache import caches
from django.utils import timezone

from . import outstanding_reader as reader

logger = logging.getLogger(__name__)

CACHE_SECONDS = 120

#: Overdue buckets, by days past the due date (or past the bill date for aging
#: "by bill date", EXIM's way).
BUCKETS = (
    ("not_due", "Not yet due", None, 0),
    ("d0_30", "1-30 days", 1, 30),
    ("d31_60", "31-60 days", 31, 60),
    ("d61_90", "61-90 days", 61, 90),
    ("d91_180", "91-180 days", 91, 180),
    ("d180", "Over 180 days", 181, None),
)

MAX_PAGE_SIZE = 200


def _cached(key: str, read: Callable, refresh: bool):
    cache = caches["shared"]
    if not refresh:
        try:
            hit = cache.get(key)
        except Exception as exc:  # a Redis that stalls must not stop the report
            logger.warning("outstanding: cache read failed: %s", exc)
            hit = None
        if hit is not None:
            return hit
    value = {"rows": read(), "read_at": timezone.now()}
    try:
        cache.set(key, value, CACHE_SECONDS)
    except Exception as exc:
        logger.warning("outstanding: cache write failed: %s", exc)
    return value


def _bucket(days: Optional[int]) -> str:
    if days is None or days <= 0:
        return "not_due"
    for key, _, low, high in BUCKETS[1:]:
        if (low is None or days >= low) and (high is None or days <= high):
            return key
    return "d180"


def _days(since, today) -> Optional[int]:
    return (today - since).days if since else None


def _matches(row: dict, q: str, fields) -> bool:
    if not q:
        return True
    q = q.lower()
    return any(q in str(row.get(f) or "").lower() for f in fields)


def _page(rows: list, page: int, page_size: int) -> dict:
    page_size = max(1, min(MAX_PAGE_SIZE, page_size))
    pages = max(1, -(-len(rows) // page_size))
    page = max(1, min(page, pages))
    start = (page - 1) * page_size
    return {"count": len(rows), "page": page, "page_size": page_size, "pages": pages,
            "results": rows[start:start + page_size]}


# ---------------------------------------------------------------------------
# Party balances
# ---------------------------------------------------------------------------

def party_outstanding(company, side: str, *, oil_suppliers=False, refresh=False) -> dict:
    """Every vendor or customer SAP holds a balance against, with their last
    bill and last payment and the days since each. A positive balance is owed
    to us (debit), a negative one owed by us (credit)."""
    data = _cached(
        f"sap_finance:parties:{company.code}:{side}:{int(oil_suppliers)}",
        lambda: reader.party_balances(company.code, side, oil_suppliers=oil_suppliers),
        refresh,
    )
    today = timezone.localdate()
    rows = []
    for row in data["rows"]:
        bill, pay = row["last_bill"], row["last_payment"]
        rows.append({
            **row,
            "days_since_bill": _days(bill["date"] if bill else None, today),
            "days_since_payment": _days(pay["date"] if pay else None, today),
        })
    owed_to_us = sum((r["balance"] for r in rows if r["balance"] > 0), Decimal("0"))
    owed_by_us = sum((r["balance"] for r in rows if r["balance"] < 0), Decimal("0"))
    return {
        "side": side,
        "oil_suppliers": oil_suppliers,
        "read_at": data["read_at"],
        "rows": rows,
        "totals": {
            "parties": len(rows),
            "debit": owed_to_us,
            "credit": owed_by_us,
            "net": owed_to_us + owed_by_us,
        },
        "groups": sorted({r["group"] for r in rows if r["group"]}),
    }


# ---------------------------------------------------------------------------
# Open bills
# ---------------------------------------------------------------------------

BILL_SORTS = {
    "due_date": lambda r: (r["due_date"] or timezone.localdate(), r["doc_num"]),
    "doc_date": lambda r: (r["doc_date"] or timezone.localdate(), r["doc_num"]),
    "due": lambda r: r["due"],
    "overdue_days": lambda r: r["overdue_days"] if r["overdue_days"] is not None else -10**6,
    "party": lambda r: (r["card_name"].lower(), r["doc_num"]),
    "doc_num": lambda r: (len(r["doc_num"]), r["doc_num"]),
}


def open_bills(company, side: str, *, q="", card_code="", group="", bucket="", oil_suppliers=False,
               sort="due_date", descending=False, page=1, page_size=50, refresh=False) -> dict:
    """Open A/P (``side`` "vendor") or A/R ("customer") invoices: a summary of
    everything open, by overdue bucket and by party, and one page of the bills
    the filters leave."""
    data = _cached(
        f"sap_finance:bills:{company.code}:{side}:{int(oil_suppliers)}",
        lambda: reader.open_bills(company.code, side, oil_suppliers=oil_suppliers),
        refresh,
    )
    today = timezone.localdate()
    every = []
    for row in data["rows"]:
        overdue = _days(row["due_date"], today)
        every.append({**row, "overdue_days": overdue, "bucket": _bucket(overdue)})

    # Every filter but the bucket: the buckets are counted from these, so
    # picking one bucket still shows what the others hold.
    unbucketed = [
        r for r in every
        if (not card_code or r["card_code"] == card_code)
        and (not group or r["group"] == group)
        and _matches(r, q, ("doc_num", "party_ref", "card_code", "card_name", "vehicle_number", "transporter",
                            "bilty_number", "lr_number", "remarks"))
    ]
    by_bucket = {key: {"key": key, "label": label, "count": 0, "due": Decimal("0")} for key, label, _, _ in BUCKETS}
    for r in unbucketed:
        by_bucket[r["bucket"]]["count"] += 1
        by_bucket[r["bucket"]]["due"] += r["due"]
    rows = [r for r in unbucketed if not bucket or r["bucket"] == bucket]
    rows.sort(key=BILL_SORTS.get(sort, BILL_SORTS["due_date"]), reverse=descending)

    by_party = {}
    for r in rows:
        party = by_party.setdefault(r["card_code"], {"card_code": r["card_code"], "card_name": r["card_name"],
                                                     "count": 0, "due": Decimal("0"), "oldest_due_date": None})
        party["count"] += 1
        party["due"] += r["due"]
        if r["due_date"] and (party["oldest_due_date"] is None or r["due_date"] < party["oldest_due_date"]):
            party["oldest_due_date"] = r["due_date"]
    return {
        "side": side,
        "read_at": data["read_at"],
        "totals": {
            "count": len(rows),
            "total": sum((r["total"] for r in rows), Decimal("0")),
            "paid": sum((r["paid"] for r in rows), Decimal("0")),
            "due": sum((r["due"] for r in rows), Decimal("0")),
            "overdue": sum((r["due"] for r in rows if r["bucket"] != "not_due"), Decimal("0")),
            "parties": len(by_party),
        },
        "buckets": list(by_bucket.values()),
        "top_parties": sorted(by_party.values(), key=lambda p: p["due"], reverse=True)[:10],
        "groups": sorted({r["group"] for r in every if r["group"]}),
        **_page(rows, page, page_size),
    }


# ---------------------------------------------------------------------------
# Open GRPOs
# ---------------------------------------------------------------------------

def open_grpos(company, *, raw_material_only=False, refresh=False) -> dict:
    """Goods received against a PO and not yet billed, oldest first."""
    data = _cached(f"sap_finance:grpos:{company.code}", lambda: reader.open_grpos(company.code), refresh)
    today = timezone.localdate()
    rows = [
        {**r, "days_open": _days(r["doc_date"], today)}
        for r in data["rows"] if r["raw_material"] or not raw_material_only
    ]
    days = [r["days_open"] for r in rows if r["days_open"] is not None]
    return {
        "read_at": data["read_at"],
        "rows": rows,
        "totals": {
            "count": len(rows),
            "value": sum((r["total"] for r in rows), Decimal("0")),
            "vendors": len({r["card_code"] for r in rows}),
            "average_days": round(sum(days) / len(days), 1) if days else None,
            "oldest_days": max(days) if days else None,
        },
        "warehouses": sorted({w for r in rows for w in r["warehouses"]}),
    }


# ---------------------------------------------------------------------------
# Customer aging
# ---------------------------------------------------------------------------

def customer_aging(company, *, basis="due", q="", group="", sales_employee="", card_code="",
                   refresh=False) -> dict:
    """What each customer owes, split by how long it has been owed: by days past
    the due date (``basis`` "due") or since the bill date ("bill", EXIM's way).
    Open credit notes count against their customer. With ``card_code``, that
    customer's open documents too."""
    data = _cached(f"sap_finance:aging:{company.code}", lambda: reader.open_receivables(company.code), refresh)
    today = timezone.localdate()
    customers = {}
    documents = []
    for row in data["rows"]:
        if group and row["group"] != group:
            continue
        if sales_employee and row["sales_employee"] != sales_employee:
            continue
        if not _matches(row, q, ("card_code", "card_name")):
            continue
        since = row["due_date"] if basis == "due" else row["doc_date"]
        days = _days(since, today)
        if basis != "due" and days is not None and days <= 0:
            days = 1  # by bill date, a bill of today is in the first bucket
        bucket = _bucket(days)
        cust = customers.setdefault(row["card_code"], {
            "card_code": row["card_code"], "card_name": row["card_name"], "group": row["group"],
            "sales_employee": row["sales_employee"], "documents": 0, "total": Decimal("0"),
            **{key: Decimal("0") for key, *_ in BUCKETS},
        })
        cust["documents"] += 1
        cust["total"] += row["due"]
        cust[bucket] += row["due"]
        if card_code and row["card_code"] == card_code:
            documents.append({**row, "days": days, "bucket": bucket})
    rows = sorted(customers.values(), key=lambda c: c["total"], reverse=True)
    totals = {key: sum((c[key] for c in rows), Decimal("0")) for key, *_ in BUCKETS}
    return {
        "basis": basis,
        "read_at": data["read_at"],
        "buckets": [{"key": key, "label": label} for key, label, _, _ in BUCKETS],
        "rows": rows,
        "totals": {"customers": len(rows), "total": sum((c["total"] for c in rows), Decimal("0")), **totals},
        "groups": sorted({r["group"] for r in data["rows"] if r["group"]}),
        "sales_employees": sorted({r["sales_employee"] for r in data["rows"] if r["sales_employee"]}),
        "documents": sorted(documents, key=lambda d: (d["due_date"] or today)) if card_code else None,
    }
