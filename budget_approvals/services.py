"""
budget_approvals/services.py

Business logic for the Budget Approvals Dashboard.

Calls the DRAFT_APPROVAL_Budget procedure, keeps only the Factory budget
lines, and serves filtered/paginated slices with summary aggregates.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from django.core.cache import cache

from sap_client.context import CompanyContext

from .hana_reader import HanaBudgetApprovalReader

logger = logging.getLogger(__name__)

# DRAFT_APPROVAL_Budget covers the Oil and Beverages branches; the reader
# mirrors it with one query per branch schema, using the Oil credentials
# (one HANA login sees every company schema) regardless of the caller's
# selected company.
CONNECTION_COMPANY_CODE = "JIVO_OIL"
BRANCH_COMPANY_CODES = {
    "OIL": "JIVO_OIL",
    "BEVERAGE": "JIVO_BEVERAGES",
}

# The dashboard is pinned to the Factory budget head (DRF1.OcrCode3).
BUDGET_FILTER = "FACTORY"

# A short cache keeps paging and filtering off SAP without going stale.
CACHE_TTL_SECONDS = 180

STATUS_CODES = {
    "pending": "W",
    "approved": "Y",
    "rejected": "N",
}
STATUS_LABELS = {
    "W": "Pending",
    "Y": "Approved",
    "N": "Rejected",
}

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500

# Columns that accept Excel-style value filters (checkbox lists of distinct
# values) and the ones headers may sort by.
COLUMN_FILTER_FIELDS = {
    "branch",
    "obj_type_label",
    "card_name",
    "acct_name",
    "sub_budget",
    "effect_month",
    "current_month",
    "state",
    "owner",
    "approver",
}
SORTABLE_FIELDS = COLUMN_FILTER_FIELDS | {
    "doc_entry",
    "doc_date",
    "amount",
    "current_month_posted_amount",
    "created_date",
    "status",
}
NUMERIC_SORT_FIELDS = {"doc_entry", "amount", "current_month_posted_amount"}
MAX_COLUMN_VALUES = 1000


class BudgetApprovalService:
    """
    Orchestrates SAP HANA reads for the Budget Approvals dashboard.

    Usage:
        service = BudgetApprovalService()
        report = service.get_report(status="pending", page=1, page_size=50)
    """

    def __init__(self):
        self.context = CompanyContext(CONNECTION_COMPANY_CODE)
        branch_schemas = {
            label: CompanyContext(code).hana["schema"]
            for label, code in BRANCH_COMPANY_CODES.items()
        }
        self.reader = HanaBudgetApprovalReader(self.context, branch_schemas)

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    def get_report(
        self,
        *,
        status: str = "",
        branch: str = "",
        effect_month: str = "",
        search: str = "",
        column_filters: Dict[str, List[str]] = None,
        sort_by: str = "",
        sort_dir: str = "desc",
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
        refresh: bool = False,
    ) -> Dict[str, Any]:
        rows, fetched_at, from_cache = self._factory_rows(refresh=refresh)

        filtered = self._apply_filters(
            rows,
            status=status,
            branch=branch,
            effect_month=effect_month,
            search=search,
        )
        filtered = self._apply_column_filters(filtered, column_filters or {})
        filtered = self._sort_rows(filtered, sort_by, sort_dir)

        total_rows = len(filtered)
        page_size = max(1, min(page_size, MAX_PAGE_SIZE))
        total_pages = max(1, -(-total_rows // page_size))
        page = max(1, min(page, total_pages))
        start = (page - 1) * page_size
        page_rows = filtered[start:start + page_size]

        return {
            "data": page_rows,
            "summary": self._build_summary(filtered),
            "options": self._build_options(rows),
            "meta": {
                "budget": BUDGET_FILTER,
                "page": page,
                "page_size": page_size,
                "total_rows": total_rows,
                "total_pages": total_pages,
                "fetched_at": fetched_at,
                "from_cache": from_cache,
            },
        }

    # ------------------------------------------------------------------
    # Data Loading
    # ------------------------------------------------------------------

    def _factory_rows(self, *, refresh: bool):
        cache_key = f"budget_approvals:factory_rows:{self.reader.schema}"

        if not refresh:
            cached = cache.get(cache_key)
            if cached is not None:
                return cached["rows"], cached["fetched_at"], True

        all_rows = self.reader.get_draft_approval_rows(BUDGET_FILTER)
        # The reader already filters on the budget dimension; this guards the
        # contract if its query ever changes.
        rows = [
            row for row in all_rows
            if (row.get("budget") or "").strip().upper() == BUDGET_FILTER
        ]
        rows.sort(
            key=lambda row: (
                row.get("created_date") or "",
                row.get("created_time") or "",
                row.get("doc_entry") or 0,
                row.get("line_num") or 0,
            ),
            reverse=True,
        )
        fetched_at = datetime.now(timezone.utc).isoformat()
        cache.set(cache_key, {"rows": rows, "fetched_at": fetched_at}, CACHE_TTL_SECONDS)

        logger.info(
            "Budget approvals refreshed from SAP: %s Factory lines (of %s total)",
            len(rows),
            len(all_rows),
        )
        return rows, fetched_at, False

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_filters(
        rows: List[Dict],
        *,
        status: str,
        branch: str,
        effect_month: str,
        search: str,
    ) -> List[Dict]:
        result = rows

        status_code = STATUS_CODES.get(status.strip().lower()) if status else None
        if status_code:
            result = [r for r in result if r["status"] == status_code]

        if branch:
            wanted = branch.strip().upper()
            result = [r for r in result if r["branch"].upper() == wanted]

        if effect_month:
            wanted = effect_month.strip().upper()
            result = [r for r in result if r["effect_month"].upper() == wanted]

        if search:
            term = search.strip().lower()
            if term:
                result = [
                    r for r in result
                    if term in r["card_code"].lower()
                    or term in r["card_name"].lower()
                    or term in r["acct_code"].lower()
                    or term in r["acct_name"].lower()
                    or term in r["owner"].lower()
                    or term in r["sub_budget"].lower()
                    or term in r["line_remarks"].lower()
                    or term in r["comments"].lower()
                    or term == str(r["doc_entry"])
                ]

        return result

    @staticmethod
    def _apply_column_filters(
        rows: List[Dict], column_filters: Dict[str, List[str]]
    ) -> List[Dict]:
        """Excel-style filters: each column keeps rows whose value is in the
        selected set."""
        for field, values in column_filters.items():
            if field not in COLUMN_FILTER_FIELDS or not values:
                continue
            wanted = set(values)
            rows = [r for r in rows if r.get(field, "") in wanted]
        return rows

    @staticmethod
    def _sort_rows(rows: List[Dict], sort_by: str, sort_dir: str) -> List[Dict]:
        if sort_by not in SORTABLE_FIELDS:
            return rows

        reverse = sort_dir != "asc"
        if sort_by in NUMERIC_SORT_FIELDS:
            key = lambda r: r.get(sort_by) or 0  # noqa: E731
        else:
            # Case-insensitive, like a spreadsheet; Nones/blanks sort together.
            key = lambda r: str(r.get(sort_by) or "").lower()  # noqa: E731
        return sorted(rows, key=key, reverse=reverse)

    # ------------------------------------------------------------------
    # Column Values (Excel-style filter dropdowns)
    # ------------------------------------------------------------------

    def get_column_values(
        self,
        *,
        field: str,
        status: str = "",
        branch: str = "",
        effect_month: str = "",
        search: str = "",
        column_filters: Dict[str, List[str]] = None,
        refresh: bool = False,
    ) -> Dict[str, Any]:
        """
        Distinct values for one column, computed over the dataset filtered by
        everything EXCEPT that column — the Excel/DBeaver dropdown contract.
        """
        rows, fetched_at, _ = self._factory_rows(refresh=refresh)
        filtered = self._apply_filters(
            rows,
            status=status,
            branch=branch,
            effect_month=effect_month,
            search=search,
        )
        other_filters = {
            f: v for f, v in (column_filters or {}).items() if f != field
        }
        filtered = self._apply_column_filters(filtered, other_filters)

        counts: Dict[str, int] = {}
        for row in filtered:
            counts[row.get(field, "") or ""] = counts.get(row.get(field, "") or "", 0) + 1

        values = sorted(counts.items(), key=lambda item: item[0].lower())
        truncated = len(values) > MAX_COLUMN_VALUES
        values = values[:MAX_COLUMN_VALUES]

        return {
            "field": field,
            "values": [{"value": v, "count": c} for v, c in values],
            "meta": {
                "total_values": len(values),
                "truncated": truncated,
                "fetched_at": fetched_at,
            },
        }

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    @staticmethod
    def _build_summary(rows: List[Dict]) -> Dict[str, Any]:
        by_status: Dict[str, Dict[str, Any]] = {}
        documents = set()

        for row in rows:
            documents.add((row["branch"], row["obj_type"], row["doc_entry"]))
            code = row["status"]
            bucket = by_status.setdefault(
                code,
                {
                    "status": code,
                    "status_label": STATUS_LABELS.get(code, code),
                    "line_count": 0,
                    "total_amount": 0.0,
                },
            )
            bucket["line_count"] += 1
            bucket["total_amount"] += row["amount"]

        for bucket in by_status.values():
            bucket["total_amount"] = round(bucket["total_amount"], 2)

        pending = by_status.get("W", {})
        return {
            "total_lines": len(rows),
            "total_documents": len(documents),
            "total_amount": round(sum(r["amount"] for r in rows), 2),
            "pending_lines": pending.get("line_count", 0),
            "pending_amount": pending.get("total_amount", 0.0),
            "by_status": sorted(by_status.values(), key=lambda b: b["status"]),
        }

    @staticmethod
    def _build_options(rows: List[Dict]) -> Dict[str, List[str]]:
        """Distinct filter values from the full Factory dataset."""
        branches = sorted({r["branch"] for r in rows if r["branch"]})

        def month_sort_key(value: str):
            # EFFECTMONTH values look like MM-YYYY; sort newest first and
            # push anything malformed to the end.
            parts = value.split("-")
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                return (0, -int(parts[1]), -int(parts[0]))
            return (1, 0, 0)

        effect_months = sorted(
            {r["effect_month"] for r in rows if r["effect_month"]},
            key=month_sort_key,
        )
        return {"branches": branches, "effect_months": effect_months}
