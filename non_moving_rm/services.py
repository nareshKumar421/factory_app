"""
non_moving_rm/services.py

Business logic for the Non-Moving Raw Material Dashboard.
Orchestrates HANA reads and computes dashboard aggregations.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from sap_client.context import CompanyContext

from .hana_reader import COMPANY_BRANCH_LABELS, HanaNonMovingRMReader

logger = logging.getLogger(__name__)

PROCEDURE_SOURCE_COMPANY_CODE = "JIVO_BEVERAGES"
PROCEDURE_SOURCE_SCHEMA = "JIVO_BEVERAGES_HANADB"


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
        self.report_context = CompanyContext(PROCEDURE_SOURCE_COMPANY_CODE)
        self.reader = HanaNonMovingRMReader(
            self.report_context,
            schema_override=PROCEDURE_SOURCE_SCHEMA,
        )
        self.company_reader = HanaNonMovingRMReader(self.context)

    # ------------------------------------------------------------------
    # Report — Non-Moving RM Data
    # ------------------------------------------------------------------

    def get_report(self, age: int, item_group: int) -> Dict:
        """
        Returns non-moving raw material report with summary stats.
        """
        rows = self.reader.get_non_moving_report(age, item_group)
        rows = [
            row for row in rows
            if self._matches_company_branch(row) and self._meets_age_threshold(row, age)
        ]
        rows = self._fallback_to_company_stock_age_report(
            rows=rows,
            age=age,
            item_group=item_group,
        )
        warehouse_distribution = self.company_reader.get_warehouse_distribution(
            [row.get("item_code", "") for row in rows]
        )

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
            "warehouse_summary": self._build_warehouse_summary(
                rows,
                warehouse_distribution,
            ),
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

    @staticmethod
    def _meets_age_threshold(row: Dict, age: int) -> bool:
        if age <= 0:
            return True
        days_since_last_movement = row.get("days_since_last_movement")
        if days_since_last_movement is None:
            return True
        return int(days_since_last_movement) > age

    def _matches_company_branch(self, row: Dict) -> bool:
        branch = row.get("branch")
        expected_branch = COMPANY_BRANCH_LABELS.get(self.company_code)
        if not expected_branch:
            return True
        return branch == expected_branch

    def _fallback_to_company_stock_age_report(
        self,
        *,
        rows: List[Dict],
        age: int,
        item_group: int,
    ) -> List[Dict]:
        if rows or not item_group:
            return rows

        branch_label = COMPANY_BRANCH_LABELS.get(self.company_code)
        if not branch_label:
            return rows

        logger.info(
            "Falling back to company stock-age query for non-moving report: company=%s item_group=%s age=%s",
            self.company_code,
            item_group,
            age,
        )
        return self.company_reader.get_stock_age_report(
            age=age,
            item_group=item_group,
            branch_label=branch_label,
        )

    def _build_warehouse_summary(
        self,
        rows: List[Dict],
        warehouse_distribution: List[Dict],
    ) -> List[Dict]:
        report_by_item: Dict[str, Dict[str, float]] = {}
        for row in rows:
            item_code = row.get("item_code") or ""
            if not item_code:
                continue
            existing = report_by_item.setdefault(
                item_code,
                {"quantity": 0.0, "value": 0.0},
            )
            existing["quantity"] += row["quantity"]
            existing["value"] += row["value"]

        distribution_by_item: Dict[str, List[Dict]] = {}
        for row in warehouse_distribution:
            distribution_by_item.setdefault(row["item_code"], []).append(row)

        buckets: Dict[str, Dict] = {}
        for item_code, report_totals in report_by_item.items():
            distributions = distribution_by_item.get(item_code, [])
            distribution_total = sum(abs(row["quantity"]) for row in distributions)
            if distribution_total <= 0:
                self._add_warehouse_bucket(
                    buckets,
                    warehouse="Unassigned",
                    warehouse_name="No current warehouse stock found",
                    quantity=report_totals["quantity"],
                    value=report_totals["value"],
                    item_code=item_code,
                )
                continue

            for distribution in distributions:
                ratio = abs(distribution["quantity"]) / distribution_total
                self._add_warehouse_bucket(
                    buckets,
                    warehouse=distribution["warehouse"],
                    warehouse_name=distribution["warehouse_name"],
                    quantity=report_totals["quantity"] * ratio,
                    value=report_totals["value"] * ratio,
                    item_code=item_code,
                )

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

    @staticmethod
    def _add_warehouse_bucket(
        buckets: Dict[str, Dict],
        *,
        warehouse: str,
        warehouse_name: str,
        quantity: float,
        value: float,
        item_code: str,
    ) -> None:
        bucket = buckets.setdefault(
            warehouse,
            {
                "warehouse": warehouse,
                "warehouse_name": warehouse_name,
                "items": {},
                "total_quantity": 0.0,
                "total_value": 0.0,
            },
        )
        item_totals = bucket["items"].setdefault(
            item_code,
            {"quantity": 0.0, "value": 0.0},
        )
        item_totals["quantity"] += quantity
        item_totals["value"] += value
        bucket["total_quantity"] += quantity
        bucket["total_value"] += value
