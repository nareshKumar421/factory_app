"""Reconciliation of app records vs SAP, pivoted by SKU / material.

Three comparisons:
* **Production (FG):** app produced quantity (``ProductionRun`` — completed
  ``total_production`` else live segment ``produced_cases``) vs SAP finished-goods
  receipts into the FG warehouse (``TransType`` **59**), matched by SKU name.
* **Material (RM/PM):** app material issued (``ProductionMaterialUsage.issued_qty``)
  vs SAP material issued to production (``TransType`` **202**), matched by SAP
  **item code** (``material_code``).
* **Wastage:** app ``WasteLog.wastage_qty`` vs SAP scrap transferred into the
  wastage warehouse (``TransType`` **67**, ``InQty``), matched by item code.
"""

import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from django.db.models import Sum

from sap_client.context import CompanyContext

from ..models import ProductionMaterialUsage, ProductionRun, ProductionSegment, WasteLog
from .reconciliation_reader import ReconciliationReader

DEFAULT_FG_WAREHOUSE = "BH-PF"
DEFAULT_MATERIAL_WAREHOUSE = "BH-PC"  # SAP-approved BOM lands here (TransType 67 InQty)
DEFAULT_WASTAGE_WAREHOUSE = "BH-WST"

# The production/material warehouse where SAP-approved BOM lands differs per
# company (Oil uses BH-PC; Beverages transfers material into BH-PP "Production
# Process"). FG (BH-PF) and wastage (BH-WST) are the same across companies.
MATERIAL_WAREHOUSE_BY_COMPANY = {
    "JIVO_OIL": "BH-PC",
    "JIVO_BEVERAGES": "BH-PP",
}

MATCH_TOLERANCE_PCT = 1.0


def _to_date(value: Optional[str], fallback: date) -> date:
    if not value:
        return fallback
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return fallback


def _to_int(value) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _norm(text: Optional[str]) -> str:
    return (text or "").strip().upper()


# e.g. "MUSTARD ... 20 PCS ROUND BOTTLE" -> 20 ; "750 GMS 12 PCS POUCH" -> 12
_PCS_RE = re.compile(r"(\d+)\s*PCS?\b", re.IGNORECASE)


def _units_per_case(*names: Optional[str]) -> int:
    """Pack size (pieces per case) parsed from the SKU name; 1 if none found."""
    for name in names:
        if not name:
            continue
        match = _PCS_RE.search(name)
        if match:
            value = int(match.group(1))
            if value > 0:
                return value
    return 1


def _status(app_qty: float, sap_qty: float) -> str:
    if app_qty > 0 and sap_qty == 0:
        return "PENDING_SYNC"
    if app_qty == 0 and sap_qty == 0:
        return "MATCHED"
    denom = max(abs(app_qty), abs(sap_qty), 1.0)
    pct = abs(app_qty - sap_qty) / denom * 100.0
    return "MATCHED" if pct <= MATCH_TOLERANCE_PCT else "MISMATCH"


def _diff_pct(app_qty: float, sap_qty: float) -> float:
    denom = max(abs(app_qty), abs(sap_qty), 1.0)
    return round((app_qty - sap_qty) / denom * 100.0, 2)


class ReconciliationService:
    """Reconcile app production / material / wastage against SAP, per company."""

    def __init__(self, company):
        self.company = company
        self.context = CompanyContext(company.code)
        self.reader = ReconciliationReader(self.context)

    # ------------------------------------------------------------------
    # Production (FG) — TransType 59
    # ------------------------------------------------------------------
    def get_production_reconciliation(
        self, date_from=None, date_to=None, warehouse=None, line=None, sap_lag_days=1
    ):
        d_to = _to_date(date_to, date.today())
        d_from = _to_date(date_from, d_to)
        whs = warehouse or DEFAULT_FG_WAREHOUSE
        line_id = _to_int(line)
        lag = max(0, _to_int(sap_lag_days) or 0)
        # SAP FG receipts are often posted a day after the app marks the run
        # complete, so look ahead `lag` days on the SAP side only. The app
        # (produced) side stays scoped to the selected range.
        sap_to = d_to + timedelta(days=lag)

        completed = self._app_production_by_sku(d_from, d_to, line_id)
        in_progress = self._app_inprogress_by_sku(d_from, d_to, line_id)
        sap_items = self.reader.fg_by_item(whs, d_from, sap_to)
        rows = self._merge_production(completed, in_progress, sap_items)

        lag_note = (
            f" SAP receipts are included up to {lag} day(s) after the range "
            "to catch next-day postings." if lag else ""
        )
        return self._envelope(rows, sap_items, d_from, d_to, whs, {
            "app_unit": "cases",
            "sap_unit": "inventory UOM",
            "has_in_progress": True,
            "sap_lag_days": lag,
            "note": "Produced = completed runs' total_production; In-progress = live segment "
            "output from running runs (cases). SAP FG receipts (TransType 59) into "
            f"{whs} are converted from pieces to cases using the pack size parsed from the "
            f"SKU name (e.g. '20 PCS' → ÷20); matched by SKU name.{lag_note}",
        })

    # ------------------------------------------------------------------
    # Material (BOM) — should-be-used vs warehouse-issued (both from app data)
    # ------------------------------------------------------------------
    def get_material_reconciliation(
        self, date_from=None, date_to=None, warehouse=None, line=None, sap_lag_days=0
    ):
        # Local import avoids a warehouse↔production_execution app cycle.
        from warehouse.models import BOMRequestLine

        d_to = _to_date(date_to, date.today())
        d_from = _to_date(date_from, d_to)
        line_id = _to_int(line)
        # `sap_lag_days` can widen the SAP-side BH-PC window by N days each side.
        # Default 0 (same day): BH-PC is a shared warehouse that handles every
        # day's material, so widening sums unrelated transfers and over-counts —
        # only turn it on deliberately.
        lag = max(0, _to_int(sap_lag_days) or 0)

        runs = ProductionRun.objects.filter(
            company=self.company, date__gte=d_from, date__lte=d_to
        )
        if line_id:
            runs = runs.filter(line_id=line_id)
        produced = self._produced_by_run(runs)  # {run_id: FG produced (cases)}

        whs = warehouse or MATERIAL_WAREHOUSE_BY_COMPANY.get(
            self.company.code, DEFAULT_MATERIAL_WAREHOUSE
        )

        # by item: should-use (FG × per-unit BOM) + app-issued (warehouse BOM issue)
        line_qs = BOMRequestLine.objects.filter(
            bom_request__company=self.company,
            bom_request__production_run__date__gte=d_from,
            bom_request__production_run__date__lte=d_to,
        )
        if line_id:
            line_qs = line_qs.filter(bom_request__production_run__line_id=line_id)

        by_item: Dict[str, Dict[str, Any]] = {}
        for ln in line_qs.values(
            "item_code", "item_name", "per_unit_qty", "approved_qty", "issued_qty",
            "bom_request__production_run",
        ):
            code = ln["item_code"] or ""
            fg = produced.get(ln["bom_request__production_run"], 0.0)
            entry = by_item.setdefault(
                code, {"name": ln["item_name"] or code, "should": 0.0, "app": 0.0}
            )
            entry["should"] += float(ln["per_unit_qty"] or 0) * fg
            # Warehouse-committed qty: issued if actually issued, else the approved value.
            issued = float(ln["issued_qty"] or 0)
            approved = float(ln["approved_qty"] or 0)
            entry["app"] += issued if issued > 0 else approved

        # Use both app sources: add production material usage for items with no BOM line.
        usage_qs = ProductionMaterialUsage.objects.filter(
            production_run__company=self.company,
            production_run__date__gte=d_from,
            production_run__date__lte=d_to,
        )
        if line_id:
            usage_qs = usage_qs.filter(production_run__line_id=line_id)
        for u in usage_qs.values("material_code", "material_name").annotate(q=Sum("issued_qty")):
            code = u["material_code"] or ""
            if code in by_item:
                continue  # warehouse BOM line already provides the app-issued figure
            by_item.setdefault(code, {"name": u["material_name"] or code, "should": 0.0, "app": 0.0})
            by_item[code]["app"] += float(u["q"] or 0)

        # SAP issued: BOM transferred into BH-PC (TransType 67 InQty), within a
        # +/- lag window of the run date to catch early/late approvals.
        sap_from = d_from - timedelta(days=lag)
        sap_to = d_to + timedelta(days=lag)
        sap_by_code = {
            it["item_code"]: {"name": it["item_name"], "qty": float(it["sap_qty"])}
            for it in self.reader.material_issues_by_item(sap_from, sap_to, whs)
        }

        rows: List[Dict[str, Any]] = []
        for code, e in by_item.items():
            sap = sap_by_code.pop(code, None)
            rows.append(
                self._material_row(e["name"], code, e["should"], e["app"], sap["qty"] if sap else 0.0)
            )
        for code, sap in sap_by_code.items():  # SAP-only items
            rows.append(self._material_row(sap["name"] or code, code, 0.0, 0.0, sap["qty"]))
        rows.sort(
            key=lambda r: max(r["should_use"], r["app_issued"], r["sap_issued"]), reverse=True
        )

        return self._material_envelope(rows, d_from, d_to, whs, lag)

    def _produced_by_run(self, runs) -> Dict[int, float]:
        rows = list(runs.values("id", "total_production"))
        seg = {
            r["production_run"]: float(r["q"] or 0)
            for r in ProductionSegment.objects.filter(production_run__in=runs)
            .values("production_run")
            .annotate(q=Sum("produced_cases"))
        }
        out: Dict[int, float] = {}
        for r in rows:
            tp = float(r["total_production"] or 0)
            out[r["id"]] = tp if tp > 0 else seg.get(r["id"], 0.0)
        return out

    def _material_row(self, sku, item_code, should, app_issued, sap_issued):
        should = round(should, 3)
        app_issued = round(app_issued, 3)
        sap_issued = round(sap_issued, 3)
        # Status compares APP issued vs SAP issued.
        if app_issued > 0 and sap_issued == 0:
            status = "PENDING_SYNC"
        elif app_issued == 0 and sap_issued == 0:
            status = "MATCHED"
        else:
            denom = max(abs(app_issued), abs(sap_issued), 1.0)
            status = (
                "MATCHED"
                if abs(app_issued - sap_issued) / denom * 100 <= MATCH_TOLERANCE_PCT
                else "MISMATCH"
            )
        return {
            "sku": sku,
            "item_code": item_code,
            "should_use": should,
            "app_issued": app_issued,
            "sap_issued": sap_issued,
            "difference": round(app_issued - sap_issued, 3),
            "difference_pct": _diff_pct(app_issued, sap_issued),
            "status": status,
        }

    def _material_envelope(self, rows, d_from, d_to, whs, lag=0):
        should = round(sum(r["should_use"] for r in rows), 3)
        app = round(sum(r["app_issued"] for r in rows), 3)
        sap = round(sum(r["sap_issued"] for r in rows), 3)
        return {
            "by_sku": rows,
            "summary": {
                "should_use": should,
                "app_issued": app,
                "sap_issued": sap,
                "difference": round(app - sap, 3),
                "difference_pct": _diff_pct(app, sap),
                "status": _status(app, sap),
                "sku_count": len(rows),
            },
            "meta": {
                "date_from": d_from.isoformat(),
                "date_to": d_to.isoformat(),
                "warehouse": whs,
                "note": "Should-use = FG produced × BOM per-unit qty. App issued = warehouse-approved "
                "BOM qty (or issued qty once material is issued; + production material usage where no "
                f"BOM line). SAP issued = BOM transferred into {whs} (TransType 67 stock transfer, "
                + ("InQty) on the run date" if not lag else f"InQty) within +/-{lag} day(s) of the run")
                + ", matched by item code. Status compares App vs SAP issued.",
            },
        }

    # ------------------------------------------------------------------
    # Wastage — TransType 60
    # ------------------------------------------------------------------
    def get_wastage_reconciliation(self, date_from=None, date_to=None, warehouse=None, line=None):
        d_to = _to_date(date_to, date.today())
        d_from = _to_date(date_from, d_to)
        whs = warehouse or DEFAULT_WASTAGE_WAREHOUSE
        line_id = _to_int(line)

        waste = WasteLog.objects.filter(
            production_run__company=self.company,
            production_run__date__gte=d_from,
            production_run__date__lte=d_to,
        )
        if line_id:
            waste = waste.filter(production_run__line_id=line_id)
        # Match by item code (fall back to name when a waste row has no code).
        app_by_code: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"name": "", "qty": 0.0})
        for row in waste.values("material_code", "material_name").annotate(qty=Sum("wastage_qty")):
            key = (row["material_code"] or "").strip() or (row["material_name"] or "—")
            entry = app_by_code[key]
            entry["qty"] += float(row["qty"] or 0)
            if not entry["name"]:
                entry["name"] = row["material_name"] or key
        sap_items = self.reader.wastage_by_item(whs, d_from, d_to)
        rows = self._merge_by_code(app_by_code, sap_items)

        return self._envelope(rows, sap_items, d_from, d_to, whs, {
            "note": "App wastage (WasteLog) vs SAP scrap transferred into "
            f"{whs} (TransType 67 stock transfer, InQty), matched by item code.",
        })

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _app_production_by_sku(self, d_from, d_to, line_id=None) -> Dict[str, float]:
        # Completed production only — the qty the operator enters at completion,
        # which is what gets posted to SAP. In-progress runs contribute 0.
        qs = ProductionRun.objects.filter(
            company=self.company, date__gte=d_from, date__lte=d_to, status="COMPLETED"
        )
        if line_id:
            qs = qs.filter(line_id=line_id)
        rows = qs.values("product").annotate(qty=Sum("total_production"))
        return {(r["product"] or "—"): float(r["qty"] or 0) for r in rows}

    def _app_inprogress_by_sku(self, d_from, d_to, line_id=None) -> Dict[str, float]:
        # Live output from in-progress runs = sum of segment produced_cases.
        runs = ProductionRun.objects.filter(
            company=self.company,
            date__gte=d_from,
            date__lte=d_to,
            status="IN_PROGRESS",
        )
        if line_id:
            runs = runs.filter(line_id=line_id)
        run_rows = list(runs.values("id", "product"))
        seg_map: Dict[int, float] = {
            row["production_run"]: float(row["q"] or 0)
            for row in ProductionSegment.objects.filter(production_run__in=runs)
            .values("production_run")
            .annotate(q=Sum("produced_cases"))
        }
        by_sku: Dict[str, float] = defaultdict(float)
        for row in run_rows:
            by_sku[(row["product"] or "—")] += seg_map.get(row["id"], 0.0)
        return dict(by_sku)

    def _merge_production(self, completed, in_progress, sap_items):
        sap_by_name = {_norm(it["item_name"]): it for it in sap_items}
        used = set()
        rows: List[Dict[str, Any]] = []
        for sku in {*completed, *in_progress}:
            key = _norm(sku)
            match = sap_by_name.get(key)
            if not match and key:
                match = next(
                    (
                        it
                        for it in sap_items
                        if _norm(it["item_name"])
                        and (key in _norm(it["item_name"]) or _norm(it["item_name"]) in key)
                    ),
                    None,
                )
            sap_raw = float(match["sap_qty"]) if match else 0.0
            code = match["item_code"] if match else ""
            # SAP InQty is in pieces; convert to cases using the pack size.
            pack = _units_per_case(match["item_name"] if match else None, sku)
            sap_qty = round(sap_raw / pack, 3)
            if match:
                used.add(match["item_code"])
            rows.append(
                self._row(
                    sku,
                    code,
                    round(completed.get(sku, 0.0), 3),
                    sap_qty,
                    round(in_progress.get(sku, 0.0), 3),
                )
            )
        for it in sap_items:
            if it["item_code"] in used:
                continue
            pack = _units_per_case(it["item_name"])
            rows.append(self._row(it["item_name"] or it["item_code"], it["item_code"], 0.0,
                                  round(float(it["sap_qty"]) / pack, 3)))
        rows.sort(key=lambda r: max(r["app_qty"], r["sap_qty"], r["in_progress"]), reverse=True)
        return rows

    def _merge_by_name(self, app_by_sku, sap_items):
        sap_by_name = {_norm(it["item_name"]): it for it in sap_items}
        used = set()
        rows: List[Dict[str, Any]] = []
        for sku, app_qty in app_by_sku.items():
            key = _norm(sku)
            match = sap_by_name.get(key)
            if not match and key:
                match = next(
                    (
                        it
                        for it in sap_items
                        if _norm(it["item_name"])
                        and (key in _norm(it["item_name"]) or _norm(it["item_name"]) in key)
                    ),
                    None,
                )
            sap_qty = float(match["sap_qty"]) if match else 0.0
            code = match["item_code"] if match else ""
            if match:
                used.add(match["item_code"])
            rows.append(self._row(sku, code, round(app_qty, 3), round(sap_qty, 3)))
        for it in sap_items:
            if it["item_code"] in used:
                continue
            rows.append(self._row(it["item_name"] or it["item_code"], it["item_code"], 0.0,
                                  round(float(it["sap_qty"]), 3)))
        rows.sort(key=lambda r: max(r["app_qty"], r["sap_qty"]), reverse=True)
        return rows

    def _merge_by_code(self, app_by_code, sap_items):
        sap_by_code = {it["item_code"]: it for it in sap_items}
        used = set()
        rows: List[Dict[str, Any]] = []
        for code, entry in app_by_code.items():
            match = sap_by_code.get(code)
            sap_qty = float(match["sap_qty"]) if match else 0.0
            if match:
                used.add(code)
            rows.append(self._row(entry["name"] or code, code, round(entry["qty"], 3),
                                  round(sap_qty, 3)))
        for it in sap_items:
            if it["item_code"] in used:
                continue
            rows.append(self._row(it["item_name"] or it["item_code"], it["item_code"], 0.0,
                                  round(float(it["sap_qty"]), 3)))
        rows.sort(key=lambda r: max(r["app_qty"], r["sap_qty"]), reverse=True)
        return rows

    def _row(self, sku, item_code, app_qty, sap_qty, in_progress=0.0):
        return {
            "sku": sku,
            "item_code": item_code,
            "app_qty": app_qty,
            "in_progress": round(in_progress, 3),
            "sap_qty": sap_qty,
            "difference": round(app_qty - sap_qty, 3),
            "difference_pct": _diff_pct(app_qty, sap_qty),
            "status": _status(app_qty, sap_qty),
        }

    def _envelope(self, rows, sap_items, d_from, d_to, whs, extra_meta):
        app_total = round(sum(r["app_qty"] for r in rows), 3)
        sap_total = round(sum(r["sap_qty"] for r in rows), 3)
        return {
            "by_sku": rows,
            "sap_by_item": sap_items,
            "summary": {
                "app_qty": app_total,
                "sap_qty": sap_total,
                "difference": round(app_total - sap_total, 3),
                "difference_pct": _diff_pct(app_total, sap_total),
                "status": _status(app_total, sap_total),
                "sku_count": len(rows),
            },
            "meta": {
                "date_from": d_from.isoformat(),
                "date_to": d_to.isoformat(),
                "warehouse": whs,
                **extra_meta,
            },
        }
