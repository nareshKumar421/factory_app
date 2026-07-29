"""
production_movements/services.py

Config helpers + the read-only production-stock board service (P0).

The board merges the per-company WarehouseRole config with LIVE SAP on-hand
(via the existing warehouse.WMSHanaReader) so operators see each production
warehouse tagged with its role and current stock.
"""

import logging
from decimal import Decimal
from typing import Dict, List, Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from company.models import Company
from sap_client.client import SAPClient
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from warehouse.services.wms_hana_reader import WMSHanaReader

from .constants import ITEM_GROUP_PM_NAME, ItemFamily
from .models import (
    MovementStatus,
    MovementType,
    WarehouseMovement,
    WarehouseMovementLine,
    WarehouseRole,
)

logger = logging.getLogger(__name__)

# SAP object type for the Inventory Transfer Request UDO (StockTransfer line BaseType).
ITR_BASE_TYPE = 1250000001


# ---------------------------------------------------------------------------
# Config helpers (used by P0 read APIs and, later, P1 writers)
# ---------------------------------------------------------------------------
def get_roles_for_company(company_code: str, active_only: bool = True) -> List[WarehouseRole]:
    qs = WarehouseRole.objects.filter(company__code=company_code)
    if active_only:
        qs = qs.filter(is_active=True)
    return list(qs)


def get_bom_issue_point(company_code: str, family: str = ItemFamily.PM) -> Optional[str]:
    """The single warehouse BOM is issued from for a company/family (e.g. Oil->BH-PC)."""
    role = (
        WarehouseRole.objects.filter(
            company__code=company_code,
            family=family,
            is_bom_issue_point=True,
            is_active=True,
        )
        .values_list("whs_code", flat=True)
        .first()
    )
    return role


def get_grpo_target(company_code: str, family: str = ItemFamily.PM) -> Optional[str]:
    """The warehouse GRPO for a family is received into (e.g. PM -> BH-PM)."""
    return (
        WarehouseRole.objects.filter(
            company__code=company_code,
            family=family,
            is_grpo_target=True,
            is_active=True,
        )
        .values_list("whs_code", flat=True)
        .first()
    )


def get_pm_store_codes(company_code: str) -> List[str]:
    """PM stores that feed the issue point (transfer sources)."""
    from .constants import WarehouseRoleType

    return list(
        WarehouseRole.objects.filter(
            company__code=company_code,
            role=WarehouseRoleType.PM_STORE,
            is_active=True,
        ).values_list("whs_code", flat=True)
    )


def get_transfer_options(company_code: str) -> Dict:
    """
    What the transfer form needs: the issue point and the stores that may feed
    it (any active warehouse whose feeds_whs_code == the issue point).
    """
    issue_point = get_bom_issue_point(company_code)
    sources = []
    if issue_point:
        for r in WarehouseRole.objects.filter(
            company__code=company_code, is_active=True, feeds_whs_code=issue_point
        ):
            sources.append({
                "whs_code": r.whs_code,
                "warehouse_name": r.warehouse_name,
                "role": r.role,
                "needs_transfer_request": r.transfer_needs_request,
            })
    return {
        "company_code": company_code,
        "issue_point": issue_point,
        "sources": sources,
        "sap_writes_enabled": bool(
            getattr(settings, "PRODUCTION_MOVEMENTS_SAP_WRITES_ENABLED", False)
        ),
    }


# ---------------------------------------------------------------------------
# Stock board
# ---------------------------------------------------------------------------
class ProductionStockService:
    """Role-tagged live-SAP stock for a company's production warehouses."""

    def __init__(self, company_code: str):
        self.company_code = company_code

    def get_stock_board(self) -> Dict:
        """
        One row per configured production warehouse, tagged with its role and
        current SAP on-hand/value. Warehouses present in SAP but NOT mapped are
        returned separately as `unmapped` so config gaps stay visible.
        """
        roles = get_roles_for_company(self.company_code)
        role_by_code = {r.whs_code: r for r in roles}

        reader = WMSHanaReader(self.company_code)
        summary_rows = reader.get_warehouse_summary()
        summary_by_code = {row["warehouse_code"]: row for row in summary_rows}

        warehouses = []
        for r in roles:
            s = summary_by_code.get(r.whs_code, {})
            warehouses.append(
                {
                    "whs_code": r.whs_code,
                    "warehouse_name": r.warehouse_name or s.get("warehouse_name", ""),
                    "role": r.role,
                    "family": r.family,
                    "is_grpo_target": r.is_grpo_target,
                    "is_bom_issue_point": r.is_bom_issue_point,
                    "feeds_whs_code": r.feeds_whs_code,
                    "needs_review": r.needs_review,
                    "notes": r.notes,
                    "total_items": s.get("total_items", 0),
                    "total_on_hand": s.get("total_on_hand", 0.0),
                    "total_value": s.get("total_value", 0.0),
                    "in_sap": r.whs_code in summary_by_code,
                }
            )

        # SAP warehouses holding stock that have no role mapping yet.
        unmapped = [
            {
                "whs_code": row["warehouse_code"],
                "warehouse_name": row["warehouse_name"],
                "total_items": row["total_items"],
                "total_on_hand": row["total_on_hand"],
                "total_value": row["total_value"],
            }
            for code, row in summary_by_code.items()
            if code and code not in role_by_code
        ]

        return {
            "company_code": self.company_code,
            "warehouses": warehouses,
            "unmapped": unmapped,
        }

    def get_warehouse_stock(self, whs_code: str, filters: Dict) -> Dict:
        """Item-level drill-down for one warehouse (optionally PM-only)."""
        reader = WMSHanaReader(self.company_code)
        reader_filters = dict(filters or {})
        reader_filters["warehouse_code"] = whs_code
        if reader_filters.pop("pm_only", False):
            reader_filters["item_group"] = ITEM_GROUP_PM_NAME
        return reader.get_stock_overview(reader_filters)


class TransferError(Exception):
    """A movement could not be built/validated (before any SAP call)."""


class TransferService:
    """
    Move packaging material FROM a PM store INTO the company's BOM issue point.

    Handles SP rule 67081: when the source store has `transfer_needs_request`
    (Oil BH-PM -> BH-PC), it posts an Inventory Transfer Request first and bases
    the StockTransfer on it; otherwise it posts a plain StockTransfer.

    Nothing is sent to SAP unless writes are enabled
    (settings.PRODUCTION_MOVEMENTS_SAP_WRITES_ENABLED) or dry_run=False is passed
    explicitly. In dry-run the movement is recorded with status DRY_RUN and the
    exact payload it *would* post, so the flow is fully exercisable pre-go-live.
    """

    def __init__(self, company_code: str, user=None):
        self.company_code = company_code
        self.user = user
        self.company = Company.objects.get(code=company_code)

    def _writes_enabled(self) -> bool:
        return bool(getattr(settings, "PRODUCTION_MOVEMENTS_SAP_WRITES_ENABLED", False))

    def _build_lines(self, lines: List[Dict], from_whs: str, to_whs: str) -> List[Dict]:
        clean = []
        for ln in lines:
            code = (ln.get("item_code") or "").strip()
            if not code:
                raise TransferError("Every line needs an item_code.")
            try:
                qty = Decimal(str(ln.get("quantity")))
            except Exception:
                raise TransferError(f"Invalid quantity for {code}.")
            if qty <= 0:
                raise TransferError(f"Quantity for {code} must be > 0.")
            clean.append({
                "item_code": code,
                "item_name": ln.get("item_name", "") or "",
                "quantity": qty,
                "uom": ln.get("uom", "") or "",
                "from_whs": from_whs,
                "to_whs": to_whs,
            })
        if not clean:
            raise TransferError("No lines to transfer.")
        return clean

    @staticmethod
    def _sap_lines(clean_lines: List[Dict], base_entry: Optional[int] = None) -> List[Dict]:
        out = []
        for i, ln in enumerate(clean_lines):
            row = {
                "ItemCode": ln["item_code"],
                "Quantity": float(ln["quantity"]),
                "FromWarehouseCode": ln["from_whs"],
                "WarehouseCode": ln["to_whs"],
            }
            if base_entry is not None:
                row["BaseType"] = ITR_BASE_TYPE
                row["BaseEntry"] = base_entry
                row["BaseLine"] = i
            out.append(row)
        return out

    def _header(self, from_whs, to_whs, doc_lines):
        return {
            "FromWarehouse": from_whs,
            "ToWarehouse": to_whs,
            "StockTransferLines": doc_lines,
        }

    @transaction.atomic
    def transfer_to_issue_point(
        self,
        from_whs: str,
        lines: List[Dict],
        posting_date: Optional[str] = None,
        dry_run: Optional[bool] = None,
        reference: str = "",
    ) -> Dict:
        """
        Returns a summary dict describing the movement(s) recorded/posted.
        Raises TransferError for validation problems, or a SAP exception if a
        live post fails (the ledger row is still saved as FAILED).
        """
        # --- resolve + validate against config ---
        to_whs = get_bom_issue_point(self.company_code)
        if not to_whs:
            raise TransferError(
                f"No BOM issue point configured for {self.company_code}."
            )
        source = (
            WarehouseRole.objects.filter(
                company=self.company, whs_code=from_whs, is_active=True
            ).first()
        )
        if not source:
            raise TransferError(f"{from_whs} is not a configured warehouse.")
        if source.is_bom_issue_point:
            raise TransferError(f"{from_whs} is the issue point — nothing to transfer into it.")
        # Only stores explicitly configured to feed the issue point may be a
        # source. This rejects FG/inactive/unrelated warehouses (e.g. BH-PF).
        if source.feeds_whs_code != to_whs:
            raise TransferError(
                f"{from_whs} is not configured to feed the issue point {to_whs} "
                f"(feeds='{source.feeds_whs_code or '-'}'). Only designated PM stores can transfer in."
            )

        clean_lines = self._build_lines(lines, from_whs, to_whs)
        needs_itr = source.transfer_needs_request
        if dry_run is None:
            dry_run = not self._writes_enabled()
        posting_date = posting_date or timezone.now().date().isoformat()

        result = {
            "company_code": self.company_code,
            "from_whs": from_whs,
            "to_whs": to_whs,
            "needs_transfer_request": needs_itr,
            "dry_run": dry_run,
            "movements": [],
        }

        itr_doc_entry = None

        # --- Step 1 (conditional): Inventory Transfer Request ---
        if needs_itr:
            itr_payload = self._header(from_whs, to_whs, self._sap_lines(clean_lines))
            itr_payload["DocDate"] = posting_date
            itr_mv = self._record(
                MovementType.TRANSFER_REQUEST, from_whs, to_whs, clean_lines,
                sap_object_type="1250000001", payload=itr_payload,
                posting_date=posting_date, reference=reference, dry_run=dry_run,
            )
            if not dry_run:
                itr_doc_entry = self._post(
                    itr_mv, lambda c: c.create_inventory_transfer_request(itr_payload)
                )
            result["movements"].append(self._summary(itr_mv))

        # --- Step 2: Stock Transfer (based on ITR when required) ---
        st_payload = self._header(
            from_whs, to_whs, self._sap_lines(clean_lines, base_entry=itr_doc_entry)
        )
        st_payload["DocDate"] = posting_date
        st_mv = self._record(
            MovementType.TRANSFER, from_whs, to_whs, clean_lines,
            sap_object_type="67", payload=st_payload, posting_date=posting_date,
            reference=reference, dry_run=dry_run, itr_doc_entry=itr_doc_entry,
        )
        if not dry_run:
            self._post(st_mv, lambda c: c.create_stock_transfer(st_payload))
        result["movements"].append(self._summary(st_mv))

        return result

    # --- ledger helpers ---
    def _record(self, mtype, from_whs, to_whs, clean_lines, *, sap_object_type,
                payload, posting_date, reference, dry_run, itr_doc_entry=None):
        mv = WarehouseMovement.objects.create(
            company=self.company,
            movement_type=mtype,
            status=MovementStatus.DRY_RUN if dry_run else MovementStatus.DRAFT,
            from_whs_code=from_whs,
            to_whs_code=to_whs,
            sap_object_type=sap_object_type,
            itr_doc_entry=itr_doc_entry,
            posting_date=posting_date,
            payload_preview=payload,
            reference=reference,
            created_by=self.user,
        )
        WarehouseMovementLine.objects.bulk_create([
            WarehouseMovementLine(
                movement=mv, item_code=ln["item_code"], item_name=ln["item_name"],
                quantity=ln["quantity"], uom=ln["uom"],
                from_whs_code=ln["from_whs"], to_whs_code=ln["to_whs"], base_line=i,
            )
            for i, ln in enumerate(clean_lines)
        ])
        return mv

    def _post(self, mv, call):
        """Run a SAP write, updating the ledger row; returns the created DocEntry."""
        client = SAPClient(self.company_code)
        try:
            data = call(client)
        except (SAPValidationError, SAPConnectionError, SAPDataError) as exc:
            mv.status = MovementStatus.FAILED
            mv.error_message = str(exc)
            mv.save(update_fields=["status", "error_message", "updated_at"])
            raise
        mv.status = MovementStatus.POSTED
        mv.sap_doc_entry = data.get("DocEntry")
        mv.sap_doc_num = str(data.get("DocNum") or "")
        mv.save(update_fields=["status", "sap_doc_entry", "sap_doc_num", "updated_at"])
        return mv.sap_doc_entry

    @staticmethod
    def _summary(mv):
        return {
            "id": mv.id,
            "movement_type": mv.movement_type,
            "status": mv.status,
            "from_whs": mv.from_whs_code,
            "to_whs": mv.to_whs_code,
            "sap_object_type": mv.sap_object_type,
            "sap_doc_entry": mv.sap_doc_entry,
            "sap_doc_num": mv.sap_doc_num,
            "itr_doc_entry": mv.itr_doc_entry,
        }
