"""Reading the plan, phasing it, and turning it into a material requirement.

Nothing here is stored. The plan is SAP's, the stock is SAP's, and the numbers
are recomputed on every request so a planner can never be looking at a cached
answer without knowing it.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

from django.conf import settings
from django.utils import timezone

from sap_client.context import CompanyContext

from ..hana_reader import BOM_LINE_TYPE_ITEM, HanaProductionPlanReader, classify_material
from . import calendar as cal
from .errors import PlanNotFound
from .producible import ProducibleMixin

logger = logging.getLogger(__name__)

ZERO = Decimal(0)


def _dec(value) -> Decimal:
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _as_date(value) -> Optional[date]:
    if value is None:
        return None
    return value.date() if hasattr(value, "date") else value


def default_stock_warehouses() -> List[str]:
    """The production-facing warehouses stock is counted in.

    A setting rather than a constant because which stores production can draw on
    is a business fact that changes when the floor is rearranged. The list covers
    both the packaging stores and the bulk-oil ones, which matters more than it
    looks: the packaging trio alone held 7.7% of raw material, so every oil
    requirement read as a near-total shortage until `BH-LO` and `BH-OT` were
    included.

    Returns an empty list when nothing is configured, and the caller then falls
    back to every warehouse. That is the safer failure: reporting no stock would
    raise a purchase order for the entire plan.
    """
    return [
        code.strip()
        for code in getattr(settings, "PLANNING_PURCHASE_STOCK_WAREHOUSES", []) or []
        if code.strip()
    ]


class PlanService(ProducibleMixin):
    """Plans, buckets, requirement and buildable-from-stock for one company."""

    def __init__(self, company_code: str):
        self.company_code = company_code
        self.context = CompanyContext(company_code)
        self.reader = HanaProductionPlanReader(self.context)

    # ------------------------------------------------------------------
    # Plans
    # ------------------------------------------------------------------

    def list_plans(self, limit: int = 36) -> Dict[str, Any]:
        rows = self.reader.list_plans(limit=limit)
        plans = [self._map_plan_header(row) for row in rows]
        return {
            "data": plans,
            "meta": {
                "company_code": self.company_code,
                "count": len(plans),
                "fetched_at": timezone.now().isoformat(),
                "source": "SAP OFCT / FCT1 (sales forecast used as the production plan)",
            },
        }

    def get_plan(
        self,
        abs_id: int,
        bucket_type: str = cal.MONTH,
        spread_policy: str = cal.POLICY_EVEN_WORKING_DAYS,
        include_actuals: bool = True,
    ) -> Dict[str, Any]:
        """One plan: its lines, its buckets, and what was produced against it."""
        header_row = self.reader.get_plan_header(abs_id)
        if not header_row:
            raise PlanNotFound(f"Plan {abs_id} was not found in SAP.")

        header = self._map_plan_header(header_row)
        period_start = header["start_date"]
        period_end = header["end_date"]

        line_rows = self.reader.get_plan_lines(abs_id)
        lines: List[Dict[str, Any]] = []
        bucket_totals: Dict[date, Decimal] = {}
        bucket_litres: Dict[date, Decimal] = {}
        bucket_cases: Dict[date, Decimal] = {}
        derived_flags: Dict[date, bool] = {}

        for row in line_rows:
            line = self._map_plan_line(row)
            buckets = cal.build_buckets(
                line["planned_qty"],
                line["bucket_date"] or period_start,
                period_start,
                period_end,
                policy=spread_policy,
            )
            line["buckets"] = buckets.get(bucket_type, [])

            # The line's own litre and case totals are authoritative, and the
            # buckets have to partition them exactly. Converting each bucket
            # independently does NOT achieve that: every product gets quantised,
            # and 26 daily roundings drifted the August plan 8 ml off its own
            # total. So allocate the line total across the buckets instead, the
            # same way `spread_even` distributes the piece remainder.
            litre_alloc = self._allocate(
                [b["planned_qty"] for b in line["buckets"]], line["planned_litres"]
            )
            case_alloc = self._allocate(
                [b["planned_qty"] for b in line["buckets"]], line["planned_cases"]
            )

            for index, bucket in enumerate(line["buckets"]):
                start = bucket["bucket_start"]
                bucket_totals[start] = bucket_totals.get(start, ZERO) + bucket["planned_qty"]
                bucket_litres[start] = bucket_litres.get(start, ZERO) + litre_alloc[index]
                bucket_cases[start] = bucket_cases.get(start, ZERO) + case_alloc[index]
                derived_flags[start] = derived_flags.get(start, False) or bucket["derived"]
            lines.append(line)

        if include_actuals and lines:
            self._attach_actuals(lines, period_start, period_end)

        planned_total = sum((line["planned_qty"] for line in lines), ZERO)
        produced_total = sum((line["produced_qty"] for line in lines), ZERO)
        planned_litres = sum((line["planned_litres"] for line in lines), ZERO)
        produced_litres = sum((line["produced_litres"] for line in lines), ZERO)
        planned_cases = sum((line["planned_cases"] for line in lines), ZERO)
        produced_cases = sum((line["produced_cases"] for line in lines), ZERO)
        without_bom = [line for line in lines if not line["has_bom"]]
        non_litre = [line for line in lines if not line["is_litre_item"]]

        header.update({
            "planned_qty": planned_total,
            "produced_qty": produced_total,
            "planned_litres": planned_litres,
            "produced_litres": produced_litres,
            "planned_cases": planned_cases,
            "produced_cases": produced_cases,
            "attainment_pct": self._pct(produced_total, planned_total),
            "line_count": len(lines),
            # Named so a litre total can never quietly under-report. Every SKU on
            # both live companies' current plans is a litre item, but a new one
            # added without the flag would otherwise just vanish from the total.
            "non_litre_item_count": len(non_litre),
            "non_litre_items": [
                {
                    "item_code": line["item_code"],
                    "item_name": line["item_name"],
                    "planned_qty": line["planned_qty"],
                }
                for line in non_litre
            ],
            "items_without_bom": [
                {
                    "item_code": line["item_code"],
                    "item_name": line["item_name"],
                    "planned_qty": line["planned_qty"],
                }
                for line in without_bom
            ],
        })

        return {
            "plan": header,
            "lines": lines,
            "buckets": [
                {
                    "bucket_type": bucket_type,
                    "bucket_start": start,
                    "label": cal.bucket_label(bucket_type, start),
                    "planned_qty": qty,
                    "planned_litres": bucket_litres.get(start, ZERO),
                    "planned_cases": bucket_cases.get(start, ZERO),
                    "derived": derived_flags.get(start, False),
                }
                for start, qty in sorted(bucket_totals.items())
            ],
            "meta": {
                "company_code": self.company_code,
                "bucket_type": bucket_type,
                "spread_policy": spread_policy,
                "fetched_at": timezone.now().isoformat(),
                "derivation_note": (
                    "SAP holds the plan as one quantity per item dated the period "
                    "start. Day and week figures are spread across the period's "
                    "working days by this module and are marked derived."
                    if spread_policy == cal.POLICY_EVEN_WORKING_DAYS
                    else "Quantities are shown on the exact date SAP recorded them."
                ),
                "unit_note": (
                    "Plan quantities are in the item's SAP inventory unit (PCS for "
                    "almost every SKU here, i.e. single bottles/tins). Cases are "
                    "derived using OITM.SalFactor2."
                ),
            },
        }

    # ------------------------------------------------------------------
    # Requirement: BOM explosion, availability, shortage
    # ------------------------------------------------------------------

    def get_requirement(
        self,
        abs_id: int,
        material_type: Optional[str] = None,
        warehouses: Optional[Sequence[str]] = None,
        include_covered: bool = True,
    ) -> Dict[str, Any]:
        """What the plan consumes, what is available, and what is short.

        Aggregated **by component**, not by SKU: one 26 mm cap runs across a dozen
        SKUs, and a per-SKU shortage list cannot be turned into a purchase order.
        Each row keeps the SKUs that drive it so the number can be checked.

        Stock is counted only in the production-facing warehouses
        (`PLANNING_PURCHASE_STOCK_WAREHOUSES`) unless the caller names its own.
        Summing the whole estate treats finished-goods godowns, non-moving stores,
        job-work locations and wastage as material production could draw on, which
        understates the shortage and under-buys.
        """
        warehouses = warehouses or default_stock_warehouses()

        header_row = self.reader.get_plan_header(abs_id)
        if not header_row:
            raise PlanNotFound(f"Plan {abs_id} was not found in SAP.")

        header = self._map_plan_header(header_row)
        line_rows = self.reader.get_plan_lines(abs_id)
        plan_qty_by_item = {
            row["ItemCode"]: _dec(row["PlannedQty"])
            for row in line_rows
            if row.get("ItemCode")
        }
        item_names = {row["ItemCode"]: row.get("ItemName") or "" for row in line_rows}

        bom_rows = self.reader.get_bom_components(list(plan_qty_by_item))
        parents_with_bom = {row["ParentCode"] for row in bom_rows}

        components: Dict[str, Dict[str, Any]] = {}
        unusable_boms: List[Dict[str, Any]] = []
        resources: Dict[str, Dict[str, Any]] = {}

        for row in bom_rows:
            parent = row["ParentCode"]
            qty_per_unit = row.get("QtyPerUnit")
            if qty_per_unit is None:
                # OITT."Qauntity" was zero — corrupt master data. Surfaced rather
                # than silently contributing nothing to the requirement.
                unusable_boms.append({
                    "parent_code": parent,
                    "component_code": row["ComponentCode"],
                    "reason": "BOM base quantity is zero in SAP (OITT.Qauntity)",
                })
                continue

            # A resource line is a conversion cost (filling, blowing, job work),
            # not something anyone can raise a purchase order for. Kept and
            # reported, because it IS real cost the plan incurs — just never
            # offered as a material to buy.
            if int(row.get("LineType") or BOM_LINE_TYPE_ITEM) != BOM_LINE_TYPE_ITEM:
                self._accumulate_resource(resources, row, plan_qty_by_item)
                continue

            code = row["ComponentCode"]
            required = plan_qty_by_item.get(parent, ZERO) * _dec(qty_per_unit)
            entry = components.get(code)
            if entry is None:
                entry = components[code] = {
                    "component_code": code,
                    "component_name": row.get("ComponentName") or "",
                    "item_group": row.get("ItemGroup") or "",
                    "material_type": classify_material(row.get("ItemGroup")),
                    "uom": row.get("Uom") or "",
                    "issue_warehouse": row.get("IssueWarehouse") or "",
                    "is_purchased": (row.get("PurchaseItem") or "N") == "Y",
                    "has_own_bom": bool(row.get("HasOwnBom")),
                    "last_purchase_price": _dec(row.get("LastPurchasePrice")),
                    "required_qty": ZERO,
                    "used_by": [],
                }
            entry["required_qty"] += required
            entry["used_by"].append({
                "item_code": parent,
                "item_name": item_names.get(parent, ""),
                "plan_qty": plan_qty_by_item.get(parent, ZERO),
                "qty_per_unit": _dec(qty_per_unit),
                "required_qty": required,
            })

        codes = list(components)
        self._attach_availability(components, codes, warehouses)
        self._attach_purchasing(components, codes, header)

        rows = list(components.values())
        if material_type:
            rows = [row for row in rows if row["material_type"] == material_type]
        if not include_covered:
            rows = [row for row in rows if row["shortage_qty"] > 0]

        # Worst first: what is short, by how much it is short, so the top of the
        # list is what somebody has to act on.
        rows.sort(key=lambda row: (row["shortage_qty"] == 0, -row["shortage_qty"]))

        return {
            "plan": header,
            "data": rows,
            "resources": sorted(
                resources.values(), key=lambda row: -row["required_qty"]
            ),
            "meta": self._requirement_meta(
                rows, plan_qty_by_item, parents_with_bom, item_names,
                unusable_boms, warehouses, resources,
            ),
        }

    @staticmethod
    def _accumulate_resource(
        resources: Dict[str, Dict[str, Any]],
        row: Dict[str, Any],
        plan_qty_by_item: Dict[str, Decimal],
    ) -> None:
        code = row["ComponentCode"]
        entry = resources.get(code)
        if entry is None:
            entry = resources[code] = {
                "resource_code": code,
                "resource_name": row.get("ComponentName") or "",
                "required_qty": ZERO,
                "used_by_count": 0,
            }
        entry["required_qty"] += (
            plan_qty_by_item.get(row["ParentCode"], ZERO) * _dec(row["QtyPerUnit"])
        )
        entry["used_by_count"] += 1

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _attach_availability(
        self,
        components: Dict[str, Dict[str, Any]],
        codes: Sequence[str],
        warehouses: Optional[Sequence[str]],
    ) -> None:
        """Fold per-warehouse stock into each component row.

        `net_available` is on-hand minus committed, not on-hand: stock already
        promised to another document is not available to this plan. `MinStock` is
        summed over the warehouses in scope and reported as the benchmark — on
        this company it is only actually configured in the packaging warehouse
        `BH-PM`, so most rows carry a benchmark of zero, and the response says so
        rather than implying the floor is met.
        """
        stock_rows = self.reader.get_item_stock(codes, warehouses)
        by_item: Dict[str, List[Dict[str, Any]]] = {}
        for row in stock_rows:
            by_item.setdefault(row["ItemCode"], []).append(row)

        for code, entry in components.items():
            item_rows = by_item.get(code, [])
            on_hand = sum((_dec(r["OnHand"]) for r in item_rows), ZERO)
            committed = sum((_dec(r["Committed"]) for r in item_rows), ZERO)
            benchmark = sum((_dec(r["MinStock"]) for r in item_rows), ZERO)
            days_since = [
                r["DaysSinceLastConsumption"]
                for r in item_rows
                if r.get("DaysSinceLastConsumption") is not None
            ]

            entry.update({
                "on_hand_qty": on_hand,
                "committed_qty": committed,
                "net_available_qty": on_hand - committed,
                "benchmark_qty": benchmark,
                "has_benchmark": benchmark > 0,
                "days_since_last_use": min(days_since) if days_since else None,
                "warehouses": [
                    {
                        "warehouse": r["WhsCode"],
                        "on_hand": _dec(r["OnHand"]),
                        "committed": _dec(r["Committed"]),
                        "min_stock": _dec(r["MinStock"]),
                    }
                    for r in item_rows
                    if _dec(r["OnHand"]) != ZERO or _dec(r["MinStock"]) != ZERO
                ],
            })
            if not entry.get("last_purchase_price") and item_rows:
                entry["last_purchase_price"] = _dec(item_rows[0].get("LastPurchasePrice"))

    def _attach_purchasing(
        self,
        components: Dict[str, Dict[str, Any]],
        codes: Sequence[str],
        header: Dict[str, Any],
    ) -> None:
        """Net off open POs, then work out the shortage, the supplier and the dates.

        Lead time and MOQ come from `supply_chain.MaterialLeadTime` where
        procurement has filled it in. That table is the right home for them — it
        already exists, already belongs to procurement, and duplicating it here
        would give the factory two answers to one question. When a component has
        no row, the shortage is still reported and `lead_time_days` is null: a
        material we cannot time is a reference-data gap to chase, not a reason to
        hide the shortage.
        """
        open_po_rows = {
            row["ItemCode"]: row for row in self.reader.get_open_purchase_qty(codes)
        }
        vendor_rows = {
            row["ItemCode"]: row for row in self.reader.get_last_vendors(codes)
        }
        lead_times = self._lead_time_map(codes)

        need_by = header["start_date"]
        today = timezone.localdate()

        for code, entry in components.items():
            po_row = open_po_rows.get(code, {})
            on_order = _dec(po_row.get("OpenQty"))
            shortage_before_po = max(ZERO, entry["required_qty"] - entry["net_available_qty"])
            shortage = max(ZERO, shortage_before_po - on_order)

            lead = lead_times.get(code)
            moq = _dec(lead["moq"]) if lead else ZERO
            lead_days = lead["lead_time_days"] if lead else None

            order_qty = shortage
            moq_applied = None
            if shortage > ZERO and moq > shortage:
                order_qty = moq
                moq_applied = moq

            order_by = None
            if lead_days is not None and need_by:
                order_by = need_by - timedelta(days=int(lead_days))

            vendor = vendor_rows.get(code, {})
            supplier_name = (lead or {}).get("supplier_name") or vendor.get("CardName") or ""

            # Cost from the item master, NOT from the last purchase order.
            # `POR1.Price` is in the purchase unit — bulk oil is bought by the
            # metric ton and consumed by the litre — so multiplying a litre
            # requirement by it overstates the spend a thousandfold. The last PO
            # price is carried alongside as evidence, clearly labelled.
            price = _dec(entry.get("last_purchase_price"))

            entry.update({
                "on_order_qty": on_order,
                "open_po_lines": po_row.get("OpenLines") or 0,
                "open_po_earliest_due": _as_date(po_row.get("EarliestDue")),
                "shortage_before_po_qty": shortage_before_po,
                "shortage_qty": shortage,
                "suggested_order_qty": order_qty,
                "moq": moq or None,
                "moq_applied": moq_applied,
                "lead_time_days": lead_days,
                "lead_time_source": "TEMPLATE" if lead_days is not None else "NONE",
                "need_by_date": need_by,
                "order_by_date": order_by,
                "urgency": self._urgency(shortage, lead_days, order_by, today),
                "vendor_code": vendor.get("CardCode") or "",
                "vendor_name": supplier_name,
                "unit_price": price,
                "price_source": "ITEM_MASTER" if price else "NONE",
                "last_po_price": _dec(vendor.get("Price")) or None,
                "last_po_date": _as_date(vendor.get("DocDate")),
                "currency": vendor.get("Currency") or "INR",
                "estimated_value": order_qty * price,
                # Committed stock exceeding what is on hand is not an arithmetic
                # slip — it means the stock is already over-promised, which is
                # worth seeing rather than smoothing away.
                "is_over_committed": entry["net_available_qty"] < ZERO,
            })

    def _lead_time_map(self, codes: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        """Lead time and MOQ from the supply-chain reference table, if the app is installed."""
        try:
            from supply_chain.models import MaterialLeadTime
        except Exception:  # pragma: no cover - supply_chain is optional
            return {}

        rows = MaterialLeadTime.objects.filter(
            company_code=self.company_code, material_code__in=list(codes), is_active=True
        ).values("material_code", "lead_time_days", "moq", "supplier_name")

        return {
            row["material_code"]: {
                "lead_time_days": row["lead_time_days"],
                "moq": row["moq"],
                "supplier_name": row["supplier_name"],
            }
            for row in rows
        }

    @staticmethod
    def _urgency(shortage, lead_days, order_by, today) -> str:
        """COVERED / NO_LEAD_TIME / OVERDUE / ORDER_NOW / SCHEDULED.

        `NO_LEAD_TIME` deliberately outranks `SCHEDULED`: a shortage nobody can
        date is the reference-data gap that needs chasing, and sorting it to the
        bottom is how it stays unchased.
        """
        if shortage <= ZERO:
            return "COVERED"
        if lead_days is None or order_by is None:
            return "NO_LEAD_TIME"
        if order_by < today:
            return "OVERDUE"
        if (order_by - today).days <= 7:
            return "ORDER_NOW"
        return "SCHEDULED"

    def _requirement_meta(
        self, rows, plan_qty_by_item, parents_with_bom, item_names,
        unusable_boms, warehouses, resources,
    ) -> Dict[str, Any]:
        short_rows = [row for row in rows if row["shortage_qty"] > 0]
        missing_bom = [
            {"item_code": code, "item_name": item_names.get(code, ""), "planned_qty": qty}
            for code, qty in plan_qty_by_item.items()
            if code not in parents_with_bom
        ]

        return {
            "company_code": self.company_code,
            "component_count": len(rows),
            "shortage_count": len(short_rows),
            "packaging_shortage_count": sum(
                1 for row in short_rows if row["material_type"] == "PACKAGING"
            ),
            "raw_shortage_count": sum(
                1 for row in short_rows if row["material_type"] == "RAW"
            ),
            "estimated_purchase_value": sum(
                (row["estimated_value"] for row in short_rows), ZERO
            ),
            "no_lead_time_count": sum(
                1 for row in short_rows if row["lead_time_days"] is None
            ),
            "no_price_count": sum(1 for row in short_rows if not row["unit_price"]),
            "over_committed_count": sum(
                1 for row in rows if row.get("is_over_committed")
            ),
            "sub_assembly_count": sum(1 for row in rows if row["has_own_bom"]),
            "resource_line_count": len(resources),
            "items_without_bom": missing_bom,
            "unusable_boms": unusable_boms,
            "warehouse_scope": list(warehouses) if warehouses else "ALL",
            "fetched_at": timezone.now().isoformat(),
            "notes": [
                "Stock is counted only in the production-facing warehouses "
                f"({', '.join(warehouses) if warehouses else 'all'}). Finished-goods "
                "godowns, non-moving stores, job-work locations and wastage are not "
                "material production can draw on.",
                "Requirement is exploded one level from the SAP production BOM "
                "(OITT/ITT1), scaled by ITT1.Quantity / OITT.Qauntity.",
                "Shortage = required - (on hand - committed) - open purchase orders.",
                "Components that are themselves manufactured are flagged, not "
                "exploded further: whether to make or buy them is a business call.",
                "Resource lines (filling, blowing and job-work costs) are listed "
                "separately and are never offered for purchase.",
                "Unit price is the item master's last purchase price, per "
                "inventory unit. The last purchase order's own price is shown as "
                "evidence only — it is in the purchase unit, which for bulk oil "
                "is a metric ton against a litre BOM.",
            ],
        }

    def _attach_actuals(
        self, lines: List[Dict[str, Any]], period_start: date, period_end: date
    ) -> None:
        """What SAP says was produced in the plan period, per item.

        Read from `OINM` TransType 59 in the item's own inventory unit, so it is
        directly comparable to the plan with no conversion. Deliberately not read
        from `ProductionRun.total_production`, which is in cases and would need
        multiplying by the pieces-per-case factor first — the mistake the existing
        plan-vs-production report makes.
        """
        codes = [line["item_code"] for line in lines if line["item_code"]]
        try:
            produced = {
                row["ItemCode"]: _dec(row["ProducedQty"])
                for row in self.reader.get_produced_quantities(
                    codes, period_start, period_end
                )
            }
        except Exception as exc:
            logger.warning("Could not read production actuals: %s", exc)
            produced = {}

        for line in lines:
            actual = produced.get(line["item_code"], ZERO)
            planned = line["planned_qty"]
            line["produced_qty"] = actual
            line["variance_qty"] = actual - planned
            line["attainment_pct"] = self._pct(actual, planned)
            line["produced_cases"] = self._cases(actual, line["pieces_per_case"])
            line["produced_litres"] = self._litres(actual, line["litres_per_unit"])
            line["variance_litres"] = line["produced_litres"] - line["planned_litres"]

    # ------------------------------------------------------------------
    # Mapping
    # ------------------------------------------------------------------

    def _map_plan_header(self, row: Dict[str, Any]) -> Dict[str, Any]:
        start = _as_date(row.get("StartDate"))
        end = _as_date(row.get("EndDate"))
        return {
            "abs_id": row["AbsID"],
            "code": row.get("Code") or "",
            "name": row.get("Name") or "",
            "start_date": start,
            "end_date": end,
            "period_view": "WEEKLY" if (row.get("FormView") or "M") == "W" else "MONTHLY",
            "line_count": row.get("LineCount") or 0,
            "item_count": row.get("ItemCount") or 0,
            "planned_qty": _dec(row.get("PlannedQty")),
            # Present on the list query; the single-header query does not compute
            # them, and get_plan() overwrites both from the lines it has read.
            "planned_litres": _dec(row.get("PlannedLitres")),
            "planned_cases": _dec(row.get("PlannedCases")).quantize(Decimal("0.01")),
            "first_bucket_date": _as_date(row.get("FirstBucketDate")),
            "last_bucket_date": _as_date(row.get("LastBucketDate")),
            "is_current": bool(
                start and end and start <= timezone.localdate() <= end
            ),
        }

    def _map_plan_line(self, row: Dict[str, Any]) -> Dict[str, Any]:
        planned = _dec(row.get("PlannedQty"))
        pieces_per_case = int(row.get("PiecesPerCase") or 1) or 1
        litres_per_unit = _dec(row.get("LitresPerUnit"))
        return {
            "line_id": row.get("LineID"),
            "item_code": row.get("ItemCode") or "",
            "item_name": row.get("ItemName") or "",
            "item_group": row.get("ItemGroup") or "",
            "bucket_date": _as_date(row.get("BucketDate")),
            "warehouse_code": row.get("WhsCode") or "",
            "planned_qty": planned,
            "uom": row.get("Uom") or "",
            "pieces_per_case": pieces_per_case,
            "planned_cases": self._cases(planned, pieces_per_case),
            # Litres are the unit an oil business reads a plan in. Zero here means
            # the item is not a litre item (`U_IsLitre` is not 'Y'), NOT that it
            # holds no volume — the UI must say "not a litre item" rather than
            # print a confident 0 L.
            "litres_per_unit": litres_per_unit,
            "is_litre_item": litres_per_unit > ZERO,
            "planned_litres": self._litres(planned, litres_per_unit),
            "has_bom": bool(row.get("HasBom")),
            "bom_base_qty": _dec(row.get("BomBaseQty")),
            "produced_qty": ZERO,
            "produced_cases": ZERO,
            "produced_litres": ZERO,
            "variance_qty": ZERO,
            "variance_litres": ZERO,
            "attainment_pct": ZERO,
            "buckets": [],
        }

    @staticmethod
    def _cases(qty: Decimal, pieces_per_case: int) -> Decimal:
        if not pieces_per_case or pieces_per_case <= 1:
            return qty
        return (qty / Decimal(pieces_per_case)).quantize(Decimal("0.01"))

    @staticmethod
    def _allocate(weights: Sequence[Decimal], total: Decimal) -> List[Decimal]:
        """Split `total` across `weights` so the parts sum to exactly `total`.

        Used to carry a line's litre and case totals down onto its buckets. The
        parts are proportional to the bucket's pieces, and whatever the rounding
        leaves over lands on the largest bucket, where it distorts least. Exact
        by construction rather than by luck, which is what lets the page promise
        that a column adds up to its own footer in every unit.
        """
        if not weights:
            return []
        weight_total = sum(weights, ZERO)
        if weight_total <= ZERO or total == ZERO:
            return [ZERO for _ in weights]

        quantum = Decimal("0.001")
        parts = [
            (total * weight / weight_total).quantize(quantum) for weight in weights
        ]
        residual = total - sum(parts, ZERO)
        if residual:
            largest = max(range(len(parts)), key=lambda i: weights[i])
            parts[largest] += residual
        return parts

    @staticmethod
    def _litres(qty: Decimal, litres_per_unit: Decimal) -> Decimal:
        """Pieces x litres per piece. Quantised to 3 dp, not rounded to whole.

        A 200 ML SKU is 0.2 L a piece and a 750 GMS pouch 0.8242, so rounding per
        line would drift the plan total by thousands of litres over 98 SKUs.
        """
        if litres_per_unit <= ZERO:
            return ZERO
        return (qty * litres_per_unit).quantize(Decimal("0.001"))

    @staticmethod
    def _pct(actual: Decimal, planned: Decimal) -> Decimal:
        if not planned:
            return ZERO
        return ((actual / planned) * 100).quantize(Decimal("0.1"))
