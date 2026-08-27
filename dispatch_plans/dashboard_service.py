"""
dispatch_plans/dashboard_service.py

Read-only aggregation service for the Dispatch Fulfilment dashboard.

Design (event-anchored, cross-company):
  - The window is anchored on the ACTUAL dispatch event — a truck's
    ``gate_out_date`` — so a daily view matches the sales-dispatch section.
    (Plan ``dispatch_date`` is only a *scheduled* date and lags the real
    gate-out by 1-4 days, so anchoring on it makes single-day views meaningless.)
  - Aggregates across ALL the companies the user can access (not one
    Company-Code), like the dispatch section.

Three quantities, all from Postgres (mirrored SAP fields — no live SAP calls):
  - DISPATCHED : SalesDispatchGateOut that left the gate in the window
                 (sap_doc_total / total_weight / total_litres / total_boxes)
  - BILLED / PLANNED : the invoices those dispatched trucks fulfil — the linked
                 DispatchPlans' invoice_amount (billed) and invoice_weight /
                 total_litres (planned target quantity)
  - BACKLOG    : open plans (PENDING / BOOKED) not yet dispatched — the pipeline
                 still to ship (a snapshot, not window-bound)
"""
from __future__ import annotations

from django.db.models import Count, Sum

from gate_core.models.sales_dispatch import (
    SalesDispatchGateOut,
    SalesDispatchGateOutStatus,
)

from .models import DispatchPlan, DispatchPlanStatus

OPEN_STATUSES = [DispatchPlanStatus.PENDING, DispatchPlanStatus.BOOKED]


def _f(value) -> float:
    """Decimal | None -> float, for JSON output."""
    return float(value) if value is not None else 0.0


class DispatchDashboardService:
    def __init__(self, company_ids, date_from, date_to, company_codes=None):
        self.company_ids = list(company_ids)
        self.date_from = date_from
        self.date_to = date_to
        self.company_codes = company_codes or []

    # ------------------------------------------------------------------ #
    # base querysets
    # ------------------------------------------------------------------ #
    def _dispatched(self):
        """Trucks that physically left the gate inside the window."""
        return SalesDispatchGateOut.objects.filter(
            company_id__in=self.company_ids,
            gate_out_date__range=(self.date_from, self.date_to),
            status=SalesDispatchGateOutStatus.DISPATCHED,
        )

    def _fulfilled_plan_ids(self):
        """Distinct plans (invoices) that had a dispatch in the window."""
        return list(
            self._dispatched()
            .exclude(dispatch_plan__isnull=True)
            .values_list("dispatch_plan_id", flat=True)
            .distinct()
        )

    def _backlog(self):
        """Open pipeline — plans not yet dispatched (snapshot, not window-bound)."""
        return DispatchPlan.objects.filter(
            company_id__in=self.company_ids,
            booking_status__in=OPEN_STATUSES,
        )

    # ------------------------------------------------------------------ #
    # sections
    # ------------------------------------------------------------------ #
    def totals(self) -> dict:
        disp = self._dispatched().aggregate(
            count=Count("id"),
            amount=Sum("sap_doc_total"),
            weight=Sum("total_weight"),
            litres=Sum("total_litres"),
            boxes=Sum("total_boxes"),
        )
        # distinct invoices (bills) fulfilled by those trucks
        bills_fulfilled = len(self._fulfilled_plan_ids())
        backlog = self._backlog().aggregate(
            count=Count("id"),
            amount=Sum("invoice_amount"),
            weight=Sum("invoice_weight"),
        )

        dispatched = {
            "count": disp["count"],          # trucks
            "bills": bills_fulfilled,         # invoices shipped
            # amount = Σ SAP invoice DocTotal of the trucks that left = the value
            # billed AND dispatched (in an invoice-first flow these are one number)
            "amount": _f(disp["amount"]),
            "weight": _f(disp["weight"]),
            "litres": _f(disp["litres"]),
            "boxes": _f(disp["boxes"]),
        }
        backlog_out = {
            "count": backlog["count"],
            "amount": _f(backlog["amount"]),
            "weight": _f(backlog["weight"]),
        }

        return {
            "dispatched": dispatched,
            "backlog": backlog_out,
        }

    def by_status(self) -> list:
        """Open backlog by booking status (the pipeline still to ship)."""
        rows = (
            self._backlog()
            .values("booking_status")
            .annotate(
                count=Count("id"),
                amount=Sum("invoice_amount"),
                weight=Sum("invoice_weight"),
                litres=Sum("total_litres"),
            )
            .order_by("-amount")
        )
        return [
            {
                "status": r["booking_status"],
                "count": r["count"],
                "amount": _f(r["amount"]),
                "weight": _f(r["weight"]),
                "litres": _f(r["litres"]),
            }
            for r in rows
        ]

    def trend(self) -> list:
        """Dispatched value/quantity per day (by actual gate-out date)."""
        rows = (
            self._dispatched()
            .values("gate_out_date")
            .annotate(
                amount=Sum("sap_doc_total"),
                weight=Sum("total_weight"),
                litres=Sum("total_litres"),
                boxes=Sum("total_boxes"),
                count=Count("id"),
                # Invoices shipped that day. Distinct, and on the same definition
                # totals() uses for `bills` -- a truck carrying four invoices is
                # four bills but one truck, and the daily trend has to agree with
                # the headline or the two disagree on the same screen.
                bills=Count("dispatch_plan_id", distinct=True),
            )
            .order_by("gate_out_date")
        )
        return [
            {
                "date": r["gate_out_date"].isoformat(),
                "dispatched_amount": _f(r["amount"]),
                "dispatched_weight": _f(r["weight"]),
                "dispatched_litres": _f(r["litres"]),
                "dispatched_boxes": _f(r["boxes"]),
                "trucks": r["count"],
                "bills": r["bills"],
            }
            for r in rows
            if r["gate_out_date"] is not None
        ]

    def by_customer(self, limit: int = 50) -> list:
        """Dispatched value/quantity per customer for the window."""
        rows = (
            self._dispatched()
            .values("customer_code", "customer_name")
            .annotate(
                amount=Sum("sap_doc_total"),
                weight=Sum("total_weight"),
                litres=Sum("total_litres"),
                boxes=Sum("total_boxes"),
                trucks=Count("id"),
            )
            .order_by("-amount")
        )
        out = []
        for r in rows:
            code = r["customer_code"] or "Unassigned"
            out.append(
                {
                    "customer_code": code,
                    "customer_name": r["customer_name"] or code,
                    "dispatched_amount": _f(r["amount"]),
                    "dispatched_weight": _f(r["weight"]),
                    "dispatched_litres": _f(r["litres"]),
                    "dispatched_boxes": _f(r["boxes"]),
                    "trucks": r["trucks"],
                }
            )
        return out[:limit]

    def build(self) -> dict:
        return {
            "filters": {
                "company_codes": self.company_codes,
                "company_count": len(self.company_ids),
                "from": self.date_from.isoformat(),
                "to": self.date_to.isoformat(),
            },
            "totals": self.totals(),
            "by_status": self.by_status(),
            "trend": self.trend(),
            "by_customer": self.by_customer(),
        }

    # ------------------------------------------------------------------ #
    # bill-wise drill-down (one row per SAP A/R invoice = one DispatchPlan)
    # ------------------------------------------------------------------ #
    def bills(
        self,
        status: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        from django.db.models import Q

        # Bills relevant to the window: scheduled in it OR dispatched in it.
        fulfilled_ids = self._fulfilled_plan_ids()
        base = (
            DispatchPlan.objects.filter(company_id__in=self.company_ids)
            .filter(
                Q(dispatch_date__range=(self.date_from, self.date_to))
                | Q(id__in=fulfilled_ids)
            )
            .select_related("vehicle", "transporter", "driver")
        )

        def apply_search(qs):
            if not search:
                return qs
            needle = search.strip()
            return qs.filter(
                Q(sap_invoice_doc_num__icontains=needle)
                | Q(customer_code__icontains=needle)
                | Q(customer_name__icontains=needle)
            )

        qs = apply_search(base)
        status_counts = {
            row["booking_status"]: row["n"]
            for row in qs.values("booking_status").annotate(n=Count("id"))
        }
        status_counts["ALL"] = sum(status_counts.values())

        if status:
            qs = qs.filter(booking_status=status)
        qs = qs.order_by("-dispatch_date", "-id")
        total = qs.count()

        page = qs.prefetch_related("sales_dispatch_gate_outs")[offset : offset + limit]
        results = [self._bill_row(plan) for plan in page]

        return {
            "count": total,
            "limit": limit,
            "offset": offset,
            "status_counts": status_counts,
            "results": results,
        }

    @staticmethod
    def _bill_row(plan) -> dict:
        gate_outs = list(plan.sales_dispatch_gate_outs.all())
        dispatched = [g for g in gate_outs if g.status == "DISPATCHED"]
        latest = max(gate_outs, key=lambda g: g.id) if gate_outs else None

        disp_amount = sum(_f(g.sap_doc_total) for g in dispatched)
        disp_weight = sum(_f(g.total_weight) for g in dispatched)
        disp_boxes = sum(_f(g.total_boxes) for g in dispatched)

        billed = _f(plan.invoice_amount)

        return {
            "id": plan.id,
            "invoice_number": plan.sap_invoice_doc_num or "—",
            "sap_doc_entry": plan.sap_invoice_doc_entry,
            "sap_doc_num": plan.sap_invoice_doc_num,
            "customer_code": plan.customer_code or "",
            "customer_name": plan.customer_name or "",
            "dispatch_date": plan.dispatch_date.isoformat() if plan.dispatch_date else None,
            "booking_status": plan.booking_status,
            "billed_amount": billed,
            "planned_weight": _f(plan.invoice_weight),
            "planned_litres": _f(plan.total_litres),
            "dispatched_amount": disp_amount,
            "dispatched_weight": disp_weight,
            "dispatched_boxes": disp_boxes,
            "fulfillment_rate": round(disp_amount / billed, 4) if billed else None,
            "dispatch_stage": latest.status if latest else None,
            "gatepass_no": (latest.gatepass_no or None) if latest else None,
            "gateout_count": len(gate_outs),
            "last_dispatched_at": (
                latest.dispatched_at.isoformat()
                if latest and latest.dispatched_at
                else None
            ),
            "place_of_supply": plan.place_of_supply or "",
            "transporter_name": plan.transporter_name or "",
            "vehicle_no": plan.vehicle_no or "",
            "eway_bill": plan.eway_bill or "",
            "priority": plan.priority or "",
            "product_variety": plan.product_variety or "",
            "dispatches": [
                {
                    "sap_doc_num": g.sap_doc_num,
                    "status": g.status,
                    "gatepass_no": g.gatepass_no or "",
                    "amount": _f(g.sap_doc_total),
                    "weight": _f(g.total_weight),
                    "boxes": _f(g.total_boxes),
                    "vehicle_no": g.vehicle_no or "",
                    "gate_out_date": g.gate_out_date.isoformat() if g.gate_out_date else None,
                    "dispatched_at": g.dispatched_at.isoformat() if g.dispatched_at else None,
                }
                for g in sorted(gate_outs, key=lambda g: g.id, reverse=True)
            ],
        }
