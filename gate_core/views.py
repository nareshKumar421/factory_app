from django.db import transaction
from django.shortcuts import get_object_or_404, render

# Create your views here.
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from company.permissions import HasCompanyContext
from driver_management.models import Driver, VehicleEntry
from vehicle_management.models import Vehicle
from quality_control.enums import FactoryHeadDecision, InspectionStatus
from quality_control.models import RawMaterialInspection
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import NotFound, ValidationError
from .permissions import (
    CanViewRawMaterialFullEntry,
    CanViewDailyNeedFullEntry,
    CanViewMaintenanceFullEntry,
    CanViewConstructionFullEntry,
)
from .models import GateAttachment, RejectedQCReturnEntry, RejectedQCReturnItem, UnitChoice
from .serializers import (
    GateAttachmentSerializer,
    RejectedQCReturnCreateSerializer,
    RejectedQCReturnEntrySerializer,
    UnitChoiceSerializer,
)

class GateAttachmentListCreateView(APIView):
    """
    API view to list and create gate attachments for a specific gate entry
    """
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, gate_entry_id):
        attachments = GateAttachment.objects.filter(gate_entry_id=gate_entry_id)
        serializer = GateAttachmentSerializer(attachments, many=True, context={'request': request})
        return Response(serializer.data)

    def post(self, request, gate_entry_id):
        # Validate that the gate entry exists and belongs to the company
        try:
            entry = VehicleEntry.objects.get(id=gate_entry_id, company=request.company.company)
        except VehicleEntry.DoesNotExist:
            raise NotFound("Gate entry not found")

        serializer = GateAttachmentSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save(gate_entry=entry)

        return Response(serializer.data, status=201)


class UnitChoiceListView(APIView):
    """
    API view to list all unit choices
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        units = UnitChoice.objects.all()
        serializer = UnitChoiceSerializer(units, many=True)
        return Response(serializer.data)


class RejectedQCReturnListCreateView(APIView):
    """List and create Rejected QC Return gate-out entries."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        qs = (
            RejectedQCReturnEntry.objects
            .filter(company=request.company.company, is_active=True)
            .select_related("vehicle", "driver", "company")
            .prefetch_related("items")
        )

        from_date = request.query_params.get("from_date")
        to_date = request.query_params.get("to_date")
        if from_date:
            qs = qs.filter(gate_out_date__gte=from_date)
        if to_date:
            qs = qs.filter(gate_out_date__lte=to_date)

        serializer = RejectedQCReturnEntrySerializer(qs, many=True)
        return Response(serializer.data)

    def post(self, request):
        serializer = RejectedQCReturnCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        vehicle = get_object_or_404(Vehicle, id=data["vehicle_id"])
        driver = get_object_or_404(Driver, id=data["driver_id"])
        inspection_ids = list(dict.fromkeys(data["inspection_ids"]))

        inspections = list(
            RawMaterialInspection.objects
            .filter(
                id__in=inspection_ids,
                arrival_slip__po_item_receipt__po_receipt__vehicle_entry__company=request.company.company,
            )
            .select_related(
                "arrival_slip",
                "arrival_slip__po_item_receipt",
                "arrival_slip__po_item_receipt__po_receipt",
                "arrival_slip__po_item_receipt__po_receipt__vehicle_entry",
            )
        )

        if len(inspections) != len(inspection_ids):
            return Response(
                {"detail": "One or more selected QC inspections were not found"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        invalid_items = []
        for inspection in inspections:
            if (
                inspection.final_status != InspectionStatus.REJECTED or
                inspection.factory_head_decision != FactoryHeadDecision.RETURN_TO_VENDOR
            ):
                invalid_items.append(inspection.report_no)
                continue

            if hasattr(inspection, "rejected_qc_return_item"):
                invalid_items.append(f"{inspection.report_no} already returned")

        if invalid_items:
            return Response(
                {
                    "detail": "Only Factory Head approved Return to Vendor QC items can be returned",
                    "invalid_items": invalid_items,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            entry = RejectedQCReturnEntry.objects.create(
                company=request.company.company,
                entry_no=RejectedQCReturnEntry.generate_entry_no(),
                vehicle=vehicle,
                driver=driver,
                gate_out_date=data["gate_out_date"],
                out_time=data.get("out_time"),
                challan_no=data.get("challan_no", ""),
                eway_bill_no=data.get("eway_bill_no", ""),
                manual_sap_reference=data.get("manual_sap_reference", ""),
                security_name=data.get("security_name", ""),
                remarks=data.get("remarks", ""),
                created_by=request.user,
                updated_by=request.user,
            )

            for inspection in inspections:
                arrival_slip = inspection.arrival_slip
                po_item = arrival_slip.po_item_receipt
                vehicle_entry = po_item.po_receipt.vehicle_entry

                RejectedQCReturnItem.objects.create(
                    entry=entry,
                    inspection=inspection,
                    gate_entry_no=vehicle_entry.entry_no,
                    report_no=inspection.report_no,
                    internal_lot_no=inspection.internal_lot_no,
                    item_name=po_item.item_name,
                    supplier_name=arrival_slip.party_name,
                    quantity=arrival_slip.billing_qty,
                    uom=arrival_slip.billing_uom,
                    created_by=request.user,
                    updated_by=request.user,
                )

        return Response(
            RejectedQCReturnEntrySerializer(entry).data,
            status=status.HTTP_201_CREATED,
        )


class RejectedQCReturnDetailView(APIView):
    """Get one Rejected QC Return gate-out entry."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, entry_id):
        entry = get_object_or_404(
            RejectedQCReturnEntry.objects
            .select_related("vehicle", "driver", "company")
            .prefetch_related("items"),
            id=entry_id,
            company=request.company.company,
            is_active=True,
        )
        return Response(RejectedQCReturnEntrySerializer(entry).data)


class RawMaterialGateEntryFullView(APIView):
    """
    Get complete raw material gate entry data (read-only)
    Includes QC status summary for each item and overall gate entry
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewRawMaterialFullEntry]

    def _get_qc_status(self, arrival_slip, inspection):
        """
        Determine QC status for an item based on arrival slip and inspection.
        Returns tuple: (status_code, status_display)
        """
        if not arrival_slip:
            return "NO_SLIP", "No Arrival Slip"

        if not inspection:
            if arrival_slip.is_submitted:
                return "AWAITING_INSPECTION", "Awaiting Inspection"
            return "SLIP_DRAFT", "Slip in Draft"

        # Has inspection - check workflow and final status
        if inspection.workflow_status == "DRAFT":
            return "INSPECTION_DRAFT", "Inspection in Draft"
        elif inspection.workflow_status == "SUBMITTED":
            return "AWAITING_CHEMIST", "Awaiting Chemist Approval"
        elif inspection.workflow_status == "QA_CHEMIST_APPROVED":
            return "AWAITING_QAM", "Awaiting QAM Approval"
        elif inspection.workflow_status == "REJECTED":
            return "REJECTED", "QC Rejected"
        elif inspection.workflow_status in ["QAM_APPROVED", "COMPLETED"]:
            # Check final status
            if inspection.final_status == "ACCEPTED":
                return "ACCEPTED", "QC Accepted"
            elif inspection.final_status == "REJECTED":
                return "REJECTED", "QC Rejected"
            elif inspection.final_status == "HOLD":
                return "HOLD", "On Hold"
            else:
                return "PENDING", "QC Pending"

        return "PENDING", "QC Pending"

    def get(self, request, gate_entry_id):

        try:
            entry = (
                VehicleEntry.objects
                .select_related(
                    "vehicle",
                    "driver",
                    "security_check",
                    "weighment",
                    "created_by"
                )
                .prefetch_related(
                    "po_receipts__items__arrival_slip__inspection__material_type",
                    "po_receipts__items__arrival_slip__inspection__qa_chemist",
                    "po_receipts__items__arrival_slip__inspection__qam",
                    "po_receipts__items__arrival_slip__inspection__rejected_by",
                    "po_receipts__items__arrival_slip__submitted_by",
                    "po_receipts__created_by"
                )
                .get(id=gate_entry_id)
            )
        except VehicleEntry.DoesNotExist:
            raise NotFound("Gate entry not found")

        # QC Summary counters
        qc_summary = {
            "total_items": 0,
            "no_slip": 0,
            "slip_draft": 0,
            "awaiting_inspection": 0,
            "inspection_draft": 0,
            "awaiting_chemist": 0,
            "awaiting_qam": 0,
            "accepted": 0,
            "rejected": 0,
            "hold": 0,
            "pending": 0,
            "can_complete": False,
        }

        response = {
            "gate_entry": {
                "id": entry.id,
                "entry_no": entry.entry_no,
                "entry_type": entry.entry_type,
                "status": entry.status,
                "status_display": entry.get_status_display() if hasattr(entry, 'get_status_display') else entry.status,
                "is_locked": entry.is_locked,
                "created_at": entry.created_at,
                "updated_at": entry.updated_at,
                "created_by": entry.created_by.email if entry.created_by else None,
            },

            "vehicle": {
                "id": entry.vehicle.id,
                "vehicle_number": entry.vehicle.vehicle_number,
                "vehicle_type": entry.vehicle.vehicle_type.name if entry.vehicle.vehicle_type else None,
                "capacity_ton": float(entry.vehicle.capacity_ton) if entry.vehicle.capacity_ton else None,
            },

            "driver": {
                "id": entry.driver.id,
                "name": entry.driver.name,
                "mobile_no": entry.driver.mobile_no,
                "license_no": entry.driver.license_no,
            },

            "security_check": None,
            "weighment": None,
            "qc_summary": qc_summary,
            "po_receipts": [],
        }

        # -------------------------
        # SECURITY CHECK
        # -------------------------
        if hasattr(entry, "security_check") and entry.security_check:
            sc = entry.security_check
            response["security_check"] = {
                "id": sc.id,
                "vehicle_condition_ok": sc.vehicle_condition_ok,
                "tyre_condition_ok": sc.tyre_condition_ok,
                "fire_extinguisher_available": sc.fire_extinguisher_available,
                "alcohol_test_done": sc.alcohol_test_done,
                "alcohol_test_passed": sc.alcohol_test_passed,
                "is_submitted": sc.is_submitted,
                "remarks": sc.remarks,
                "inspected_by": sc.inspected_by_name,
                "created_at": sc.created_at,
                "updated_at": sc.updated_at,
            }

        # -------------------------
        # WEIGHMENT
        # -------------------------
        if hasattr(entry, "weighment") and entry.weighment:
            w = entry.weighment
            response["weighment"] = {
                "id": w.id,
                "gross_weight": float(w.gross_weight) if w.gross_weight else None,
                "tare_weight": float(w.tare_weight) if w.tare_weight else None,
                "net_weight": float(w.net_weight) if w.net_weight else None,
                "weighbridge_slip_no": w.weighbridge_slip_no,
                "created_at": w.created_at,
                "updated_at": w.updated_at,
            }

        # -------------------------
        # PO RECEIPTS + ITEMS + QC
        # -------------------------
        all_items_completed = True

        for po in entry.po_receipts.all():
            po_data = {
                "id": po.id,
                "po_number": po.po_number,
                "po_date": po.po_date if hasattr(po, 'po_date') else None,
                "supplier_code": po.supplier_code,
                "supplier_name": po.supplier_name,
                "created_by": po.created_by.email if po.created_by else None,
                "created_at": po.created_at,
                "items": []
            }

            for item in po.items.all():
                qc_summary["total_items"] += 1

                arrival_slip = getattr(item, "arrival_slip", None)
                inspection = getattr(arrival_slip, "inspection", None) if arrival_slip else None

                # Get QC status
                qc_status_code, qc_status_display = self._get_qc_status(arrival_slip, inspection)

                # Update QC summary counters
                status_map = {
                    "NO_SLIP": "no_slip",
                    "SLIP_DRAFT": "slip_draft",
                    "AWAITING_INSPECTION": "awaiting_inspection",
                    "INSPECTION_DRAFT": "inspection_draft",
                    "AWAITING_CHEMIST": "awaiting_chemist",
                    "AWAITING_QAM": "awaiting_qam",
                    "ACCEPTED": "accepted",
                    "REJECTED": "rejected",
                    "HOLD": "hold",
                    "PENDING": "pending",
                }
                if qc_status_code in status_map:
                    qc_summary[status_map[qc_status_code]] += 1

                # Check if this item is completed (for gate completion check)
                if qc_status_code not in ["ACCEPTED", "REJECTED"]:
                    all_items_completed = False

                item_data = {
                    "id": item.id,
                    "item_code": item.po_item_code,
                    "item_name": item.item_name,
                    "ordered_qty": float(item.ordered_qty),
                    "received_qty": float(item.received_qty),
                    "short_qty": float(item.short_qty),
                    "uom": item.uom,
                    "qc_status": {
                        "code": qc_status_code,
                        "display": qc_status_display,
                    },
                    "arrival_slip": None,
                    "inspection": None
                }

                if arrival_slip:
                    item_data["arrival_slip"] = {
                        "id": arrival_slip.id,
                        "status": arrival_slip.status,
                        "status_display": arrival_slip.get_status_display() if hasattr(arrival_slip, 'get_status_display') else arrival_slip.status,
                        "is_submitted": arrival_slip.is_submitted,
                        "particulars": arrival_slip.particulars,
                        "party_name": arrival_slip.party_name,
                        "billing_qty": float(arrival_slip.billing_qty),
                        "billing_uom": arrival_slip.billing_uom,
                        "arrival_datetime": arrival_slip.arrival_datetime,
                        "truck_no_as_per_bill": arrival_slip.truck_no_as_per_bill,
                        "commercial_invoice_no": arrival_slip.commercial_invoice_no,
                        "eway_bill_no": arrival_slip.eway_bill_no,
                        "bilty_no": arrival_slip.bilty_no,
                        "has_certificate_of_analysis": arrival_slip.has_certificate_of_analysis,
                        "has_certificate_of_quantity": arrival_slip.has_certificate_of_quantity,
                        "weighing_required": arrival_slip.weighing_required,
                        "in_time_to_qa": arrival_slip.in_time_to_qa,
                        "submitted_at": arrival_slip.submitted_at,
                        "submitted_by": arrival_slip.submitted_by.email if arrival_slip.submitted_by else None,
                        "remarks": arrival_slip.remarks,
                        "created_at": arrival_slip.created_at,
                    }

                if inspection:
                    item_data["inspection"] = {
                        "id": inspection.id,
                        "report_no": inspection.report_no,
                        "internal_lot_no": inspection.internal_lot_no,
                        "inspection_date": inspection.inspection_date,
                        "description_of_material": inspection.description_of_material,
                        "sap_code": inspection.sap_code,
                        "material_type": inspection.material_type.name if inspection.material_type else None,
                        "material_type_id": inspection.material_type.id if inspection.material_type else None,
                        "supplier_name": inspection.supplier_name,
                        "manufacturer_name": inspection.manufacturer_name,
                        "supplier_batch_lot_no": inspection.supplier_batch_lot_no,
                        "unit_packing": inspection.unit_packing,
                        "purchase_order_no": inspection.purchase_order_no,
                        "invoice_bill_no": inspection.invoice_bill_no,
                        "vehicle_no": inspection.vehicle_no,
                        "workflow_status": inspection.workflow_status,
                        "workflow_status_display": inspection.get_workflow_status_display() if hasattr(inspection, 'get_workflow_status_display') else inspection.workflow_status,
                        "final_status": inspection.final_status,
                        "final_status_display": inspection.get_final_status_display() if hasattr(inspection, 'get_final_status_display') else inspection.final_status,
                        "is_locked": inspection.is_locked,
                        "qa_chemist": inspection.qa_chemist.email if inspection.qa_chemist else None,
                        "qa_chemist_approved_at": inspection.qa_chemist_approved_at,
                        "qa_chemist_remarks": inspection.qa_chemist_remarks,
                        "qam": inspection.qam.email if inspection.qam else None,
                        "qam_approved_at": inspection.qam_approved_at,
                        "qam_remarks": inspection.qam_remarks,
                        "rejected_by": inspection.rejected_by.email if inspection.rejected_by else None,
                        "rejected_at": inspection.rejected_at,
                        "remarks": inspection.remarks,
                        "created_at": inspection.created_at,
                    }

                po_data["items"].append(item_data)

            response["po_receipts"].append(po_data)

        # Set can_complete flag
        qc_summary["can_complete"] = (
            qc_summary["total_items"] > 0 and
            all_items_completed
        )

        return Response(response)
    

class DailyNeedGateEntryFullView(APIView):
    """
    Get complete Daily Need / Canteen gate entry data
    (Human readable, no serializers)
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewDailyNeedFullEntry]

    def get(self, request, gate_entry_id):

        try:
            entry = (
                VehicleEntry.objects
                .select_related(
                    "vehicle",
                    "driver",
                    "security_check",
                    "daily_need_entry"
                )
                .get(id=gate_entry_id)
            )
        except VehicleEntry.DoesNotExist:
            raise NotFound("Gate entry not found")

        # ✅ ensure correct type
        if entry.entry_type != "DAILY_NEED":
            raise ValidationError("Not a daily need gate entry")

        daily = getattr(entry, "daily_need_entry", None)
        security = getattr(entry, "security_check", None)

        response = {
            # -----------------------
            # Gate Info
            # -----------------------
            "gate_entry": {
                "id": entry.id,
                "entry_no": entry.entry_no,
                "status": entry.status,
                "is_locked": entry.is_locked,
                "created_at": entry.created_at,
                "updated_at": entry.updated_at,
                "entry_type": entry.entry_type,
            },

            # -----------------------
            # Vehicle
            # -----------------------
            "vehicle": {
                "vehicle_number": entry.vehicle.vehicle_number,
                "vehicle_type": entry.vehicle.vehicle_type.name if entry.vehicle.vehicle_type else None,
                "capacity_ton": entry.vehicle.capacity_ton,
            },

            # -----------------------
            # Driver
            # -----------------------
            "driver": {
                "name": entry.driver.name,
                "mobile_no": entry.driver.mobile_no,
                "license_no": entry.driver.license_no,
            },

            # -----------------------
            # Security
            # -----------------------
            "security_check": None,

            # -----------------------
            # Daily Need Details
            # -----------------------
            "daily_need_details": None,
        }

        # =========================
        # SECURITY SECTION
        # =========================
        if security:
            response["security_check"] = {
                "vehicle_condition_ok": security.vehicle_condition_ok,
                "tyre_condition_ok": security.tyre_condition_ok,
                "alcohol_test_passed": security.alcohol_test_passed,
                "is_submitted": security.is_submitted,
                "remarks": security.remarks,
                "inspected_by": (
                    security.inspected_by_name
                ),
            }

        # =========================
        # DAILY NEED SECTION
        # =========================
        if daily:
            response["daily_need_details"] = {
                "category": daily.item_category.category_name,
                "supplier_name": daily.supplier_name,
                "material_name": daily.material_name,
                "quantity": float(daily.quantity),
                "unit": daily.unit.name if daily.unit else None,
                "receiving_department": daily.receiving_department.name,

                "bill_number": daily.bill_number,
                "delivery_challan_number": daily.delivery_challan_number,

                "canteen_supervisor": daily.canteen_supervisor,
                "vehicle_or_person_name": daily.vehicle_or_person_name,
                "contact_number": daily.contact_number,

                "remarks": daily.remarks,

                "created_by": (
                    daily.created_by.email
                    if daily.created_by else None
                ),
                "created_at": daily.created_at,
                "updated_at": daily.updated_at,
            }

        return Response(response)


class MaintenanceGateEntryFullView(APIView):
    """
    Get complete Maintenance & Repair Material gate entry data
    (Human readable, no serializers)
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewMaintenanceFullEntry]

    def get(self, request, gate_entry_id):

        try:
            entry = (
                VehicleEntry.objects
                .select_related(
                    "vehicle",
                    "driver",
                    "security_check",
                    "maintenance_entry",
                    "maintenance_entry__maintenance_type",
                    "maintenance_entry__receiving_department",
                    "maintenance_entry__created_by",
                )
                .get(id=gate_entry_id)
            )
        except VehicleEntry.DoesNotExist:
            raise NotFound("Gate entry not found")

        # Ensure correct type
        if entry.entry_type != "MAINTENANCE":
            raise ValidationError("Not a maintenance gate entry")

        maintenance = getattr(entry, "maintenance_entry", None)
        security = getattr(entry, "security_check", None)

        response = {
            # -----------------------
            # Gate Info
            # -----------------------
            "gate_entry": {
                "id": entry.id,
                "entry_no": entry.entry_no,
                "status": entry.status,
                "is_locked": entry.is_locked,
                "created_at": entry.created_at,
                "updated_at": entry.updated_at,
                "entry_type": entry.entry_type,
            },

            # -----------------------
            # Vehicle
            # -----------------------
            "vehicle": {
                "vehicle_number": entry.vehicle.vehicle_number,
                "vehicle_type": entry.vehicle.vehicle_type.name if entry.vehicle.vehicle_type else None,
                "capacity_ton": entry.vehicle.capacity_ton,
            },

            # -----------------------
            # Driver
            # -----------------------
            "driver": {
                "name": entry.driver.name,
                "mobile_no": entry.driver.mobile_no,
                "license_no": entry.driver.license_no,
            },

            # -----------------------
            # Security
            # -----------------------
            "security_check": None,

            # -----------------------
            # Maintenance Details
            # -----------------------
            "maintenance_details": None,
        }

        # =========================
        # SECURITY SECTION
        # =========================
        if security:
            response["security_check"] = {
                "vehicle_condition_ok": security.vehicle_condition_ok,
                "tyre_condition_ok": security.tyre_condition_ok,
                "alcohol_test_passed": security.alcohol_test_passed,
                "is_submitted": security.is_submitted,
                "remarks": security.remarks,
                "inspected_by": security.inspected_by_name,
            }

        # =========================
        # MAINTENANCE SECTION
        # =========================
        if maintenance:
            response["maintenance_details"] = {
                "work_order_number": maintenance.work_order_number,
                "maintenance_type": (
                    maintenance.maintenance_type.type_name
                    if maintenance.maintenance_type else None
                ),
                "supplier_name": maintenance.supplier_name,
                "material_description": maintenance.material_description,
                "part_number": maintenance.part_number,
                "quantity": float(maintenance.quantity),
                "unit": maintenance.unit.name if maintenance.unit else None,
                "invoice_number": maintenance.invoice_number,
                "equipment_id": maintenance.equipment_id,
                "receiving_department": (
                    maintenance.receiving_department.name
                    if maintenance.receiving_department else None
                ),
                "urgency_level": maintenance.urgency_level,
                "inward_time": maintenance.inward_time,
                "remarks": maintenance.remarks,
                "created_by": (
                    maintenance.created_by.email
                    if maintenance.created_by else None
                ),
                "created_at": maintenance.created_at,
                "updated_at": maintenance.updated_at,
            }

        return Response(response)


class ConstructionGateEntryFullView(APIView):
    """
    Get complete Construction / Civil Work Material gate entry data
    (Human readable, no serializers)
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewConstructionFullEntry]

    def get(self, request, gate_entry_id):

        try:
            entry = (
                VehicleEntry.objects
                .select_related(
                    "vehicle",
                    "driver",
                    "security_check",
                    "construction_entry",
                    "construction_entry__material_category",
                    "construction_entry__created_by",
                )
                .get(id=gate_entry_id)
            )
        except VehicleEntry.DoesNotExist:
            raise NotFound("Gate entry not found")

        # Ensure correct type
        if entry.entry_type != "CONSTRUCTION":
            raise ValidationError("Not a construction gate entry")

        construction = getattr(entry, "construction_entry", None)
        security = getattr(entry, "security_check", None)

        response = {
            # -----------------------
            # Gate Info
            # -----------------------
            "gate_entry": {
                "id": entry.id,
                "entry_no": entry.entry_no,
                "status": entry.status,
                "is_locked": entry.is_locked,
                "created_at": entry.created_at,
                "updated_at": entry.updated_at,
                "entry_type": entry.entry_type,
            },

            # -----------------------
            # Vehicle
            # -----------------------
            "vehicle": {
                "vehicle_number": entry.vehicle.vehicle_number,
                "vehicle_type": entry.vehicle.vehicle_type.name if entry.vehicle.vehicle_type else None,
                "capacity_ton": entry.vehicle.capacity_ton,
            },

            # -----------------------
            # Driver
            # -----------------------
            "driver": {
                "name": entry.driver.name,
                "mobile_no": entry.driver.mobile_no,
                "license_no": entry.driver.license_no,
            },

            # -----------------------
            # Security
            # -----------------------
            "security_check": None,

            # -----------------------
            # Construction Details
            # -----------------------
            "construction_details": None,
        }

        # =========================
        # SECURITY SECTION
        # =========================
        if security:
            response["security_check"] = {
                "vehicle_condition_ok": security.vehicle_condition_ok,
                "tyre_condition_ok": security.tyre_condition_ok,
                "alcohol_test_passed": security.alcohol_test_passed,
                "is_submitted": security.is_submitted,
                "remarks": security.remarks,
                "inspected_by": security.inspected_by_name,
            }

        # =========================
        # CONSTRUCTION SECTION
        # =========================
        if construction:
            response["construction_details"] = {
                "work_order_number": construction.work_order_number,
                "project_name": construction.project_name,
                "material_category": (
                    construction.material_category.category_name
                    if construction.material_category else None
                ),
                "contractor_name": construction.contractor_name,
                "contractor_contact": construction.contractor_contact,
                "material_description": construction.material_description,
                "quantity": float(construction.quantity),
                "unit": construction.unit.name if construction.unit else None,
                "challan_number": construction.challan_number,
                "invoice_number": construction.invoice_number,
                "site_engineer": construction.site_engineer,
                "security_approval": construction.security_approval,
                "inward_time": construction.inward_time,
                "remarks": construction.remarks,
                "created_by": (
                    construction.created_by.email
                    if construction.created_by else None
                ),
                "created_at": construction.created_at,
                "updated_at": construction.updated_at,
            }

        return Response(response)
