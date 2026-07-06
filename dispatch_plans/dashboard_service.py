"""
dispatch_plans/dashboard_service.py

Read-only aggregation service for the Dispatch Fulfilment dashboard.

For one company over a date window it compares three quantities, all sourced
from Postgres (mirrored SAP fields — no live SAP/HANA calls):

  - PLANNED    : DispatchPlan invoiced targets
                 (invoice_amount / invoice_weight / total_litres)
  - BILLED     : the same SAP A/R invoice amount. This system is invoice-first
                 (a plan is created *from* an invoice), so billed == the plan's
                 invoice_amount. Surfaced separately for clarity.
  - DISPATCHED : SalesDispatchGateOut actuals for trucks that physically left
                 (sap_doc_total / total_weight / total_litres / total_boxes)

Tables are small (~1k plans, ~300 gate-outs) so plain ORM .aggregate() /
.values().annotate() is instant. Sums of an empty/all-null set come back as
None and are normalised to 0.0 by ``_f`` — no Coalesce needed.
"""
from __future__ import annotations

from django.db.models import CharField, Count, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce, NullIf

from gate_core.models.sales_dispatch import (
    SalesDispatchGateOut,
    SalesDispatchGateOutStatus,
)

from .models import DispatchPlan, DispatchPlanStatus

UNASSIGNED = "Unassigned"


def _f(value) -> float:
    """Decimal | None -> float, for JSON output."""
    return float(value) if value is not None else 0.0


class DispatchDashboardService:
    def __init__(self, company, date_from, date_to):
        self.company = company
        self.date_from = date_from
        self.date_to = date_to

    # ------------------------------------------------------------------ #
    # base querysets
    # ------------------------------------------------------------------ #
    def _plans(self, include_cancelled: bool = False):
        qs = DispatchPlan.objects.filter(
            company=self.company,
            dispatch_date__range=(self.date_from, self.date_to),
        )
        if not include_cancelled:
            qs = qs.exclude(booking_status=DispatchPlanStatus.CANCELLED)
        return qs

    def _dispatches(self):
        # "Dispatched" == a truck that actually left the gate.
        return SalesDispatchGateOut.objects.filter(
            company=self.company,
            gate_out_date__range=(self.date_from, self.date_to),
            status=SalesDispatchGateOutStatus.DISPATCHED,
        )

    # ------------------------------------------------------------------ #
    # sections
    # ------------------------------------------------------------------ #
    def totals(self) -> dict:
        plan = self._plans().aggregate(
            count=Count("id"),
            amount=Sum("invoice_amount"),
            weight=Sum("invoice_weight"),
            litres=Sum("total_litres"),
        )
        disp = self._dispatches().aggregate(
            count=Count("id"),
            amount=Sum("sap_doc_total"),
            weight=Sum("total_weight"),
            litres=Sum("total_litres"),
            boxes=Sum("total_boxes"),
        )

        planned = {
            "count": plan["count"],
            "amount": _f(plan["amount"]),
            "weight": _f(plan["weight"]),
            "litres": _f(plan["litres"]),
            "boxes": None,  # plans do not store a box count
        }
        dispatched = {
            "count": disp["count"],
            "amount": _f(disp["amount"]),
            "weight": _f(disp["weight"]),
            "litres": _f(disp["litres"]),
            "boxes": _f(disp["boxes"]),
        }
        billed = {"count": plan["count"], "amount": _f(plan["amount"])}

        def rate(measure: str):
            planned_val = planned.get(measure) or 0
            dispatched_val = dispatched.get(measure) or 0
            return round(dispatched_val / planned_val, 4) if planned_val else None

        return {
            "billed": billed,
            "planned": planned,
            "dispatched": dispatched,
            "fulfillment_rate": {
                "amount": rate("amount"),
                "weight": rate("weight"),
                "litres": rate("litres"),
            },
        }

    def by_status(self) -> list:
        rows = (
            self._plans(include_cancelled=True)
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
        plans = (
            self._plans()
            .values("dispatch_date")
            .annotate(
                amount=Sum("invoice_amount"),
                weight=Sum("invoice_weight"),
                litres=Sum("total_litres"),
                count=Count("id"),
            )
        )
        dispatches = (
            self._dispatches()
            .values("gate_out_date")
            .annotate(
                amount=Sum("sap_doc_total"),
                weight=Sum("total_weight"),
                litres=Sum("total_litres"),
                boxes=Sum("total_boxes"),
                count=Count("id"),
            )
        )

        bucket: dict[str, dict] = {}

        def blank(key: str) -> dict:
            return bucket.setdefault(
                key,
                {
                    "date": key,
                    "planned_amount": 0.0,
                    "planned_weight": 0.0,
                    "planned_litres": 0.0,
                    "billed_amount": 0.0,
                    "dispatched_amount": 0.0,
                    "dispatched_weight": 0.0,
                    "dispatched_litres": 0.0,
                    "dispatched_boxes": 0.0,
                },
            )

        for r in plans:
            if r["dispatch_date"] is None:
                continue
            row = blank(r["dispatch_date"].isoformat())
            row["planned_amount"] = _f(r["amount"])
            row["planned_weight"] = _f(r["weight"])
            row["planned_litres"] = _f(r["litres"])
            row["billed_amount"] = _f(r["amount"])

        for r in dispatches:
            if r["gate_out_date"] is None:
                continue
            row = blank(r["gate_out_date"].isoformat())
            row["dispatched_amount"] = _f(r["amount"])
            row["dispatched_weight"] = _f(r["weight"])
            row["dispatched_litres"] = _f(r["litres"])
            row["dispatched_boxes"] = _f(r["boxes"])

        return [bucket[key] for key in sorted(bucket)]

    def by_customer(self, limit: int = 50) -> list:
        # DispatchPlan.customer_code is populated on only ~28% of rows, but the
        # linked gate-out carries it reliably. Fall back to the earliest linked
        # gate-out's customer when the plan's own field is blank.
        gateout_code = (
            SalesDispatchGateOut.objects.filter(dispatch_plan_id=OuterRef("pk"))
            .exclude(customer_code__isnull=True)
            .exclude(customer_code="")
            .order_by("id")
            .values("customer_code")[:1]
        )
        gateout_name = (
            SalesDispatchGateOut.objects.filter(dispatch_plan_id=OuterRef("pk"))
            .exclude(customer_name__isnull=True)
            .exclude(customer_name="")
            .order_by("id")
            .values("customer_name")[:1]
        )
        plans = (
            self._plans()
            .annotate(
                cust_code=Coalesce(
                    NullIf("customer_code", Value("")),
                    Subquery(gateout_code),
                    Value(""),
                    output_field=CharField(),
                ),
                cust_name=Coalesce(
                    NullIf("customer_name", Value("")),
                    Subquery(gateout_name),
                    Value(""),
                    output_field=CharField(),
                ),
            )
            .values("cust_code", "cust_name")
            .annotate(
                amount=Sum("invoice_amount"),
                weight=Sum("invoice_weight"),
                litres=Sum("total_litres"),
                count=Count("id"),
            )
        )
        dispatches = (
            self._dispatches()
            .values("customer_code")
            .annotate(
                amount=Sum("sap_doc_total"),
                weight=Sum("total_weight"),
                litres=Sum("total_litres"),
                boxes=Sum("total_boxes"),
                count=Count("id"),
            )
        )

        bucket: dict[str, dict] = {}

        def blank(code: str, name: str = "") -> dict:
            code = code or UNASSIGNED
            entry = bucket.setdefault(
                code,
                {
                    "customer_code": code,
                    "customer_name": name or code,
                    "planned_amount": 0.0,
                    "planned_weight": 0.0,
                    "planned_litres": 0.0,
                    "planned_count": 0,
                    "dispatched_amount": 0.0,
                    "dispatched_weight": 0.0,
                    "dispatched_litres": 0.0,
                    "dispatched_boxes": 0.0,
                    "dispatched_count": 0,
                },
            )
            if name and entry["customer_name"] in ("", code):
                entry["customer_name"] = name
            return entry

        for r in plans:
            row = blank(r["cust_code"], r["cust_name"])
            row["planned_amount"] = _f(r["amount"])
            row["planned_weight"] = _f(r["weight"])
            row["planned_litres"] = _f(r["litres"])
            row["planned_count"] = r["count"]

        for r in dispatches:
            row = blank(r["customer_code"])
            row["dispatched_amount"] = _f(r["amount"])
            row["dispatched_weight"] = _f(r["weight"])
            row["dispatched_litres"] = _f(r["litres"])
            row["dispatched_boxes"] = _f(r["boxes"])
            row["dispatched_count"] = r["count"]

        rows = list(bucket.values())
        for row in rows:
            planned = row["planned_amount"]
            row["fulfillment_rate"] = (
                round(row["dispatched_amount"] / planned, 4) if planned else None
            )
        rows.sort(key=lambda x: x["planned_amount"], reverse=True)
        return rows[:limit]

    def build(self) -> dict:
        return {
            "filters": {
                "company_code": self.company.code,
                "company_name": self.company.name,
                "from": self.date_from.isoformat(),
                "to": self.date_to.isoformat(),
            },
            "totals": self.totals(),
            "by_status": self.by_status(),
            "trend": self.trend(),
            "by_customer": self.by_customer(),
        }
