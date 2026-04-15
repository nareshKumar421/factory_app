import logging

from django.db import transaction
from rest_framework.exceptions import ValidationError, APIException
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from company.permissions import HasCompanyContext
from driver_management.models import VehicleEntry
from gate_core.enums import GateEntryStatus
from sap_client.client import SAPClient
from sap_client.exceptions import SAPConnectionError, SAPDataError
from .models import POReceipt, POItemReceipt
from .serializers import POReceiveRequestSerializer, POItemReceiveSerializer
from .services import validate_received_quantity, complete_gate_entry
from .permissions import CanReceivePO, CanViewPOReceipt, CanCompleteRawMaterialEntry

logger = logging.getLogger(__name__)


class ReceivePOAPI(APIView):
    """
    Receive raw material PO items against a vehicle entry
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanReceivePO]

    @transaction.atomic
    def post(self, request, gate_entry_id):
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company=request.company.company
        )
    
        # Validate request payload
        request_serializer = POReceiveRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        validated_data = request_serializer.validated_data
    
        po_number = validated_data["po_number"]
        supplier_code = validated_data["supplier_code"]
        supplier_name = validated_data["supplier_name"]
        items_data = validated_data["items"]
    
        # Fetch SAP remaining quantities first (before creating any records)
        try:
            client = SAPClient(company_code=request.company.company.code)
            sap_pos = client.get_open_pos(supplier_code)
        except SAPConnectionError as e:
            logger.error(f"SAP connection error in ReceivePOAPI: {e}")
            # Raise exception to trigger rollback
            raise APIException(
                detail="SAP system is currently unavailable. Please try again later.",
                code=503
            )
        except SAPDataError as e:
            logger.error(f"SAP data error in ReceivePOAPI: {e}")
            # Raise exception to trigger rollback
            raise APIException(
                detail="Failed to retrieve PO data from SAP.",
                code=502
            )
    
        # Build SAP items map keyed by line_num (POR1.LineNum is the unique
        # PO line identifier in SAP — po_item_code can repeat across lines).
        sap_items_map = {}
        sap_doc_entry = None
        sap_branch_id = None
        sap_vendor_ref = ""
        for po in sap_pos:
            if po.po_number == po_number:
                sap_doc_entry = po.doc_entry
                sap_branch_id = po.branch_id
                sap_vendor_ref = po.vendor_ref or ""
                for i in po.items:
                    sap_items_map[i.line_num] = {
                        "po_item_code": i.po_item_code,
                        "remaining_qty": i.remaining_qty,
                        "rate": i.rate,
                        "tax_code": i.tax_code,
                        "warehouse_code": i.warehouse_code,
                        "account_code": i.account_code,
                        "variety": i.variety,
                    }

        # Create PO receipt header (after SAP validation succeeds)
        po_receipt = POReceipt.objects.create(
            vehicle_entry=entry,
            po_number=po_number,
            supplier_code=supplier_code,
            supplier_name=supplier_name,
            sap_doc_entry=sap_doc_entry,
            branch_id=sap_branch_id,
            vendor_ref=sap_vendor_ref,
            created_by=request.user
        )
    
        # Validate and create item receipts
        for item_data in items_data:
            line_num = item_data.pop("line_num")
            po_item_code = item_data["po_item_code"]
            received_qty = item_data["received_qty"]

            sap_item_info = sap_items_map.get(line_num)
            if sap_item_info is None:
                # Raise exception to trigger rollback
                raise ValidationError(
                    {"detail": f"Invalid PO line {line_num} for item {po_item_code}"}
                )

            # Sanity check: item code on the request must match the PO line
            if sap_item_info["po_item_code"] != po_item_code:
                raise ValidationError(
                    {"detail": f"Item code {po_item_code} does not match PO line {line_num}"}
                )

            remaining_qty = sap_item_info["remaining_qty"]

            # This will automatically raise ValueError if validation fails
            try:
                validate_received_quantity(
                    item_data["ordered_qty"],
                    remaining_qty,
                    received_qty
                )
            except ValueError as e:
                # Convert to ValidationError to trigger rollback
                raise ValidationError({"error": str(e)})

            POItemReceipt.objects.create(
                po_receipt=po_receipt,
                **item_data,
                sap_line_num=line_num,
                unit_price=sap_item_info["rate"] or None,
                tax_code=sap_item_info["tax_code"],
                warehouse_code=sap_item_info["warehouse_code"],
                gl_account=sap_item_info["account_code"],
                variety=sap_item_info.get("variety", ""),
                created_by=request.user
            )
    
        # Update status after successful receipt
        if entry.status == GateEntryStatus.IN_PROGRESS:
            entry.status = GateEntryStatus.QC_PENDING
            entry.save(update_fields=["status"])
    
        return Response(
            {"message": "PO items received successfully"},
            status=status.HTTP_201_CREATED
        )


class GatePOListAPI(APIView):
    """
    List all PO receipts for a gate entry
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewPOReceipt]

    def get(self, request, gate_entry_id):
        entry = get_object_or_404(
            VehicleEntry,
            id=gate_entry_id,
            company=request.company.company
        )

        response = []
        for po in entry.po_receipts.all():
            response.append({
                "id": po.id,
                "po_number": po.po_number,
                "supplier_code": po.supplier_code,
                "supplier_name": po.supplier_name,
                "created_at": po.created_at,
                "updated_at": po.updated_at,
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
                    }
                    for item in po.items.all()
                ]
            })

        return Response(response)


class CompleteGateEntryAPI(APIView):
    """
    Complete and lock a gate entry
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
        return Response({"message": "Gate entry completed successfully"})

