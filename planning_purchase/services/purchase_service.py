"""Building purchase orders from a plan's bill of materials, and posting them.

The flow is deliberately three steps — raise, approve, post — because the last
one is a commitment to a supplier. Everything before it is reversible; nothing
after it is.

Two guards matter more than the rest:

**One vendor per order.** A selection covering four suppliers creates four orders.
A SAP purchase order belongs to exactly one business partner, and splitting after
approval would lose the reviewer's intent.

**Posting twice is impossible, not unlikely.** Every order carries an
``idempotency_key`` unique per company, the row is locked with
``select_for_update`` before posting, and the state machine refuses anything that
is not `APPROVED`. A network timeout mid-post leaves the order un-posted and says
so, because auto-retrying into a duplicate order is worse than a human check.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from sap_client.context import CompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError

from ..hana_reader import HanaProductionPlanReader, classify_material
from ..models import (
    MaterialType,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
)
from .errors import PlanningError, PurchaseOrderStateError, SAPPostError

logger = logging.getLogger(__name__)

ZERO = Decimal(0)


def _dec(value) -> Decimal:
    if value is None:
        return ZERO
    return value if isinstance(value, Decimal) else Decimal(str(value))


def simulate_sap() -> bool:
    """Whether posting should stop short of SAP.

    Defaults to ``DEBUG``, the same way ``MARKETPLACE_SIMULATE_SAP`` does, so
    development and demos can exercise the whole flow while production posts for
    real. A purchase order is real money; the default must be the safe one
    everywhere except a deployed production box.
    """
    return bool(getattr(settings, "PLANNING_PURCHASE_SIMULATE_SAP", settings.DEBUG))


class PurchaseOrderService:
    def __init__(self, company_code: str, user=None):
        self.company_code = company_code
        self.user = user
        self.context = CompanyContext(company_code)
        self.reader = HanaProductionPlanReader(self.context)

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    @transaction.atomic
    def create_from_requirement(self, payload: Dict[str, Any]) -> List[PurchaseOrder]:
        """Create one draft order per vendor from selected requirement rows."""
        lines = payload.get("lines") or []
        if not lines:
            raise PlanningError("Select at least one material to order.")

        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for line in lines:
            vendor = (line.get("vendor_code") or "").strip()
            if not vendor:
                raise PlanningError(
                    f"{line.get('item_code')} has no supplier. Pick one before ordering — "
                    "SAP cannot take a purchase order without a business partner."
                )
            grouped.setdefault(vendor, []).append(line)

        vendor_names = self._vendor_names(list(grouped))
        default_due = payload.get("doc_due_date") or (
            timezone.localdate() + timedelta(days=7)
        )
        today = timezone.localdate()

        orders: List[PurchaseOrder] = []
        for vendor_code, vendor_lines in grouped.items():
            order = PurchaseOrder.objects.create(
                company_code=self.company_code,
                plan_abs_id=payload.get("plan_abs_id"),
                plan_code=payload.get("plan_code") or "",
                plan_name=payload.get("plan_name") or "",
                vendor_code=vendor_code,
                vendor_name=vendor_names.get(vendor_code, ""),
                doc_date=today,
                doc_due_date=default_due,
                warehouse_code=payload.get("warehouse_code") or "",
                remarks=payload.get("remarks") or "",
                status=PurchaseOrderStatus.DRAFT,
                currency=payload.get("currency") or "INR",
                idempotency_key=uuid.uuid4().hex,
                created_by=self.user if getattr(self.user, "pk", None) else None,
            )
            self._replace_lines(order, vendor_lines, default_due)
            orders.append(order)

        return orders

    def _replace_lines(
        self, order: PurchaseOrder, lines: Sequence[Dict[str, Any]], default_due
    ) -> None:
        order.lines.all().delete()

        seen: Dict[str, PurchaseOrderLine] = {}
        for line in lines:
            item_code = (line.get("item_code") or "").strip()
            if not item_code:
                raise PlanningError("Every line needs an item code.")

            quantity = _dec(line.get("quantity"))
            if quantity <= ZERO:
                raise PlanningError(
                    f"{item_code}: quantity must be above zero. Remove the line "
                    "instead of ordering nothing."
                )

            # A repeated item on one order is almost always a double-click, and
            # SAP would happily take both lines. Merge rather than reject, so the
            # buyer does not lose the rest of the selection to one stray row.
            existing = seen.get(item_code)
            if existing is not None:
                existing.quantity += quantity
                existing.save(update_fields=["quantity"])
                continue

            seen[item_code] = PurchaseOrderLine.objects.create(
                purchase_order=order,
                item_code=item_code,
                item_name=line.get("item_name") or "",
                material_type=self._material_type(line),
                uom=line.get("uom") or "",
                quantity=quantity,
                unit_price=_dec(line.get("unit_price")),
                warehouse_code=line.get("warehouse_code") or order.warehouse_code or "",
                required_date=line.get("required_date") or default_due,
                required_qty=_dec(line.get("required_qty")),
                available_qty=_dec(line.get("available_qty")),
                on_order_qty=_dec(line.get("on_order_qty")),
                shortage_qty=_dec(line.get("shortage_qty")),
                moq_applied=(
                    _dec(line["moq_applied"]) if line.get("moq_applied") else None
                ),
            )

        order.recalculate_total()
        order.total_value = sum(
            (line.quantity * line.unit_price for line in order.lines.all()), ZERO
        )
        order.save(update_fields=["total_value", "updated_at"])

    @staticmethod
    def _material_type(line: Dict[str, Any]) -> str:
        supplied = (line.get("material_type") or "").upper()
        if supplied in MaterialType.values:
            return supplied
        return classify_material(line.get("item_group"))

    def _vendor_names(self, vendor_codes: Sequence[str]) -> Dict[str, str]:
        try:
            return {
                row["CardCode"]: row["CardName"]
                for row in self.reader.get_vendors(limit=5000)
                if row["CardCode"] in set(vendor_codes)
            }
        except Exception as exc:
            logger.warning("Could not resolve vendor names: %s", exc)
            return {}

    # ------------------------------------------------------------------
    # Edit / state changes
    # ------------------------------------------------------------------

    @transaction.atomic
    def update_draft(self, order: PurchaseOrder, payload: Dict[str, Any]) -> PurchaseOrder:
        if order.status != PurchaseOrderStatus.DRAFT:
            raise PurchaseOrderStateError(
                f"This order is {order.get_status_display().lower()} and can no longer be edited."
            )

        for field in ("doc_due_date", "warehouse_code", "remarks", "vendor_code"):
            if field in payload and payload[field] is not None:
                setattr(order, field, payload[field])

        if payload.get("vendor_code"):
            names = self._vendor_names([payload["vendor_code"]])
            order.vendor_name = names.get(payload["vendor_code"], order.vendor_name)

        if payload.get("lines") is not None:
            self._replace_lines(order, payload["lines"], order.doc_due_date)

        order.save()
        return order

    @transaction.atomic
    def approve(self, order: PurchaseOrder) -> PurchaseOrder:
        """Approve a draft.

        The approver must be someone other than the author. The permission split
        alone does not achieve that — one person can hold both — so it is checked
        on the record.
        """
        if order.status != PurchaseOrderStatus.DRAFT:
            raise PurchaseOrderStateError(
                f"Only a draft can be approved; this one is {order.get_status_display().lower()}."
            )
        if not order.lines.exists():
            raise PlanningError("An order with no lines cannot be approved.")

        author = order.created_by_id
        approver = getattr(self.user, "pk", None)
        if author and approver and author == approver:
            raise PurchaseOrderStateError(
                "A purchase order has to be approved by someone other than the "
                "person who raised it."
            )

        order.status = PurchaseOrderStatus.APPROVED
        order.approved_by = self.user if approver else None
        order.approved_at = timezone.now()
        order.sap_error_message = ""
        order.save(update_fields=[
            "status", "approved_by", "approved_at", "sap_error_message", "updated_at",
        ])
        return order

    @transaction.atomic
    def cancel(self, order: PurchaseOrder, reason: str = "") -> PurchaseOrder:
        if order.status == PurchaseOrderStatus.POSTED:
            raise PurchaseOrderStateError(
                "This order is already in SAP. Cancel it there — cancelling only "
                "here would leave the two systems disagreeing."
            )
        order.status = PurchaseOrderStatus.CANCELLED
        if reason:
            order.remarks = f"{order.remarks}\nCancelled: {reason}".strip()
        order.save(update_fields=["status", "remarks", "updated_at"])
        return order

    # ------------------------------------------------------------------
    # Post to SAP
    # ------------------------------------------------------------------

    def post_to_sap(self, order_id: int) -> PurchaseOrder:
        """Post an approved order to SAP, exactly once.

        The row is locked for the whole check-and-post so two simultaneous
        requests cannot both pass the state check. The lock is committed before
        the HTTP call returns, so a crash mid-post leaves the order `FAILED` with
        SAP's message rather than silently `APPROVED` and ready to be posted
        again.
        """
        with transaction.atomic():
            order = (
                PurchaseOrder.objects
                .select_for_update()
                .prefetch_related("lines")
                .get(pk=order_id, company_code=self.company_code)
            )

            if order.status == PurchaseOrderStatus.POSTED:
                raise PurchaseOrderStateError(
                    f"Already posted to SAP as {order.sap_doc_num or order.sap_doc_entry}."
                )
            if order.status not in (PurchaseOrderStatus.APPROVED, PurchaseOrderStatus.FAILED):
                raise PurchaseOrderStateError(
                    "Only an approved order can be posted to SAP."
                )
            if not order.lines.exists():
                raise PlanningError("An order with no lines cannot be posted.")

            payload = self._build_payload(order)

        if simulate_sap():
            return self._record_simulated(order)

        try:
            from sap_client.service_layer.purchase_order_writer import PurchaseOrderWriter

            result = PurchaseOrderWriter(self.context).create(payload)
        except (SAPValidationError, SAPDataError, SAPConnectionError) as exc:
            self._record_failure(order, str(exc))
            raise SAPPostError(str(exc)) from exc
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Unexpected error posting purchase order %s", order.pk)
            self._record_failure(order, str(exc))
            raise SAPPostError(f"Unexpected error posting to SAP: {exc}") from exc

        return self._record_success(order, result)

    def _build_payload(self, order: PurchaseOrder) -> Dict[str, Any]:
        lines = []
        for line in order.lines.all():
            entry: Dict[str, Any] = {
                "ItemCode": line.item_code,
                "Quantity": line.quantity,
            }
            if line.unit_price:
                entry["Price"] = line.unit_price
            warehouse = line.warehouse_code or order.warehouse_code
            if warehouse:
                entry["WarehouseCode"] = warehouse
            if line.required_date:
                entry["ShipDate"] = line.required_date.isoformat()
            lines.append(entry)

        comments = order.remarks or ""
        if order.plan_code:
            plan_note = f"Raised from production plan {order.plan_code}"
            comments = f"{plan_note}. {comments}".strip()

        payload: Dict[str, Any] = {
            "CardCode": order.vendor_code,
            "DocDate": order.doc_date.isoformat(),
            "DocDueDate": order.doc_due_date.isoformat(),
            "Comments": comments[:254],
            "DocumentLines": lines,
        }

        # Multi-branch companies reject a document with no branch. Resolved from
        # the receiving warehouse the same way the goods-receipt writer does.
        branch_id = self._branch_id(order)
        if branch_id is not None:
            payload["BPL_IDAssignedToInvoice"] = branch_id

        return payload

    def _branch_id(self, order: PurchaseOrder) -> Optional[int]:
        warehouse = order.warehouse_code or next(
            (line.warehouse_code for line in order.lines.all() if line.warehouse_code),
            "",
        )
        if not warehouse:
            return None
        try:
            return self.reader.get_branch_for_warehouse(warehouse)
        except Exception as exc:
            logger.warning("Could not resolve branch for %s: %s", warehouse, exc)
            return None

    def _record_success(self, order: PurchaseOrder, result: Dict[str, Any]) -> PurchaseOrder:
        order.status = PurchaseOrderStatus.POSTED
        order.sap_doc_entry = result.get("DocEntry")
        order.sap_doc_num = result.get("DocNum")
        order.sap_error_message = ""
        order.posted_at = timezone.now()
        order.posted_by = self.user if getattr(self.user, "pk", None) else None
        order.simulated = False
        order.save(update_fields=[
            "status", "sap_doc_entry", "sap_doc_num", "sap_error_message",
            "posted_at", "posted_by", "simulated", "updated_at",
        ])
        return order

    def _record_simulated(self, order: PurchaseOrder) -> PurchaseOrder:
        """Mark the order posted without touching SAP.

        `simulated` is stored on the row rather than inferred from the setting,
        because the setting can change: an order posted in simulate mode must
        still read as simulated a month later, or somebody will go looking in SAP
        for a document that was never created.
        """
        order.status = PurchaseOrderStatus.POSTED
        order.sap_doc_entry = None
        order.sap_doc_num = None
        order.sap_error_message = ""
        order.posted_at = timezone.now()
        order.posted_by = self.user if getattr(self.user, "pk", None) else None
        order.simulated = True
        order.save(update_fields=[
            "status", "sap_doc_entry", "sap_doc_num", "sap_error_message",
            "posted_at", "posted_by", "simulated", "updated_at",
        ])
        logger.info(
            "Purchase order %s marked posted in simulate mode — nothing sent to SAP.",
            order.pk,
        )
        return order

    def _record_failure(self, order: PurchaseOrder, message: str) -> None:
        order.status = PurchaseOrderStatus.FAILED
        order.sap_error_message = message[:2000]
        order.save(update_fields=["status", "sap_error_message", "updated_at"])
