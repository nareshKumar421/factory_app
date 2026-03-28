"""
non_moving_rm/services.py

Business logic for the Non-Moving Raw Material Dashboard.
Orchestrates HANA reads and computes dashboard aggregations.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from sap_client.context import CompanyContext

from .hana_reader import HanaNonMovingRMReader

logger = logging.getLogger(__name__)


class NonMovingRMService:
    """
    Orchestrates SAP HANA reads for the non-moving RM dashboard.

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
        rows = self.reader.get_non_moving_report(age, item_group)

        total_items = len(rows)
        total_value = sum(r["value"] for r in rows)
        total_quantity = sum(r["quantity"] for r in rows)

        # Group by branch for summary
        branch_summary = {}
        for r in rows:
            branch = r["branch"]
            if branch not in branch_summary:
                branch_summary[branch] = {
                    "branch": branch,
                    "item_count": 0,
                    "total_value": 0.0,
                    "total_quantity": 0.0,
                }
            branch_summary[branch]["item_count"] += 1
            branch_summary[branch]["total_value"] += r["value"]
            branch_summary[branch]["total_quantity"] += r["quantity"]

        # Round branch summary values
        for b in branch_summary.values():
            b["total_value"] = round(b["total_value"], 2)
            b["total_quantity"] = round(b["total_quantity"], 2)

        return {
            "data": rows,
            "summary": {
                "total_items": total_items,
                "total_value": round(total_value, 2),
                "total_quantity": round(total_quantity, 2),
                "by_branch": list(branch_summary.values()),
            },
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
