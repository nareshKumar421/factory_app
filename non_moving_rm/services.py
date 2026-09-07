"""
non_moving_rm/services.py

Business logic for the Non-Moving Raw Material Dashboard.
Orchestrates HANA reads and computes dashboard aggregations.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List

from sap_client.context import CompanyContext

from .hana_reader import COMPANY_BRANCH_LABELS, HanaNonMovingRMReader

logger = logging.getLogger(__name__)


class NonMovingRMService:
    """
    Orchestrates SAP HANA reads for the non-moving RM dashboard.

    Everything is read from the selected company's own schema — see
    ``hana_reader`` for why the central SAP procedure is no longer called.

    Usage:
        service = NonMovingRMService(company_code="JIVO_OIL")
        report = service.get_report(age=45, item_group=105)
    """

    def __init__(self, company_code: str):
        self.company_code = company_code
        self.context = CompanyContext(company_code)
        self.reader = HanaNonMovingRMReader(self.context)

    # ------------------------------------------------------------------
    # Report — Non-Moving RM Data
    # ------------------------------------------------------------------

    def get_report(self, age: int, item_group: int) -> Dict:
        """
        Returns non-moving raw material report with summary stats.
        """
        rows = self.reader.get_non_moving_report(
            age=age,
            item_group=item_group,
            branch_label=self._branch_label(),
        )

        total_value = sum(r["value"] for r in rows)
        total_quantity = sum(r["quantity"] for r in rows)

        # An item held in three warehouses is one item, three rows.
        total_items = len({r["item_code"] for r in rows})

        # Group by branch for summary
        branch_summary = {}
        for r in rows:
            branch = r["branch"]
            if branch not in branch_summary:
                branch_summary[branch] = {
                    "branch": branch,
                    "item_codes": set(),
                    "total_value": 0.0,
                    "total_quantity": 0.0,
                }
            branch_summary[branch]["item_codes"].add(r["item_code"])
            branch_summary[branch]["total_value"] += r["value"]
            branch_summary[branch]["total_quantity"] += r["quantity"]

        by_branch = [
            {
                "branch": b["branch"],
                "item_count": len(b["item_codes"]),
                "total_value": round(b["total_value"], 2),
                "total_quantity": round(b["total_quantity"], 2),
            }
            for b in branch_summary.values()
        ]

        return {
            "data": rows,
            "summary": {
                "total_items": total_items,
                "total_value": round(total_value, 2),
                "total_quantity": round(total_quantity, 2),
                "by_branch": by_branch,
            },
            "warehouse_summary": self._build_warehouse_summary(rows),
            "meta": {
                "age_days": age,
                "item_group": item_group,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            },
        }

    # ------------------------------------------------------------------
    # Dropdown — Item Groups
    # ------------------------------------------------------------------

    def get_item_groups(self) -> Dict:
        """
        Returns item groups for the dropdown filter.
        """
        groups = self.reader.get_item_groups()

        return {
            "data": groups,
            "meta": {
                "total_groups": len(groups),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            },
        }

    def _branch_label(self) -> str:
        return COMPANY_BRANCH_LABELS.get(self.company_code, self.company_code)

    def _build_warehouse_summary(self, rows: List[Dict]) -> List[Dict]:
        """Rolls the report up per warehouse.

        The report rows already carry the warehouse SAP holds the stock in, so
        these totals are the same numbers added a different way — nothing here
        is estimated.
        """
        buckets: Dict[str, Dict] = {}

        for row in rows:
            warehouse = row.get("warehouse") or ""
            if not warehouse:
                continue

            bucket = buckets.setdefault(
                warehouse,
                {
                    "warehouse": warehouse,
                    "warehouse_name": row.get("warehouse_name") or warehouse,
                    "items": {},
                    "total_quantity": 0.0,
                    "total_value": 0.0,
                },
            )
            item_totals = bucket["items"].setdefault(
                row["item_code"],
                {"quantity": 0.0, "value": 0.0},
            )
            item_totals["quantity"] += row["quantity"]
            item_totals["value"] += row["value"]
            bucket["total_quantity"] += row["quantity"]
            bucket["total_value"] += row["value"]

        summary = []
        for bucket in buckets.values():
            items = sorted(
                (
                    {
                        "item_code": item_code,
                        "quantity": round(totals["quantity"], 3),
                        "value": round(totals["value"], 2),
                    }
                    for item_code, totals in bucket["items"].items()
                ),
                key=lambda row: row["value"],
                reverse=True,
            )
            summary.append({
                "warehouse": bucket["warehouse"],
                "warehouse_name": bucket["warehouse_name"],
                "item_count": len(bucket["items"]),
                "total_quantity": round(bucket["total_quantity"], 3),
                "total_value": round(bucket["total_value"], 2),
                "items": items,
            })

        return sorted(summary, key=lambda row: row["total_value"], reverse=True)
