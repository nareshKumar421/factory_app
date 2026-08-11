"""Pull orders from OMS into our mirror. Idempotent by construction.

Running this twice must not create a second copy of anything (Rule 8). Two things
guarantee that:

* Orders and lines are upserted on their OMS primary keys (``oms_order_id``,
  ``oms_line_id``), which never change.
* The watermark advances only to the highest ``updated_at`` we actually consumed,
  and is read back from the mirror rather than stored separately — so a crashed
  run resumes from the last order genuinely written, not from where it hoped to
  get to.

What this deliberately does NOT do: overwrite our own workflow ``state``. OMS owns
the order; we own what we have decided about it. A re-sync that reset an allocated
order back to RECEIVED would lose real work.
"""
import logging
import uuid

from django.db import transaction
from django.utils import timezone

from ..integrations.oms import mapper, reader
from ..integrations.oms.reader import OmsUnavailable
from ..models import (
    OmsOrder,
    OmsOrderLine,
    OmsSyncRun,
    OrderState,
    ProcessingEvent,
    SyncStatus,
)

logger = logging.getLogger(__name__)

# Fields OMS owns. Refreshed on every sync; `state` is pointedly not among them.
ORDER_MIRROR_FIELDS = (
    "order_number", "customer_code", "customer_name", "company_code",
    "branch_bpl_id", "branch_name", "oms_status", "order_type", "po_number",
    "ship_to_address", "total_amount", "is_foc", "remarks", "delivery_date",
    "delivery_date_raw", "sap_created", "sap_doc_number", "quotation_cancelled",
    "oms_created_at", "oms_updated_at",
)

LINE_MIRROR_FIELDS = (
    "item_code", "item_name", "category", "brand", "sub_group", "quantity",
    "pack_size", "cases", "litres", "scheme_quantity", "unit_price", "line_total",
    "warehouse_code", "issues",
)


def current_watermark():
    """Highest ``oms_updated_at`` we have actually stored.

    Read from the mirror rather than kept in a counter: the two can disagree after
    a crash, and the mirror is the one that is true.
    """
    latest = OmsOrder.objects.order_by("-oms_updated_at").values_list(
        "oms_updated_at", flat=True
    ).first()
    return latest


def log_event(event, *, correlation_id, entity_type="", entity_id="", source="OMS",
              actor="", old_state="", new_state="", result="OK", detail=None, error=""):
    return ProcessingEvent.objects.create(
        correlation_id=correlation_id, event=event, entity_type=entity_type,
        entity_id=str(entity_id), source=source, actor=actor, old_state=old_state,
        new_state=new_state, result=result, detail=detail or {}, error=error,
    )


@transaction.atomic
def _write_order(order_row, line_rows, run):
    """Upsert one order and its lines. Returns ``(created, lines, issues)``."""
    data = mapper.map_order(order_row)
    oms_id = data.pop("oms_order_id")

    order, created = OmsOrder.objects.get_or_create(
        oms_order_id=oms_id,
        defaults={**data, "state": OrderState.RECEIVED, "last_sync_run": run},
    )
    if not created:
        for field in ORDER_MIRROR_FIELDS:
            setattr(order, field, data[field])
        order.last_sync_run = run
        order.save(update_fields=list(ORDER_MIRROR_FIELDS) + ["last_sync_run", "last_synced_at"])

    # An order cancelled in OMS is cancelled here too — that IS OMS's decision to
    # make, unlike the rest of our workflow state.
    if order.quotation_cancelled and order.state != OrderState.CANCELLED:
        previous, order.state = order.state, OrderState.CANCELLED
        order.save(update_fields=["state"])
        log_event("ORDER_CANCELLED", correlation_id=run.pk and str(run.pk) or "",
                  entity_type="OmsOrder", entity_id=order.oms_order_id,
                  old_state=previous, new_state=order.state)

    issues = 0
    seen_line_ids = []
    for raw in line_rows:
        line_data = mapper.map_line(raw)
        line_id = line_data.pop("oms_line_id")
        seen_line_ids.append(line_id)
        issues += len(line_data["issues"])
        OmsOrderLine.objects.update_or_create(
            oms_line_id=line_id, defaults={**line_data, "order": order},
        )

    # A line removed in OMS must disappear here, or we keep planning for demand
    # that no longer exists.
    OmsOrderLine.objects.filter(order=order).exclude(oms_line_id__in=seen_line_ids).delete()
    return created, len(seen_line_ids), issues


def sync_orders(*, since=None, limit=None, statuses=None, order_ids=None,
                actor="", full=False):
    """Pull changed orders from OMS. Returns the :class:`OmsSyncRun`.

    ``full=True`` ignores the watermark and re-reads everything — safe at any time
    precisely because the upsert is idempotent.
    """
    correlation_id = uuid.uuid4().hex
    watermark = None if full else (since or current_watermark())
    # A FILTERED pull must never advance the shared watermark. It sees only part
    # of the window, so moving the mark past the rest permanently hides every
    # order the filter excluded — 69 REJECTED orders went missing exactly this way
    # after one `--status COMPLETED` run, and only reconciliation found them.
    narrowed = bool(statuses or order_ids or limit)
    run = OmsSyncRun.objects.create(watermark_from=watermark, triggered_by=actor or "system")

    log_event("SYNC_STARTED", correlation_id=correlation_id, entity_type="OmsSyncRun",
              entity_id=run.pk, source="SYSTEM", actor=actor,
              detail={"since": watermark.isoformat() if watermark else None, "full": full})

    try:
        order_rows = reader.fetch_orders(
            since=watermark, limit=limit, statuses=statuses, order_ids=order_ids
        )
    except OmsUnavailable as exc:
        run.status, run.error, run.finished_at = SyncStatus.FAILED, str(exc), timezone.now()
        run.save(update_fields=["status", "error", "finished_at"])
        log_event("SYNC_FAILED", correlation_id=correlation_id, entity_type="OmsSyncRun",
                  entity_id=run.pk, source="OMS", result="FAILED", error=str(exc))
        raise

    if not order_rows:
        run.status, run.finished_at = SyncStatus.SUCCESS, timezone.now()
        run.watermark_to = watermark
        run.save(update_fields=["status", "finished_at", "watermark_to"])
        log_event("SYNC_FINISHED", correlation_id=correlation_id, entity_type="OmsSyncRun",
                  entity_id=run.pk, source="SYSTEM", detail={"orders": 0})
        return run

    ids = [r["id"] for r in order_rows]
    try:
        all_lines = reader.fetch_lines(ids)
    except OmsUnavailable as exc:
        run.status, run.error, run.finished_at = SyncStatus.FAILED, str(exc), timezone.now()
        run.save(update_fields=["status", "error", "finished_at"])
        log_event("SYNC_FAILED", correlation_id=correlation_id, entity_type="OmsSyncRun",
                  entity_id=run.pk, source="OMS", result="FAILED", error=str(exc))
        raise

    lines_by_order = {}
    for row in all_lines:
        lines_by_order.setdefault(row["order_id"], []).append(row)

    created_count = updated_count = line_count = issue_count = 0
    highest = watermark
    for row in order_rows:
        try:
            created, lines, issues = _write_order(row, lines_by_order.get(row["id"], []), run)
        except Exception as exc:  # noqa: BLE001 — one bad order must not stop the sync
            logger.warning("Order %s failed to sync: %s", row.get("id"), exc)
            log_event("ORDER_SYNC_FAILED", correlation_id=correlation_id,
                      entity_type="OmsOrder", entity_id=row.get("id"),
                      result="FAILED", error=str(exc))
            continue
        created_count += int(created)
        updated_count += int(not created)
        line_count += lines
        issue_count += issues
        # Advance only past orders actually written, so a failure mid-run is
        # retried. Made aware first: OMS's column is `timestamp WITHOUT time zone`
        # and the running watermark is aware, so comparing them raw raises.
        seen_at = mapper.to_aware(row.get("updated_at"))
        if seen_at and (highest is None or seen_at > highest):
            highest = seen_at

    run.status = SyncStatus.SUCCESS
    run.finished_at = timezone.now()
    run.watermark_to = watermark if narrowed else highest
    run.orders_seen = len(order_rows)
    run.orders_created = created_count
    run.orders_updated = updated_count
    run.lines_written = line_count
    run.issues_found = issue_count
    run.save()

    log_event("SYNC_FINISHED", correlation_id=correlation_id, entity_type="OmsSyncRun",
              entity_id=run.pk, source="SYSTEM", actor=actor,
              detail={"orders": len(order_rows), "created": created_count,
                      "updated": updated_count, "lines": line_count,
                      "issues": issue_count})
    return run
