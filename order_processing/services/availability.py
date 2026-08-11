"""Can we fulfil this order? — the first genuinely useful answer in the module.

Per line: what is required, what SAP has free, how much of it we can promise, and
what is short. Nothing is written to SAP and nothing is reserved here; this is a
read that produces a verdict.

The one subtlety worth understanding, because getting it wrong makes every number
wrong in one direction or the other:

    available_to_promise = SAP(OnHand - IsCommited) - demand SAP does not know about

``IsCommited`` already covers every OMS order that reached SAP, because OMS posts
Sales Orders. Subtracting those again would double-count them and invent a
shortage. But the ~273 orders with ``sap_created = false`` are demand SAP has
never been told about, and ignoring *those* would promise the same stock twice.
So exactly one of the two is netted off locally, and which one is decided by
``sap_created`` — not by a guess.

Unknown is carried through as unknown. A line whose warehouse is missing, whose
item SAP has never stocked, or whose stock read failed is reported as
``UNKNOWN``, never as zero — a zero would read as "make more of it".
"""
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from django.conf import settings
from django.db.models import Q, Sum

from ..integrations.sap import inventory
from ..models import LineIssue, OmsOrder, OmsOrderLine

logger = logging.getLogger(__name__)

ZERO = Decimal("0")


class Verdict:
    AVAILABLE = "AVAILABLE"          # everything the line needs is free
    PARTIAL = "PARTIAL"              # some of it is
    SHORT = "SHORT"                  # none of it is
    UNKNOWN = "UNKNOWN"              # we could not tell, and will not pretend


@dataclass
class LineAvailability:
    line_id: int
    item_code: str
    item_name: str
    warehouse_code: str
    required: Decimal = ZERO
    on_hand: Decimal = ZERO
    committed_in_sap: Decimal = ZERO
    local_demand: Decimal = ZERO
    available: Decimal = ZERO
    allocatable: Decimal = ZERO
    short: Decimal = ZERO
    verdict: str = Verdict.UNKNOWN
    notes: list = field(default_factory=list)
    # Where the goods actually are. GP-FG -- the warehouse OMS books against --
    # runs near-empty while Bahadurgarh holds the stock, so the booking warehouse
    # alone answers "is it here?" and not "can we supply it?".
    elsewhere: dict = field(default_factory=dict)
    available_in_group: Decimal = ZERO


@dataclass
class OrderAvailability:
    order_id: int
    order_number: str
    company_code: str
    sap_company: str = ""
    lines: list = field(default_factory=list)
    checked_at: object = None
    errors: list = field(default_factory=list)

    @property
    def verdict(self):
        """The order's verdict is the worst of its lines — a shipment is not
        partially dispatchable just because one line is fine."""
        verdicts = {line.verdict for line in self.lines}
        if not verdicts or Verdict.UNKNOWN in verdicts:
            return Verdict.UNKNOWN
        if verdicts == {Verdict.AVAILABLE}:
            return Verdict.AVAILABLE
        if Verdict.AVAILABLE in verdicts or Verdict.PARTIAL in verdicts:
            return Verdict.PARTIAL
        return Verdict.SHORT

    @property
    def total_short(self):
        return sum((line.short for line in self.lines), ZERO)


def sourcing_warehouses(booking_warehouse):
    """Warehouses that may supply an order booked against this one.

    Configuration, not inference: whether stock in another warehouse can serve an
    order implies a transfer, and that is an operational decision. Empty means
    "only the booking warehouse", which is strictly what SAP says.
    """
    mapping = getattr(settings, "OMS_WAREHOUSE_SOURCING", {}) or {}
    return [w for w in mapping.get(booking_warehouse, []) if w != booking_warehouse]


def sap_company_for(company_code, category):
    """Which SAP company database to ask.

    Deliberately a lookup, not logic: OMS's ``company`` holds ``'1'`` (Jivo
    Wellness) or ``'2'`` (Jivo Mart), and company 1 carries BOTH OIL and BEVERAGES
    lines — so the company code alone cannot pick the database. Category is
    consulted first for that reason. Returns "" when neither resolves, and the
    caller reports UNKNOWN rather than guessing into the wrong company's stock.
    """
    by_category = getattr(settings, "OMS_CATEGORY_SAP_COMPANY", {}) or {}
    resolved = by_category.get((category or "").strip().upper())
    if resolved:
        return resolved
    by_company = getattr(settings, "OMS_COMPANY_SAP_COMPANY", {}) or {}
    return by_company.get((company_code or "").strip(), "")


def unpushed_demand(item_codes, warehouse_code, *, exclude_order_id=None):
    """Demand from orders SAP has NOT been told about, per item.

    Only ``sap_created = false`` orders count. Everything else is already inside
    ``IsCommited``, and counting it here as well would manufacture a shortage out
    of nothing.
    """
    if not item_codes or not warehouse_code:
        return {}

    demand_states = list(settings.OMS_SHIPPING_STATUSES) + list(settings.OMS_PIPELINE_STATUSES)
    qs = OmsOrderLine.objects.filter(
        item_code__in=list(item_codes),
        warehouse_code=warehouse_code,
        order__sap_created=False,
        order__quotation_cancelled=False,
        order__oms_status__in=demand_states,
    ).exclude(order__state="CANCELLED")
    if exclude_order_id is not None:
        qs = qs.exclude(order__oms_order_id=exclude_order_id)

    rows = qs.values("item_code").annotate(total=Sum("quantity"))
    return {r["item_code"]: Decimal(r["total"] or 0) for r in rows}


def check_order(order, *, include_unpushed=True):
    """Availability for one order. Reads SAP; writes nothing.

    Lines are grouped by (SAP company, warehouse) so each distinct pair costs one
    SAP round trip rather than one per line — an order of 20 lines in one
    warehouse should not be 20 queries.
    """
    from django.utils import timezone

    result = OrderAvailability(
        order_id=order.oms_order_id, order_number=order.order_number,
        company_code=order.company_code, checked_at=timezone.now(),
    )
    lines = list(order.lines.all())
    if not lines:
        result.errors.append("Order has no lines.")
        return result

    groups = defaultdict(list)
    for line in lines:
        sap_company = sap_company_for(order.company_code, line.category)
        groups[(sap_company, line.warehouse_code)].append(line)

    for (sap_company, warehouse), group in groups.items():
        result.sap_company = result.sap_company or sap_company

        if not sap_company or not warehouse:
            # Nowhere to look. Report it per line rather than failing the order:
            # the rest of the order may be perfectly answerable.
            reason = ("No SAP company resolved for this line's category/company."
                      if not sap_company else
                      "No warehouse rule for this category — OMS sends none for it.")
            for line in group:
                result.lines.append(LineAvailability(
                    line_id=line.oms_line_id, item_code=line.item_code,
                    item_name=line.item_name, warehouse_code=warehouse,
                    required=Decimal(line.quantity), verdict=Verdict.UNKNOWN,
                    notes=[reason],
                ))
            result.errors.append(reason)
            continue

        codes = [line.item_code for line in group]
        snapshot = inventory.fetch_stock(sap_company, codes, warehouse)
        # One extra read per supply warehouse, not per line.
        supply = {
            other: inventory.fetch_stock(sap_company, codes, other)
            for other in sourcing_warehouses(warehouse)
        }
        local = unpushed_demand(
            codes, warehouse, exclude_order_id=order.oms_order_id
        ) if include_unpushed else {}
        if not snapshot.ok:
            result.errors.append(snapshot.error)

        for line in group:
            stock = snapshot.get(line.item_code)
            required = Decimal(line.quantity)
            entry = LineAvailability(
                line_id=line.oms_line_id, item_code=line.item_code,
                item_name=line.item_name, warehouse_code=warehouse, required=required,
            )

            # A line we already know is untrustworthy stays untrustworthy: its
            # quantity may be in the wrong unit entirely (see the two OMS
            # quantity conventions), so an availability answer would be fiction.
            if LineIssue.QTY_DISAGREES.value in (line.issues or []):
                entry.verdict = Verdict.UNKNOWN
                entry.notes.append(
                    "Quantity is inconsistent with cases x pack size — "
                    "resolve the line before trusting any stock answer."
                )
                result.lines.append(entry)
                continue

            if not snapshot.ok or not stock.known:
                entry.verdict = Verdict.UNKNOWN
                entry.notes.append(
                    snapshot.error or f"SAP has no stock record for {line.item_code} in {warehouse}."
                )
                result.lines.append(entry)
                continue

            entry.on_hand = stock.on_hand
            entry.committed_in_sap = stock.committed
            entry.local_demand = local.get(line.item_code, ZERO)
            # The heart of it: SAP's own availability, less demand SAP has never
            # heard about. Floored, because "less than nothing" is still nothing.
            entry.available = max(stock.available - entry.local_demand, ZERO)
            entry.allocatable = min(required, entry.available)
            entry.short = max(required - entry.available, ZERO)

            if required <= 0:
                entry.verdict = Verdict.UNKNOWN
                entry.notes.append("Line has no quantity.")
            elif entry.short <= 0:
                entry.verdict = Verdict.AVAILABLE
            elif entry.allocatable > 0:
                entry.verdict = Verdict.PARTIAL
            else:
                entry.verdict = Verdict.SHORT

            # What the rest of the group holds, so a "short here" order can still
            # show that the goods exist a warehouse away.
            entry.elsewhere = {
                name: snap.get(line.item_code).available
                for name, snap in supply.items()
                if snap.ok and snap.get(line.item_code).known
                and snap.get(line.item_code).available > 0
            }
            entry.available_in_group = entry.available + sum(
                entry.elsewhere.values(), ZERO
            )
            if entry.elsewhere and entry.short > 0:
                where = ", ".join(f"{n}:{q}" for n, q in entry.elsewhere.items())
                entry.notes.append(f"Available elsewhere — {where}.")

            if entry.local_demand > 0:
                entry.notes.append(
                    f"{entry.local_demand} reserved for orders not yet in SAP."
                )
            result.lines.append(entry)

    return result


def check_orders(orders, **kwargs):
    return [check_order(order, **kwargs) for order in orders]


def pending_orders(limit=None):
    """Orders worth checking: real demand, not yet resolved.

    Excludes cancelled and rejected outright — counting those makes the factory
    look permanently short, which is the fastest way to get the whole system
    ignored.
    """
    demand_states = list(settings.OMS_SHIPPING_STATUSES) + list(settings.OMS_PIPELINE_STATUSES)
    qs = (OmsOrder.objects
          .filter(oms_status__in=demand_states, quotation_cancelled=False)
          .exclude(state__in=["CANCELLED", "FULFILLED"])
          .prefetch_related("lines")
          .order_by("-oms_created_at"))
    return qs[:limit] if limit else qs
