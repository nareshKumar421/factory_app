"""Marketplace Gate Check — the out-gate verification before parcels leave.

After Outward scan + Confirm (delivery note posted), an order's parcels are physically
ready to go out. A gate person reviews them per SHEET — parcel count, buyer/destination,
items, DN, tracking IDs — and marks the sheet APPROVED (OK from gate) or HOLD (a problem).

Modelled on gate_core's status-driven gate check: the action records who / when / a
remark on each CONFIRMED dispatch (``gate_status``).
"""
from collections import defaultdict

from django.utils import timezone

from ..models import (
    MarketplaceDispatch,
    MarketplaceDispatchStatus,
    MarketplaceGateStatus,
    OrderImportBatch,
)
from .errors import MarketplaceError


def _confirmed(company, channel, batch_id=None):
    """CONFIRMED dispatches (parcels ready to leave), optionally for one sheet."""
    qs = (
        MarketplaceDispatch.objects
        .filter(company=company, channel=channel, status=MarketplaceDispatchStatus.CONFIRMED)
        .select_related("order", "order__import_batch", "gate_checked_by")
        .prefetch_related("order__lines")
    )
    if batch_id:
        qs = qs.filter(order__import_batch_id=batch_id)
    return qs


def _parcels(order):
    """Parcel count = distinct tracking IDs on the order (min 1 per order)."""
    tids = {(l.tracking_id or "").strip() for l in order.lines.all() if (l.tracking_id or "").strip()}
    return len(tids) or 1


def gate_queue(company, channel):
    """Sheets with CONFIRMED orders + parcel counts and gate-status breakdown — the
    gate person's work-list (each sheet is approved / held as a whole)."""
    dispatches = list(_confirmed(company, channel))
    by_batch = defaultdict(list)
    for d in dispatches:
        if d.order.import_batch_id:
            by_batch[d.order.import_batch_id].append(d)
    batches = {b.id: b for b in OrderImportBatch.objects.filter(id__in=list(by_batch))}

    sheets = []
    for bid, ds in by_batch.items():
        b = batches.get(bid)
        counts = {"PENDING": 0, "APPROVED": 0, "HOLD": 0}
        parcels = 0
        for d in ds:
            parcels += _parcels(d.order)
            counts[d.gate_status] = counts.get(d.gate_status, 0) + 1
        sheets.append({
            "batch_id": bid,
            "filename": b.filename if b else "",
            "created_at": b.created_at.isoformat() if b else None,
            "orders": len(ds),
            "parcels": parcels,
            "gate_pending": counts["PENDING"],
            "gate_approved": counts["APPROVED"],
            "gate_hold": counts["HOLD"],
        })
    sheets.sort(key=lambda s: s["created_at"] or "", reverse=True)
    return {
        "sheets": sheets,
        "total_sheets": len(sheets),
        "total_parcels": sum(s["parcels"] for s in sheets),
        "total_pending": sum(s["gate_pending"] for s in sheets),
    }


def sheet_gate_detail(company, channel, batch_id):
    """The confirmed orders in a sheet with everything the gate person checks."""
    ds = list(_confirmed(company, channel, batch_id).order_by("order__order_id"))
    if not ds:
        raise MarketplaceError(
            "No confirmed orders for this sheet.", code="NOT_FOUND", status_code=404)
    orders = []
    for d in ds:
        o = d.order
        lines = list(o.lines.all())
        tids = sorted({(l.tracking_id or "").strip() for l in lines if (l.tracking_id or "").strip()})
        items = defaultdict(float)
        for l in lines:
            items[l.sku_name or l.marketplace_sku] += float(l.ordered_quantity)
        orders.append({
            "dispatch_id": d.id,
            "order_id": o.order_id,
            "buyer_name": o.buyer_name,
            "city": o.city,
            "state": o.state,
            "parcels": len(tids) or 1,
            "tracking_ids": tids,
            "items": [{"name": k, "quantity": v} for k, v in items.items()],
            "dn_number": d.sap_delivery_note_num,
            "gate_status": d.gate_status,
            "gate_checked_by": d.gate_checked_by.full_name if d.gate_checked_by_id else "",
            "gate_checked_at": d.gate_checked_at.isoformat() if d.gate_checked_at else None,
            "gate_remarks": d.gate_remarks,
        })
    b = ds[0].order.import_batch
    return {
        "batch_id": batch_id,
        "filename": b.filename if b else "",
        "orders": orders,
        "total_orders": len(orders),
        "total_parcels": sum(o["parcels"] for o in orders),
    }


def _set_gate(company, channel, batch_id, *, status, from_statuses, user, remarks=""):
    ids = list(
        _confirmed(company, channel, batch_id)
        .filter(gate_status__in=from_statuses)
        .values_list("id", flat=True)
    )
    if not ids:
        return 0
    return MarketplaceDispatch.objects.filter(id__in=ids).update(
        gate_status=status, gate_checked_by=user, gate_checked_at=timezone.now(),
        gate_remarks=remarks, updated_by=user, updated_at=timezone.now(),
    )


def approve_sheet(company, channel, batch_id, *, user):
    """Approve out (OK from gate) all pending/held orders in the sheet."""
    n = _set_gate(
        company, channel, batch_id, status=MarketplaceGateStatus.APPROVED,
        from_statuses=[MarketplaceGateStatus.PENDING, MarketplaceGateStatus.HOLD], user=user)
    return {"approved": n}


def hold_sheet(company, channel, batch_id, *, user, remarks=""):
    """Hold at the gate (flag a problem) all pending/approved orders in the sheet."""
    n = _set_gate(
        company, channel, batch_id, status=MarketplaceGateStatus.HOLD,
        from_statuses=[MarketplaceGateStatus.PENDING, MarketplaceGateStatus.APPROVED],
        user=user, remarks=remarks)
    return {"held": n}
