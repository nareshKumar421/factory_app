"""
pm_demand/app_reader.py

The same board, read from FactoryFlow's own records instead of SAP.

WHY THIS IS NOT A MIRROR OF THE SAP READER
------------------------------------------
The app does not hold everything SAP does, and pretending otherwise would put
zeroes on screen where the answer is really "we do not record that". Measured
on the live database for Oil, August 2026:

    figure              app                     SAP
    FG produced         1,353,337 pcs           1,754,431 pcs
    FG dispatched         710,808 pcs           1,971,861 pcs
    PM issued                   0  <- empty     6,833,909
    PM approved        22,290,627 (RM+PM)              n/a
    PM stock                 none                   3.3 Cr
    open POs                 none              8.6 M units

Two fields that ought to carry this are never written by any code path:
``ProductionMaterialUsage.issued_qty`` sums to 0.000 across all 1,147 August
rows, and so does ``BOMRequestLine.issued_qty``. So the app simply has no
record of what the line issued.

What it does have is what the warehouse **approved**:
``BOMRequestLine.approved_qty``. That is a different fact from consumption --
it is the BOM signed off before the run, not the material that went through
the machine -- so this reader returns it under its own name and the API
reports ``consumption_basis: "approved"`` so nothing can quietly read it as
actual issue.

WHAT ALWAYS COMES FROM SAP, WHATEVER THE TOGGLE SAYS
----------------------------------------------------
**The recipe.** A bill of material is a master record and it lives in
``OITT``/``ITT1``. The app's ``BOMRequestLine`` is a per-run copy, so building
a per-unit recipe from it would average across runs and disagree with itself
between periods. Both modes explode through the SAP BOM.

**Stock and open purchase orders.** The app has neither, so days of cover is
SAP-sourced in both modes. The panel says so.

**Which item codes are packing material.** ``OITM.ItmsGrpCod`` = 105 is the
only authority; the app tables carry item codes but no item group. So the SAP
item master is read even in app mode, and app rows are filtered against it.
That also keeps 'PM' prefix matching out of this module -- a prefix is a
naming convention and 22.3 M of ``approved_qty`` is RM and PM mixed together,
which without a real group filter would report oil as packaging.

DATES
-----
Production is dated by ``ProductionRun.date`` -- the run's own production date,
not when the row was typed in. Dispatch is dated by ``created_at`` on the
gate-out line rather than ``sap_doc_date``, because the gate-out IS the app's
record of the truck leaving and 974 lines carry a created_at against 786 with
a SAP doc date; dating by the SAP field would silently drop the difference.
"""

import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence

from django.db.models import Sum

logger = logging.getLogger(__name__)


class PmDemandAppReader:
    """Reads FactoryFlow's own tables for one company."""

    def __init__(self, company_code: str):
        self.company_code = company_code

    # ------------------------------------------------------------------
    # Finished goods produced
    # ------------------------------------------------------------------

    def fg_produced(self, date_from, date_to) -> List[Dict[str, Any]]:
        """Finished goods the floor recorded making, per item, in PIECES.

        Production runs are entered in CASES, so every quantity is multiplied
        by the run's own ``pieces_per_case`` before it can be compared with
        anything from SAP -- which counts in pieces throughout. The factor is
        taken from the run rather than parsed out of the SKU name: the name
        states pack size and carton size separately and lies about both, which
        is the trap ``dispatch_plans`` had to be rewritten to stop falling
        into.

        A run with no ``item_code`` yet is keyed by product NAME, because the
        board still has to show it. It will not match an SAP BOM, and the
        response's coverage block is what makes that visible.
        """
        from production_execution.models import ProductionRun

        runs = ProductionRun.objects.filter(
            company__code=self.company_code,
            date__gte=date_from,
            date__lte=date_to,
        ).values("item_code", "product", "total_production", "pieces_per_case")

        totals: Dict[str, Dict[str, Any]] = {}
        for run in runs:
            code = (run["item_code"] or "").strip() or (run["product"] or "").strip()
            if not code:
                continue
            cases = float(run["total_production"] or 0)
            per_case = float(run["pieces_per_case"] or 1) or 1
            bucket = totals.setdefault(
                code, {"item_code": code, "item_name": run["product"] or "", "qty": 0.0}
            )
            bucket["qty"] += cases * per_case

        return [row for row in totals.values() if row["qty"]]

    # ------------------------------------------------------------------
    # Finished goods dispatched
    # ------------------------------------------------------------------

    def fg_dispatched(self, date_from, date_to) -> List[Dict[str, Any]]:
        """Finished goods the gate recorded leaving, per item.

        ``dispatched_quantity`` looks like the obvious field and is NULL in
        every one of the 2,510 August rows, so the billed ``quantity`` is what
        is summed. That is what the gate pass said should go, which for this
        board is the honest app-side answer.

        No intercompany split and no credit-note netting: the app records a
        truck leaving, not who was invoiced, so the summary's intercompany
        figures stay SAP-only. Nothing here is netted against returns either --
        a return comes back through the returns module, not as a negative
        gate-out line.
        """
        from gate_core.models import SalesDispatchGateOutItem

        rows = (
            SalesDispatchGateOutItem.objects.filter(
                sales_dispatch__company__code=self.company_code,
                created_at__date__gte=date_from,
                created_at__date__lte=date_to,
            )
            .values("item_code", "item_name")
            .annotate(qty=Sum("quantity"))
        )

        return [
            {
                "item_code": (row["item_code"] or "").strip(),
                "item_name": row["item_name"] or "",
                "qty": float(row["qty"] or 0),
                # The app cannot tell a group-company truck from any other, so
                # these stay zero rather than guess. The response says so.
                "intercompany_qty": 0.0,
                "return_qty": 0.0,
            }
            for row in rows
            if (row["item_code"] or "").strip() and row["qty"]
        ]

    # ------------------------------------------------------------------
    # Packing material approved to the line
    # ------------------------------------------------------------------

    def pm_movements(
        self, date_from, date_to, pm_codes: Iterable[str]
    ) -> List[Dict[str, Any]]:
        """What the warehouse approved, and what the floor logged as waste.

        ``approved_qty`` NOT ``issued_qty``: the latter is written by nothing
        and sums to zero. It is returned in the ``issued_qty`` slot so the
        rest of the module needs no special case, and the API labels the whole
        column ``approved`` so the screen cannot present it as actual issue.

        Filtered to ``pm_codes`` -- the packing-material codes from the SAP
        item master -- because ``approved_qty`` covers raw material too:
        22.3 M unfiltered, most of it loose oil in litres.

        ``in_house_qty`` is always zero. The app has no record of the factory
        making its own packaging, so the in-house badge is an SAP-mode fact.
        """
        from production_execution.models import WasteLog
        from warehouse.models import BOMRequestLine

        wanted = {code for code in pm_codes if code}
        if not wanted:
            return []

        approved = (
            BOMRequestLine.objects.filter(
                bom_request__company__code=self.company_code,
                bom_request__production_run__date__gte=date_from,
                bom_request__production_run__date__lte=date_to,
                item_code__in=wanted,
            )
            .values("item_code")
            .annotate(qty=Sum("approved_qty"), required=Sum("required_qty"))
        )

        wastage = (
            WasteLog.objects.filter(
                company__code=self.company_code,
                production_run__date__gte=date_from,
                production_run__date__lte=date_to,
                material_code__in=wanted,
            )
            .values("material_code")
            .annotate(qty=Sum("wastage_qty"))
        )

        merged: Dict[str, Dict[str, Any]] = {}
        for row in approved:
            code = (row["item_code"] or "").strip()
            if not code:
                continue
            merged.setdefault(code, self._blank(code))
            merged[code]["issued_qty"] = float(row["qty"] or 0)
            merged[code]["required_qty"] = float(row["required"] or 0)

        for row in wastage:
            code = (row["material_code"] or "").strip()
            if not code:
                continue
            merged.setdefault(code, self._blank(code))
            merged[code]["wastage_qty"] = float(row["qty"] or 0)

        return list(merged.values())

    @staticmethod
    def _blank(code: str) -> Dict[str, Any]:
        return {
            "item_code": code,
            "issued_qty": 0.0,
            "required_qty": 0.0,
            "wastage_qty": 0.0,
            "in_house_qty": 0.0,
            "upstream_qty": 0.0,
        }
