import logging

from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from driver_management.models import VehicleEntry
from gate_core.enums import GATE_PHASE_STATUSES, GateEntryStatus
from raw_material_gatein.models import POItemReceipt, POReceipt
from raw_material_gatein.serializers import POReceiveRequestSerializer
from raw_material_gatein.services import validate_received_quantity
from sap_client.client import SAPClient
from sap_client.exceptions import SAPConnectionError, SAPDataError

from .permissions import (
    CanCompleteFGEntry,
    CanDeleteFGEntry,
    CanReceiveFGPO,
    CanViewFGReceipt,
)
from .services import complete_fg_gate_entry

logger = logging.getLogger(__name__)

FG_ENTRY_TYPE = "FINISHED_GOODS"


def _po_receipt_lock_reason(po_receipt):
    """Why an FG PO receipt can no longer be edited (None if it can).

    Finished goods have no QC, so the only locks are a locked/finished gate entry
    or a GRPO already posted / line activity against the PO.
    """
    entry = po_receipt.vehicle_entry

    if entry.is_locked:
        return "This gate entry is locked and cannot be modified."

    if entry.status in [GateEntryStatus.COMPLETED, GateEntryStatus.CANCELLED]:
        return "This gate entry is completed or cancelled and cannot be modified."

    if (
        po_receipt.grpo_postings.filter(status="POSTED").exists()
        or po_receipt.merged_grpo_postings.filter(status="POSTED").exists()
    ):
        return "This PO has already been posted to GRPO and cannot be modified."

    for item in po_receipt.items.all():
        if item.grpo_lines.exists():
            return "This PO has GRPO line activity and cannot be modified."

    return None


def _serialize_po_receipt(po_receipt):
    lock_reason = _po_receipt_lock_reason(po_receipt)

    return {
        "id": po_receipt.id,
        "po_number": po_receipt.po_number,
        "supplier_code": po_receipt.supplier_code,
        "supplier_name": po_receipt.supplier_name,
        "created_at": po_receipt.created_at,
        "updated_at": po_receipt.updated_at,
        "is_editable": lock_reason is None,
        "lock_reason": lock_reason,
        "items": [
            {
                "id": item.id,
                "sap_line_num": item.sap_line_num,
                "po_item_code": item.po_item_code,
                "item_name": item.item_name,
                "ordered_qty": item.ordered_qty,
                "received_qty": item.received_qty,
                "short_qty": item.short_qty,
                "uom": item.uom,
                "unit_price": item.unit_price,
            }
            for item in po_receipt.items.all()
        ],
    }


def _ensure_entry_accepts_po_changes(entry):
    if entry.is_locked:
        raise ValidationError({"detail": "This gate entry is locked and cannot be modified."})
    if entry.status in [GateEntryStatus.COMPLETED, GateEntryStatus.CANCELLED]:
        raise ValidationError({
            "detail": "Cannot add or edit PO receipts for a completed or cancelled gate entry."
        })


def _get_sap_fg_po_details(company_code, supplier_code, po_number):
    """Fetch the finished-goods lines of an open PO from SAP (item group 102)."""
    try:
        client = SAPClient(company_code=company_code)
        sap_pos = client.get_open_finished_goods_pos(supplier_code)
    except SAPConnectionError as e:
        logger.error("SAP connection error in FG ReceivePOAPI: %s", e)
        raise APIException(
            detail="SAP system is currently unavailable. Please try again later.",
            code=503,
        )
    except SAPDataError as e:
        logger.error("SAP data error in FG ReceivePOAPI: %s", e)
        raise APIException(detail="Failed to retrieve PO data from SAP.", code=502)

    sap_items_map = {}
    sap_header = {
        "sap_doc_entry": None,
        "branch_id": None,
        "vendor_ref": "",
        "po_date": None,
    }

    for po in sap_pos:
        if po.po_number == po_number:
            sap_header = {
                "sap_doc_entry": po.doc_entry,
                "branch_id": po.branch_id,
                "vendor_ref": po.vendor_ref or "",
                "po_date": po.doc_date,
            }
            for item in po.items:
                sap_items_map[item.line_num] = {
                    "po_item_code": item.po_item_code,
                    "remaining_qty": item.remaining_qty,
                    "rate": item.rate,
                    "tax_code": item.tax_code,
                    "warehouse_code": item.warehouse_code,
                    "account_code": item.account_code,
                    "variety": item.variety,
                }
            break

    if not sap_items_map:
        raise ValidationError(
            {"detail": f"Open finished-goods PO {po_number} was not found for this supplier."}
        )

    return sap_header, sap_items_map


def _save_po_items(po_receipt, items_data, sap_items_map, user):
    existing_by_line = {
        item.sap_line_num: item
        for item in po_receipt.items.all()
        if item.sap_line_num is not None
    }
    seen_line_nums = set()

    for item_data in items_data:
        line_num = item_data["line_num"]
        po_item_code = item_data["po_item_code"]
        received_qty = item_data["received_qty"]

        if line_num in seen_line_nums:
            raise ValidationError({"detail": f"Duplicate PO line {line_num} in request."})
        seen_line_nums.add(line_num)

        sap_item_info = sap_items_map.get(line_num)
        if sap_item_info is None:
            raise ValidationError(
                {"detail": f"Invalid PO line {line_num} for item {po_item_code}"}
            )

        if sap_item_info["po_item_code"] != po_item_code:
            raise ValidationError(
                {"detail": f"Item code {po_item_code} does not match PO line {line_num}"}
            )

        try:
            validate_received_quantity(
                item_data["ordered_qty"],
                sap_item_info["remaining_qty"],
                received_qty,
            )
        except ValueError as e:
            raise ValidationError({"error": str(e)})

        defaults = {
            "po_item_code": po_item_code,
            "item_name": item_data["item_name"],
            "ordered_qty": item_data["ordered_qty"],
            "received_qty": received_qty,
            "uom": item_data["uom"],
            "sap_line_num": line_num,
            "unit_price": sap_item_info["rate"] or None,
            "tax_code": sap_item_info["tax_code"],
            "warehouse_code": sap_item_info["warehouse_code"],
            "gl_account": sap_item_info["account_code"],
            "variety": sap_item_info.get("variety", ""),
            # No QC for finished goods: everything received is accepted.
            "accepted_qty": received_qty,
            "rejected_qty": 0,
        }

        item = existing_by_line.get(line_num)
        if item is None:
            POItemReceipt.objects.create(
                po_receipt=po_receipt, **defaults, created_by=user
            )
            continue

        for field, value in defaults.items():
            setattr(item, field, value)
        item.updated_by = user
        item.save()

    po_receipt.items.exclude(sap_line_num__in=seen_line_nums).delete()


def _bump_entry_in_progress(entry):
    if entry.status == GateEntryStatus.DRAFT:
        entry.status = GateEntryStatus.IN_PROGRESS
        entry.save(update_fields=["status"])


class ReceiveFGPOAPI(APIView):
    """Receive finished-goods PO items against a FINISHED_GOODS gate entry."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanReceiveFGPO]

    @transaction.atomic
    def post(self, request, gate_entry_id):
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company=request.company.company,
            entry_type=FG_ENTRY_TYPE,
        )
        _ensure_entry_accepts_po_changes(entry)

        request_serializer = POReceiveRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        validated_data = request_serializer.validated_data

        po_number = validated_data["po_number"]
        supplier_code = validated_data["supplier_code"]
        supplier_name = validated_data["supplier_name"]
        items_data = validated_data["items"]

        if POReceipt.objects.filter(vehicle_entry=entry, po_number=po_number).exists():
            raise ValidationError(
                {"detail": f"PO {po_number} is already added to this gate entry."}
            )

        sap_header, sap_items_map = _get_sap_fg_po_details(
            request.company.company.code, supplier_code, po_number
        )

        try:
            po_receipt = POReceipt.objects.create(
                vehicle_entry=entry,
                po_number=po_number,
                supplier_code=supplier_code,
                supplier_name=supplier_name,
                **sap_header,
                created_by=request.user,
            )
        except IntegrityError:
            raise ValidationError(
                {"detail": f"PO {po_number} is already added to this gate entry."}
            )

        _save_po_items(po_receipt, items_data, sap_items_map, request.user)
        _bump_entry_in_progress(entry)

        return Response(
            {
                "message": "Finished goods PO items received successfully",
                "po_receipt": _serialize_po_receipt(po_receipt),
            },
            status=status.HTTP_201_CREATED,
        )


class FGReceiptDetailAPI(APIView):
    """Update an FG PO receipt until a GRPO is posted against it."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanReceiveFGPO]

    @transaction.atomic
    def put(self, request, gate_entry_id, po_receipt_id):
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company=request.company.company,
            entry_type=FG_ENTRY_TYPE,
        )
        _ensure_entry_accepts_po_changes(entry)

        po_receipt = get_object_or_404(
            POReceipt.objects.select_related("vehicle_entry").prefetch_related("items"),
            id=po_receipt_id,
            vehicle_entry=entry,
        )

        lock_reason = _po_receipt_lock_reason(po_receipt)
        if lock_reason:
            raise ValidationError({"detail": lock_reason})

        request_serializer = POReceiveRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        validated_data = request_serializer.validated_data

        po_number = validated_data["po_number"]
        supplier_code = validated_data["supplier_code"]

        duplicate_exists = (
            POReceipt.objects.filter(vehicle_entry=entry, po_number=po_number)
            .exclude(id=po_receipt.id)
            .exists()
        )
        if duplicate_exists:
            raise ValidationError(
                {"detail": f"PO {po_number} is already added to this gate entry."}
            )

        sap_header, sap_items_map = _get_sap_fg_po_details(
            request.company.company.code, supplier_code, po_number
        )

        po_receipt.po_number = po_number
        po_receipt.supplier_code = supplier_code
        po_receipt.supplier_name = validated_data["supplier_name"]
        po_receipt.sap_doc_entry = sap_header["sap_doc_entry"]
        po_receipt.branch_id = sap_header["branch_id"]
        po_receipt.vendor_ref = sap_header["vendor_ref"]
        po_receipt.po_date = sap_header["po_date"]
        po_receipt.updated_by = request.user
        po_receipt.save()

        _save_po_items(po_receipt, validated_data["items"], sap_items_map, request.user)
        _bump_entry_in_progress(entry)

        po_receipt = POReceipt.objects.prefetch_related("items").get(id=po_receipt.id)
        return Response(_serialize_po_receipt(po_receipt), status=status.HTTP_200_OK)


class FGGatePOListAPI(APIView):
    """List all PO receipts for a finished-goods gate entry."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewFGReceipt]

    def get(self, request, gate_entry_id):
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company=request.company.company,
            entry_type=FG_ENTRY_TYPE,
        )
        po_receipts = entry.po_receipts.prefetch_related("items")
        return Response([_serialize_po_receipt(po) for po in po_receipts])


class CompleteFGGateEntryAPI(APIView):
    """Complete and lock a finished-goods gate entry (no QC)."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanCompleteFGEntry]

    def post(self, request, gate_entry_id):
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company=request.company.company,
            entry_type=FG_ENTRY_TYPE,
        )
        try:
            complete_fg_gate_entry(entry)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"message": "Finished goods gate entry completed successfully"})


class FGGateEntryDeleteAPI(APIView):
    """Delete an in-progress finished-goods gate entry (before GRPO)."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanDeleteFGEntry]

    @transaction.atomic
    def delete(self, request, gate_entry_id):
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company=request.company.company,
            entry_type=FG_ENTRY_TYPE,
        )

        if entry.is_locked:
            raise ValidationError({"detail": "This gate entry is locked and cannot be deleted."})

        if entry.status not in GATE_PHASE_STATUSES:
            raise ValidationError(
                {"detail": "Only an in-progress gate entry can be deleted."}
            )

        for po in entry.po_receipts.all():
            if (
                po.grpo_postings.filter(status="POSTED").exists()
                or po.merged_grpo_postings.filter(status="POSTED").exists()
            ):
                raise ValidationError(
                    {"detail": "This entry has a posted GRPO and cannot be deleted."}
                )
            for item in po.items.all():
                if item.grpo_lines.exists():
                    raise ValidationError(
                        {"detail": "This entry has GRPO line activity and cannot be deleted."}
                    )

        entry_no = entry.entry_no
        try:
            entry.delete()
        except ProtectedError:
            raise ValidationError(
                {"detail": "This entry is referenced by other records and cannot be deleted."}
            )

        logger.info(
            "Finished goods gate entry %s (id=%s) deleted by user %s",
            entry_no,
            gate_entry_id,
            getattr(request.user, "id", None),
        )
        return Response(status=status.HTTP_204_NO_CONTENT)
