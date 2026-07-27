"""Warehouse-side insights for the marketplace issue flow.

Answers the questions the godown/packing team cares about:
  * pipeline — how many issue requests in each state
  * how much did we ISSUE vs how much has been DISPATCHED (per item), and how much
    is therefore still sitting in packing (issued − dispatched)
  * short-issued items (approved < required)
  * orders awaiting dispatch (issued but not yet shipped)

Quantities are returned as strings (like the rest of the marketplace API).
"""
from collections import OrderedDict
from decimal import Decimal

from django.db.models import Count

from ..models import (
    MarketplaceIssueLine,
    MarketplaceIssueRequest,
    MarketplaceOrder,
    MarketplaceOrderStatus,
)
from .dispatch_gate import READY_ISSUE_STATUSES, issued_subquery
from .resolve_service import load_mappings, resolve_order

_NUMERIC = ("required", "approved", "issued", "received", "dispatched", "in_packing")


def _s(value):
    """Format a Decimal: integers with no decimals ('0', '3'), else trimmed ('1.5')."""
    if value == value.to_integral_value():
        return str(int(value))
    return format(value, "f").rstrip("0").rstrip(".")


def build(company, channel):
    reqs = MarketplaceIssueRequest.objects.filter(company=company, channel=channel)
    by_status = {r["status"]: r["n"] for r in reqs.values("status").annotate(n=Count("id"))}

    agg = OrderedDict()  # (item_code_upper, type) -> row dict of Decimals

    def row(code, name, ctype):
        key = (code.strip().upper(), ctype)
        if key not in agg:
            agg[key] = {
                "item_code": code, "item_name": name or "", "component_type": ctype,
                "required": Decimal("0"), "approved": Decimal("0"), "issued": Decimal("0"),
                "received": Decimal("0"), "dispatched": Decimal("0"),
            }
        return agg[key]

    # ISSUED / RECEIVED requests → what the warehouse actually gave out.
    issued_lines = MarketplaceIssueLine.objects.filter(
        request__company=company, request__channel=channel,
        request__status__in=READY_ISSUE_STATUSES,
    ).values("item_code", "item_name", "component_type",
             "required_qty", "approved_qty", "issued_qty", "received_qty")
    for l in issued_lines:
        r = row(l["item_code"], l["item_name"], l["component_type"])
        r["required"] += l["required_qty"]
        r["approved"] += l["approved_qty"]
        r["issued"] += l["issued_qty"]
        r["received"] += l["received_qty"]

    # DISPATCHED orders → what actually shipped (resolve each to component items).
    mappings = load_mappings(company, channel)
    dispatched_orders = MarketplaceOrder.objects.filter(
        company=company, channel=channel, status=MarketplaceOrderStatus.DISPATCHED
    ).prefetch_related("lines")
    for order in dispatched_orders:
        for rl in resolve_order(order, mappings)["resolved_lines"]:
            r = row(rl["item_code"], rl["item_name"], rl["component_type"])
            r["dispatched"] += rl["required_quantity"]

    by_item = []
    totals = {k: Decimal("0") for k in ("required", "approved", "issued", "received", "dispatched")}
    shortfalls = []
    for r in agg.values():
        r["in_packing"] = r["issued"] - r["dispatched"]
        for k in totals:
            totals[k] += r[k]
        if r["approved"] < r["required"]:
            shortfalls.append({
                "item_code": r["item_code"], "item_name": r["item_name"],
                "required": _s(r["required"]), "approved": _s(r["approved"]),
                "short": _s(r["required"] - r["approved"]),
            })
        by_item.append({**{k: r[k] for k in ("item_code", "item_name", "component_type")},
                        **{k: _s(r[k]) for k in _NUMERIC}})

    # Order pipeline: issued-but-not-yet-shipped vs shipped.
    ready_qs = MarketplaceOrder.objects.filter(company=company, channel=channel).annotate(
        _ready=issued_subquery()
    )
    awaiting = ready_qs.filter(
        _ready=True, status=MarketplaceOrderStatus.OPEN, is_cancelled=False
    ).count()
    dispatched_count = MarketplaceOrder.objects.filter(
        company=company, channel=channel, status=MarketplaceOrderStatus.DISPATCHED
    ).count()

    by_item.sort(key=lambda x: (x["component_type"], x["item_code"]))
    return {
        "requests": {"total": reqs.count(), "by_status": by_status},
        "orders": {"awaiting_dispatch": awaiting, "dispatched": dispatched_count},
        "totals": {k: _s(v) for k, v in totals.items()},
        "by_item": by_item,
        "shortfalls": shortfalls,
    }
