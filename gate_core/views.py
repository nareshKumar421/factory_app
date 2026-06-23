import datetime as dt
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Count, Prefetch
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.dateparse import parse_date

# Create your views here.
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from company.permissions import HasCompanyContext
from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
from driver_management.models import Driver, VehicleEntry
from vehicle_management.models import Vehicle
from quality_control.enums import FactoryHeadDecision, InspectionStatus
from quality_control.models import RawMaterialInspection
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import NotFound, ValidationError
from sap_client.client import SAPClient
from sap_client.exceptions import SAPConnectionError, SAPDataError
from .permissions import (
    CanViewRawMaterialFullEntry,
    CanViewDailyNeedFullEntry,
    CanViewMaintenanceFullEntry,
    CanViewConstructionFullEntry,
)
from .enums import GRPO_READY_STATUSES
from .services.empty_vehicle_dispatch import (
    record_dispatch_covers,
    replicate_dispatch_gate_in_across_companies,
    retire_empty_in,
)
from .services.user_scope import user_company_ids, wants_all_companies
from .models import (
    BSTGateIn,
    BSTGateInItem,
    BSTGateOut,
    BSTGateOutItem,
    BSTGateReturn,
    EmptyVehicleGateIn,
    EmptyVehicleGateInItem,
    EmptyVehicleGateInRetireReason,
    EmptyVehicleGateOut,
    GateAttachment,
    JobWorkGateIn,
    JobWorkGateInItem,
    RejectedQCReturnEntry,
    RejectedQCReturnItem,
    SalesDispatchBoxScan,
    SalesDispatchDocumentType,
    SalesDispatchGateOut,
    SalesDispatchGateOutItem,
    SalesDispatchGateOutStatus,
    UnitChoice,
)
from .serializers import (
    BSTGateInCreateSerializer,
    BSTGateInSerializer,
    BSTGateOutCancelSerializer,
    BSTGateOutCreateSerializer,
    BSTGateOutSerializer,
    BSTGateReturnCreateSerializer,
    BSTGateReturnSerializer,
    EmptyVehicleGateInCreateSerializer,
    EmptyVehicleGateInSerializer,
    EmptyVehicleGateInUpdateSerializer,
    EmptyVehicleEligibleEntrySerializer,
    EmptyVehicleGateOutCancelSerializer,
    EmptyVehicleGateOutCreateSerializer,
    EmptyVehicleGateOutSerializer,
    GateAttachmentSerializer,
    JobWorkGateInCreateSerializer,
    JobWorkGateInSerializer,
    RejectedQCReturnCreateSerializer,
    RejectedQCReturnEntrySerializer,
    SAPGRPOSerializer,
    SAPProductionOrderSerializer,
    SAPStockTransferSerializer,
    SalesDispatchBSTEligibleOutSerializer,
    UnitChoiceSerializer,
)


def has_required_weighment(vehicle_entry):
    if not hasattr(vehicle_entry, "weighment"):
        return False

    weighment = vehicle_entry.weighment
    return (
        weighment.gross_weight is not None
        and weighment.tare_weight is not None
        and weighment.gross_weight > 0
        and weighment.tare_weight >= 0
        and weighment.tare_weight <= weighment.gross_weight
    )


def required_weighment_response():
    return Response(
        {"detail": "Weighment is required before completing this gate-out entry"},
        status=status.HTTP_400_BAD_REQUEST,
    )


def has_gatepass_attachment(vehicle_entry):
    return GateAttachment.objects.filter(gate_entry=vehicle_entry).exists()


def required_gatepass_response():
    return Response(
        {"detail": "Gatepass document upload is required before completing this gate-out entry"},
        status=status.HTTP_400_BAD_REQUEST,
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


class EmptyVehicleGateInReasonListView(APIView):
    """List supported empty vehicle gate-in reasons."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        reason_field = EmptyVehicleGateIn._meta.get_field("reason")
        return Response([
            {"value": value, "label": label}
            for value, label in reason_field.choices
        ])


def get_sap_stock_transfer_or_error(request, doc_entry):
    try:
        client = SAPClient(company_code=request.company.company.code)
        sap_transfer = client.get_stock_transfer(doc_entry)
    except SAPConnectionError:
        return None, Response(
            {"detail": "SAP system is currently unavailable. Please try again later."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    except SAPDataError:
        return None, Response(
            {"detail": "Failed to retrieve selected BST document from SAP."},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    if not sap_transfer:
        return None, None

    return sap_transfer, None


def apply_sap_transfer_to_empty_gate_in(gate_in, sap_transfer):
    gate_in.sap_doc_entry = sap_transfer["doc_entry"]
    gate_in.sap_doc_num = sap_transfer["doc_num"]
    gate_in.sap_doc_date = sap_transfer.get("doc_date")
    gate_in.sap_from_warehouse = sap_transfer.get("from_warehouse", "")
    gate_in.sap_to_warehouse = sap_transfer.get("to_warehouse", "")
    gate_in.sap_reference = sap_transfer.get("reference", "")
    gate_in.sap_comments = sap_transfer.get("comments", "")
    gate_in.sap_line_count = sap_transfer.get("line_count", 0)
    gate_in.sap_total_quantity = sap_transfer.get("total_quantity", 0) or 0


def clear_sap_transfer_from_empty_gate_in(gate_in):
    gate_in.sap_doc_entry = None
    gate_in.sap_doc_num = ""
    gate_in.sap_doc_date = None
    gate_in.sap_from_warehouse = ""
    gate_in.sap_to_warehouse = ""
    gate_in.sap_reference = ""
    gate_in.sap_comments = ""
    gate_in.sap_line_count = 0
    gate_in.sap_total_quantity = 0


def parse_line_quantities(raw_items, quantity_field, label):
    quantities = {}
    for item in raw_items or []:
        line_num = item.get("line_num")
        if line_num is None:
            raise ValidationError({"items": f"Line number is required for {label}."})

        value = item.get(quantity_field)
        if value in (None, ""):
            raise ValidationError({"items": f"{label} is required for line {line_num}."})

        try:
            quantity = Decimal(str(value))
            line_num = int(line_num)
        except (InvalidOperation, TypeError, ValueError):
            raise ValidationError({"items": f"Enter a valid {label.lower()} for line {line_num}."})

        if quantity < 0:
            raise ValidationError({"items": f"{label} cannot be negative for line {line_num}."})

        quantities[line_num] = quantity
    return quantities


def sync_empty_gate_in_items(gate_in, sap_transfer, actual_quantities, user):
    gate_in.items.all().delete()

    for line in sap_transfer.get("lines", []):
        sap_quantity = Decimal(str(line.get("quantity", 0) or 0))
        line_num = int(line["line_num"])
        EmptyVehicleGateInItem.objects.create(
            empty_vehicle_gate_in=gate_in,
            line_num=line_num,
            item_code=line.get("item_code", ""),
            item_name=line.get("item_name", ""),
            sap_quantity=sap_quantity,
            actual_quantity=actual_quantities.get(line_num, sap_quantity),
            uom=line.get("uom", ""),
            from_warehouse=line.get("from_warehouse", ""),
            to_warehouse=line.get("to_warehouse", ""),
            created_by=user,
            updated_by=user,
        )


def sync_bst_gate_in_items(bst_in, bst_source, receiving_quantities, user):
    bst_in.items.all().delete()

    for item in bst_source.items.all():
        is_docking_stock_transfer_item = isinstance(item, SalesDispatchGateOutItem)
        quantity = item.quantity
        actual_quantity = getattr(item, "actual_quantity", None)
        if actual_quantity is None:
            actual_quantity = quantity
        received_quantity = receiving_quantities.get(
            item.line_num,
            actual_quantity,
        )
        BSTGateInItem.objects.create(
            bst_gate_in=bst_in,
            bst_gate_out_item=None if is_docking_stock_transfer_item else item,
            sales_dispatch_gate_out_item=item if is_docking_stock_transfer_item else None,
            line_num=item.line_num,
            item_code=item.item_code,
            item_name=item.item_name,
            quantity=quantity,
            actual_quantity=actual_quantity,
            receiving_quantity=received_quantity,
            uom=item.uom,
            from_warehouse=item.from_warehouse or getattr(item, "warehouse_code", ""),
            to_warehouse=item.to_warehouse,
            created_by=user,
            updated_by=user,
        )


def find_active_empty_vehicle_bst_link(company, sap_doc_entry, exclude_id=None):
    qs = (
        EmptyVehicleGateIn.objects
        .filter(
            company=company,
            reason="BST",
            sap_doc_entry=sap_doc_entry,
            is_active=True,
        )
        .exclude(vehicle_entry__status__in=["COMPLETED", "CANCELLED"])
        .order_by("-created_at")
    )
    if exclude_id:
        qs = qs.exclude(id=exclude_id)
    return qs.first()


def empty_vehicle_bst_already_linked_response(linked_gate_in):
    return Response(
        {
            "detail": (
                "This SAP BST document is already linked to "
                f"Empty Vehicle In entry {linked_gate_in.entry_no} "
                f"(entryId {linked_gate_in.id})."
            ),
            "linked_empty_vehicle_gate_in_id": linked_gate_in.id,
            "linked_entry_no": linked_gate_in.entry_no,
            "linked_entry_id": linked_gate_in.id,
            "linked_vehicle_entry_id": linked_gate_in.vehicle_entry_id,
        },
        status=status.HTTP_400_BAD_REQUEST,
    )


def empty_in_pipeline_prefetch():
    """Prefetch a gate-in's covers' plans + their gate-outs (direct and via
    ``documents``) so the serializer's aggregate ``pipeline_status`` is O(1) (no
    per-cover stage queries). Returns a list; splat it into ``prefetch_related``."""

    def docking_qs():
        return SalesDispatchGateOut.objects.order_by("-created_at").annotate(
            box_scan_count=Count("box_scans")
        )

    return [
        Prefetch(
            "covers__dispatch_plan__sales_dispatch_gate_outs",
            queryset=docking_qs(),
        ),
        Prefetch(
            "covers__dispatch_plan__sales_dispatch_gate_out_documents__sales_dispatch",
            queryset=docking_qs(),
        ),
    ]


class EmptyVehicleGateInListCreateView(APIView):
    """List and create empty vehicle gate-in records."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        # The gate is one physical place for all of a user's companies: when the
        # client opts in, span every company the user belongs to instead of just
        # the active Company-Code one.
        if wants_all_companies(request):
            company_filter = {"company_id__in": user_company_ids(request)}
        else:
            company_filter = {"company": request.company.company}
        qs = (
            EmptyVehicleGateIn.objects
            .filter(is_active=True, **company_filter)
            .select_related(
                "vehicle_entry",
                "vehicle",
                "vehicle__vehicle_type",
                "vehicle__transporter",
                "driver",
                "company",
            )
            .prefetch_related("bst_gate_outs", "items", "covers", "covers__dispatch_plan", "covers__dispatch_plan__linked_vehicle_entry", *empty_in_pipeline_prefetch())
        )

        reason = request.query_params.get("reason")
        from_date = request.query_params.get("from_date")
        to_date = request.query_params.get("to_date")
        inside_only = request.query_params.get("inside_only")

        if reason:
            qs = qs.filter(reason=reason)
        if from_date:
            qs = qs.filter(gate_in_date__gte=from_date)
        if to_date:
            qs = qs.filter(gate_in_date__lte=to_date)
        if inside_only in ("1", "true", "True", "yes"):
            # "Inside" = the truck is physically in and has not left: a live gate-in
            # (in-progress, or completed and not yet departed). Not retired
            # (dispatched / emptied out) and no completed empty-vehicle or BST
            # gate-out. (A completed gate-in still counts as inside -- the vehicle
            # finished gate-in but is parked in, not gone.)
            qs = (
                qs.filter(retired_at__isnull=True)
                .exclude(vehicle_entry__status="CANCELLED")
                .exclude(vehicle_entry__empty_vehicle_gate_out__status="COMPLETED")
                .exclude(bst_gate_outs__status="COMPLETED")
            )

        serializer = EmptyVehicleGateInSerializer(qs, many=True)
        return Response(serializer.data)

    def _resolve_dispatch_company(self, request, vehicle):
        """Owning company for a DISPATCH empty-in: the one whose booked bills the
        truck carries, not the active Company-Code. Prefers the active company when
        it has bills (no surprise), else the company that actually does; falls back
        to the active company for a manual entry with no bills yet."""
        from company.models import Company

        ids = user_company_ids(request)
        active = request.company.company
        booked = DispatchPlan.objects.filter(
            company_id__in=ids,
            vehicle=vehicle,
            booking_status=DispatchPlanStatus.BOOKED,
            linked_vehicle_entry__isnull=True,
            is_active=True,
        )
        if booked.filter(company_id=active.id).exists():
            return active
        company_id = (
            booked.values_list("company_id", flat=True).order_by("company_id").first()
        )
        if company_id is None:
            return active
        return Company.objects.get(id=company_id)

    def post(self, request):
        serializer = EmptyVehicleGateInCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        vehicle = get_object_or_404(Vehicle, id=data["vehicle_id"])
        driver = get_object_or_404(Driver, id=data["driver_id"])
        sap_transfer = None

        # One physical truck can be inside only once: block a new gate-in while the
        # vehicle still has a live one that has not left the gate. "Inside" covers a
        # gate-in still being processed (IN_PROGRESS) AND a completed one whose truck
        # has not yet departed -- it has not been retired (dispatched / emptied out)
        # and has no completed empty-vehicle or BST gate-out.
        existing_inside = (
            EmptyVehicleGateIn.objects
            .filter(
                company_id__in=user_company_ids(request),
                vehicle=vehicle,
                is_active=True,
                retired_at__isnull=True,
            )
            .exclude(vehicle_entry__status="CANCELLED")
            .exclude(vehicle_entry__empty_vehicle_gate_out__status="COMPLETED")
            .exclude(bst_gate_outs__status="COMPLETED")
            .order_by("-created_at")
            .first()
        )

        if existing_inside:
            return Response(
                {
                    "detail": (
                        f"{vehicle.vehicle_number} is already inside under gate entry "
                        f"{existing_inside.entry_no} and has not left yet. Finish its "
                        f"dispatch, or do an empty-vehicle-out, before starting a new entry."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if data["reason"] == "BST":
            linked_gate_in = find_active_empty_vehicle_bst_link(
                request.company.company,
                data["sap_doc_entry"],
            )
            if linked_gate_in:
                return empty_vehicle_bst_already_linked_response(linked_gate_in)

            linked_bst_out = (
                BSTGateOut.objects
                .filter(
                    company=request.company.company,
                    sap_doc_entry=data["sap_doc_entry"],
                    is_active=True,
                    status__in=["IN_PROGRESS", "COMPLETED"],
                )
                .order_by("-created_at")
                .first()
            )
            if linked_bst_out:
                return sap_bst_already_linked_response(linked_bst_out)

            sap_transfer, error_response = get_sap_stock_transfer_or_error(
                request,
                data["sap_doc_entry"],
            )
            if error_response:
                return error_response
            if not sap_transfer:
                raise NotFound("Selected BST document was not found in SAP")

        actual_quantities = parse_line_quantities(
            data.get("items"),
            "actual_quantity",
            "Actual quantity",
        )

        entry_no = EmptyVehicleGateIn.generate_entry_no()

        # The owning company follows the truck's booked bills, not the active
        # Company-Code (the selector is a decorator): a DISPATCH entry is created
        # under the company whose bills the truck carries, then replicated to the
        # others on completion. BST / other reasons stay on the active company.
        if data["reason"] == "DISPATCH":
            company = self._resolve_dispatch_company(request, vehicle)
        else:
            company = request.company.company

        with transaction.atomic():
            vehicle_entry = VehicleEntry.objects.create(
                company=company,
                entry_no=entry_no,
                vehicle=vehicle,
                driver=driver,
                entry_type="EMPTY_VEHICLE",
                status="IN_PROGRESS",
                remarks=data.get("remarks", ""),
                created_by=request.user,
                updated_by=request.user,
            )

            gate_in = EmptyVehicleGateIn.objects.create(
                company=company,
                entry_no=entry_no,
                vehicle_entry=vehicle_entry,
                vehicle=vehicle,
                driver=driver,
                reason=data["reason"],
                gate_in_date=data["gate_in_date"],
                in_time=data["in_time"],
                # DISPATCH derives reference/notes from its covers on read, so they
                # are not stored (avoids duplicating the bill text). Other reasons
                # keep the provided values.
                document_reference=(
                    "" if data["reason"] == "DISPATCH" else data.get("document_reference", "")
                ),
                document_notes=(
                    "" if data["reason"] == "DISPATCH" else data.get("document_notes", "")
                ),
                security_name=data.get("security_name", ""),
                remarks=data.get("remarks", ""),
                created_by=request.user,
                updated_by=request.user,
            )
            if sap_transfer:
                apply_sap_transfer_to_empty_gate_in(gate_in, sap_transfer)
                sync_empty_gate_in_items(gate_in, sap_transfer, actual_quantities, request.user)
                gate_in.save(update_fields=[
                    "sap_doc_entry",
                    "sap_doc_num",
                    "sap_doc_date",
                    "sap_from_warehouse",
                    "sap_to_warehouse",
                    "sap_reference",
                    "sap_comments",
                    "sap_line_count",
                    "sap_total_quantity",
                    "updated_at",
                ])

        return Response(
            EmptyVehicleGateInSerializer(gate_in).data,
            status=status.HTTP_201_CREATED,
        )


class EmptyVehicleGateInDetailView(APIView):
    """Get or update one empty vehicle gate-in record."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get_object(self, request, entry_id):
        # Resolve across the user's companies (not the active Company-Code) so a
        # cross-company row in the aggregated board opens/edits in place without
        # switching companies. Out-of-scope -> 404.
        return get_object_or_404(
            EmptyVehicleGateIn.objects.select_related(
                "vehicle_entry",
                "vehicle",
                "vehicle__vehicle_type",
                "vehicle__transporter",
                "driver",
                "company",
            ).prefetch_related("bst_gate_outs", "items", "covers", "covers__dispatch_plan", "covers__dispatch_plan__linked_vehicle_entry", *empty_in_pipeline_prefetch()),
            id=entry_id,
            company_id__in=user_company_ids(request),
            is_active=True,
        )

    def get(self, request, entry_id):
        gate_in = self.get_object(request, entry_id)
        return Response(EmptyVehicleGateInSerializer(gate_in).data)

    def patch(self, request, entry_id):
        gate_in = self.get_object(request, entry_id)
        serializer = EmptyVehicleGateInUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        has_active_bst_out = gate_in.bst_gate_outs.filter(
            is_active=True,
            status__in=["IN_PROGRESS", "COMPLETED"],
        ).exists()
        document_fields = {"sap_doc_entry", "document_reference", "document_notes", "items"}

        if has_active_bst_out and document_fields.intersection(data.keys()):
            return Response(
                {"detail": "BST document details cannot be edited after BST out is started"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        sap_transfer = None
        item_payload = data.get("items") if "items" in data else None
        actual_quantities = (
            parse_line_quantities(item_payload, "actual_quantity", "Actual quantity")
            if item_payload
            else None
        )
        if actual_quantities is not None and gate_in.reason != "BST":
            return Response(
                {"detail": "Actual quantity can only be captured for BST empty vehicle entries"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if "sap_doc_entry" in data:
            if gate_in.reason != "BST" and data["sap_doc_entry"]:
                return Response(
                    {"detail": "SAP BST document can only be linked when the reason is BST"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if gate_in.reason == "BST" and not data["sap_doc_entry"]:
                return Response(
                    {"detail": "Select the SAP BST document for this empty vehicle entry"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if data["sap_doc_entry"] and data["sap_doc_entry"] != gate_in.sap_doc_entry:
                linked_gate_in = find_active_empty_vehicle_bst_link(
                    request.company.company,
                    data["sap_doc_entry"],
                    exclude_id=gate_in.id,
                )
                if linked_gate_in:
                    return empty_vehicle_bst_already_linked_response(linked_gate_in)

                linked_bst_out = (
                    BSTGateOut.objects
                    .filter(
                        company=request.company.company,
                        sap_doc_entry=data["sap_doc_entry"],
                        is_active=True,
                        status__in=["IN_PROGRESS", "COMPLETED"],
                    )
                    .order_by("-created_at")
                    .first()
                )
                if linked_bst_out:
                    return sap_bst_already_linked_response(linked_bst_out)

                sap_transfer, error_response = get_sap_stock_transfer_or_error(
                    request,
                    data["sap_doc_entry"],
                )
                if error_response:
                    return error_response
                if not sap_transfer:
                    raise NotFound("Selected BST document was not found in SAP")

        if (
            actual_quantities is not None
            and gate_in.reason == "BST"
            and gate_in.sap_doc_entry
            and not sap_transfer
            and not gate_in.items.exists()
        ):
            sap_transfer, error_response = get_sap_stock_transfer_or_error(
                request,
                gate_in.sap_doc_entry,
            )
            if error_response:
                return error_response
            if not sap_transfer:
                raise NotFound("Selected BST document was not found in SAP")

        with transaction.atomic():
            if "sap_doc_entry" in data:
                if sap_transfer:
                    apply_sap_transfer_to_empty_gate_in(gate_in, sap_transfer)
                    sync_empty_gate_in_items(
                        gate_in,
                        sap_transfer,
                        actual_quantities or {},
                        request.user,
                    )
                elif not data["sap_doc_entry"]:
                    clear_sap_transfer_from_empty_gate_in(gate_in)
                    gate_in.items.all().delete()
            elif sap_transfer:
                apply_sap_transfer_to_empty_gate_in(gate_in, sap_transfer)
                sync_empty_gate_in_items(
                    gate_in,
                    sap_transfer,
                    actual_quantities or {},
                    request.user,
                )
            elif actual_quantities is not None:
                existing_lines = {item.line_num: item for item in gate_in.items.all()}
                unknown_lines = set(actual_quantities) - set(existing_lines)
                if unknown_lines:
                    line_list = ", ".join(str(line) for line in sorted(unknown_lines))
                    raise ValidationError({"items": f"Unknown BST line(s): {line_list}."})

                for line_num, quantity in actual_quantities.items():
                    item = existing_lines[line_num]
                    item.actual_quantity = quantity
                    item.updated_by = request.user
                    item.save(update_fields=["actual_quantity", "updated_by", "updated_at"])

            for field in ["document_reference", "document_notes", "security_name", "remarks"]:
                if field in data:
                    setattr(gate_in, field, data[field])

            gate_in.updated_by = request.user
            gate_in.save()

        if hasattr(gate_in, "_prefetched_objects_cache"):
            gate_in._prefetched_objects_cache.pop("items", None)

        return Response(EmptyVehicleGateInSerializer(gate_in).data)


def has_empty_vehicle_tare_weighment(vehicle_entry):
    if not hasattr(vehicle_entry, "weighment"):
        return False

    tare_weight = vehicle_entry.weighment.tare_weight
    return tare_weight is not None and tare_weight >= 0


class EmptyVehicleGateInCompleteView(APIView):
    """Complete empty vehicle gate-in after the required tare weighment is saved."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, entry_id):
        # Cross-company: complete a gate-in for any of the user's companies, so a
        # truck shown on the aggregated board is finished in place (no company switch).
        gate_in = get_object_or_404(
            EmptyVehicleGateIn.objects.select_related(
                "vehicle_entry",
                "vehicle",
                "vehicle__vehicle_type",
                "vehicle__transporter",
                "driver",
                "company",
            ).prefetch_related("bst_gate_outs", "items", "covers", "covers__dispatch_plan", "covers__dispatch_plan__linked_vehicle_entry", *empty_in_pipeline_prefetch()),
            id=entry_id,
            company_id__in=user_company_ids(request),
            is_active=True,
        )

        vehicle_entry = gate_in.vehicle_entry
        if vehicle_entry.status == "CANCELLED":
            return Response(
                {"detail": "Cancelled empty vehicle entries cannot be completed."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not has_empty_vehicle_tare_weighment(vehicle_entry):
            return Response(
                {"detail": "Tare weighment is required before completing this empty vehicle entry."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            vehicle_entry.status = "COMPLETED"
            vehicle_entry.is_locked = False
            vehicle_entry.updated_by = request.user
            vehicle_entry.save(update_fields=["status", "is_locked", "updated_by", "updated_at"])

            if gate_in.reason == "DISPATCH":
                # Snapshot the bills booked to this vehicle as the gate-in's covers
                # (and link them). Bill-accurate from here on: only these covered
                # bills can dock against this gate-in.
                record_dispatch_covers(gate_in, request.user)
                # One physical truck, many companies: mark it in across every other
                # company that has booked bills for it (one arrival, a gate-in copy
                # per company) so the whole factory flow sees the same vehicle.
                replicate_dispatch_gate_in_across_companies(
                    gate_in, request.user, user_company_ids(request)
                )

        return Response(EmptyVehicleGateInSerializer(gate_in).data)


class EmptyVehicleGateInEligibleView(APIView):
    """List empty vehicles currently inside and available for outbound flows."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        qs = (
            EmptyVehicleGateIn.objects
            .filter(company=request.company.company, is_active=True)
            .exclude(vehicle_entry__status__in=["COMPLETED", "CANCELLED"])
            .exclude(
                bst_gate_outs__is_active=True,
                bst_gate_outs__status__in=["IN_PROGRESS", "COMPLETED"],
            )
            .select_related(
                "vehicle_entry",
                "vehicle",
                "vehicle__vehicle_type",
                "vehicle__transporter",
                "driver",
                "company",
            )
            .prefetch_related("bst_gate_outs", "items", "covers", "covers__dispatch_plan", "covers__dispatch_plan__linked_vehicle_entry", *empty_in_pipeline_prefetch())
            .distinct()
        )

        reason = request.query_params.get("reason")
        if reason:
            qs = qs.filter(reason=reason)

        serializer = EmptyVehicleGateInSerializer(qs, many=True)
        return Response(serializer.data)


class SAPStockTransferListView(APIView):
    """List SAP inventory transfers available for BST gate-out reference."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        try:
            limit = int(request.query_params.get("limit", 50))
        except (TypeError, ValueError):
            limit = 50

        try:
            client = SAPClient(company_code=request.company.company.code)
            transfers = client.list_stock_transfers(
                search=request.query_params.get("search"),
                from_date=parse_date(request.query_params.get("from_date") or ""),
                to_date=parse_date(request.query_params.get("to_date") or ""),
                limit=limit,
            )
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError:
            return Response(
                {"detail": "Failed to retrieve BST documents from SAP."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(SAPStockTransferSerializer(transfers, many=True).data)


class SAPStockTransferDetailView(APIView):
    """Get one SAP inventory transfer with line details."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, doc_entry):
        try:
            client = SAPClient(company_code=request.company.company.code)
            transfer = client.get_stock_transfer(doc_entry)
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError:
            return Response(
                {"detail": "Failed to retrieve BST document from SAP."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if not transfer:
            raise NotFound("BST document not found in SAP")

        return Response(SAPStockTransferSerializer(transfer).data)


class BSTGateOutListCreateView(APIView):
    """List and create BST gate-out records."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        qs = (
            BSTGateOut.objects
            .filter(company=request.company.company, is_active=True)
            .select_related(
                "vehicle_entry",
                "empty_vehicle_gate_in",
                "vehicle",
                "vehicle__vehicle_type",
                "vehicle__transporter",
                "driver",
                "company",
            )
            .prefetch_related("items")
        )

        status_filter = request.query_params.get("status")
        from_date = request.query_params.get("from_date")
        to_date = request.query_params.get("to_date")

        if status_filter:
            qs = qs.filter(status=status_filter)
        if from_date:
            qs = qs.filter(gate_out_date__gte=from_date)
        if to_date:
            qs = qs.filter(gate_out_date__lte=to_date)

        serializer = BSTGateOutSerializer(qs, many=True)
        return Response(serializer.data)

    def post(self, request):
        serializer = BSTGateOutCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        empty_gate_in = get_object_or_404(
            EmptyVehicleGateIn.objects.select_related(
                "vehicle_entry",
                "vehicle",
                "driver",
                "company",
            ).prefetch_related("items"),
            id=data["empty_vehicle_gate_in_id"],
            company=request.company.company,
            reason="BST",
            is_active=True,
        )

        vehicle_entry = empty_gate_in.vehicle_entry
        if vehicle_entry.status in ["COMPLETED", "CANCELLED"]:
            return Response(
                {"detail": "This BST vehicle is no longer available for gate out"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if BSTGateOut.objects.filter(
            empty_vehicle_gate_in=empty_gate_in,
            is_active=True,
            status__in=["IN_PROGRESS", "COMPLETED"],
        ).exists():
            return Response(
                {"detail": "BST out has already been started for this vehicle"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if empty_gate_in.sap_doc_entry and data["sap_doc_entry"] != empty_gate_in.sap_doc_entry:
            return Response(
                {"detail": "BST out must use the SAP BST document linked at empty vehicle gate-in"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        linked_bst_out = BSTGateOut.objects.filter(
            company=request.company.company,
            sap_doc_entry=data["sap_doc_entry"],
            is_active=True,
            status__in=["IN_PROGRESS", "COMPLETED"],
        ).order_by("-created_at").first()
        if linked_bst_out:
            return sap_bst_already_linked_response(linked_bst_out)

        try:
            client = SAPClient(company_code=request.company.company.code)
            sap_transfer = client.get_stock_transfer(data["sap_doc_entry"])
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError:
            return Response(
                {"detail": "Failed to retrieve selected BST document from SAP."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if not sap_transfer:
            raise NotFound("Selected BST document was not found in SAP")

        with transaction.atomic():
            bst_out = BSTGateOut.objects.create(
                company=request.company.company,
                entry_no=BSTGateOut.generate_entry_no(),
                vehicle_entry=vehicle_entry,
                empty_vehicle_gate_in=empty_gate_in,
                vehicle=empty_gate_in.vehicle,
                driver=empty_gate_in.driver,
                sap_doc_entry=sap_transfer["doc_entry"],
                sap_doc_num=sap_transfer["doc_num"],
                sap_doc_date=sap_transfer.get("doc_date"),
                sap_from_warehouse=sap_transfer.get("from_warehouse", ""),
                sap_to_warehouse=sap_transfer.get("to_warehouse", ""),
                sap_reference=sap_transfer.get("reference", ""),
                sap_comments=sap_transfer.get("comments", ""),
                gate_out_date=data["gate_out_date"],
                out_time=data["out_time"],
                security_name=data.get("security_name", ""),
                remarks=data.get("remarks", ""),
                created_by=request.user,
                updated_by=request.user,
            )

            actual_by_line = {
                item.line_num: item.actual_quantity
                for item in empty_gate_in.items.all()
            }
            for line in sap_transfer.get("lines", []):
                line_num = int(line["line_num"])
                sap_quantity = Decimal(str(line.get("quantity", 0) or 0))
                BSTGateOutItem.objects.create(
                    bst_gate_out=bst_out,
                    line_num=line_num,
                    item_code=line.get("item_code", ""),
                    item_name=line.get("item_name", ""),
                    quantity=sap_quantity,
                    actual_quantity=actual_by_line.get(line_num, sap_quantity),
                    uom=line.get("uom", ""),
                    from_warehouse=line.get("from_warehouse", ""),
                    to_warehouse=line.get("to_warehouse", ""),
                    created_by=request.user,
                    updated_by=request.user,
                )

            vehicle_entry.status = "IN_PROGRESS"
            vehicle_entry.updated_by = request.user
            vehicle_entry.save(update_fields=["status", "updated_by", "updated_at"])

        return Response(
            BSTGateOutSerializer(bst_out).data,
            status=status.HTTP_201_CREATED,
        )


def sap_bst_already_linked_response(linked_bst_out):
    """Build a duplicate SAP BST response with the entry id needed to open it."""
    return Response(
        {
            "detail": (
                "This SAP BST document is already linked to "
                f"BST Out entry {linked_bst_out.entry_no} "
                f"(entryId {linked_bst_out.vehicle_entry_id})."
            ),
            "linked_bst_out_id": linked_bst_out.id,
            "linked_entry_no": linked_bst_out.entry_no,
            "linked_entry_id": linked_bst_out.vehicle_entry_id,
        },
        status=status.HTTP_400_BAD_REQUEST,
    )


def update_bst_gate_out(request, bst_out):
    """Update editable BST gate-out step 1 fields while the entry is in progress."""
    serializer = BSTGateOutCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    if bst_out.status != "IN_PROGRESS" or bst_out.vehicle_entry.is_locked:
        return Response(
            {"detail": "Completed BST out entries cannot be edited"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if data["empty_vehicle_gate_in_id"] != bst_out.empty_vehicle_gate_in_id:
        return Response(
            {"detail": "Vehicle cannot be changed after BST out has been started"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if (
        bst_out.empty_vehicle_gate_in.sap_doc_entry
        and data["sap_doc_entry"] != bst_out.empty_vehicle_gate_in.sap_doc_entry
    ):
        return Response(
            {"detail": "BST document cannot be changed after BST out has been started"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    sap_transfer = None
    if data["sap_doc_entry"] != bst_out.sap_doc_entry:
        linked_bst_out = BSTGateOut.objects.filter(
            company=request.company.company,
            sap_doc_entry=data["sap_doc_entry"],
            is_active=True,
            status__in=["IN_PROGRESS", "COMPLETED"],
        ).exclude(id=bst_out.id).order_by("-created_at").first()
        if linked_bst_out:
            return sap_bst_already_linked_response(linked_bst_out)

        try:
            client = SAPClient(company_code=request.company.company.code)
            sap_transfer = client.get_stock_transfer(data["sap_doc_entry"])
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError:
            return Response(
                {"detail": "Failed to retrieve selected BST document from SAP."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if not sap_transfer:
            raise NotFound("Selected BST document was not found in SAP")

    with transaction.atomic():
        if sap_transfer:
            bst_out.sap_doc_entry = sap_transfer["doc_entry"]
            bst_out.sap_doc_num = sap_transfer["doc_num"]
            bst_out.sap_doc_date = sap_transfer.get("doc_date")
            bst_out.sap_from_warehouse = sap_transfer.get("from_warehouse", "")
            bst_out.sap_to_warehouse = sap_transfer.get("to_warehouse", "")
            bst_out.sap_reference = sap_transfer.get("reference", "")
            bst_out.sap_comments = sap_transfer.get("comments", "")

            bst_out.items.all().delete()
            actual_by_line = {
                item.line_num: item.actual_quantity
                for item in bst_out.empty_vehicle_gate_in.items.all()
            }
            for line in sap_transfer.get("lines", []):
                line_num = int(line["line_num"])
                sap_quantity = Decimal(str(line.get("quantity", 0) or 0))
                BSTGateOutItem.objects.create(
                    bst_gate_out=bst_out,
                    line_num=line_num,
                    item_code=line.get("item_code", ""),
                    item_name=line.get("item_name", ""),
                    quantity=sap_quantity,
                    actual_quantity=actual_by_line.get(line_num, sap_quantity),
                    uom=line.get("uom", ""),
                    from_warehouse=line.get("from_warehouse", ""),
                    to_warehouse=line.get("to_warehouse", ""),
                    created_by=request.user,
                    updated_by=request.user,
                )

        bst_out.gate_out_date = data["gate_out_date"]
        bst_out.out_time = data["out_time"]
        bst_out.security_name = data.get("security_name", "")
        bst_out.remarks = data.get("remarks", "")
        bst_out.updated_by = request.user
        bst_out.save()

    if hasattr(bst_out, "_prefetched_objects_cache"):
        bst_out._prefetched_objects_cache.pop("items", None)

    return Response(BSTGateOutSerializer(bst_out).data)


def get_active_bst_gate_out_by_vehicle_entry(request, vehicle_entry_id):
    bst_out = (
        BSTGateOut.objects
        .select_related(
            "vehicle_entry",
            "empty_vehicle_gate_in",
            "vehicle",
            "vehicle__vehicle_type",
            "vehicle__transporter",
            "driver",
            "company",
        )
        .prefetch_related("items")
        .filter(
            vehicle_entry_id=vehicle_entry_id,
            company=request.company.company,
            is_active=True,
            status__in=["IN_PROGRESS", "COMPLETED"],
        )
        .order_by("-created_at")
        .first()
    )

    if not bst_out:
        raise NotFound("Active BST out entry not found")

    return bst_out


class BSTGateOutDetailView(APIView):
    """Get one BST gate-out record by BST record id."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, entry_id):
        bst_out = get_object_or_404(
            BSTGateOut.objects.select_related(
                "vehicle_entry",
                "empty_vehicle_gate_in",
                "vehicle",
                "vehicle__vehicle_type",
                "vehicle__transporter",
                "driver",
                "company",
            ).prefetch_related("items"),
            id=entry_id,
            company=request.company.company,
            is_active=True,
        )
        return Response(BSTGateOutSerializer(bst_out).data)

    def put(self, request, entry_id):
        bst_out = get_object_or_404(
            BSTGateOut.objects.select_related(
                "vehicle_entry",
                "empty_vehicle_gate_in",
                "vehicle",
                "vehicle__vehicle_type",
                "vehicle__transporter",
                "driver",
                "company",
            ).prefetch_related("items"),
            id=entry_id,
            company=request.company.company,
            is_active=True,
        )
        return update_bst_gate_out(request, bst_out)


class BSTGateOutByVehicleEntryView(APIView):
    """Get one BST gate-out record by its underlying vehicle entry id."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, vehicle_entry_id):
        bst_out = get_active_bst_gate_out_by_vehicle_entry(request, vehicle_entry_id)
        return Response(BSTGateOutSerializer(bst_out).data)

    def put(self, request, vehicle_entry_id):
        bst_out = get_active_bst_gate_out_by_vehicle_entry(request, vehicle_entry_id)
        return update_bst_gate_out(request, bst_out)


class BSTGateOutCancelView(APIView):
    """Cancel an in-progress BST gate-out and release its empty vehicle gate-in."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, entry_id):
        serializer = BSTGateOutCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        bst_out = get_object_or_404(
            BSTGateOut.objects.select_related(
                "vehicle_entry",
                "empty_vehicle_gate_in",
                "vehicle",
                "vehicle__vehicle_type",
                "vehicle__transporter",
                "driver",
                "company",
            ).prefetch_related("items"),
            id=entry_id,
            company=request.company.company,
            is_active=True,
        )

        if bst_out.status != "IN_PROGRESS" or bst_out.vehicle_entry.is_locked:
            return Response(
                {"detail": "Only in-progress BST out entries can be cancelled"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if hasattr(bst_out.vehicle_entry, "weighment"):
            return Response(
                {"detail": "BST out cannot be cancelled after weighment has been recorded"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if GateAttachment.objects.filter(gate_entry=bst_out.vehicle_entry).exists():
            return Response(
                {"detail": "BST out cannot be cancelled after attachments have been uploaded"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        bst_out.status = "CANCELLED"
        bst_out.cancel_reason = serializer.validated_data["cancel_reason"]
        bst_out.cancelled_at = timezone.now()
        bst_out.cancelled_by = request.user
        bst_out.updated_by = request.user
        bst_out.save(update_fields=[
            "status",
            "cancel_reason",
            "cancelled_at",
            "cancelled_by",
            "updated_by",
            "updated_at",
        ])

        return Response(BSTGateOutSerializer(bst_out).data)


class BSTGateOutCompleteView(APIView):
    """Complete BST gate-out and close the empty vehicle visit."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, vehicle_entry_id):
        bst_out = get_active_bst_gate_out_by_vehicle_entry(request, vehicle_entry_id)

        if not has_required_weighment(bst_out.vehicle_entry):
            return required_weighment_response()

        if not has_gatepass_attachment(bst_out.vehicle_entry):
            return required_gatepass_response()

        with transaction.atomic():
            bst_out.status = "COMPLETED"
            bst_out.updated_by = request.user
            bst_out.save(update_fields=["status", "updated_by", "updated_at"])

            vehicle_entry = bst_out.vehicle_entry
            vehicle_entry.status = "COMPLETED"
            vehicle_entry.is_locked = True
            vehicle_entry.updated_by = request.user
            vehicle_entry.save(update_fields=["status", "is_locked", "updated_by", "updated_at"])

        return Response(BSTGateOutSerializer(bst_out).data)


class SAPGRPOListView(APIView):
    """List SAP GRPO documents already posted for job-work gate-in reference."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        try:
            limit = int(request.query_params.get("limit", 50))
        except (TypeError, ValueError):
            limit = 50

        try:
            client = SAPClient(company_code=request.company.company.code)
            grpos = client.list_grpos(
                search=request.query_params.get("search"),
                from_date=parse_date(request.query_params.get("from_date") or ""),
                to_date=parse_date(request.query_params.get("to_date") or ""),
                limit=limit,
                crude_oil_only=True,
            )
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError:
            return Response(
                {"detail": "Failed to retrieve GRPO documents from SAP."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(SAPGRPOSerializer(grpos, many=True).data)


class SAPGRPODetailView(APIView):
    """Get one SAP GRPO document with line details."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, doc_entry):
        try:
            client = SAPClient(company_code=request.company.company.code)
            grpo = client.get_grpo(doc_entry, crude_oil_only=True)
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError:
            return Response(
                {"detail": "Failed to retrieve GRPO document from SAP."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if not grpo:
            raise NotFound("Crude oil GRPO document not found in SAP")

        return Response(SAPGRPOSerializer(grpo).data)


def sap_calendar_date(value):
    """Convert SAP date columns returned as datetimes into plain dates."""
    if isinstance(value, dt.datetime):
        return value.date()
    return value


def normalize_sap_production_order(row, components=None):
    """Return production order data using gate-friendly snake_case keys."""
    return {
        "doc_entry": row.get("DocEntry"),
        "doc_num": str(row.get("DocNum") or ""),
        "item_code": row.get("ItemCode") or "",
        "item_name": row.get("ProdName") or "",
        "planned_qty": row.get("PlannedQty") or 0,
        "completed_qty": row.get("CmpltQty") or 0,
        "rejected_qty": row.get("RjctQty") or 0,
        "remaining_qty": row.get("RemainingQty") or 0,
        "start_date": sap_calendar_date(row.get("StartDate")),
        "due_date": sap_calendar_date(row.get("DueDate")),
        "warehouse": row.get("Warehouse") or "",
        "status": row.get("Status") or "",
        "components": [
            {
                "line_num": component.get("LineNum") or 0,
                "item_code": component.get("ItemCode") or "",
                "item_name": component.get("ItemName") or "",
                "planned_qty": component.get("PlannedQty") or 0,
                "issued_qty": component.get("IssuedQty") or 0,
                "warehouse": component.get("Warehouse") or "",
                "uom": component.get("UomCode") or "",
            }
            for component in (components or [])
        ],
    }


def filter_sap_production_orders(rows, search):
    query = (search or "").strip().lower()
    if not query:
        return rows

    def matches(row):
        values = [
            row.get("DocEntry"),
            row.get("DocNum"),
            row.get("ItemCode"),
            row.get("ProdName"),
            row.get("Warehouse"),
            row.get("Status"),
        ]
        return any(query in str(value or "").lower() for value in values)

    return [row for row in rows if matches(row)]


class SAPProductionOrderListView(APIView):
    """List open SAP production orders for later oil-refining entry linking."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        try:
            limit = int(request.query_params.get("limit", 50))
        except (TypeError, ValueError):
            limit = 50

        try:
            from production_execution.services.sap_reader import (
                ProductionOrderReader,
                SAPReadError,
            )

            reader = ProductionOrderReader(request.company.company.code)
            rows = reader.get_open_production_orders()
        except SAPReadError as exc:
            return Response(
                {"detail": str(exc) or "Failed to retrieve production orders from SAP."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        rows = filter_sap_production_orders(rows, request.query_params.get("search"))
        orders = [normalize_sap_production_order(row) for row in rows[:limit]]
        return Response(SAPProductionOrderSerializer(orders, many=True).data)


class SAPProductionOrderDetailView(APIView):
    """Get one SAP production order with component details."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, doc_entry):
        try:
            from production_execution.services.sap_reader import (
                ProductionOrderReader,
                SAPReadError,
            )

            reader = ProductionOrderReader(request.company.company.code)
            detail = reader.get_production_order_detail(doc_entry)
        except SAPReadError as exc:
            return Response(
                {"detail": str(exc) or "Failed to retrieve production order from SAP."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        order = normalize_sap_production_order(
            detail["header"],
            detail.get("components", []),
        )
        return Response(SAPProductionOrderSerializer(order).data)


def sap_grpo_already_linked_response(linked_job_work):
    """Build a duplicate SAP GRPO response with the entry id needed to open it."""
    return Response(
        {
            "detail": (
                "This SAP GRPO document is already linked to "
                f"Job Work entry {linked_job_work.entry_no} "
                f"(entryId {linked_job_work.vehicle_entry_id})."
            ),
            "linked_job_work_id": linked_job_work.id,
            "linked_entry_no": linked_job_work.entry_no,
            "linked_entry_id": linked_job_work.vehicle_entry_id,
        },
        status=status.HTTP_400_BAD_REQUEST,
    )


def create_job_work_items(job_work, sap_grpo, user):
    for line in sap_grpo.get("lines", []):
        JobWorkGateInItem.objects.create(
            job_work_gate_in=job_work,
            line_num=line["line_num"],
            item_code=line.get("item_code", ""),
            item_name=line.get("item_name", ""),
            quantity=line.get("quantity", 0),
            uom=line.get("uom", ""),
            warehouse_code=line.get("warehouse_code", ""),
            base_type=line.get("base_type"),
            base_entry=line.get("base_entry"),
            base_line=line.get("base_line"),
            created_by=user,
            updated_by=user,
        )


def sap_production_order_already_linked_response(linked_job_work):
    """Build a duplicate SAP production order response with the entry id needed to open it."""
    return Response(
        {
            "detail": (
                "This SAP production order is already linked to "
                f"Job Work entry {linked_job_work.entry_no} "
                f"(entryId {linked_job_work.vehicle_entry_id})."
            ),
            "linked_job_work_id": linked_job_work.id,
            "linked_entry_no": linked_job_work.entry_no,
            "linked_entry_id": linked_job_work.vehicle_entry_id,
        },
        status=status.HTTP_400_BAD_REQUEST,
    )


def decimal_from_sap_value(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def get_sap_production_order_for_job_work(request, doc_entry):
    try:
        from production_execution.services.sap_reader import (
            ProductionOrderReader,
            SAPReadError,
        )

        reader = ProductionOrderReader(request.company.company.code)
        detail = reader.get_production_order_detail(doc_entry)
    except SAPReadError as exc:
        return None, Response(
            {"detail": str(exc) or "Failed to retrieve selected production order from SAP."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    return normalize_sap_production_order(
        detail["header"],
        detail.get("components", []),
    ), None


def clear_job_work_production_order(job_work):
    job_work.production_order_doc_entry = None
    job_work.production_order_doc_num = ""
    job_work.production_item_code = ""
    job_work.production_item_name = ""
    job_work.production_planned_qty = None
    job_work.production_completed_qty = None
    job_work.production_rejected_qty = None
    job_work.production_remaining_qty = None
    job_work.production_start_date = None
    job_work.production_due_date = None
    job_work.production_warehouse = ""
    job_work.production_status = ""
    job_work.items.all().delete()


def apply_job_work_production_order(job_work, production_order, user):
    job_work.production_order_doc_entry = production_order["doc_entry"]
    job_work.production_order_doc_num = production_order["doc_num"]
    job_work.production_item_code = production_order["item_code"]
    job_work.production_item_name = production_order["item_name"]
    job_work.production_planned_qty = decimal_from_sap_value(production_order["planned_qty"])
    job_work.production_completed_qty = decimal_from_sap_value(production_order["completed_qty"])
    job_work.production_rejected_qty = decimal_from_sap_value(production_order["rejected_qty"])
    job_work.production_remaining_qty = decimal_from_sap_value(production_order["remaining_qty"])
    job_work.production_start_date = production_order["start_date"]
    job_work.production_due_date = production_order["due_date"]
    job_work.production_warehouse = production_order["warehouse"] or ""
    job_work.production_status = production_order["status"] or ""

    job_work.items.all().delete()
    for component in production_order.get("components", []):
        JobWorkGateInItem.objects.create(
            job_work_gate_in=job_work,
            line_num=component["line_num"],
            item_code=component.get("item_code", ""),
            item_name=component.get("item_name", ""),
            quantity=component.get("planned_qty", 0),
            uom=component.get("uom", ""),
            warehouse_code=component.get("warehouse", ""),
            base_type=202,
            base_entry=production_order["doc_entry"],
            base_line=component["line_num"],
            created_by=user,
            updated_by=user,
        )


def select_job_work_gate_in_queryset():
    return (
        JobWorkGateIn.objects
        .select_related(
            "vehicle_entry",
            "vehicle",
            "vehicle__vehicle_type",
            "vehicle__transporter",
            "driver",
            "company",
        )
        .prefetch_related("items")
    )


def get_active_job_work_by_vehicle_entry(request, vehicle_entry_id):
    job_work = (
        select_job_work_gate_in_queryset()
        .filter(
            vehicle_entry_id=vehicle_entry_id,
            company=request.company.company,
            is_active=True,
            status__in=["IN_PROGRESS", "COMPLETED"],
        )
        .order_by("-created_at")
        .first()
    )

    if not job_work:
        raise NotFound("Active job work entry not found")

    return job_work


def update_job_work_gate_in(request, job_work):
    """Update job-work gate fields and optional SAP production-order link."""
    serializer = JobWorkGateInCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    if job_work.status == "CANCELLED":
        return Response(
            {"detail": "Cancelled job work entries cannot be edited"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    vehicle = get_object_or_404(Vehicle, id=data["vehicle_id"])
    driver = get_object_or_404(Driver, id=data["driver_id"])

    production_order = None
    production_order_was_provided = "production_order_doc_entry" in data
    requested_production_order = data.get("production_order_doc_entry")

    if (
        production_order_was_provided
        and requested_production_order
        and requested_production_order != job_work.production_order_doc_entry
    ):
        linked_job_work = (
            JobWorkGateIn.objects
            .filter(
                company=request.company.company,
                production_order_doc_entry=requested_production_order,
                is_active=True,
                status__in=["IN_PROGRESS", "COMPLETED"],
            )
            .exclude(id=job_work.id)
            .order_by("-created_at")
            .first()
        )
        if linked_job_work:
            return sap_production_order_already_linked_response(linked_job_work)

        production_order, error_response = get_sap_production_order_for_job_work(
            request,
            requested_production_order,
        )
        if error_response:
            return error_response

    with transaction.atomic():
        job_work.vehicle = vehicle
        job_work.driver = driver
        job_work.gate_in_date = data["gate_in_date"]
        job_work.in_time = data["in_time"]
        job_work.security_name = data.get("security_name", "")
        job_work.remarks = data.get("remarks", "")

        if production_order_was_provided:
            if requested_production_order:
                if production_order:
                    apply_job_work_production_order(job_work, production_order, request.user)
            else:
                clear_job_work_production_order(job_work)

        job_work.updated_by = request.user
        job_work.save()

        vehicle_entry = job_work.vehicle_entry
        vehicle_entry.vehicle = vehicle
        vehicle_entry.driver = driver
        vehicle_entry.remarks = data.get("remarks", "")
        vehicle_entry.updated_by = request.user
        vehicle_entry.save(update_fields=[
            "vehicle", "driver", "remarks", "updated_by", "updated_at",
        ])

    if hasattr(job_work, "_prefetched_objects_cache"):
        job_work._prefetched_objects_cache.pop("items", None)

    return Response(JobWorkGateInSerializer(job_work).data)


class JobWorkGateInListCreateView(APIView):
    """List and create job-work gate-in records."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        qs = select_job_work_gate_in_queryset().filter(
            company=request.company.company,
            is_active=True,
        )

        status_filter = request.query_params.get("status")
        from_date = request.query_params.get("from_date")
        to_date = request.query_params.get("to_date")

        if status_filter:
            qs = qs.filter(status=status_filter)
        if from_date:
            qs = qs.filter(gate_in_date__gte=from_date)
        if to_date:
            qs = qs.filter(gate_in_date__lte=to_date)

        serializer = JobWorkGateInSerializer(qs, many=True)
        return Response(serializer.data)

    def post(self, request):
        serializer = JobWorkGateInCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        vehicle = get_object_or_404(Vehicle, id=data["vehicle_id"])
        driver = get_object_or_404(Driver, id=data["driver_id"])

        requested_production_order = data.get("production_order_doc_entry")
        production_order = None

        if requested_production_order:
            linked_job_work = (
                JobWorkGateIn.objects
                .filter(
                    company=request.company.company,
                    production_order_doc_entry=requested_production_order,
                    is_active=True,
                    status__in=["IN_PROGRESS", "COMPLETED"],
                )
                .order_by("-created_at")
                .first()
            )
            if linked_job_work:
                return sap_production_order_already_linked_response(linked_job_work)

            production_order, error_response = get_sap_production_order_for_job_work(
                request,
                requested_production_order,
            )
            if error_response:
                return error_response

        with transaction.atomic():
            entry_no = JobWorkGateIn.generate_entry_no()
            vehicle_entry = VehicleEntry.objects.create(
                company=request.company.company,
                entry_no=entry_no,
                vehicle=vehicle,
                driver=driver,
                entry_type="JOB_WORK",
                status="IN_PROGRESS",
                remarks=data.get("remarks", ""),
                created_by=request.user,
                updated_by=request.user,
            )

            job_work = JobWorkGateIn.objects.create(
                company=request.company.company,
                entry_no=entry_no,
                vehicle_entry=vehicle_entry,
                vehicle=vehicle,
                driver=driver,
                gate_in_date=data["gate_in_date"],
                in_time=data["in_time"],
                security_name=data.get("security_name", ""),
                remarks=data.get("remarks", ""),
                created_by=request.user,
                updated_by=request.user,
            )

            if production_order:
                apply_job_work_production_order(job_work, production_order, request.user)
                job_work.save()

        return Response(
            JobWorkGateInSerializer(job_work).data,
            status=status.HTTP_201_CREATED,
        )


class JobWorkGateInDetailView(APIView):
    """Get or update one job-work gate-in record by job-work id."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, entry_id):
        job_work = get_object_or_404(
            select_job_work_gate_in_queryset(),
            id=entry_id,
            company=request.company.company,
            is_active=True,
        )
        return Response(JobWorkGateInSerializer(job_work).data)

    def put(self, request, entry_id):
        job_work = get_object_or_404(
            select_job_work_gate_in_queryset(),
            id=entry_id,
            company=request.company.company,
            is_active=True,
        )
        return update_job_work_gate_in(request, job_work)


class JobWorkGateInByVehicleEntryView(APIView):
    """Get or update one job-work gate-in record by vehicle entry id."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, vehicle_entry_id):
        job_work = get_active_job_work_by_vehicle_entry(request, vehicle_entry_id)
        return Response(JobWorkGateInSerializer(job_work).data)

    def put(self, request, vehicle_entry_id):
        job_work = get_active_job_work_by_vehicle_entry(request, vehicle_entry_id)
        return update_job_work_gate_in(request, job_work)


class JobWorkGateInCompleteView(APIView):
    """Complete job-work gate movement while keeping the entry editable."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, vehicle_entry_id):
        job_work = get_active_job_work_by_vehicle_entry(request, vehicle_entry_id)

        if not has_required_weighment(job_work.vehicle_entry):
            return required_weighment_response()

        with transaction.atomic():
            job_work.status = "COMPLETED"
            job_work.updated_by = request.user
            job_work.save(update_fields=["status", "updated_by", "updated_at"])

            vehicle_entry = job_work.vehicle_entry
            vehicle_entry.status = "COMPLETED"
            vehicle_entry.is_locked = False
            vehicle_entry.updated_by = request.user
            vehicle_entry.save(update_fields=["status", "is_locked", "updated_by", "updated_at"])

        return Response(JobWorkGateInSerializer(job_work).data)


def select_bst_gate_out_queryset():
    return (
        BSTGateOut.objects
        .select_related(
            "vehicle_entry",
            "empty_vehicle_gate_in",
            "vehicle",
            "vehicle__vehicle_type",
            "vehicle__transporter",
            "driver",
            "company",
        )
        .prefetch_related("items")
    )


def select_bst_gate_in_queryset():
    return (
        BSTGateIn.objects
        .select_related(
            "vehicle_entry",
            "bst_gate_out",
            "bst_gate_out__vehicle_entry",
            "bst_gate_out__empty_vehicle_gate_in",
            "sales_dispatch_gate_out",
            "sales_dispatch_gate_out__vehicle_entry",
            "sales_dispatch_gate_out__vehicle",
            "sales_dispatch_gate_out__vehicle__vehicle_type",
            "sales_dispatch_gate_out__vehicle__transporter",
            "sales_dispatch_gate_out__driver",
            "vehicle",
            "vehicle__vehicle_type",
            "vehicle__transporter",
            "driver",
            "company",
        )
        .prefetch_related("items", "bst_gate_out__items", "sales_dispatch_gate_out__items")
    )


def select_bst_gate_return_queryset():
    return (
        BSTGateReturn.objects
        .select_related(
            "vehicle_entry",
            "bst_gate_out",
            "bst_gate_out__vehicle_entry",
            "bst_gate_out__empty_vehicle_gate_in",
            "vehicle",
            "vehicle__vehicle_type",
            "vehicle__transporter",
            "driver",
            "company",
        )
        .prefetch_related("bst_gate_out__items")
    )


def get_receivable_bst_gate_out(request, bst_gate_out_id):
    return get_object_or_404(
        select_bst_gate_out_queryset(),
        id=bst_gate_out_id,
        company=request.company.company,
        is_active=True,
        status="COMPLETED",
    )


def select_sales_dispatch_bst_source_queryset():
    return (
        SalesDispatchGateOut.objects
        .select_related(
            "vehicle_entry",
            "vehicle",
            "vehicle__vehicle_type",
            "vehicle__transporter",
            "transporter",
            "driver",
            "company",
        )
        .prefetch_related("items")
    )


def get_receivable_sales_dispatch_gate_out(request, sales_dispatch_gate_out_id):
    return get_object_or_404(
        select_sales_dispatch_bst_source_queryset(),
        id=sales_dispatch_gate_out_id,
        company=request.company.company,
        is_active=True,
        document_type=SalesDispatchDocumentType.STOCK_TRANSFER,
        status=SalesDispatchGateOutStatus.DISPATCHED,
    )


def get_receivable_bst_in_source(request, data):
    sales_dispatch_gate_out_id = data.get("sales_dispatch_gate_out_id")
    if sales_dispatch_gate_out_id:
        return (
            "DOCKING_STOCK_TRANSFER",
            get_receivable_sales_dispatch_gate_out(request, sales_dispatch_gate_out_id),
        )

    return (
        "LEGACY_BST_OUT",
        get_receivable_bst_gate_out(request, data["bst_gate_out_id"]),
    )


def ensure_bst_gate_out_not_received(bst_gate_out, exclude_gate_in_id=None):
    qs = BSTGateIn.objects.filter(
        bst_gate_out=bst_gate_out,
        is_active=True,
        status__in=["IN_PROGRESS", "COMPLETED"],
    )
    if exclude_gate_in_id:
        qs = qs.exclude(id=exclude_gate_in_id)

    linked_gate_in = qs.order_by("-created_at").first()
    if linked_gate_in:
        return Response(
            {
                "detail": (
                    "This BST out is already linked to "
                    f"BST In entry {linked_gate_in.entry_no} "
                    f"(entryId {linked_gate_in.vehicle_entry_id})."
                ),
                "linked_bst_in_id": linked_gate_in.id,
                "linked_entry_no": linked_gate_in.entry_no,
                "linked_entry_id": linked_gate_in.vehicle_entry_id,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    return None


def ensure_sales_dispatch_gate_out_not_received(sales_dispatch_gate_out, exclude_gate_in_id=None):
    qs = BSTGateIn.objects.filter(
        sales_dispatch_gate_out=sales_dispatch_gate_out,
        is_active=True,
        status__in=["IN_PROGRESS", "COMPLETED"],
    )
    if exclude_gate_in_id:
        qs = qs.exclude(id=exclude_gate_in_id)

    linked_gate_in = qs.order_by("-created_at").first()
    if linked_gate_in:
        return Response(
            {
                "detail": (
                    "This Docking stock-transfer gate-out is already linked to "
                    f"BST In entry {linked_gate_in.entry_no} "
                    f"(entryId {linked_gate_in.vehicle_entry_id})."
                ),
                "linked_bst_in_id": linked_gate_in.id,
                "linked_entry_no": linked_gate_in.entry_no,
                "linked_entry_id": linked_gate_in.vehicle_entry_id,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    return None


def ensure_bst_in_source_not_received(source_type, source, exclude_gate_in_id=None):
    if source_type == "DOCKING_STOCK_TRANSFER":
        return ensure_sales_dispatch_gate_out_not_received(source, exclude_gate_in_id)
    return ensure_bst_gate_out_not_received(source, exclude_gate_in_id)


def ensure_bst_gate_out_not_returned(bst_gate_out, exclude_return_id=None):
    qs = BSTGateReturn.objects.filter(
        bst_gate_out=bst_gate_out,
        is_active=True,
        status__in=["IN_PROGRESS", "COMPLETED"],
    )
    if exclude_return_id:
        qs = qs.exclude(id=exclude_return_id)

    linked_return = qs.order_by("-created_at").first()
    if linked_return:
        return Response(
            {
                "detail": (
                    "This BST out is already linked to "
                    f"BST Return entry {linked_return.entry_no} "
                    f"(entryId {linked_return.vehicle_entry_id})."
                ),
                "linked_bst_return_id": linked_return.id,
                "linked_entry_no": linked_return.entry_no,
                "linked_entry_id": linked_return.vehicle_entry_id,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    return None


class BSTGateInEligibleOutsView(APIView):
    """List dispatched Docking stock-transfer gate-outs pending destination BST In."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        qs = (
            select_sales_dispatch_bst_source_queryset()
            .filter(
                company=request.company.company,
                is_active=True,
                document_type=SalesDispatchDocumentType.STOCK_TRANSFER,
                status=SalesDispatchGateOutStatus.DISPATCHED,
            )
            .exclude(
                bst_gate_ins__is_active=True,
                bst_gate_ins__status__in=["IN_PROGRESS", "COMPLETED"],
            )
            .distinct()
        )

        search = (request.query_params.get("search") or "").strip().lower()
        if search:
            qs = [
                entry for entry in qs
                if any(
                    search in str(value or "").lower()
                    for value in [
                        entry.entry_no,
                        entry.vehicle_no,
                        entry.vehicle.vehicle_number,
                        entry.driver_name,
                        entry.driver.name,
                        entry.sap_doc_num,
                        entry.from_warehouse,
                        entry.to_warehouse,
                        entry.gatepass_no,
                        entry.gate_out_date,
                        entry.out_time,
                    ]
                )
            ]

        return Response(SalesDispatchBSTEligibleOutSerializer(qs, many=True).data)


class BSTGateInListCreateView(APIView):
    """List and create BST gate-in receiving records."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        qs = (
            select_bst_gate_in_queryset()
            .filter(company=request.company.company, is_active=True)
        )

        status_filter = request.query_params.get("status")
        from_date = request.query_params.get("from_date")
        to_date = request.query_params.get("to_date")

        if status_filter:
            qs = qs.filter(status=status_filter)
        if from_date:
            qs = qs.filter(gate_in_date__gte=from_date)
        if to_date:
            qs = qs.filter(gate_in_date__lte=to_date)

        serializer = BSTGateInSerializer(qs, many=True)
        return Response(serializer.data)

    def post(self, request):
        serializer = BSTGateInCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        source_type, bst_source = get_receivable_bst_in_source(request, data)
        duplicate_response = ensure_bst_in_source_not_received(source_type, bst_source)
        if duplicate_response is not None:
            return duplicate_response
        receiving_quantities = parse_line_quantities(
            data.get("items"),
            "receiving_quantity",
            "Receiving quantity",
        )

        with transaction.atomic():
            entry_no = BSTGateIn.generate_entry_no()
            vehicle_entry = VehicleEntry.objects.create(
                company=request.company.company,
                entry_no=entry_no,
                vehicle=bst_source.vehicle,
                driver=bst_source.driver,
                entry_type="BST_IN",
                status="IN_PROGRESS",
                remarks=data.get("remarks", ""),
                created_by=request.user,
                updated_by=request.user,
            )

            bst_in = BSTGateIn.objects.create(
                company=request.company.company,
                entry_no=entry_no,
                vehicle_entry=vehicle_entry,
                bst_gate_out=bst_source if source_type == "LEGACY_BST_OUT" else None,
                sales_dispatch_gate_out=(
                    bst_source if source_type == "DOCKING_STOCK_TRANSFER" else None
                ),
                vehicle=bst_source.vehicle,
                driver=bst_source.driver,
                gate_in_date=data["gate_in_date"],
                in_time=data["in_time"],
                sap_receipt_doc_num=data.get("sap_receipt_doc_num", ""),
                sap_receipt_doc_date=data.get("sap_receipt_doc_date"),
                sap_receipt_reference=data.get("sap_receipt_reference", ""),
                security_name=data.get("security_name", ""),
                remarks=data.get("remarks", ""),
                created_by=request.user,
                updated_by=request.user,
            )
            sync_bst_gate_in_items(bst_in, bst_source, receiving_quantities, request.user)

        return Response(
            BSTGateInSerializer(bst_in).data,
            status=status.HTTP_201_CREATED,
        )


def update_bst_gate_in(request, bst_in):
    """Update editable BST gate-in step 1 fields while the entry is in progress."""
    serializer = BSTGateInCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    if bst_in.status != "IN_PROGRESS" or bst_in.vehicle_entry.is_locked:
        return Response(
            {"detail": "Completed BST in entries cannot be edited"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    current_source_type = (
        "DOCKING_STOCK_TRANSFER" if bst_in.sales_dispatch_gate_out_id else "LEGACY_BST_OUT"
    )
    current_source_id = bst_in.sales_dispatch_gate_out_id or bst_in.bst_gate_out_id
    source_type, bst_source = get_receivable_bst_in_source(request, data)
    receiving_quantities = parse_line_quantities(
        data.get("items"),
        "receiving_quantity",
        "Receiving quantity",
    )
    if source_type != current_source_type or bst_source.id != current_source_id:
        duplicate_response = ensure_bst_in_source_not_received(
            source_type,
            bst_source,
            exclude_gate_in_id=bst_in.id,
        )
        if duplicate_response is not None:
            return duplicate_response

    with transaction.atomic():
        if source_type != current_source_type or bst_source.id != current_source_id:
            bst_in.bst_gate_out = bst_source if source_type == "LEGACY_BST_OUT" else None
            bst_in.sales_dispatch_gate_out = (
                bst_source if source_type == "DOCKING_STOCK_TRANSFER" else None
            )
            bst_in.vehicle = bst_source.vehicle
            bst_in.driver = bst_source.driver

            vehicle_entry = bst_in.vehicle_entry
            vehicle_entry.vehicle = bst_source.vehicle
            vehicle_entry.driver = bst_source.driver
            vehicle_entry.updated_by = request.user
            vehicle_entry.save(update_fields=["vehicle", "driver", "updated_by", "updated_at"])

        bst_in.gate_in_date = data["gate_in_date"]
        bst_in.in_time = data["in_time"]
        bst_in.sap_receipt_doc_num = data.get("sap_receipt_doc_num", "")
        bst_in.sap_receipt_doc_date = data.get("sap_receipt_doc_date")
        bst_in.sap_receipt_reference = data.get("sap_receipt_reference", "")
        bst_in.security_name = data.get("security_name", "")
        bst_in.remarks = data.get("remarks", "")
        bst_in.updated_by = request.user
        bst_in.save()
        sync_bst_gate_in_items(bst_in, bst_source, receiving_quantities, request.user)

    if hasattr(bst_in, "_prefetched_objects_cache"):
        bst_in._prefetched_objects_cache.pop("items", None)

    return Response(BSTGateInSerializer(bst_in).data)


def get_active_bst_gate_in_by_vehicle_entry(request, vehicle_entry_id):
    bst_in = (
        select_bst_gate_in_queryset()
        .filter(
            vehicle_entry_id=vehicle_entry_id,
            company=request.company.company,
            is_active=True,
            status__in=["IN_PROGRESS", "COMPLETED"],
        )
        .order_by("-created_at")
        .first()
    )

    if not bst_in:
        raise NotFound("Active BST in entry not found")

    return bst_in


class BSTGateInDetailView(APIView):
    """Get one BST gate-in record by BST record id."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, entry_id):
        bst_in = get_object_or_404(
            select_bst_gate_in_queryset(),
            id=entry_id,
            company=request.company.company,
            is_active=True,
        )
        return Response(BSTGateInSerializer(bst_in).data)

    def put(self, request, entry_id):
        bst_in = get_object_or_404(
            select_bst_gate_in_queryset(),
            id=entry_id,
            company=request.company.company,
            is_active=True,
        )
        return update_bst_gate_in(request, bst_in)


class BSTGateInByVehicleEntryView(APIView):
    """Get or update one BST gate-in record by its underlying vehicle entry id."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, vehicle_entry_id):
        bst_in = get_active_bst_gate_in_by_vehicle_entry(request, vehicle_entry_id)
        return Response(BSTGateInSerializer(bst_in).data)

    def put(self, request, vehicle_entry_id):
        bst_in = get_active_bst_gate_in_by_vehicle_entry(request, vehicle_entry_id)
        return update_bst_gate_in(request, bst_in)


class BSTGateInCompleteView(APIView):
    """Complete BST gate-in after the receiving branch confirms vehicle arrival."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, vehicle_entry_id):
        bst_in = get_active_bst_gate_in_by_vehicle_entry(request, vehicle_entry_id)

        if not (bst_in.sap_receipt_doc_num or "").strip():
            return Response(
                {"detail": "SAP receiving document is required before completing this BST in entry"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            bst_in.status = "COMPLETED"
            bst_in.updated_by = request.user
            bst_in.save(update_fields=["status", "updated_by", "updated_at"])

            vehicle_entry = bst_in.vehicle_entry
            vehicle_entry.status = "COMPLETED"
            vehicle_entry.is_locked = True
            vehicle_entry.updated_by = request.user
            vehicle_entry.save(update_fields=["status", "is_locked", "updated_by", "updated_at"])

        return Response(BSTGateInSerializer(bst_in).data)


class BSTGateReturnEligibleOutsView(APIView):
    """List completed BST gate-outs that returned before destination BST In."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        qs = (
            select_bst_gate_out_queryset()
            .filter(
                company=request.company.company,
                is_active=True,
                status="COMPLETED",
            )
            .exclude(
                bst_gate_ins__is_active=True,
                bst_gate_ins__status__in=["IN_PROGRESS", "COMPLETED"],
            )
            .exclude(
                bst_gate_returns__is_active=True,
                bst_gate_returns__status__in=["IN_PROGRESS", "COMPLETED"],
            )
            .distinct()
        )

        search = (request.query_params.get("search") or "").strip().lower()
        if search:
            qs = [
                entry for entry in qs
                if any(
                    search in str(value or "").lower()
                    for value in [
                        entry.entry_no,
                        entry.vehicle.vehicle_number,
                        entry.driver.name,
                        entry.sap_doc_num,
                        entry.sap_from_warehouse,
                        entry.sap_to_warehouse,
                        entry.gate_out_date,
                        entry.out_time,
                    ]
                )
            ]

        return Response(BSTGateOutSerializer(qs, many=True).data)


class BSTGateReturnListCreateView(APIView):
    """List and create source-side BST return records."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        qs = (
            select_bst_gate_return_queryset()
            .filter(company=request.company.company, is_active=True)
        )

        status_filter = request.query_params.get("status")
        from_date = request.query_params.get("from_date")
        to_date = request.query_params.get("to_date")

        if status_filter:
            qs = qs.filter(status=status_filter)
        if from_date:
            qs = qs.filter(gate_in_date__gte=from_date)
        if to_date:
            qs = qs.filter(gate_in_date__lte=to_date)

        serializer = BSTGateReturnSerializer(qs, many=True)
        return Response(serializer.data)

    def post(self, request):
        serializer = BSTGateReturnCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        bst_out = get_receivable_bst_gate_out(request, data["bst_gate_out_id"])
        received_response = ensure_bst_gate_out_not_received(bst_out)
        if received_response is not None:
            return received_response

        duplicate_response = ensure_bst_gate_out_not_returned(bst_out)
        if duplicate_response is not None:
            return duplicate_response

        with transaction.atomic():
            entry_no = BSTGateReturn.generate_entry_no()
            vehicle_entry = VehicleEntry.objects.create(
                company=request.company.company,
                entry_no=entry_no,
                vehicle=bst_out.vehicle,
                driver=bst_out.driver,
                entry_type="BST_RETURN",
                status="IN_PROGRESS",
                remarks=data.get("remarks", ""),
                created_by=request.user,
                updated_by=request.user,
            )

            bst_return = BSTGateReturn.objects.create(
                company=request.company.company,
                entry_no=entry_no,
                vehicle_entry=vehicle_entry,
                bst_gate_out=bst_out,
                vehicle=bst_out.vehicle,
                driver=bst_out.driver,
                gate_in_date=data["gate_in_date"],
                in_time=data["in_time"],
                sap_return_doc_num=data.get("sap_return_doc_num", ""),
                sap_return_doc_date=data.get("sap_return_doc_date"),
                sap_return_reference=data.get("sap_return_reference", ""),
                security_name=data.get("security_name", ""),
                remarks=data.get("remarks", ""),
                created_by=request.user,
                updated_by=request.user,
            )

        return Response(
            BSTGateReturnSerializer(bst_return).data,
            status=status.HTTP_201_CREATED,
        )


def update_bst_gate_return(request, bst_return):
    """Update editable BST return step 1 fields while the entry is in progress."""
    serializer = BSTGateReturnCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    if bst_return.status != "IN_PROGRESS" or bst_return.vehicle_entry.is_locked:
        return Response(
            {"detail": "Completed BST return entries cannot be edited"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    bst_out = bst_return.bst_gate_out
    if data["bst_gate_out_id"] != bst_return.bst_gate_out_id:
        bst_out = get_receivable_bst_gate_out(request, data["bst_gate_out_id"])
        received_response = ensure_bst_gate_out_not_received(bst_out)
        if received_response is not None:
            return received_response

        duplicate_response = ensure_bst_gate_out_not_returned(
            bst_out,
            exclude_return_id=bst_return.id,
        )
        if duplicate_response is not None:
            return duplicate_response

    with transaction.atomic():
        if bst_out.id != bst_return.bst_gate_out_id:
            bst_return.bst_gate_out = bst_out
            bst_return.vehicle = bst_out.vehicle
            bst_return.driver = bst_out.driver

            vehicle_entry = bst_return.vehicle_entry
            vehicle_entry.vehicle = bst_out.vehicle
            vehicle_entry.driver = bst_out.driver
            vehicle_entry.updated_by = request.user
            vehicle_entry.save(update_fields=["vehicle", "driver", "updated_by", "updated_at"])

        bst_return.gate_in_date = data["gate_in_date"]
        bst_return.in_time = data["in_time"]
        bst_return.sap_return_doc_num = data.get("sap_return_doc_num", "")
        bst_return.sap_return_doc_date = data.get("sap_return_doc_date")
        bst_return.sap_return_reference = data.get("sap_return_reference", "")
        bst_return.security_name = data.get("security_name", "")
        bst_return.remarks = data.get("remarks", "")
        bst_return.updated_by = request.user
        bst_return.save()

    return Response(BSTGateReturnSerializer(bst_return).data)


def get_active_bst_gate_return_by_vehicle_entry(request, vehicle_entry_id):
    bst_return = (
        select_bst_gate_return_queryset()
        .filter(
            vehicle_entry_id=vehicle_entry_id,
            company=request.company.company,
            is_active=True,
            status__in=["IN_PROGRESS", "COMPLETED"],
        )
        .order_by("-created_at")
        .first()
    )

    if not bst_return:
        raise NotFound("Active BST return entry not found")

    return bst_return


class BSTGateReturnDetailView(APIView):
    """Get one BST return record by BST return record id."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, entry_id):
        bst_return = get_object_or_404(
            select_bst_gate_return_queryset(),
            id=entry_id,
            company=request.company.company,
            is_active=True,
        )
        return Response(BSTGateReturnSerializer(bst_return).data)

    def put(self, request, entry_id):
        bst_return = get_object_or_404(
            select_bst_gate_return_queryset(),
            id=entry_id,
            company=request.company.company,
            is_active=True,
        )
        return update_bst_gate_return(request, bst_return)


class BSTGateReturnByVehicleEntryView(APIView):
    """Get or update one BST return record by its underlying vehicle entry id."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, vehicle_entry_id):
        bst_return = get_active_bst_gate_return_by_vehicle_entry(request, vehicle_entry_id)
        return Response(BSTGateReturnSerializer(bst_return).data)

    def put(self, request, vehicle_entry_id):
        bst_return = get_active_bst_gate_return_by_vehicle_entry(request, vehicle_entry_id)
        return update_bst_gate_return(request, bst_return)


class BSTGateReturnCompleteView(APIView):
    """Complete BST return after the source gate confirms the vehicle came back."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, vehicle_entry_id):
        bst_return = get_active_bst_gate_return_by_vehicle_entry(request, vehicle_entry_id)

        if not (bst_return.sap_return_doc_num or "").strip():
            return Response(
                {"detail": "SAP return/reversal document is required before completing this BST return entry"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            bst_return.status = "COMPLETED"
            bst_return.updated_by = request.user
            bst_return.save(update_fields=["status", "updated_by", "updated_at"])

            vehicle_entry = bst_return.vehicle_entry
            vehicle_entry.status = "COMPLETED"
            vehicle_entry.is_locked = True
            vehicle_entry.updated_by = request.user
            vehicle_entry.save(update_fields=["status", "is_locked", "updated_by", "updated_at"])

        return Response(BSTGateReturnSerializer(bst_return).data)


class EmptyVehicleEligibleEntriesView(APIView):
    """List inward vehicle entries that can be marked out empty."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        qs = (
            VehicleEntry.objects
            .filter(company=request.company.company)
            .filter(status__in=list(GRPO_READY_STATUSES))
            .select_related("vehicle", "vehicle__vehicle_type", "driver", "company")
            .order_by("-entry_time")
        )
        completed_gate_out_entry_ids = EmptyVehicleGateOut.objects.filter(
            company=request.company.company,
            is_active=True,
            status="COMPLETED",
        ).values("vehicle_entry_id")
        qs = qs.exclude(id__in=completed_gate_out_entry_ids)

        # A dispatch vehicle stays eligible to leave empty until box scanning
        # starts at docking. Once any box is scanned against a plan linked to
        # this entry, the vehicle is committed to loading and no longer eligible.
        scanned_entry_ids = (
            SalesDispatchBoxScan.objects.filter(
                is_active=True,
                sales_dispatch__dispatch_plan__linked_vehicle_entry__isnull=False,
            )
            .values_list(
                "sales_dispatch__dispatch_plan__linked_vehicle_entry_id",
                flat=True,
            )
        )
        qs = qs.exclude(id__in=scanned_entry_ids)

        entry_type = request.query_params.get("entry_type")
        from_date = request.query_params.get("from_date")
        to_date = request.query_params.get("to_date")

        if entry_type:
            qs = qs.filter(entry_type=entry_type)
        if from_date:
            qs = qs.filter(entry_time__date__gte=from_date)
        if to_date:
            qs = qs.filter(entry_time__date__lte=to_date)

        entries = list(qs)
        empty_out_release_preview(request.company.company, entries)
        serializer = EmptyVehicleEligibleEntrySerializer(entries, many=True)
        return Response(serializer.data)


# Docking gate-out statuses that are past the point an empty-out should unwind.
_EMPTY_OUT_FINALIZED_GATE_OUT_STATUSES = (
    SalesDispatchGateOutStatus.PRINT_COMMITTED,
    SalesDispatchGateOutStatus.DISPATCHED,
    SalesDispatchGateOutStatus.REJECTED,
    SalesDispatchGateOutStatus.CANCELLED,
)


def empty_out_release_preview(company, entries):
    """Annotate eligible empty-out entries with the side effects of marking out.

    Sets ``release_invoice_count`` (booked invoices that would be released) and
    ``release_cancels_docking`` (whether an un-scanned docking gate-out would be
    cancelled) on each entry, computed in bulk to avoid per-entry queries.
    """
    entry_ids = [entry.id for entry in entries]
    if not entry_ids:
        return

    invoice_counts = dict(
        DispatchPlan.objects.filter(
            company=company,
            is_active=True,
            linked_vehicle_entry_id__in=entry_ids,
            booking_status=DispatchPlanStatus.BOOKED,
        )
        .values_list("linked_vehicle_entry_id")
        .annotate(count=Count("id"))
    )
    docking_entry_ids = set(
        SalesDispatchGateOut.objects.filter(
            company=company,
            is_active=True,
            dispatch_plan__linked_vehicle_entry_id__in=entry_ids,
        )
        .exclude(status__in=_EMPTY_OUT_FINALIZED_GATE_OUT_STATUSES)
        .exclude(box_scans__is_active=True)
        .values_list("dispatch_plan__linked_vehicle_entry_id", flat=True)
    )
    for entry in entries:
        entry.release_invoice_count = invoice_counts.get(entry.id, 0)
        entry.release_cancels_docking = entry.id in docking_entry_ids


def release_dispatch_plans_for_empty_out(vehicle_entry, user):
    """Undo the empty vehicle gate-in when the vehicle leaves empty.

    Treats the empty-in as if it never happened so the flow can start again: the
    plans booked to this entry keep their vehicle booking (still ``BOOKED``) but
    the ``linked_vehicle_entry`` is cleared. That unlocks vehicle-linking edits
    (the lock keys on a COMPLETED ``linked_vehicle_entry``) and lets a fresh
    empty-in re-match. Any un-scanned docking gate-out created for those plans is
    cancelled too, so the pipeline resets fully. Returns the number of plans
    released.
    """
    now = timezone.now()

    # The truck left empty: retire its dispatch gate-in so it stops making bills
    # eligible, even if no bookings remain to release.
    gate_in = getattr(vehicle_entry, "empty_vehicle_gate_in", None)
    if gate_in is not None and gate_in.reason == "DISPATCH":
        retire_empty_in(gate_in, EmptyVehicleGateInRetireReason.EMPTY_OUT, user)

    plan_ids = list(
        DispatchPlan.objects.filter(
            company=vehicle_entry.company,
            is_active=True,
            linked_vehicle_entry=vehicle_entry,
            booking_status=DispatchPlanStatus.BOOKED,
        ).values_list("id", flat=True)
    )
    if not plan_ids:
        return 0

    # Cancel any docking gate-out for these plans that has not begun scanning.
    # Scanned vehicles are excluded from empty-out eligibility, so this only
    # unwinds the "docked but not loaded" state.
    gate_outs = (
        SalesDispatchGateOut.objects.filter(
            company=vehicle_entry.company,
            is_active=True,
            dispatch_plan_id__in=plan_ids,
        )
        .exclude(status__in=_EMPTY_OUT_FINALIZED_GATE_OUT_STATUSES)
        .exclude(box_scans__is_active=True)
        .select_related("vehicle_entry")
        .distinct()
    )
    for gate_out in gate_outs:
        gate_out.status = SalesDispatchGateOutStatus.CANCELLED
        gate_out.cancel_reason = "Vehicle left empty before loading (empty vehicle out)."
        gate_out.cancelled_by = user
        gate_out.cancelled_at = now
        gate_out.updated_by = user
        gate_out.save(
            update_fields=[
                "status",
                "cancel_reason",
                "cancelled_by",
                "cancelled_at",
                "updated_by",
                "updated_at",
            ]
        )
        dock_entry = gate_out.vehicle_entry
        if dock_entry is not None:
            dock_entry.status = "CANCELLED"
            dock_entry.updated_by = user
            dock_entry.save(update_fields=["status", "updated_by", "updated_at"])

    return DispatchPlan.objects.filter(id__in=plan_ids).update(
        linked_vehicle_entry=None,
        updated_by=user,
        updated_at=now,
    )


class EmptyVehicleGateOutListCreateView(APIView):
    """List and create empty vehicle gate-out records."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        qs = (
            EmptyVehicleGateOut.objects
            .filter(company=request.company.company, is_active=True)
            .select_related(
                "vehicle_entry",
                "vehicle_entry__vehicle",
                "vehicle_entry__driver",
                "vehicle",
                "driver",
                "company",
            )
        )

        from_date = request.query_params.get("from_date")
        to_date = request.query_params.get("to_date")
        if from_date:
            qs = qs.filter(gate_out_date__gte=from_date)
        if to_date:
            qs = qs.filter(gate_out_date__lte=to_date)

        serializer = EmptyVehicleGateOutSerializer(qs, many=True)
        return Response(serializer.data)

    def post(self, request):
        serializer = EmptyVehicleGateOutCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        vehicle_entry = get_object_or_404(
            VehicleEntry.objects.select_related("vehicle", "driver", "company"),
            id=data["vehicle_entry_id"],
            company=request.company.company,
        )

        if vehicle_entry.status == "CANCELLED":
            return Response(
                {"detail": "Cancelled gate entries cannot be marked out"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if vehicle_entry.status not in GRPO_READY_STATUSES:
            return Response(
                {"detail": "This gate entry must be completed before empty vehicle out"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not has_required_weighment(vehicle_entry):
            return required_weighment_response()

        # Gatepass document upload is optional for empty vehicle out: an empty
        # vehicle may have no gatepass to attach (e.g. it came in empty for repair).
        # The upload stays available, it's just no longer required to mark out.

        if EmptyVehicleGateOut.objects.filter(
            vehicle_entry=vehicle_entry,
            is_active=True,
            status="COMPLETED",
        ).exists():
            return Response(
                {"detail": "This vehicle entry is already marked out"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            gate_out = EmptyVehicleGateOut.objects.create(
                company=request.company.company,
                entry_no=EmptyVehicleGateOut.generate_entry_no(),
                vehicle_entry=vehicle_entry,
                vehicle=vehicle_entry.vehicle,
                driver=vehicle_entry.driver,
                gate_out_date=data["gate_out_date"],
                out_time=data["out_time"],
                security_name=data.get("security_name", ""),
                remarks=data.get("remarks", ""),
                created_by=request.user,
                updated_by=request.user,
            )
            # The vehicle leaves empty, so release any invoices booked to it
            # back to PENDING and unlink them for re-planning.
            release_dispatch_plans_for_empty_out(vehicle_entry, request.user)

        return Response(
            EmptyVehicleGateOutSerializer(gate_out).data,
            status=status.HTTP_201_CREATED,
        )


class EmptyVehicleGateOutDetailView(APIView):
    """Get one empty vehicle gate-out record."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, entry_id):
        gate_out = get_object_or_404(
            EmptyVehicleGateOut.objects.select_related(
                "vehicle_entry",
                "vehicle_entry__vehicle",
                "vehicle_entry__driver",
                "vehicle",
                "driver",
                "company",
            ),
            id=entry_id,
            company=request.company.company,
            is_active=True,
        )
        return Response(EmptyVehicleGateOutSerializer(gate_out).data)


class EmptyVehicleGateOutCancelView(APIView):
    """Cancel a completed empty vehicle gate-out record."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, entry_id):
        serializer = EmptyVehicleGateOutCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        gate_out = get_object_or_404(
            EmptyVehicleGateOut.objects.select_related(
                "vehicle_entry",
                "vehicle_entry__vehicle",
                "vehicle_entry__driver",
                "vehicle",
                "driver",
                "company",
            ),
            id=entry_id,
            company=request.company.company,
            is_active=True,
        )

        if gate_out.status == "CANCELLED":
            return Response(
                {"detail": "This empty vehicle out entry is already cancelled"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        gate_out.status = "CANCELLED"
        gate_out.cancel_reason = serializer.validated_data["cancel_reason"]
        gate_out.cancelled_at = timezone.now()
        gate_out.cancelled_by = request.user
        gate_out.updated_by = request.user
        gate_out.save(update_fields=[
            "status",
            "cancel_reason",
            "cancelled_at",
            "cancelled_by",
            "updated_by",
            "updated_at",
        ])

        return Response(EmptyVehicleGateOutSerializer(gate_out).data)


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
                gross_weight=data["gross_weight"],
                tare_weight=data["tare_weight"],
                weighbridge_slip_no=data.get("weighbridge_slip_no", ""),
                first_weighment_time=data.get("first_weighment_time"),
                second_weighment_time=data.get("second_weighment_time"),
                gatepass_documents=data["gatepass_documents"],
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
                return "ACCEPTED", "QC Approved"
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
                        "chemist_decision": {
                            "decision": inspection.qa_chemist_decision or None,
                            "label": inspection.get_qa_chemist_decision_display() if inspection.qa_chemist_decision else "Pending",
                            "by": inspection.qa_chemist.email if inspection.qa_chemist else None,
                            "decided_at": inspection.qa_chemist_approved_at,
                            "remarks": inspection.qa_chemist_remarks,
                        },
                        "manager_decision": {
                            "decision": inspection.manager_decision or None,
                            "label": inspection.get_qam_decision_display() if inspection.manager_decision else "Pending",
                            "by": inspection.qam.email if inspection.qam else None,
                            "decided_at": inspection.qam_approved_at,
                            "remarks": inspection.qam_remarks,
                        },
                        "qc_stage": inspection.qc_stage,
                        "qc_decision": inspection.manager_decision or None,
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
                .prefetch_related("daily_need_entry__items__unit")
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
            daily_items = [
                {
                    "id": item.id,
                    "line_no": item.line_no,
                    "material_name": item.material_name,
                    "quantity": float(item.quantity),
                    "unit": item.unit.name if item.unit else None,
                }
                for item in daily.items.all()
            ]
            if not daily_items:
                daily_items = [{
                    "id": None,
                    "line_no": 1,
                    "material_name": daily.material_name,
                    "quantity": float(daily.quantity),
                    "unit": daily.unit.name if daily.unit else None,
                }]

            response["daily_need_details"] = {
                "category": daily.item_category.category_name,
                "supplier_name": daily.supplier_name,
                "material_name": daily.material_name,
                "quantity": float(daily.quantity),
                "unit": daily.unit.name if daily.unit else None,
                "items": daily_items,
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
                    "maintenance_entry__maintenance_link",
                    "maintenance_entry__maintenance_link__asset",
                    "maintenance_entry__maintenance_link__work_order",
                    "maintenance_entry__maintenance_link__spare",
                    "maintenance_entry__maintenance_link__received_by",
                )
                .get(id=gate_entry_id, company=request.company.company)
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
            maintenance_link = getattr(maintenance, "maintenance_link", None)
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
                "maintenance_link": None,
            }
            if maintenance_link:
                response["maintenance_details"]["maintenance_link"] = {
                    "id": maintenance_link.id,
                    "asset": maintenance_link.asset_id,
                    "asset_code": maintenance_link.asset.asset_code if maintenance_link.asset else "",
                    "asset_name": maintenance_link.asset.name if maintenance_link.asset else "",
                    "work_order": maintenance_link.work_order_id,
                    "work_order_no": (
                        maintenance_link.work_order.work_order_no
                        if maintenance_link.work_order else ""
                    ),
                    "work_order_title": (
                        maintenance_link.work_order.title
                        if maintenance_link.work_order else ""
                    ),
                    "spare": maintenance_link.spare_id,
                    "spare_part_number": (
                        maintenance_link.spare.part_number
                        if maintenance_link.spare else ""
                    ),
                    "spare_name": maintenance_link.spare.name if maintenance_link.spare else "",
                    "spare_uom": maintenance_link.spare.uom if maintenance_link.spare else "",
                    "spare_is_critical": (
                        maintenance_link.spare.is_critical
                        if maintenance_link.spare else False
                    ),
                    "qc_required": maintenance_link.qc_required,
                    "qc_status": maintenance_link.qc_status,
                    "grpo_reference": maintenance_link.grpo_reference,
                    "grpo_doc_entry": maintenance_link.grpo_doc_entry,
                    "grpo_doc_num": maintenance_link.grpo_doc_num,
                    "receipt_status": maintenance_link.receipt_status,
                    "received_quantity": maintenance_link.received_quantity,
                    "received_at": maintenance_link.received_at,
                    "received_by": maintenance_link.received_by_id,
                    "received_by_name": (
                        maintenance_link.received_by.full_name
                        if maintenance_link.received_by else ""
                    ),
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
