"""The order processing engine: validate → check stock → decide → record.

    RECEIVED → VALIDATED → STOCK_CHECKED → ┬ READY_FOR_FULFILLMENT
                                           ├ PARTIALLY_AVAILABLE
                                           └ PRODUCTION_REQUIRED

**Idempotent** (Rule 8). Processing the same order twice must not create a second
production requirement. That is guaranteed structurally rather than by a guard
flag: requirements are keyed on (item, warehouse) and a line's contribution is
keyed on the line, so re-processing *replaces* a contribution instead of adding
one. Re-running after stock has moved therefore corrects the requirement rather
than inflating it — which is what a daily re-check needs to do.

**Requirements are shared.** Three orders short of the same SKU are one thing to
produce, not three. The requirement's quantity is the sum of its sources, so it
shrinks correctly when one order is cancelled or its stock arrives.

Nothing here writes to SAP or to OMS.
"""
import logging
import uuid

from django.db import transaction
from django.utils import timezone

from ..models import (
    OmsOrder,
    OrderState,
    ProductionRequirement,
    RequirementSource,
    RequirementStatus,
    StockCheck,
    StockCheckLine,
)
from . import availability
from .order_sync import log_event

logger = logging.getLogger(__name__)


def _record_check(order, result, *, actor="", correlation_id=""):
    """Persist the availability answer so a later decision stays explainable."""
    check = StockCheck.objects.create(
        order=order, checked_by=actor, sap_company=result.sap_company,
        verdict=result.verdict, total_short=result.total_short,
        errors=list(dict.fromkeys(result.errors)), correlation_id=correlation_id,
    )
    by_line_id = {line.oms_line_id: line for line in order.lines.all()}
    StockCheckLine.objects.bulk_create([
        StockCheckLine(
            stock_check=check, line=by_line_id.get(entry.line_id),
            item_code=entry.item_code, warehouse_code=entry.warehouse_code,
            required=entry.required, on_hand=entry.on_hand,
            committed_in_sap=entry.committed_in_sap, local_demand=entry.local_demand,
            available=entry.available, available_in_group=entry.available_in_group,
            elsewhere={k: str(v) for k, v in entry.elsewhere.items()},
            allocatable=entry.allocatable, short=entry.short,
            verdict=entry.verdict, notes=entry.notes,
        )
        for entry in result.lines
    ])
    return check


@transaction.atomic
def _apply_requirements(order, result, *, correlation_id=""):
    """Create, update or retire this order's contribution to production requirements.

    Keyed on the LINE, so re-processing replaces rather than accumulates. A line
    that is no longer short has its contribution removed, and a requirement left
    with no sources is retired — otherwise yesterday's shortage would keep the
    factory making something nobody needs.
    """
    created, updated, retired = 0, 0, 0
    shortfalls = {entry.line_id: entry for entry in result.lines if entry.short > 0}

    # Drop contributions from lines that are no longer short.
    stale = RequirementSource.objects.filter(order=order).exclude(
        line__oms_line_id__in=list(shortfalls)
    )
    touched = {s.requirement_id for s in stale}
    retired += stale.count()
    stale.delete()

    for line in order.lines.all():
        entry = shortfalls.get(line.oms_line_id)
        if entry is None:
            continue
        requirement, was_created = ProductionRequirement.objects.get_or_create(
            item_code=entry.item_code,
            warehouse_code=entry.warehouse_code,
            status__in=[RequirementStatus.REQUIRED, RequirementStatus.PLANNED],
            defaults={
                "item_name": line.item_name, "sap_company": result.sap_company,
                "status": RequirementStatus.REQUIRED,
            },
        )
        created += int(was_created)
        source, source_created = RequirementSource.objects.update_or_create(
            requirement=requirement, line=line,
            defaults={"order": order, "shortfall": entry.short},
        )
        updated += int(not source_created)
        touched.add(requirement.pk)

    # Recompute every requirement we touched from its sources — the sum is the
    # truth, and deriving it avoids drift between the parts and the whole.
    for requirement in ProductionRequirement.objects.filter(pk__in=touched):
        sources = list(requirement.sources.select_related("order"))
        if not sources:
            if requirement.status == RequirementStatus.REQUIRED:
                requirement.status = RequirementStatus.CANCELLED
                requirement.notes = "No order still needs this."
                requirement.save(update_fields=["status", "notes", "updated_at"])
                log_event("REQUIREMENT_RETIRED", correlation_id=correlation_id,
                          entity_type="ProductionRequirement", entity_id=requirement.pk,
                          source="SYSTEM", detail={"item": requirement.item_code})
            continue
        total = sum((s.shortfall for s in sources), start=type(sources[0].shortfall)(0))
        dates = [s.order.delivery_date for s in sources if s.order.delivery_date]
        requirement.quantity = total
        requirement.needed_by = min(dates) if dates else None
        requirement.save(update_fields=["quantity", "needed_by", "updated_at"])

    return created, updated, retired


def process_order(order, *, actor="", correlation_id=None):
    """Run one order through the engine. Returns ``(order, check, result)``."""
    correlation_id = correlation_id or uuid.uuid4().hex
    previous = order.state

    if not order.is_demand:
        # Rejected or cancelled orders must not consume stock or raise production.
        # Retire anything they were previously driving.
        _apply_requirements(order, availability.OrderAvailability(
            order_id=order.oms_order_id, order_number=order.order_number,
            company_code=order.company_code, checked_at=timezone.now(),
        ), correlation_id=correlation_id)
        if order.state != OrderState.CANCELLED:
            order.state = OrderState.CANCELLED
            order.save(update_fields=["state"])
        log_event("ORDER_SKIPPED", correlation_id=correlation_id, entity_type="OmsOrder",
                  entity_id=order.oms_order_id, source="SYSTEM", actor=actor,
                  old_state=previous, new_state=order.state, result="SKIPPED",
                  detail={"reason": f"status {order.oms_status}"})
        return order, None, None

    result = availability.check_order(order)
    check = _record_check(order, result, actor=actor, correlation_id=correlation_id)
    created, updated, retired = _apply_requirements(
        order, result, correlation_id=correlation_id
    )

    # The state answers "what has to happen next", not "how much is on the shelf".
    # PARTIAL always means something is short, so it lands on PRODUCTION_REQUIRED
    # alongside SHORT -- calling it PARTIALLY_AVAILABLE would read as reassuring
    # while a requirement sits unmade underneath it. How much IS available is on
    # the stored check, where it belongs.
    if result.verdict == availability.Verdict.AVAILABLE:
        order.state = OrderState.READY_FOR_FULFILLMENT
    elif result.verdict == availability.Verdict.UNKNOWN:
        # An answer we could not get is not a decision. Leaving it STOCK_CHECKED
        # keeps it in the queue instead of quietly declaring it fulfillable.
        order.state = OrderState.STOCK_CHECKED
    else:
        order.state = OrderState.PRODUCTION_REQUIRED
    order.save(update_fields=["state"])

    log_event("ORDER_PROCESSED", correlation_id=correlation_id, entity_type="OmsOrder",
              entity_id=order.oms_order_id, source="SYSTEM", actor=actor,
              old_state=previous, new_state=order.state,
              detail={"verdict": result.verdict, "short": str(result.total_short),
                      "requirements_created": created, "requirements_updated": updated,
                      "sources_retired": retired, "check_id": check.pk})
    return order, check, result


def process_pending(limit=None, *, actor=""):
    """Process the pending queue. Returns a per-verdict tally."""
    correlation_id = uuid.uuid4().hex
    tally = {}
    for order in availability.pending_orders(limit=limit):
        try:
            _order, _check, result = process_order(
                order, actor=actor, correlation_id=correlation_id
            )
        except Exception as exc:  # noqa: BLE001 — one order must not stop the queue
            logger.warning("Order %s failed to process: %s", order.oms_order_id, exc)
            log_event("ORDER_PROCESS_FAILED", correlation_id=correlation_id,
                      entity_type="OmsOrder", entity_id=order.oms_order_id,
                      result="FAILED", error=str(exc))
            tally["FAILED"] = tally.get("FAILED", 0) + 1
            continue
        key = result.verdict if result else "SKIPPED"
        tally[key] = tally.get(key, 0) + 1
    return tally


def open_requirements():
    return (ProductionRequirement.objects
            .filter(status__in=[RequirementStatus.REQUIRED, RequirementStatus.PLANNED])
            .prefetch_related("sources__order")
            .order_by("needed_by", "-quantity"))
