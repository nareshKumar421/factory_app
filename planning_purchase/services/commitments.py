"""Explaining a committed-stock figure.

`OITW."IsCommited"` is the number that decides whether a component reads as
available, and SAP publishes it with no explanation at all. That matters here
because on this company most components are over-committed — more reserved than
physically present — and a buyer looking at a shortage has no way to tell whether
the reservation is a real upcoming run or an order somebody abandoned two years
ago.

So this returns the documents behind the figure, and always checks that they add
up. If the breakdown and `IsCommited` disagree, the response says so rather than
presenting a partial list as the whole story: a confident-looking explanation that
is missing a document type would be worse than no explanation.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.utils import timezone

from .errors import PlanningError

logger = logging.getLogger(__name__)

ZERO = Decimal(0)

# A reservation whose due date passed this long ago is almost certainly not a run
# anybody is still waiting for. On this company mustard oil is held by production
# orders due in November 2024, which is the single most useful thing this screen
# can tell a planner: that stock can probably be released.
STALE_AFTER_DAYS = 60

SOURCE_LABELS = {
    "PRODUCTION_ORDER": "Production order",
    "TRANSFER_REQUEST": "Transfer request",
    "SALES_ORDER": "Sales order",
}


def _dec(value) -> Decimal:
    if value is None:
        return ZERO
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _as_date(value) -> Optional[date]:
    if value is None:
        return None
    return value.date() if hasattr(value, "date") else value


class CommitmentsMixin:
    """Committed-stock breakdown. Mixed into `PlanService`."""

    def get_commitments(self, item_code: str, warehouse: str) -> Dict[str, Any]:
        item_code = (item_code or "").strip()
        warehouse = (warehouse or "").strip()
        if not item_code or not warehouse:
            raise PlanningError("An item code and a warehouse are both required.")

        stock_row = self.reader.get_item_warehouse_stock(item_code, warehouse)
        if not stock_row:
            raise PlanningError(
                f"{item_code} has no stock record in {warehouse}.",
                "not_found",
                404,
            )

        committed = _dec(stock_row.get("IsCommited"))
        rows = self.reader.get_commitment_breakdown(item_code, warehouse)
        today = timezone.localdate()

        documents = [self._map_document(row, today) for row in rows]
        documents.sort(key=lambda d: (d["due_date"] is None, d["due_date"]))

        explained = sum((d["committed_qty"] for d in documents), ZERO)
        stale = [d for d in documents if d["is_stale"]]

        return {
            "item_code": item_code,
            "item_name": stock_row.get("ItemName") or "",
            "warehouse": warehouse,
            "uom": stock_row.get("Uom") or "",
            "on_hand_qty": _dec(stock_row.get("OnHand")),
            "committed_qty": committed,
            "on_order_qty": _dec(stock_row.get("OnOrder")),
            "free_qty": _dec(stock_row.get("OnHand")) - committed,
            "documents": documents,
            "by_source": self._by_source(documents),
            "meta": {
                "company_code": self.company_code,
                "document_count": len(documents),
                "explained_qty": explained,
                # The honesty check. SAP recalculates IsCommited on its own
                # schedule, so a small gap can be timing rather than a missing
                # document type -- either way the reader is told, not guessed at.
                "unexplained_qty": committed - explained,
                "reconciles": abs(committed - explained) < Decimal("0.001"),
                "stale_document_count": len(stale),
                "stale_qty": sum((d["committed_qty"] for d in stale), ZERO),
                "stale_after_days": STALE_AFTER_DAYS,
                "fetched_at": timezone.now().isoformat(),
                "notes": [
                    "A production order reserves what it has left to draw "
                    "(planned minus issued) while it is Planned or Released.",
                    "A transfer request reserves its open quantity at the sending "
                    "warehouse.",
                    "A sales order reserves its open quantity. Rare on a factory "
                    "warehouse, but included so the total can be trusted.",
                ],
            },
        }

    def _map_document(self, row: Dict[str, Any], today: date) -> Dict[str, Any]:
        source = row.get("Source") or ""
        due = _as_date(row.get("DueDate"))
        days_overdue = (today - due).days if due and due < today else 0

        return {
            "source": source,
            "source_label": SOURCE_LABELS.get(source, source),
            "doc_entry": row.get("DocEntry"),
            "doc_num": row.get("DocNum"),
            "doc_status": row.get("DocStatus") or "",
            # For a production order this is the finished good being made; for a
            # transfer, the receiving warehouse; for a sales order, the customer.
            # One column, because "what is this reservation for" is one question.
            "reference_code": row.get("RefCode") or "",
            "reference_name": row.get("RefName") or "",
            "to_warehouse": row.get("ToWarehouse") or "",
            "planned_qty": _dec(row.get("PlannedQty")),
            "issued_qty": _dec(row.get("IssuedQty")),
            "committed_qty": _dec(row.get("CommittedQty")),
            "doc_date": _as_date(row.get("DocDate")),
            "due_date": due,
            "days_overdue": days_overdue,
            "is_stale": days_overdue > STALE_AFTER_DAYS,
        }

    @staticmethod
    def _by_source(documents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        totals: Dict[str, Dict[str, Any]] = {}
        for doc in documents:
            entry = totals.setdefault(doc["source"], {
                "source": doc["source"],
                "source_label": doc["source_label"],
                "document_count": 0,
                "committed_qty": ZERO,
                "stale_qty": ZERO,
            })
            entry["document_count"] += 1
            entry["committed_qty"] += doc["committed_qty"]
            if doc["is_stale"]:
                entry["stale_qty"] += doc["committed_qty"]
        return sorted(totals.values(), key=lambda e: -e["committed_qty"])
