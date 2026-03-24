"""
Core business logic for outbound dispatch operations.
"""
import logging
from typing import List, Dict, Any, Optional
from decimal import Decimal

from django.utils import timezone
from django.db import transaction

from sap_client.client import SAPClient
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError

from ..models import (
    ShipmentOrder, ShipmentOrderItem, ShipmentStatus, PickStatus,
    PickTask, PickTaskStatus,
    OutboundLoadRecord, TrailerCondition,
    GoodsIssuePosting, GoodsIssueStatus,
)

logger = logging.getLogger(__name__)


class OutboundService:
    """Service for outbound dispatch operations."""

    def __init__(self, company_code: str):
        self.company_code = company_code

    # ------------------------------------------------------------------
    # List / Detail
    # ------------------------------------------------------------------

    def get_shipments(self, filters: Dict[str, Any] = None) -> List[ShipmentOrder]:
        """List shipment orders with optional filters."""
        qs = ShipmentOrder.objects.filter(
            company__code=self.company_code
        ).select_related("vehicle_entry").prefetch_related("items")

        filters = filters or {}
        if filters.get("status"):
            qs = qs.filter(status=filters["status"])
        if filters.get("scheduled_date"):
            qs = qs.filter(scheduled_date=filters["scheduled_date"])
        if filters.get("customer_code"):
            qs = qs.filter(customer_code=filters["customer_code"])

        return qs.order_by("-created_at")

    def get_shipment_detail(self, shipment_id: int) -> ShipmentOrder:
        """Get a single shipment with all related data."""
        try:
            return ShipmentOrder.objects.select_related(
                "vehicle_entry", "company"
            ).prefetch_related(
                "items", "items__pick_tasks"
            ).get(id=shipment_id, company__code=self.company_code)
        except ShipmentOrder.DoesNotExist:
            raise ValueError(f"Shipment {shipment_id} not found")

    # ------------------------------------------------------------------
    # Dock Bay Assignment
    # ------------------------------------------------------------------

    def assign_dock_bay(self, shipment_id: int, dock_bay: str,
                        slot_start=None, slot_end=None) -> ShipmentOrder:
        """Assign a dock bay and time slot to a shipment."""
        shipment = self.get_shipment_detail(shipment_id)

        # Validate Zone C (bays 19-30)
        try:
            bay_num = int(dock_bay)
        except (ValueError, TypeError):
            raise ValueError("Invalid dock bay number")
        if not (19 <= bay_num <= 30):
            raise ValueError("Outbound dock bay must be in Zone C (bays 19-30)")

        shipment.dock_bay = dock_bay
        shipment.dock_slot_start = slot_start
        shipment.dock_slot_end = slot_end
        shipment.save()
        return shipment

    # ------------------------------------------------------------------
    # Pick Wave
    # ------------------------------------------------------------------

    @transaction.atomic
    def generate_pick_tasks(self, shipment_id: int) -> List[PickTask]:
        """Generate pick tasks for all items in a shipment."""
        shipment = self.get_shipment_detail(shipment_id)

        if shipment.status != ShipmentStatus.RELEASED:
            raise ValueError(
                f"Cannot generate picks. Shipment status is {shipment.status}, "
                f"must be RELEASED."
            )

        # Delete any existing pending pick tasks
        PickTask.objects.filter(
            shipment_item__shipment_order=shipment,
            status=PickTaskStatus.PENDING
        ).delete()

        tasks = []
        for item in shipment.items.all():
            if item.ordered_qty <= 0:
                continue
            task = PickTask.objects.create(
                shipment_item=item,
                pick_location=item.warehouse_code,
                pick_qty=item.ordered_qty,
                status=PickTaskStatus.PENDING,
            )
            tasks.append(task)

        shipment.status = ShipmentStatus.PICKING
        shipment.save()

        logger.info(f"Generated {len(tasks)} pick tasks for shipment {shipment_id}")
        return tasks

    def update_pick_task(self, task_id: int, data: Dict[str, Any], user=None) -> PickTask:
        """Update a pick task (assign, start, complete, short)."""
        try:
            task = PickTask.objects.select_related("shipment_item").get(id=task_id)
        except PickTask.DoesNotExist:
            raise ValueError(f"Pick task {task_id} not found")

        if "assigned_to" in data:
            task.assigned_to_id = data["assigned_to"]
        if "status" in data:
            task.status = data["status"]
        if "actual_qty" in data:
            task.actual_qty = Decimal(str(data["actual_qty"]))
        if "scanned_barcode" in data:
            task.scanned_barcode = data["scanned_barcode"]

        if task.status in (PickTaskStatus.COMPLETED, PickTaskStatus.SHORT):
            task.picked_at = timezone.now()
            # Update parent item picked_qty
            item = task.shipment_item
            total_picked = sum(
                t.actual_qty or 0
                for t in item.pick_tasks.exclude(id=task.id)
                if t.status == PickTaskStatus.COMPLETED
            ) + (task.actual_qty or 0)
            item.picked_qty = total_picked
            item.pick_status = (
                PickStatus.PICKED if total_picked >= item.ordered_qty
                else PickStatus.SHORT
            )
            item.save()

        if user:
            task.updated_by = user
        task.save()
        return task

    def record_scan(self, task_id: int, barcode: str) -> PickTask:
        """Record a barcode scan during picking."""
        try:
            task = PickTask.objects.get(id=task_id)
        except PickTask.DoesNotExist:
            raise ValueError(f"Pick task {task_id} not found")

        task.scanned_barcode = barcode
        if task.status == PickTaskStatus.PENDING:
            task.status = PickTaskStatus.IN_PROGRESS
        task.save()
        return task

    # ------------------------------------------------------------------
    # Pack & Stage
    # ------------------------------------------------------------------

    @transaction.atomic
    def confirm_pack(self, shipment_id: int) -> ShipmentOrder:
        """Confirm packing is complete for a shipment."""
        shipment = self.get_shipment_detail(shipment_id)

        if shipment.status != ShipmentStatus.PICKING:
            raise ValueError(f"Shipment must be in PICKING status, got {shipment.status}")

        # Check all items are picked or marked SHORT
        for item in shipment.items.all():
            if item.pick_status == PickStatus.PENDING:
                raise ValueError(
                    f"Item {item.item_code} has not been picked yet"
                )

        # Set packed_qty = picked_qty for all items
        for item in shipment.items.all():
            item.packed_qty = item.picked_qty
            item.save()

        shipment.status = ShipmentStatus.PACKED
        shipment.save()
        return shipment

    def stage_shipment(self, shipment_id: int) -> ShipmentOrder:
        """Mark shipment as staged at dock bay."""
        shipment = self.get_shipment_detail(shipment_id)

        if shipment.status != ShipmentStatus.PACKED:
            raise ValueError(f"Shipment must be PACKED before staging, got {shipment.status}")

        shipment.status = ShipmentStatus.STAGED
        shipment.save()
        return shipment

    # ------------------------------------------------------------------
    # Vehicle Link
    # ------------------------------------------------------------------

    def link_vehicle(self, shipment_id: int, vehicle_entry_id: int) -> ShipmentOrder:
        """Link an arriving carrier vehicle to a shipment."""
        from driver_management.models import VehicleEntry

        shipment = self.get_shipment_detail(shipment_id)
        try:
            vehicle = VehicleEntry.objects.get(id=vehicle_entry_id)
        except VehicleEntry.DoesNotExist:
            raise ValueError(f"Vehicle entry {vehicle_entry_id} not found")

        shipment.vehicle_entry = vehicle
        shipment.save()
        return shipment

    # ------------------------------------------------------------------
    # Trailer Inspection
    # ------------------------------------------------------------------

    def inspect_trailer(self, shipment_id: int, data: Dict[str, Any],
                        user=None) -> OutboundLoadRecord:
        """Create or update trailer inspection record."""
        shipment = self.get_shipment_detail(shipment_id)

        if not shipment.vehicle_entry:
            raise ValueError("Vehicle must be linked before trailer inspection")

        record, _ = OutboundLoadRecord.objects.get_or_create(
            shipment_order=shipment,
            defaults={"created_by": user}
        )

        record.trailer_condition = data.get("trailer_condition", TrailerCondition.CLEAN)
        record.trailer_temp_ok = data.get("trailer_temp_ok")
        record.trailer_temp_reading = data.get("trailer_temp_reading")
        record.inspected_by = user
        record.inspected_at = timezone.now()
        record.save()

        return record

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    @transaction.atomic
    def record_loading(self, shipment_id: int, items_loaded: List[Dict],
                       user=None) -> ShipmentOrder:
        """Record pallet loading onto truck."""
        shipment = self.get_shipment_detail(shipment_id)

        # Check trailer inspection passed
        try:
            load_record = shipment.load_record
        except OutboundLoadRecord.DoesNotExist:
            raise ValueError("Trailer must be inspected before loading")

        if load_record.trailer_condition == TrailerCondition.REJECTED:
            raise ValueError("Trailer was rejected during inspection. Cannot load.")

        if load_record.trailer_condition == TrailerCondition.DAMAGED:
            if not load_record.supervisor_confirmed:
                raise ValueError(
                    "Damaged trailer requires supervisor override before loading"
                )

        # Start loading timer if not started
        if not load_record.loading_started_at:
            load_record.loading_started_at = timezone.now()
            load_record.loaded_by = user
            load_record.save()

        # Update loaded quantities
        for item_data in items_loaded:
            try:
                item = shipment.items.get(id=item_data["item_id"])
            except ShipmentOrderItem.DoesNotExist:
                raise ValueError(f"Item {item_data['item_id']} not found in shipment")

            loaded_qty = Decimal(str(item_data["loaded_qty"]))
            if loaded_qty > item.packed_qty:
                raise ValueError(
                    f"loaded_qty ({loaded_qty}) cannot exceed packed_qty "
                    f"({item.packed_qty}) for {item.item_code}"
                )
            item.loaded_qty = loaded_qty
            item.save()

        shipment.status = ShipmentStatus.LOADING
        # Calculate total weight
        shipment.total_weight = sum(
            (i.loaded_qty * (i.weight or 0)) for i in shipment.items.all()
        )
        shipment.save()
        return shipment

    # ------------------------------------------------------------------
    # Supervisor Confirmation
    # ------------------------------------------------------------------

    def supervisor_confirm(self, shipment_id: int, user=None) -> OutboundLoadRecord:
        """Supervisor final confirmation of loading."""
        shipment = self.get_shipment_detail(shipment_id)

        try:
            load_record = shipment.load_record
        except OutboundLoadRecord.DoesNotExist:
            raise ValueError("No load record found for this shipment")

        load_record.supervisor_confirmed = True
        load_record.supervisor = user
        load_record.confirmed_at = timezone.now()
        load_record.loading_completed_at = timezone.now()
        load_record.save()

        return load_record

    # ------------------------------------------------------------------
    # BOL Generation
    # ------------------------------------------------------------------

    def generate_bol(self, shipment_id: int) -> Dict[str, Any]:
        """Generate Bill of Lading data."""
        shipment = self.get_shipment_detail(shipment_id)

        # Auto-generate BOL number
        if not shipment.bill_of_lading_no:
            bol_no = f"BOL-{shipment.sap_doc_num}-{timezone.now().strftime('%Y%m%d%H%M')}"
            shipment.bill_of_lading_no = bol_no
            shipment.save()

        return {
            "bol_number": shipment.bill_of_lading_no,
            "shipment_id": shipment.id,
            "customer_name": shipment.customer_name,
            "ship_to_address": shipment.ship_to_address,
            "carrier_name": shipment.carrier_name,
            "scheduled_date": str(shipment.scheduled_date),
            "total_weight": str(shipment.total_weight or 0),
            "items": [
                {
                    "item_code": item.item_code,
                    "item_name": item.item_name,
                    "loaded_qty": str(item.loaded_qty),
                    "uom": item.uom,
                    "batch_number": item.batch_number,
                }
                for item in shipment.items.all()
            ],
        }

    # ------------------------------------------------------------------
    # Dispatch (Seal + Goods Issue)
    # ------------------------------------------------------------------

    @transaction.atomic
    def dispatch(self, shipment_id: int, seal_number: str, user=None,
                 branch_id: int = None) -> ShipmentOrder:
        """
        Seal trailer, post Goods Issue to SAP, mark as DISPATCHED.
        """
        shipment = self.get_shipment_detail(shipment_id)

        # Validations
        if shipment.status != ShipmentStatus.LOADING:
            raise ValueError(f"Shipment must be in LOADING status, got {shipment.status}")

        try:
            load_record = shipment.load_record
        except OutboundLoadRecord.DoesNotExist:
            raise ValueError("No load record found")

        if not load_record.supervisor_confirmed:
            raise ValueError("Supervisor confirmation required before dispatch")

        if not shipment.bill_of_lading_no:
            raise ValueError("Bill of Lading must be generated before dispatch")

        if not seal_number:
            raise ValueError("Seal number is required for dispatch")

        # Record seal number
        shipment.seal_number = seal_number
        shipment.save()

        # Post Goods Issue to SAP
        gi_posting = self._post_goods_issue(shipment, user, branch_id)

        if gi_posting.status == GoodsIssueStatus.POSTED:
            shipment.status = ShipmentStatus.DISPATCHED
            shipment.save()
            logger.info(f"Shipment {shipment_id} dispatched. GI DocNum: {gi_posting.sap_doc_num}")
        else:
            logger.error(
                f"Goods Issue failed for shipment {shipment_id}: {gi_posting.error_message}"
            )
            raise SAPDataError(
                f"Goods Issue posting failed: {gi_posting.error_message}"
            )

        return shipment

    def _post_goods_issue(self, shipment: ShipmentOrder, user=None,
                          branch_id: int = None) -> GoodsIssuePosting:
        """Post Goods Issue (InventoryGenExits) to SAP."""
        gi_posting, _ = GoodsIssuePosting.objects.get_or_create(
            shipment_order=shipment,
            defaults={
                "status": GoodsIssueStatus.PENDING,
                "posted_by": user,
            }
        )

        # Build SAP payload
        document_lines = []
        for item in shipment.items.all():
            if item.loaded_qty <= 0:
                continue
            line = {
                "ItemCode": item.item_code,
                "Quantity": str(item.loaded_qty),
                "WarehouseCode": item.warehouse_code,
            }
            # Link to Sales Order
            if shipment.sap_doc_entry and item.sap_line_num is not None:
                line["BaseEntry"] = shipment.sap_doc_entry
                line["BaseLine"] = item.sap_line_num
                line["BaseType"] = 17  # 17 = Sales Order

            if item.batch_number:
                line["BatchNumbers"] = [{
                    "BatchNumber": item.batch_number,
                    "Quantity": str(item.loaded_qty),
                }]
            document_lines.append(line)

        if not document_lines:
            gi_posting.status = GoodsIssueStatus.FAILED
            gi_posting.error_message = "No loaded quantities to post"
            gi_posting.save()
            raise ValueError("No loaded quantities to post")

        payload = {
            "Comments": (
                f"App: FactoryApp v2 | "
                f"SO: {shipment.sap_doc_num} | "
                f"BOL: {shipment.bill_of_lading_no} | "
                f"Seal: {shipment.seal_number}"
            ),
            "DocumentLines": document_lines,
        }

        if branch_id:
            payload["BPL_IDAssignedToInvoice"] = branch_id

        sap_client = SAPClient(company_code=self.company_code)

        try:
            result = sap_client.create_goods_issue(payload)

            gi_posting.sap_doc_entry = result.get("DocEntry")
            gi_posting.sap_doc_num = result.get("DocNum")
            gi_posting.status = GoodsIssueStatus.POSTED
            gi_posting.posted_at = timezone.now()
            gi_posting.posted_by = user
            gi_posting.save()

            return gi_posting

        except SAPValidationError as e:
            gi_posting.status = GoodsIssueStatus.FAILED
            gi_posting.error_message = str(e)
            gi_posting.retry_count += 1
            gi_posting.save()
            raise

        except SAPConnectionError as e:
            gi_posting.status = GoodsIssueStatus.FAILED
            gi_posting.error_message = "SAP system unavailable"
            gi_posting.retry_count += 1
            gi_posting.save()
            raise

        except SAPDataError as e:
            gi_posting.status = GoodsIssueStatus.FAILED
            gi_posting.error_message = str(e)
            gi_posting.retry_count += 1
            gi_posting.save()
            raise

    def retry_goods_issue(self, shipment_id: int, user=None,
                          branch_id: int = None) -> GoodsIssuePosting:
        """Retry a failed Goods Issue posting."""
        shipment = self.get_shipment_detail(shipment_id)

        try:
            gi_posting = shipment.goods_issue
        except GoodsIssuePosting.DoesNotExist:
            raise ValueError("No Goods Issue posting found for this shipment")

        if gi_posting.status != GoodsIssueStatus.FAILED:
            raise ValueError(f"Can only retry FAILED postings, got {gi_posting.status}")

        # Delete old posting and re-post
        gi_posting.delete()
        gi_posting = self._post_goods_issue(shipment, user, branch_id)

        if gi_posting.status == GoodsIssueStatus.POSTED:
            shipment.status = ShipmentStatus.DISPATCHED
            shipment.save()

        return gi_posting

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------

    def get_dashboard(self) -> Dict[str, Any]:
        """Get outbound dashboard KPIs."""
        qs = ShipmentOrder.objects.filter(company__code=self.company_code)

        total = qs.count()
        by_status = {}
        for status_choice in ShipmentStatus:
            by_status[status_choice.value] = qs.filter(status=status_choice.value).count()

        # Today's dispatches
        today = timezone.now().date()
        today_dispatched = qs.filter(
            status=ShipmentStatus.DISPATCHED,
            updated_at__date=today
        ).count()
        today_scheduled = qs.filter(scheduled_date=today).count()

        # Zone C bay utilisation
        active_bays = qs.filter(
            status__in=[ShipmentStatus.STAGED, ShipmentStatus.LOADING],
            dock_bay__isnull=False
        ).exclude(dock_bay="").values_list("dock_bay", flat=True).distinct().count()
        bay_utilisation = round((active_bays / 12) * 100, 1)  # 12 bays in Zone C

        return {
            "total_shipments": total,
            "by_status": by_status,
            "today_dispatched": today_dispatched,
            "today_scheduled": today_scheduled,
            "zone_c_active_bays": active_bays,
            "zone_c_bay_utilisation_pct": bay_utilisation,
        }
