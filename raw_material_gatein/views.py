import logging

from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from driver_management.models import VehicleEntry
from gate_core.enums import GateEntryStatus
from quality_control.enums import ArrivalSlipStatus, InspectionWorkflowStatus
from sap_client.client import SAPClient
from sap_client.exceptions import SAPConnectionError, SAPDataError

from .models import POItemReceipt, POReceipt, POReplacementLog
from .notifications import notify_gate_entry_completed, notify_po_received
from .permissions import CanCompleteRawMaterialEntry, CanReceivePO, CanViewPOReceipt
from .serializers import POReceiveRequestSerializer, POReplaceRequestSerializer
from .services import complete_gate_entry, validate_received_quantity

logger = logging.getLogger(__name__)


def _get_arrival_slip(item):
    try:
        return item.arrival_slip
    except ObjectDoesNotExist:
        return None


def _get_inspection(arrival_slip):
    try:
        return arrival_slip.inspection
    except ObjectDoesNotExist:
        return None


def _po_receipt_lock_reason(po_receipt):
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

        arrival_slip = _get_arrival_slip(item)
        if arrival_slip is None:
            continue

        if _get_inspection(arrival_slip) is not None:
            return "This PO has already moved to QC inspection and cannot be modified."

        # submitted_at is audit history. Sent-back/rejected slips keep their
        # prior submission timestamp but are open for gate corrections.
        if arrival_slip.is_submitted or arrival_slip.status == ArrivalSlipStatus.SUBMITTED:
            return "This PO cannot be edited after its arrival slip is submitted to QC."

    return None


def _po_receipt_replace_block_reason(po_receipt):
    """Why a wrong PO on a sent-back arrival slip cannot be replaced (None if it
    can). Unlike the edit lock, this allows a draft QC inspection (it gets voided
    on replace) but requires the slip to have been sent back by QC, and blocks
    once QC inspection has actually started or GRPO activity exists.
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
        return "This PO has already been posted to GRPO and cannot be replaced."

    sent_back = entry.status == GateEntryStatus.ARRIVAL_SLIP_REJECTED
    for item in po_receipt.items.all():
        if item.grpo_lines.exists():
            return "This PO has GRPO line activity and cannot be replaced."

        arrival_slip = _get_arrival_slip(item)
        if arrival_slip is None:
            continue

        if arrival_slip.sent_back_at is not None:
            sent_back = True

        inspection = _get_inspection(arrival_slip)
        if (
            inspection is not None
            and inspection.workflow_status != InspectionWorkflowStatus.DRAFT
        ):
            return "QC inspection has already started for this PO and it cannot be replaced."

    if not sent_back:
        return (
            "Replace PO is only for an arrival slip that QC has sent back. "
            "Edit the PO directly before it is submitted to QC."
        )

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


def _get_sap_po_details(company_code, supplier_code, po_number):
    try:
        client = SAPClient(company_code=company_code)
        sap_pos = client.get_open_pos(supplier_code)
    except SAPConnectionError as e:
        logger.error("SAP connection error in ReceivePOAPI: %s", e)
        raise APIException(
            detail="SAP system is currently unavailable. Please try again later.",
            code=503
        )
    except SAPDataError as e:
        logger.error("SAP data error in ReceivePOAPI: %s", e)
        raise APIException(
            detail="Failed to retrieve PO data from SAP.",
            code=502
        )

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
        raise ValidationError({"detail": f"Open PO {po_number} was not found for this supplier."})

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
                received_qty
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
            "accepted_qty": 0,
            "rejected_qty": 0,
        }

        item = existing_by_line.get(line_num)
        if item is None:
            POItemReceipt.objects.create(
                po_receipt=po_receipt,
                **defaults,
                created_by=user
            )
            continue

        for field, value in defaults.items():
            setattr(item, field, value)
        item.updated_by = user
        item.save()

    po_receipt.items.exclude(sap_line_num__in=seen_line_nums).delete()


def _set_entry_back_to_qc_pending(entry):
    if entry.status != GateEntryStatus.QC_PENDING:
        entry.status = GateEntryStatus.QC_PENDING
        entry.save(update_fields=["status"])


class ReceivePOAPI(APIView):
    """
    Receive raw material PO items against a vehicle entry.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanReceivePO]

    @transaction.atomic
    def post(self, request, gate_entry_id):
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company=request.company.company
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
            raise ValidationError({"detail": f"PO {po_number} is already added to this gate entry."})

        sap_header, sap_items_map = _get_sap_po_details(
            request.company.company.code,
            supplier_code,
            po_number
        )

        try:
            po_receipt = POReceipt.objects.create(
                vehicle_entry=entry,
                po_number=po_number,
                supplier_code=supplier_code,
                supplier_name=supplier_name,
                **sap_header,
                created_by=request.user
            )
        except IntegrityError:
            raise ValidationError({"detail": f"PO {po_number} is already added to this gate entry."})

        _save_po_items(po_receipt, items_data, sap_items_map, request.user)
        _set_entry_back_to_qc_pending(entry)
        notify_po_received(po_receipt, request.user)

        return Response(
            {
                "message": "PO items received successfully",
                "po_receipt": _serialize_po_receipt(po_receipt),
            },
            status=status.HTTP_201_CREATED
        )


class POReceiptDetailAPI(APIView):
    """
    Update PO receipt details until its arrival slip is submitted to QC.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanReceivePO]

    @transaction.atomic
    def put(self, request, gate_entry_id, po_receipt_id):
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company=request.company.company
        )
        _ensure_entry_accepts_po_changes(entry)

        po_receipt = get_object_or_404(
            POReceipt.objects.select_related("vehicle_entry").prefetch_related(
                "items",
                "items__arrival_slip",
                "items__arrival_slip__inspection",
            ),
            id=po_receipt_id,
            vehicle_entry=entry
        )

        lock_reason = _po_receipt_lock_reason(po_receipt)
        if lock_reason:
            raise ValidationError({"detail": lock_reason})

        request_serializer = POReceiveRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        validated_data = request_serializer.validated_data

        po_number = validated_data["po_number"]
        supplier_code = validated_data["supplier_code"]

        duplicate_exists = POReceipt.objects.filter(
            vehicle_entry=entry,
            po_number=po_number
        ).exclude(id=po_receipt.id).exists()
        if duplicate_exists:
            raise ValidationError({"detail": f"PO {po_number} is already added to this gate entry."})

        sap_header, sap_items_map = _get_sap_po_details(
            request.company.company.code,
            supplier_code,
            po_number
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
        _set_entry_back_to_qc_pending(entry)

        po_receipt = POReceipt.objects.prefetch_related("items").get(id=po_receipt.id)
        notify_po_received(po_receipt, request.user, is_update=True)
        return Response(_serialize_po_receipt(po_receipt), status=status.HTTP_200_OK)


class POReceiptReplaceAPI(APIView):
    """Replace a wrong PO on an arrival slip that QC sent back to gate.

    An explicit, audited correction for when the gate operator booked the wrong
    PO and submitted it to QC. Requires a reason, only works while the slip is in
    a sent-back state with no real QC inspection or GRPO activity, and records the
    old -> new PO swap. The swap replaces the PO header and re-fetches the new
    PO's items from SAP, discarding the old items and their draft slips/inspection.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanReceivePO]

    @transaction.atomic
    def post(self, request, gate_entry_id, po_receipt_id):
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company=request.company.company
        )
        _ensure_entry_accepts_po_changes(entry)

        po_receipt = get_object_or_404(
            POReceipt.objects.select_related("vehicle_entry").prefetch_related(
                "items",
                "items__arrival_slip",
                "items__arrival_slip__inspection",
            ),
            id=po_receipt_id,
            vehicle_entry=entry
        )

        block_reason = _po_receipt_replace_block_reason(po_receipt)
        if block_reason:
            raise ValidationError({"detail": block_reason})

        request_serializer = POReplaceRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        validated_data = request_serializer.validated_data

        po_number = validated_data["po_number"]
        supplier_code = validated_data["supplier_code"]

        duplicate_exists = POReceipt.objects.filter(
            vehicle_entry=entry,
            po_number=po_number
        ).exclude(id=po_receipt.id).exists()
        if duplicate_exists:
            raise ValidationError({"detail": f"PO {po_number} is already added to this gate entry."})

        # Snapshot the old PO and the QC remark before the swap deletes them.
        old_po_number = po_receipt.po_number
        old_supplier_code = po_receipt.supplier_code
        old_supplier_name = po_receipt.supplier_name
        previous_qc_remark = ""
        for item in po_receipt.items.all():
            arrival_slip = _get_arrival_slip(item)
            if arrival_slip and arrival_slip.remarks:
                previous_qc_remark = arrival_slip.remarks
                break

        sap_header, sap_items_map = _get_sap_po_details(
            request.company.company.code,
            supplier_code,
            po_number
        )

        supplier_changed = (old_supplier_code or "").strip() != (supplier_code or "").strip()

        po_receipt.po_number = po_number
        po_receipt.supplier_code = supplier_code
        po_receipt.supplier_name = validated_data["supplier_name"]
        po_receipt.sap_doc_entry = sap_header["sap_doc_entry"]
        po_receipt.branch_id = sap_header["branch_id"]
        po_receipt.vendor_ref = sap_header["vendor_ref"]
        po_receipt.po_date = sap_header["po_date"]
        po_receipt.updated_by = request.user
        po_receipt.save()

        # Clean slate: drop the old items (cascading their draft slips/inspection)
        # so the new PO starts fresh instead of reusing slips by line number.
        po_receipt.items.all().delete()
        _save_po_items(po_receipt, validated_data["items"], sap_items_map, request.user)

        POReplacementLog.objects.create(
            vehicle_entry=entry,
            old_po_number=old_po_number,
            old_supplier_code=old_supplier_code,
            old_supplier_name=old_supplier_name,
            new_po_number=po_number,
            new_supplier_code=supplier_code,
            new_supplier_name=validated_data["supplier_name"],
            supplier_changed=supplier_changed,
            reason=validated_data["reason"],
            previous_qc_remark=previous_qc_remark,
            created_by=request.user,
        )

        _set_entry_back_to_qc_pending(entry)

        po_receipt = POReceipt.objects.prefetch_related("items").get(id=po_receipt.id)
        notify_po_received(po_receipt, request.user, is_update=True)

        response_data = _serialize_po_receipt(po_receipt)
        response_data["supplier_changed"] = supplier_changed
        return Response(response_data, status=status.HTTP_200_OK)


class GatePOListAPI(APIView):
    """
    List all PO receipts for a gate entry.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewPOReceipt]

    def get(self, request, gate_entry_id):
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company=request.company.company
        )

        po_receipts = entry.po_receipts.prefetch_related(
            "items",
            "items__arrival_slip",
            "items__arrival_slip__inspection",
        )
        return Response([_serialize_po_receipt(po) for po in po_receipts])


class CompleteGateEntryAPI(APIView):
    """
    Complete and lock a gate entry.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCompleteRawMaterialEntry]

    def post(self, request, gate_entry_id):
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company=request.company.company
        )
        try:
            complete_gate_entry(entry)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        notify_gate_entry_completed(entry, request.user)
        return Response({"message": "Gate entry completed successfully"})
