"""Reconciliation — does our mirror still match the systems it came from?

The specification asks for this between OMS ↔ us and SAP ↔ us, and it is not
ceremony. Three systems hold overlapping truth, the mirror is a copy, and a copy
drifts silently: nobody notices a missing order until someone asks why it was
never made.

What is checked, and why each one can actually happen:

* **Orders in OMS but not here** — a sync that failed mid-run, or a watermark that
  advanced past an order that errored. The most consequential gap: demand nobody
  is planning for.
* **Orders here but no longer in OMS** — deleted or archived upstream. We keep
  planning for them until told otherwise.
* **Line counts that disagree** — a line added or removed in OMS after our copy.
* **Status drift** — the mirrored status no longer matches OMS, usually an order
  that moved on while we were not looking.
* **`sap_created` disagreeing with the payload logs** — an order OMS believes it
  pushed but with no successful log, or the reverse.

Read-only against both systems. Reports; never repairs. A reconciliation that
silently fixes things hides the fault that caused the drift.
"""
import logging
from collections import Counter

from django.db.models import Count

from ..integrations.oms import reader
from ..integrations.oms.reader import OmsUnavailable
from ..models import OmsOrder, OmsOrderLine
from .order_sync import log_event

logger = logging.getLogger(__name__)


def reconcile_orders(*, since=None, limit=None, correlation_id=""):
    """Compare the mirror against OMS. Returns a report dict.

    ``since`` narrows the window; without it the whole mirror is compared, which
    is the honest default — drift you only look for in the last week is drift you
    will find a week late.
    """
    try:
        oms_rows = reader.fetch_orders(since=since, limit=limit)
    except OmsUnavailable as exc:
        log_event("RECONCILE_FAILED", correlation_id=correlation_id,
                  entity_type="Reconciliation", source="OMS", result="FAILED", error=str(exc))
        return {"ok": False, "error": str(exc)}

    oms_by_id = {row["id"]: row for row in oms_rows}
    mirrored = {
        o.oms_order_id: o
        for o in OmsOrder.objects.filter(oms_order_id__in=list(oms_by_id) or [0])
    }

    # Only meaningful when the whole set was pulled: with a limit or a watermark,
    # "here but not in OMS" would flag every order outside the window.
    full_scan = since is None and not limit
    ours = set(OmsOrder.objects.values_list("oms_order_id", flat=True)) if full_scan else set()

    missing_here, missing_there, status_drift, line_drift, sap_drift = [], [], [], [], []

    line_counts = {
        row["order__oms_order_id"]: row["n"]
        for row in OmsOrderLine.objects.values("order__oms_order_id").annotate(n=Count("id"))
    }
    oms_line_counts = Counter()
    try:
        for row in reader.fetch_lines(list(oms_by_id)):
            oms_line_counts[row["order_id"]] += 1
    except OmsUnavailable:
        oms_line_counts = None   # counted as unknown rather than as zero

    for oms_id, row in oms_by_id.items():
        order = mirrored.get(oms_id)
        if order is None:
            missing_here.append({
                "oms_order_id": oms_id, "order_number": row.get("order_number"),
                "status": row.get("status_code"), "updated_at": str(row.get("updated_at")),
            })
            continue
        if (row.get("status_code") or "") != order.oms_status:
            status_drift.append({
                "oms_order_id": oms_id, "order_number": order.order_number,
                "ours": order.oms_status, "oms": row.get("status_code"),
            })
        if bool(row.get("sap_created")) != order.sap_created:
            sap_drift.append({
                "oms_order_id": oms_id, "order_number": order.order_number,
                "ours": order.sap_created, "oms": bool(row.get("sap_created")),
            })
        if oms_line_counts is not None:
            here, there = line_counts.get(oms_id, 0), oms_line_counts.get(oms_id, 0)
            if here != there:
                line_drift.append({
                    "oms_order_id": oms_id, "order_number": order.order_number,
                    "ours": here, "oms": there,
                })

    if full_scan:
        for oms_id in sorted(ours - set(oms_by_id)):
            order = OmsOrder.objects.filter(oms_order_id=oms_id).first()
            missing_there.append({
                "oms_order_id": oms_id,
                "order_number": order.order_number if order else "",
            })

    report = {
        "ok": True,
        "compared": len(oms_by_id),
        "full_scan": full_scan,
        "lines_checked": oms_line_counts is not None,
        "missing_here": missing_here,
        "missing_in_oms": missing_there,
        "status_drift": status_drift,
        "line_drift": line_drift,
        "sap_created_drift": sap_drift,
        "clean": not (missing_here or missing_there or status_drift
                      or line_drift or sap_drift),
    }
    log_event("RECONCILED", correlation_id=correlation_id, entity_type="Reconciliation",
              source="SYSTEM",
              detail={k: (len(v) if isinstance(v, list) else v)
                      for k, v in report.items() if k != "ok"})
    return report
