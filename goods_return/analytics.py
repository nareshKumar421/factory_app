"""Read-only analytics over customer returns, for the Customer Returns dashboard.

Kept out of ``services.py`` on purpose: that file orchestrates the workflow (a
return is created, gated in, received, posted), and every method on it writes.
Nothing here writes, and nothing here is on the path of a return -- it only reads
the rows the workflow has already produced.

Two things are worth knowing before reading the numbers this produces.

**What day a return counts on.** A return is created days before its truck shows
up, so neither ``created_at`` nor ``gated_in_at`` alone answers "how many came
back in March". The window is taken on ``COALESCE(gated_in_at, created_at)``: a
return that has arrived counts on the day it physically arrived, and one still on
the road counts on the day it was booked, which is the only date it has. The
serialised rows say which of the two was used, so the caller never has to guess.

**Where "leaked" comes from -- two places, on purpose.** ``LEAKED`` is now a
stored member of ``GoodsReturnItemCondition``, so a leak keyed in today is a
counted fact. Every return keyed in BEFORE that member existed is still sitting in
the database as ``DAMAGED`` with the word in its free-text ``reason``, and no
migration can safely reclassify those -- "carton damaged, some leakage" is one
clerk's sentence, not a machine-readable flag. So both readings are reported, and
they are NOT the same cut of the data:

  * ``by_condition`` is stored, exact, and mutually exclusive.
  * ``by_reason`` is inferred by keyword from operator-typed text. It is a reading
    aid, not a ledger. A line whose text matches nothing lands in OTHER, and a line
    with no text at all lands in UNSPECIFIED -- kept apart deliberately, because
    "we wrote something the buckets don't cover" and "nobody wrote anything" are
    different problems and only the second one is fixable by the returns clerk.

``totals.leaked_quantity`` is the union of the two: a line counts once if it is
either keyed ``LEAKED`` or reads as leakage. That is the figure to quote for "how
much leaked", because a window spanning the change contains both kinds and either
one alone under-counts. ``totals.leaked_recorded`` / ``leaked_inferred`` split it,
so it stays visible how much of the answer is still resting on free text -- a
number that should fall to zero as the old returns age out of the window.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import (
    GoodsReturn,
    GoodsReturnItem,
    GoodsReturnItemCondition,
    GoodsReturnStatus,
)

# How far back the dashboard looks when the caller names no window.
DEFAULT_WINDOW_DAYS = 90

# Above this many days the trend is drawn by month rather than by day -- a year
# of daily points is 365 bars nobody can read.
DAILY_TREND_MAX_DAYS = 92

# Lists are cut to this; the dashboard shows a leaderboard, not a report.
TOP_N = 12

# A return that was cancelled never came back, so it is excluded from every
# figure except its own count. Drafts are kept: creating a return already puts it
# in front of the gate, so a draft is a truck that is genuinely expected.
EXCLUDED_STATUSES = (GoodsReturnStatus.CANCELLED,)

# The statuses that mean the goods are physically in.
ARRIVED_STATUSES = (
    GoodsReturnStatus.ARRIVED,
    GoodsReturnStatus.RECEIVED,
    GoodsReturnStatus.PARTIALLY_POSTED,
    GoodsReturnStatus.POSTED,
)

# Reason buckets, matched against the line's `reason` + `remarks`, FIRST MATCH
# WINS -- so the order of this tuple is part of the definition, not decoration.
#
# LEAKAGE and BREAKAGE sit above DAMAGE because "oil leaked and the carton is
# damaged" is a leak, and "damage" is the word that appears in almost every such
# sentence. EXPIRY sits above WRONG_SHORT so "short shelf life" is read as an
# expiry problem rather than a short supply.
REASON_BUCKETS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "LEAKAGE",
        "Leakage",
        ("leak", "leek", "leakag", "leakeg", "spill", "spilt", "spillage", "seepage", "oil out"),
    ),
    (
        "BREAKAGE",
        "Breakage",
        ("break", "broke", "brake", "crack", "burst", "bursted", "puncture", "smash"),
    ),
    (
        "DAMAGE",
        "Damaged packing",
        ("damag", "dent", "crush", "torn", "tear", "seal", "cap loose", "loose cap", "label"),
    ),
    (
        "EXPIRY",
        "Expiry / short shelf life",
        (
            "expir",
            "shelf life",
            "shelf-life",
            "date over",
            "old stock",
            "near expiry",
            "short life",
            "mfg date",
            "batch date",
        ),
    ),
    (
        "QUALITY",
        "Quality complaint",
        (
            "quality",
            "smell",
            "odour",
            "odor",
            "taste",
            "colour",
            "color",
            "rancid",
            "fungus",
            "sediment",
            "particle",
            "complaint",
            "impur",
        ),
    ),
    (
        "WRONG_SHORT",
        "Wrong / short supply",
        (
            "wrong",
            "mismatch",
            "short",
            "excess",
            "extra",
            "not ordered",
            "over supply",
            "oversupply",
            "wrong item",
            "wrong size",
        ),
    ),
    (
        "UNSOLD",
        "Unsold / market return",
        (
            "not sold",
            "unsold",
            "no sale",
            "slow moving",
            "market return",
            "shop closed",
            "scheme",
            "stock return",
            "non moving",
            "non-moving",
        ),
    ),
)

REASON_OTHER = ("OTHER", "Other stated reason")
REASON_UNSPECIFIED = ("UNSPECIFIED", "No reason recorded")

REASON_LABELS = {key: label for key, label, _ in REASON_BUCKETS}
REASON_LABELS[REASON_OTHER[0]] = REASON_OTHER[1]
REASON_LABELS[REASON_UNSPECIFIED[0]] = REASON_UNSPECIFIED[1]

# Bucket order for the serialised list, so the dashboard's colours do not shuffle
# between two loads that happen to rank the buckets differently.
REASON_ORDER = [key for key, _, _ in REASON_BUCKETS] + [REASON_OTHER[0], REASON_UNSPECIFIED[0]]

CONDITION_LABELS = dict(GoodsReturnItemCondition.choices)
STATUS_LABELS = dict(GoodsReturnStatus.choices)


def classify_reason(text: str) -> str:
    """The bucket a line's free text falls in. See the module docstring."""
    cleaned = (text or "").strip().lower()
    if not cleaned:
        return REASON_UNSPECIFIED[0]
    for key, _label, keywords in REASON_BUCKETS:
        if any(word in cleaned for word in keywords):
            return key
    return REASON_OTHER[0]


def resolve_window(from_date: date | None, to_date: date | None) -> tuple[date, date]:
    """The window to report on, defaulting to the last ``DEFAULT_WINDOW_DAYS``.

    A caller who sends the two dates the wrong way round gets them swapped rather
    than an empty dashboard -- the only other reading of that request is "show me
    nothing", which nobody means.
    """
    today = timezone.localdate()
    end = to_date or today
    start = from_date or (end - timedelta(days=DEFAULT_WINDOW_DAYS - 1))
    if start > end:
        start, end = end, start
    return start, end


def _q(value) -> float:
    """A Decimal as a JSON number. Quantities are litres/pieces, not money, and
    three decimals is what the column stores."""
    return float(round(Decimal(value or 0), 3))


def _money(value) -> float:
    return float(round(Decimal(value or 0), 2))


def _pct(part: float, whole: float) -> float:
    return round(part * 100 / whole, 1) if whole else 0.0


def _local_date(stamp) -> date:
    """The stamp's date in the plant's timezone. A return gated in at 00:30 IST is
    stored as the previous day in UTC, and the gate would not recognise it there."""
    if stamp is None:
        return timezone.localdate()
    return (timezone.localtime(stamp) if timezone.is_aware(stamp) else stamp).date()


def _bucket_key(stamp, by_day: bool) -> str:
    day = _local_date(stamp)
    return day.isoformat() if by_day else f"{day.year:04d}-{day.month:02d}"


def build_dashboard(
    company_ids,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
) -> dict:
    """Every figure the Customer Returns dashboard draws, in one payload.

    One round trip rather than a call per panel: the panels are cuts of the same
    few hundred rows, and splitting them into separate endpoints would mean the
    SKU table and the condition ring could disagree about which returns they had.
    """
    start, end = resolve_window(from_date, to_date)

    # `arrived_on` is the dashboard's day for a return -- see the module docstring.
    returns = (
        GoodsReturn.objects.filter(is_active=True, company_id__in=company_ids)
        .annotate(arrived_on=Coalesce("gated_in_at", "created_at"))
        .filter(arrived_on__date__gte=start, arrived_on__date__lte=end)
    )

    cancelled_count = returns.filter(status=GoodsReturnStatus.CANCELLED).count()
    live = returns.exclude(status__in=EXCLUDED_STATUSES)

    headers = list(
        live.values(
            "id",
            "entry_no",
            "status",
            "basis",
            "customer_code",
            "customer_name",
            "company_id",
            "requires_approval",
            "approval_status",
            "gated_in_at",
            "arrived_on",
        )
    )
    header_by_id = {row["id"]: row for row in headers}

    lines = list(
        GoodsReturnItem.objects.filter(
            is_active=True,
            goods_return_id__in=header_by_id.keys(),
        ).values(
            "goods_return_id",
            "item_code",
            "item_name",
            "uom",
            "return_quantity",
            "unit_price",
            "condition",
            "reason",
            "remarks",
        )
    )

    # -- line-level cuts -------------------------------------------------------
    #
    # Done in Python rather than as five GROUP BY queries. The reason bucket does
    # not exist in the database (it is derived from free text), so the SKU table
    # and the reason ring would have had to be built here anyway -- and doing the
    # rest the same way guarantees every panel is reading exactly one row set.

    by_condition: dict[str, dict] = {
        key: {"lines": 0, "quantity": Decimal(0), "value": Decimal(0)} for key in CONDITION_LABELS
    }
    by_reason: dict[str, dict] = {
        key: {"lines": 0, "quantity": Decimal(0), "value": Decimal(0)} for key in REASON_ORDER
    }
    skus: dict[str, dict] = {}
    customers: dict[str, dict] = {}
    total_qty = Decimal(0)
    total_value = Decimal(0)
    # The union described in the module docstring: keyed LEAKED, or -- for the
    # returns booked before that choice existed -- reading as leakage. Counted in
    # two halves so the board can show how much of the answer is still a guess.
    leaked_recorded = Decimal(0)
    leaked_inferred = Decimal(0)

    for line in lines:
        header = header_by_id.get(line["goods_return_id"])
        if header is None:  # pragma: no cover -- the filter above rules this out
            continue

        qty = Decimal(line["return_quantity"] or 0)
        value = qty * Decimal(line["unit_price"] or 0)
        condition = line["condition"] or GoodsReturnItemCondition.OTHER
        reason_key = classify_reason(f"{line['reason']} {line['remarks']}")

        total_qty += qty
        total_value += value

        if condition == GoodsReturnItemCondition.LEAKED:
            leaked_recorded += qty
        elif reason_key == "LEAKAGE":
            # `elif`, so a line that is both never lands in the total twice.
            leaked_inferred += qty

        bucket = by_condition.setdefault(
            condition, {"lines": 0, "quantity": Decimal(0), "value": Decimal(0)}
        )
        bucket["lines"] += 1
        bucket["quantity"] += qty
        bucket["value"] += value

        bucket = by_reason[reason_key]
        bucket["lines"] += 1
        bucket["quantity"] += qty
        bucket["value"] += value

        code = line["item_code"] or "(not coded)"
        sku = skus.get(code)
        if sku is None:
            sku = skus[code] = {
                "item_code": code,
                "item_name": line["item_name"] or "",
                "uom": line["uom"] or "",
                "lines": 0,
                "quantity": Decimal(0),
                "value": Decimal(0),
                "returns": set(),
                "customers": set(),
                "conditions": defaultdict(Decimal),
                "reasons": defaultdict(Decimal),
            }
        sku["lines"] += 1
        sku["quantity"] += qty
        sku["value"] += value
        sku["returns"].add(header["id"])
        sku["customers"].add(header["customer_code"] or header["customer_name"])
        sku["conditions"][condition] += qty
        sku["reasons"][reason_key] += qty
        if not sku["item_name"] and line["item_name"]:
            sku["item_name"] = line["item_name"]

        key = header["customer_code"] or header["customer_name"] or "(unnamed)"
        customer = customers.get(key)
        if customer is None:
            customer = customers[key] = {
                "customer_code": header["customer_code"] or "",
                "customer_name": header["customer_name"] or "",
                "lines": 0,
                "quantity": Decimal(0),
                "value": Decimal(0),
                "returns": set(),
                "skus": set(),
                "conditions": defaultdict(Decimal),
            }
        customer["lines"] += 1
        customer["quantity"] += qty
        customer["value"] += value
        customer["returns"].add(header["id"])
        customer["skus"].add(code)
        customer["conditions"][condition] += qty

    # A return with no lines still counts as a return that came back -- it is a
    # truck at the gate whose paperwork is not keyed in yet, which is exactly the
    # gap the dashboard should show rather than hide.
    for header in headers:
        key = header["customer_code"] or header["customer_name"] or "(unnamed)"
        customer = customers.get(key)
        if customer is None:
            customer = customers[key] = {
                "customer_code": header["customer_code"] or "",
                "customer_name": header["customer_name"] or "",
                "lines": 0,
                "quantity": Decimal(0),
                "value": Decimal(0),
                "returns": set(),
                "skus": set(),
                "conditions": defaultdict(Decimal),
            }
        customer["returns"].add(header["id"])

    # -- header-level cuts -----------------------------------------------------

    status_counts: dict[str, int] = defaultdict(int)
    basis_counts: dict[str, int] = defaultdict(int)
    arrived = awaiting = posted = pending_approval = 0
    for header in headers:
        status_counts[header["status"]] += 1
        basis_counts[header["basis"]] += 1
        if header["status"] in ARRIVED_STATUSES or header["gated_in_at"]:
            arrived += 1
        else:
            awaiting += 1
        if header["status"] == GoodsReturnStatus.POSTED:
            posted += 1
        if header["requires_approval"] and header["approval_status"] == "PENDING":
            pending_approval += 1

    # -- trend -----------------------------------------------------------------

    span_days = (end - start).days + 1
    by_day = span_days <= DAILY_TREND_MAX_DAYS
    qty_by_return: dict[int, Decimal] = defaultdict(Decimal)
    for line in lines:
        qty_by_return[line["goods_return_id"]] += Decimal(line["return_quantity"] or 0)

    # A line carries no date of its own -- it happened on its return's day -- so
    # the mapping is built once and both loops below read it.
    bucket_of_return = {
        header["id"]: _bucket_key(header["arrived_on"], by_day) for header in headers
    }

    trend_buckets: dict[str, dict] = {}
    for header in headers:
        key = bucket_of_return[header["id"]]
        bucket = trend_buckets.get(key)
        if bucket is None:
            bucket = trend_buckets[key] = {
                "bucket": key,
                "returns": 0,
                "quantity": Decimal(0),
                "damaged_quantity": Decimal(0),
            }
        bucket["returns"] += 1
        bucket["quantity"] += qty_by_return.get(header["id"], Decimal(0))

    for line in lines:
        if line["condition"] != GoodsReturnItemCondition.GOOD:
            key = bucket_of_return.get(line["goods_return_id"])
            if key in trend_buckets:
                trend_buckets[key]["damaged_quantity"] += Decimal(line["return_quantity"] or 0)

    trend = [
        {
            "bucket": row["bucket"],
            "returns": row["returns"],
            "quantity": _q(row["quantity"]),
            "damaged_quantity": _q(row["damaged_quantity"]),
        }
        for row in sorted(trend_buckets.values(), key=lambda r: r["bucket"])
    ]

    # -- the returns themselves, newest first ----------------------------------

    lines_per_return: dict[int, int] = defaultdict(int)
    for line in lines:
        lines_per_return[line["goods_return_id"]] += 1

    recent = sorted(headers, key=lambda h: h["arrived_on"], reverse=True)[:TOP_N * 2]
    recent_rows = [
        {
            "id": header["id"],
            "entry_no": header["entry_no"],
            "status": header["status"],
            "status_label": STATUS_LABELS.get(header["status"], header["status"]),
            "basis": header["basis"],
            "customer_code": header["customer_code"],
            "customer_name": header["customer_name"],
            "arrived_on": header["arrived_on"].isoformat() if header["arrived_on"] else None,
            # Which of the two dates `arrived_on` actually is, so the dashboard can
            # label a row "booked" rather than claim a truck arrived.
            "has_arrived": bool(header["gated_in_at"]),
            "lines": lines_per_return.get(header["id"], 0),
            "quantity": _q(qty_by_return.get(header["id"], 0)),
        }
        for header in recent
    ]

    # -- serialise -------------------------------------------------------------

    total_lines = len(lines)
    damaged_qty = sum(
        (data["quantity"] for key, data in by_condition.items() if key != GoodsReturnItemCondition.GOOD),
        Decimal(0),
    )

    def _top(source: dict, sort_key: str, shape) -> list[dict]:
        rows = sorted(source.values(), key=lambda r: r[sort_key], reverse=True)
        return [shape(row) for row in rows[:TOP_N]]

    top_skus = _top(
        skus,
        "quantity",
        lambda row: {
            "item_code": row["item_code"],
            "item_name": row["item_name"],
            "uom": row["uom"],
            "lines": row["lines"],
            "returns": len(row["returns"]),
            "customers": len(row["customers"]),
            "quantity": _q(row["quantity"]),
            "value": _money(row["value"]),
            "share": _pct(_q(row["quantity"]), _q(total_qty)),
            "conditions": {
                key: _q(row["conditions"].get(key, 0)) for key in CONDITION_LABELS
            },
            "reasons": {
                key: _q(row["reasons"].get(key, 0)) for key in REASON_ORDER if row["reasons"].get(key)
            },
        },
    )

    top_customers = _top(
        customers,
        "quantity",
        lambda row: {
            "customer_code": row["customer_code"],
            "customer_name": row["customer_name"] or row["customer_code"],
            "returns": len(row["returns"]),
            "lines": row["lines"],
            "skus": len(row["skus"]),
            "quantity": _q(row["quantity"]),
            "value": _money(row["value"]),
            "share": _pct(_q(row["quantity"]), _q(total_qty)),
            "conditions": {
                key: _q(row["conditions"].get(key, 0)) for key in CONDITION_LABELS
            },
        },
    )

    return {
        "window": {
            "from_date": start.isoformat(),
            "to_date": end.isoformat(),
            "days": span_days,
            "granularity": "day" if by_day else "month",
        },
        "totals": {
            "returns": len(headers),
            "arrived": arrived,
            "awaiting_arrival": awaiting,
            "posted": posted,
            "cancelled": cancelled_count,
            "pending_approval": pending_approval,
            "lines": total_lines,
            "quantity": _q(total_qty),
            # Only invoice-basis lines carry a price, so this is the value of the
            # returns that came back against a bill -- never the full picture.
            "value": _money(total_value),
            "customers": len(customers),
            "skus": len(skus),
            "damaged_quantity": _q(damaged_qty),
            "damaged_share": _pct(_q(damaged_qty), _q(total_qty)),
            # Quote this one for "how much leaked" -- either half alone
            # under-counts a window that spans the day LEAKED was added.
            "leaked_quantity": _q(leaked_recorded + leaked_inferred),
            "leaked_share": _pct(_q(leaked_recorded + leaked_inferred), _q(total_qty)),
            "leaked_recorded": _q(leaked_recorded),
            "leaked_inferred": _q(leaked_inferred),
        },
        "by_status": [
            {
                "status": key,
                "label": STATUS_LABELS.get(key, key),
                "returns": count,
                "share": _pct(count, len(headers)),
            }
            for key, count in sorted(status_counts.items(), key=lambda kv: -kv[1])
        ],
        "by_basis": [
            {
                "basis": key,
                "returns": count,
                "share": _pct(count, len(headers)),
            }
            for key, count in sorted(basis_counts.items(), key=lambda kv: -kv[1])
        ],
        "by_condition": [
            {
                "condition": key,
                "label": CONDITION_LABELS.get(key, key),
                "lines": data["lines"],
                "quantity": _q(data["quantity"]),
                "value": _money(data["value"]),
                "share": _pct(_q(data["quantity"]), _q(total_qty)),
            }
            for key, data in sorted(
                by_condition.items(), key=lambda kv: -kv[1]["quantity"]
            )
        ],
        "by_reason": [
            {
                "reason": key,
                "label": REASON_LABELS[key],
                "lines": by_reason[key]["lines"],
                "quantity": _q(by_reason[key]["quantity"]),
                "value": _money(by_reason[key]["value"]),
                "share": _pct(_q(by_reason[key]["quantity"]), _q(total_qty)),
            }
            for key in REASON_ORDER
        ],
        "trend": trend,
        "top_skus": top_skus,
        "top_customers": top_customers,
        "recent_returns": recent_rows,
    }
