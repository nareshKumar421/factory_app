from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.http import HttpResponse
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from barcode.models import Box, BoxStatus, EntityType, ScanResult
from barcode.services.scan_service import ScanService
from company.permissions import HasCompanyContext
from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
from driver_management.models import Driver, VehicleEntry
from sap_client.exceptions import SAPConnectionError, SAPDataError
from vehicle_management.models import Vehicle
from weighment.models import Weighment

from gate_core.permissions import HasRequiredDjangoPermission
from gate_core.models import (
    EmptyVehicleGateIn,
    SalesDispatchAttachment,
    SalesDispatchAttachmentType,
    SalesDispatchBoxScan,
    SalesDispatchDocumentType,
    SalesDispatchGateOut,
    SalesDispatchGateOutDocument,
    SalesDispatchGateOutItem,
    SalesDispatchGateOutStatus,
    SalesDispatchGatepassPrintLog,
    SalesDispatchGatepassPrintType,
    SalesDispatchLock,
)
from gate_core.models.empty_vehicle_gate_in import EmptyVehicleGateInReason
from gate_core.serializers_sales_dispatch import (
    SalesDispatchAttachmentSerializer,
    SalesDispatchAttachmentUploadSerializer,
    SalesDispatchBoxScanCreateSerializer,
    SalesDispatchBoxScanSerializer,
    SalesDispatchDocumentSerializer,
    SalesDispatchGateOutCreateSerializer,
    SalesDispatchGateOutSerializer,
    SalesDispatchGateOutUpdateSerializer,
    SalesDispatchGatepassPrintLogSerializer,
    SalesDispatchGatepassPrintSerializer,
    SalesDispatchGatepassReprintSerializer,
    SalesDispatchLockSerializer,
    SalesDispatchLockUpdateSerializer,
    SalesDispatchReasonSerializer,
)
from gate_core.services.sales_dispatch_documents import SalesDispatchDocumentService
from gate_core.services.sales_dispatch_gatepass import (
    can_edit,
    ensure_gatepass_ready,
    get_gatepass_readiness,
)
from gate_core.services.sales_dispatch_gatepass_pdf import (
    GatepassPdfError,
    render_sales_dispatch_gatepass_pdf,
)


SALES_DISPATCH_ACTIVE_STATUSES = [
    SalesDispatchGateOutStatus.DOCKED,
    SalesDispatchGateOutStatus.PHOTO_ATTACHED,
    SalesDispatchGateOutStatus.READY_FOR_GATEPASS,
    SalesDispatchGateOutStatus.GATEPASS_PRINTED,
    SalesDispatchGateOutStatus.PRINT_COMMITTED,
    SalesDispatchGateOutStatus.DISPATCHED,
]


def sales_dispatch_queryset(company):
    return (
        SalesDispatchGateOut.objects
        .filter(company=company, is_active=True)
        .select_related(
            "company",
            "vehicle_entry",
            "dispatch_plan",
            "vehicle",
            "vehicle__vehicle_type",
            "vehicle__transporter",
            "transporter",
            "driver",
        )
        .prefetch_related("documents", "items", "attachments", "box_scans")
        .prefetch_related("gatepass_print_logs")
    )


def get_sales_dispatch_or_404(company, entry_id):
    return get_object_or_404(sales_dispatch_queryset(company), id=entry_id)


def get_sales_dispatch_dispatch_weight_error(entry):
    weighment = getattr(entry.vehicle_entry, "weighment", None)
    if not weighment:
        return "Gross and tare weighment are required before marking Docking as dispatched."

    gross_weight = weighment.gross_weight
    tare_weight = weighment.tare_weight
    if gross_weight is None or gross_weight <= 0:
        return "Gross weight is required before marking Docking as dispatched."
    if tare_weight is None or tare_weight < 0:
        return "Tare weight from empty vehicle in is required before marking Docking as dispatched."
    if tare_weight > gross_weight:
        return "Tare weight cannot be greater than gross weight."

    return ""


def get_sales_dispatch_for_update_or_404(company, entry_id):
    return get_object_or_404(
        SalesDispatchGateOut.objects.select_for_update().filter(
            company=company,
            is_active=True,
        ),
        id=entry_id,
    )


def print_request_context(request):
    return {
        "ip_address": request.META.get("REMOTE_ADDR") or None,
        "user_agent": request.META.get("HTTP_USER_AGENT", ""),
    }


def apply_sales_dispatch_filters(qs, query_params):
    status_filter = query_params.get("status")
    document_type = query_params.get("document_type")
    from_date = query_params.get("from_date")
    to_date = query_params.get("to_date")
    search = (query_params.get("search") or "").strip()

    if status_filter:
        qs = qs.filter(status=status_filter)
    if document_type:
        qs = qs.filter(Q(document_type=document_type) | Q(documents__document_type=document_type))
    if from_date:
        qs = qs.filter(created_at__date__gte=from_date)
    if to_date:
        qs = qs.filter(created_at__date__lte=to_date)
    if search:
        qs = qs.filter(
            Q(entry_no__icontains=search)
            | Q(gatepass_no__icontains=search)
            | Q(sap_doc_num__icontains=search)
            | Q(documents__sap_doc_num__icontains=search)
            | Q(vehicle_no__icontains=search)
            | Q(customer_name__icontains=search)
            | Q(documents__customer_name__icontains=search)
        )
    return qs.distinct()


def pending_dispatch_plan_queryset(company):
    active_plan_ids = SalesDispatchGateOut.objects.filter(
        company=company,
        is_active=True,
        dispatch_plan_id__isnull=False,
        status__in=SALES_DISPATCH_ACTIVE_STATUSES,
    ).values_list("dispatch_plan_id", flat=True)
    active_document_plan_ids = SalesDispatchGateOutDocument.objects.filter(
        company=company,
        is_active=True,
        dispatch_plan_id__isnull=False,
        sales_dispatch__is_active=True,
        sales_dispatch__status__in=SALES_DISPATCH_ACTIVE_STATUSES,
    ).values_list("dispatch_plan_id", flat=True)
    active_document_doc_entries = SalesDispatchGateOutDocument.objects.filter(
        company=company,
        is_active=True,
        document_type=SalesDispatchDocumentType.INVOICE,
        sales_dispatch__is_active=True,
        sales_dispatch__status__in=SALES_DISPATCH_ACTIVE_STATUSES,
    ).values_list("sap_doc_entry", flat=True)
    completed_dispatch_gate_in_vehicle_ids = EmptyVehicleGateIn.objects.filter(
        company=company,
        is_active=True,
        reason=EmptyVehicleGateInReason.DISPATCH,
        vehicle_entry__status="COMPLETED",
    ).values_list("vehicle_id", flat=True)

    return (
        DispatchPlan.objects
        .filter(
            company=company,
            is_active=True,
            booking_status=DispatchPlanStatus.BOOKED,
            vehicle_id__in=completed_dispatch_gate_in_vehicle_ids,
        )
        .exclude(id__in=active_plan_ids)
        .exclude(id__in=active_document_plan_ids)
        .exclude(sap_invoice_doc_entry__in=active_document_doc_entries)
        .select_related(
            "vehicle",
            "vehicle__vehicle_type",
            "vehicle__transporter",
            "transporter",
            "driver",
            "linked_vehicle_entry",
            "linked_vehicle_entry__vehicle",
            "linked_vehicle_entry__vehicle__transporter",
            "linked_vehicle_entry__driver",
        )
        .order_by("dispatch_date", "updated_at", "id")
    )


def apply_pending_dispatch_plan_filters(qs, query_params):
    from_date = query_params.get("from_date")
    to_date = query_params.get("to_date")
    search = (query_params.get("search") or "").strip()
    dispatch_plan_ids = parse_id_list(query_params.get("dispatch_plan_ids"))

    if dispatch_plan_ids:
        return qs.filter(id__in=dispatch_plan_ids)
    if from_date:
        qs = qs.filter(
            Q(dispatch_date__gte=from_date)
            | Q(dispatch_date__isnull=True, updated_at__date__gte=from_date)
        )
    if to_date:
        qs = qs.filter(
            Q(dispatch_date__lte=to_date)
            | Q(dispatch_date__isnull=True, updated_at__date__lte=to_date)
        )
    if search:
        qs = qs.filter(
            Q(sap_invoice_doc_num__icontains=search)
            | Q(invoice_number__icontains=search)
            | Q(eway_bill__icontains=search)
            | Q(vehicle_no__icontains=search)
            | Q(vehicle__vehicle_number__icontains=search)
            | Q(driver_name__icontains=search)
            | Q(driver__name__icontains=search)
            | Q(transporter_name__icontains=search)
            | Q(transporter__name__icontains=search)
            | Q(bilty_no__icontains=search)
            | Q(product_variety__icontains=search)
            | Q(place_of_supply__icontains=search)
        )
    return qs


def parse_id_list(value):
    ids = []
    for raw_id in str(value or "").split(","):
        raw_id = raw_id.strip()
        if not raw_id:
            continue
        try:
            ids.append(int(raw_id))
        except ValueError:
            continue
    return list(dict.fromkeys(ids))


def serialize_pending_booking_groups(plans):
    groups = {}
    for plan in plans:
        groups.setdefault(pending_booking_group_key(plan), []).append(plan)

    return [
        serialize_pending_booking_group(group_plans)
        for group_plans in sorted(
            groups.values(),
            key=lambda grouped: (
                grouped[0].dispatch_date or timezone.localdate(),
                grouped[0].updated_at,
                grouped[0].id,
            ),
        )
    ]


def pending_booking_group_key(plan):
    bilty_date = plan.bilty_date.isoformat() if plan.bilty_date else ""
    dispatch_date = plan.dispatch_date.isoformat() if plan.dispatch_date else ""
    return (
        plan.linked_vehicle_entry_id or 0,
        pending_booking_vehicle_id(plan) or 0,
        pending_booking_driver_id(plan) or 0,
        plan.transporter_id or 0,
        plan.bilty_no.strip().upper(),
        bilty_date,
        dispatch_date,
    )


def serialize_pending_booking_group(plans):
    plans = sorted(plans, key=lambda plan: plan.sap_invoice_doc_num or plan.sap_invoice_doc_entry)
    primary = plans[0]
    plan_ids = [plan.id for plan in plans]
    documents = [serialize_pending_booking_document(plan) for plan in plans]
    updated_at = max(plan.updated_at for plan in plans)
    created_at = min(plan.created_at for plan in plans)

    return {
        "row_type": "PENDING_BOOKING",
        "id": f"booking:{','.join(str(plan_id) for plan_id in plan_ids)}",
        "dispatch_plan_ids": plan_ids,
        "document_count": len(plans),
        "document_numbers": [
            plan.sap_invoice_doc_num or str(plan.sap_invoice_doc_entry)
            for plan in plans
        ],
        "documents": documents,
        "document_type": SalesDispatchDocumentType.INVOICE,
        "sap_doc_entry": primary.sap_invoice_doc_entry,
        "sap_doc_num": join_unique(
            plan.sap_invoice_doc_num or str(plan.sap_invoice_doc_entry)
            for plan in plans
        ),
        "sap_doc_date": None,
        "sap_doc_total": sum_decimal(
            plan.invoice_amount
            for plan in plans
            if plan.invoice_amount is not None
        ),
        "customer_code": "",
        "customer_name": "",
        "place_of_supply": join_unique(plan.place_of_supply for plan in plans),
        "eway_bill": join_unique(plan.eway_bill for plan in plans),
        "item_summary": join_unique(
            plan.product_variety or plan.invoice_number or plan.sap_invoice_doc_num
            for plan in plans
        ),
        "total_litres": sum_decimal(
            plan.total_litres
            for plan in plans
            if plan.total_litres is not None
        ),
        "total_weight": sum_decimal(
            plan.invoice_weight
            for plan in plans
            if plan.invoice_weight is not None
        ),
        "vehicle": pending_booking_vehicle_id(primary),
        "vehicle_entry": primary.linked_vehicle_entry_id,
        "vehicle_entry_no": (
            primary.linked_vehicle_entry.entry_no
            if primary.linked_vehicle_entry_id
            else ""
        ),
        "vehicle_no": vehicle_number(primary),
        "transporter": primary.transporter_id,
        "transporter_name": transporter_name(primary),
        "transporter_gstin": primary.transporter_gstin,
        "transporter_contact_person": primary.contact_person,
        "transporter_mobile_no": primary.mobile_no,
        "driver": pending_booking_driver_id(primary),
        "driver_name": driver_name(primary),
        "driver_mobile_no": driver_field(primary, "mobile_no", "driver_mobile_no"),
        "driver_license_no": driver_field(primary, "license_no", "driver_license_no"),
        "driver_id_proof_type": driver_field(primary, "id_proof_type", "driver_id_proof_type"),
        "driver_id_proof_number": driver_field(
            primary,
            "id_proof_number",
            "driver_id_proof_number",
        ),
        "bilty_no": primary.bilty_no,
        "bilty_date": primary.bilty_date,
        "freight": sum_decimal(plan.freight for plan in plans if plan.freight is not None),
        "total_freight": sum_decimal(
            plan.total_freight
            for plan in plans
            if plan.total_freight is not None
        ),
        "dispatch_date": primary.dispatch_date,
        "gate_out_date": None,
        "out_time": None,
        "gatepass_no": None,
        "status": "PENDING_DOCKING",
        "created_at": created_at,
        "updated_at": updated_at,
    }


def serialize_pending_booking_document(plan):
    return {
        "document_type": SalesDispatchDocumentType.INVOICE,
        "doc_entry": plan.sap_invoice_doc_entry,
        "doc_num": plan.sap_invoice_doc_num or str(plan.sap_invoice_doc_entry),
        "doc_date": None,
        "doc_total": plan.invoice_amount,
        "card_code": "",
        "card_name": "",
        "place_of_supply": plan.place_of_supply,
        "eway_bill": plan.eway_bill,
        "vehicle_no": vehicle_number(plan),
        "transporter_name": transporter_name(plan),
        "bilty_no": plan.bilty_no,
        "bilty_date": plan.bilty_date,
        "item_summary": plan.product_variety or plan.invoice_number,
        "total_litres": plan.total_litres,
        "total_weight": plan.invoice_weight,
        "line_count": 0,
        "items": [],
        "plan": {
            "id": plan.id,
            "sap_invoice_doc_entry": plan.sap_invoice_doc_entry,
            "sap_invoice_doc_num": plan.sap_invoice_doc_num,
            "booking_status": plan.booking_status,
        },
    }


def vehicle_number(plan):
    if plan.linked_vehicle_entry_id and plan.linked_vehicle_entry.vehicle_id:
        return plan.linked_vehicle_entry.vehicle.vehicle_number
    if plan.vehicle_no:
        return plan.vehicle_no
    if plan.vehicle_id:
        return plan.vehicle.vehicle_number
    return ""


def transporter_name(plan):
    if plan.transporter_name:
        return plan.transporter_name
    if plan.transporter_id:
        return plan.transporter.name
    if (
        plan.linked_vehicle_entry_id
        and plan.linked_vehicle_entry.vehicle_id
        and plan.linked_vehicle_entry.vehicle.transporter_id
    ):
        return plan.linked_vehicle_entry.vehicle.transporter.name
    if plan.vehicle_id and plan.vehicle.transporter_id:
        return plan.vehicle.transporter.name
    return ""


def driver_name(plan):
    if plan.linked_vehicle_entry_id and plan.linked_vehicle_entry.driver_id:
        return plan.linked_vehicle_entry.driver.name
    if plan.driver_name:
        return plan.driver_name
    if plan.driver_id:
        return plan.driver.name
    return ""


def pending_booking_vehicle_id(plan):
    if plan.linked_vehicle_entry_id:
        return plan.linked_vehicle_entry.vehicle_id
    return plan.vehicle_id


def pending_booking_driver_id(plan):
    if plan.linked_vehicle_entry_id:
        return plan.linked_vehicle_entry.driver_id
    return plan.driver_id


def driver_field(plan, driver_attr, plan_attr):
    if plan.linked_vehicle_entry_id and plan.linked_vehicle_entry.driver_id:
        return getattr(plan.linked_vehicle_entry.driver, driver_attr, "")
    snapshot_value = getattr(plan, plan_attr, "")
    if snapshot_value:
        return snapshot_value
    if plan.driver_id:
        return getattr(plan.driver, driver_attr, "")
    return ""


def join_unique(values):
    result = []
    for value in values:
        value = str(value or "").strip()
        if value and value not in result:
            result.append(value)
    return ", ".join(result)


def sum_decimal(values):
    values = list(values)
    if not values:
        return None
    return sum(values, Decimal("0"))


def sales_dispatch_locked_response(company):
    lock = SalesDispatchLock.for_company(company)
    if not lock.is_locked:
        return None

    detail = "Docking gatepass printing is locked."
    if lock.reason:
        detail = f"{detail} Reason: {lock.reason}"
    return Response(
        {
            "detail": detail,
            "lock": SalesDispatchLockSerializer(lock).data,
        },
        status=423,
    )


def ensure_sales_dispatch_scan_permission(user):
    if user.has_perm("gate_core.can_create_sales_dispatch_out") or user.has_perm(
        "gate_core.can_edit_sales_dispatch_out"
    ):
        return
    raise PermissionDenied("You do not have permission to scan Docking boxes.")


class SalesDispatchLockView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = {
        "GET": "gate_core.can_view_sales_dispatch_out",
        "PATCH": "gate_core.can_manage_sales_dispatch_lock",
    }

    def get(self, request):
        lock = SalesDispatchLock.for_company(request.company.company)
        return Response(SalesDispatchLockSerializer(lock).data)

    def patch(self, request):
        serializer = SalesDispatchLockUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        lock = SalesDispatchLock.for_company(request.company.company)
        lock.is_locked = data["is_locked"]
        lock.reason = data.get("reason", "") if data["is_locked"] else ""
        lock.changed_by = request.user
        lock.changed_at = timezone.now()
        lock.updated_by = request.user
        lock.save(
            update_fields=[
                "is_locked",
                "reason",
                "changed_by",
                "changed_at",
                "updated_by",
                "updated_at",
            ]
        )
        return Response(SalesDispatchLockSerializer(lock).data)


class SalesDispatchReportView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "gate_core.can_view_sales_dispatch_reports"

    def get(self, request):
        qs = apply_sales_dispatch_filters(
            sales_dispatch_queryset(request.company.company),
            request.query_params,
        )
        terminal_statuses = [
            SalesDispatchGateOutStatus.DISPATCHED,
            SalesDispatchGateOutStatus.CANCELLED,
            SalesDispatchGateOutStatus.REJECTED,
        ]
        active = qs.exclude(status__in=terminal_statuses)
        missing_photo = active.filter(
            Q(truck_photo="")
            | Q(truck_photo__isnull=True)
            | Q(photo_latitude__isnull=True)
            | Q(photo_longitude__isnull=True)
        )
        gatepass_pending = active.filter(
            status__in=[
                SalesDispatchGateOutStatus.DOCKED,
                SalesDispatchGateOutStatus.PHOTO_ATTACHED,
                SalesDispatchGateOutStatus.READY_FOR_GATEPASS,
            ]
        )
        printed_not_committed = qs.filter(status=SalesDispatchGateOutStatus.GATEPASS_PRINTED)
        ready_for_dispatch = qs.filter(status=SalesDispatchGateOutStatus.PRINT_COMMITTED)
        dispatched = qs.filter(status=SalesDispatchGateOutStatus.DISPATCHED)
        rejected_cancelled = qs.filter(
            status__in=[
                SalesDispatchGateOutStatus.REJECTED,
                SalesDispatchGateOutStatus.CANCELLED,
            ]
        )
        truck_with_photo = qs.exclude(
            Q(truck_photo="")
            | Q(truck_photo__isnull=True)
            | Q(photo_latitude__isnull=True)
            | Q(photo_longitude__isnull=True)
        )
        limit = self._report_limit(request.query_params.get("limit"))

        return Response(
            {
                "counts": {
                    "total": qs.count(),
                    "waiting_inside": active.count(),
                    "missing_photo": missing_photo.count(),
                    "gatepass_pending": gatepass_pending.count(),
                    "printed_not_committed": printed_not_committed.count(),
                    "ready_for_dispatch": ready_for_dispatch.count(),
                    "dispatched": dispatched.count(),
                    "rejected_cancelled": rejected_cancelled.count(),
                    "truck_with_photo": truck_with_photo.count(),
                },
                "waiting_inside": SalesDispatchGateOutSerializer(
                    active.order_by("created_at")[:limit],
                    many=True,
                ).data,
                "missing_photo": SalesDispatchGateOutSerializer(
                    missing_photo.order_by("created_at")[:limit],
                    many=True,
                ).data,
                "gatepass_pending": SalesDispatchGateOutSerializer(
                    gatepass_pending.order_by("created_at")[:limit],
                    many=True,
                ).data,
                "printed_not_committed": SalesDispatchGateOutSerializer(
                    printed_not_committed.order_by("printed_at", "created_at")[:limit],
                    many=True,
                ).data,
                "ready_for_dispatch": SalesDispatchGateOutSerializer(
                    ready_for_dispatch.order_by("print_committed_at", "created_at")[:limit],
                    many=True,
                ).data,
                "dispatched": SalesDispatchGateOutSerializer(
                    dispatched.order_by("-dispatched_at", "-updated_at")[:limit],
                    many=True,
                ).data,
                "rejected_cancelled": SalesDispatchGateOutSerializer(
                    rejected_cancelled.order_by("-updated_at")[:limit],
                    many=True,
                ).data,
                "truck_vs_invoices_with_photo": SalesDispatchGateOutSerializer(
                    truck_with_photo.order_by("-photo_uploaded_at", "-created_at")[:limit],
                    many=True,
                ).data,
                "truck_status_with_photo": SalesDispatchGateOutSerializer(
                    truck_with_photo.order_by("status", "-photo_uploaded_at", "-created_at")[:limit],
                    many=True,
                ).data,
            }
        )

    @staticmethod
    def _report_limit(value):
        try:
            return min(max(int(value or 20), 1), 1000)
        except (TypeError, ValueError):
            return 20


class SalesDispatchPendingBookingListView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "gate_core.can_view_sales_dispatch_out"

    def get(self, request):
        qs = apply_pending_dispatch_plan_filters(
            pending_dispatch_plan_queryset(request.company.company),
            request.query_params,
        )
        limit = min(int(request.query_params.get("limit") or 200), 1000)
        groups = serialize_pending_booking_groups(qs[:limit])
        return Response(groups)


class SalesDispatchDocumentListView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "gate_core.can_create_sales_dispatch_out"

    def get(self, request):
        service = SalesDispatchDocumentService(request.company.company)
        try:
            documents = service.list_documents(
                request.query_params.get("document_type", "ALL"),
                {
                    "search": request.query_params.get("search", ""),
                    "from_date": request.query_params.get("from_date"),
                    "to_date": request.query_params.get("to_date"),
                    "branch": request.query_params.get("branch", ""),
                    "booking_status": request.query_params.get("booking_status", "all"),
                    "limit": request.query_params.get("limit", 100),
                },
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError:
            return Response(
                {"detail": "Failed to retrieve Docking documents from SAP."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(SalesDispatchDocumentSerializer(documents, many=True).data)


class SalesDispatchDocumentDetailView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "gate_core.can_create_sales_dispatch_out"

    def get(self, request, document_type, doc_entry):
        service = SalesDispatchDocumentService(request.company.company)
        try:
            document = service.get_document(document_type, doc_entry)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError:
            return Response(
                {"detail": "Failed to retrieve Docking document from SAP."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if not document:
            raise NotFound("Docking document not found in SAP")
        return Response(SalesDispatchDocumentSerializer(document).data)


class SalesDispatchGateOutListCreateView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = {
        "GET": "gate_core.can_view_sales_dispatch_out",
        "POST": "gate_core.can_create_sales_dispatch_out",
    }

    def get(self, request):
        qs = apply_sales_dispatch_filters(
            sales_dispatch_queryset(request.company.company),
            request.query_params,
        )
        return Response(SalesDispatchGateOutSerializer(qs, many=True).data)

    def post(self, request):
        serializer = SalesDispatchGateOutCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        service = SalesDispatchDocumentService(request.company.company)
        document_inputs = data["documents"]
        documents = []
        try:
            for document_input in document_inputs:
                document = service.get_document(
                    document_input["document_type"],
                    document_input["sap_doc_entry"],
                )
                if not document:
                    raise NotFound("Selected Docking document was not found in SAP")
                documents.append(document)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError:
            return Response(
                {"detail": "Failed to retrieve selected Docking document from SAP."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        validation_error = self._validate_document_set(request.company.company, documents)
        if validation_error:
            return validation_error

        duplicate_response = self._duplicate_response(request.company.company, documents)
        if duplicate_response:
            return duplicate_response

        vehicle = get_object_or_404(Vehicle, id=data["vehicle_id"])
        driver = get_object_or_404(Driver, id=data["driver_id"])
        transporter = vehicle.transporter
        dispatch_plans_by_doc_entry = {}
        for document_input, document in zip(document_inputs, documents):
            dispatch_plan_id = document_input.get("dispatch_plan_id")
            if not dispatch_plan_id:
                continue
            dispatch_plan = get_object_or_404(
                DispatchPlan,
                id=dispatch_plan_id,
                company=request.company.company,
                is_active=True,
            )
            if document["document_type"] == SalesDispatchDocumentType.INVOICE:
                if dispatch_plan.sap_invoice_doc_entry != document["doc_entry"]:
                    return Response(
                        {"detail": "Dispatch plan does not match selected invoice."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            dispatch_plans_by_doc_entry[document["doc_entry"]] = dispatch_plan

        primary_document = documents[0]
        dispatch_plan = dispatch_plans_by_doc_entry.get(primary_document["doc_entry"])
        warnings = self._document_warnings(documents)

        with transaction.atomic():
            vehicle_entry = VehicleEntry.objects.create(
                entry_no=SalesDispatchGateOut.generate_vehicle_entry_no(),
                company=request.company.company,
                vehicle=vehicle,
                driver=driver,
                entry_type="SALES_DISPATCH",
                status="IN_PROGRESS",
                remarks=data.get("remarks", ""),
                created_by=request.user,
                updated_by=request.user,
            )
            self._copy_empty_vehicle_tare_weighment(
                source_entry=getattr(dispatch_plan, "linked_vehicle_entry", None),
                target_entry=vehicle_entry,
                user=request.user,
            )
            entry = SalesDispatchGateOut.objects.create(
                company=request.company.company,
                entry_no=SalesDispatchGateOut.generate_entry_no(),
                vehicle_entry=vehicle_entry,
                dispatch_plan=dispatch_plan,
                vehicle=vehicle,
                transporter=transporter,
                driver=driver,
                **self._header_snapshot(documents),
                **self._transport_snapshot(vehicle, driver, transporter),
                bilty_no=data.get("bilty_no") or primary_document.get("bilty_no", ""),
                bilty_date=data.get("bilty_date") or primary_document.get("bilty_date"),
                freight=data.get("freight"),
                total_freight=data.get("total_freight"),
                dock_incharge=data.get("dock_incharge", ""),
                security_name=data.get("security_name", ""),
                remarks=data.get("remarks", ""),
                created_by=request.user,
                updated_by=request.user,
            )
            next_line_num = 0
            for document in documents:
                document_row = SalesDispatchGateOutDocument.objects.create(
                    sales_dispatch=entry,
                    company=request.company.company,
                    dispatch_plan=dispatch_plans_by_doc_entry.get(document["doc_entry"]),
                    created_by=request.user,
                    updated_by=request.user,
                    **self._document_snapshot(document),
                )
                next_line_num = self._create_items(
                    entry,
                    document_row,
                    document,
                    request.user,
                    next_line_num,
                )

        response_data = SalesDispatchGateOutSerializer(entry).data
        response_data["warnings"] = warnings
        return Response(response_data, status=status.HTTP_201_CREATED)

    @staticmethod
    def _copy_empty_vehicle_tare_weighment(source_entry, target_entry, user):
        source_weighment = getattr(source_entry, "weighment", None)
        if not source_weighment or source_weighment.tare_weight is None:
            return

        Weighment.objects.create(
            vehicle_entry=target_entry,
            tare_weight=source_weighment.tare_weight,
            weighbridge_slip_no=source_weighment.weighbridge_slip_no,
            first_weighment_time=source_weighment.first_weighment_time,
            second_weighment_time=source_weighment.second_weighment_time,
            remarks=source_weighment.remarks,
            created_by=user,
            updated_by=user,
        )

    @staticmethod
    def _active_statuses():
        return SALES_DISPATCH_ACTIVE_STATUSES

    def _validate_document_set(self, company, documents):
        document_types = {document["document_type"] for document in documents}
        if len(document_types) > 1:
            return Response(
                {"detail": "Invoice and stock transfer documents cannot be mixed."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        document_type = next(iter(document_types))
        if document_type == SalesDispatchDocumentType.STOCK_TRANSFER and len(documents) > 1:
            return Response(
                {"detail": "Stock transfer Docking supports one SAP document for now."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        branch_ids = {
            document.get("branch_id")
            for document in documents
            if document.get("branch_id") is not None
        }
        if len(branch_ids) > 1:
            return Response(
                {"detail": "Selected invoices must belong to the same SAP branch."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return None

    def _duplicate_response(self, company, documents):
        for document in documents:
            duplicate_document = (
                SalesDispatchGateOutDocument.objects
                .filter(
                    company=company,
                    document_type=document["document_type"],
                    sap_doc_entry=document["doc_entry"],
                    is_active=True,
                    sales_dispatch__is_active=True,
                    sales_dispatch__status__in=self._active_statuses(),
                )
                .select_related("sales_dispatch")
                .first()
            )
            duplicate = duplicate_document.sales_dispatch if duplicate_document else None
            if not duplicate:
                duplicate = SalesDispatchGateOut.objects.filter(
                    company=company,
                    document_type=document["document_type"],
                    sap_doc_entry=document["doc_entry"],
                    is_active=True,
                    status__in=self._active_statuses(),
                ).first()
            if duplicate:
                return Response(
                    {
                        "detail": (
                            f"SAP document {document.get('doc_num') or document['doc_entry']} "
                            f"is already docked as {duplicate.entry_no}."
                        ),
                        "linked_sales_dispatch_id": duplicate.id,
                        "linked_entry_no": duplicate.entry_no,
                        "linked_entry_id": duplicate.vehicle_entry_id,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
        return None

    @staticmethod
    def _document_warnings(documents):
        warnings = []
        customer_names = {
            (document.get("card_name") or document.get("card_code") or "").strip()
            for document in documents
            if (document.get("card_name") or document.get("card_code") or "").strip()
        }
        eway_bills = {
            (document.get("eway_bill") or "").strip()
            for document in documents
            if (document.get("eway_bill") or "").strip()
        }
        if len(customer_names) > 1:
            warnings.append(
                {
                    "code": "MULTIPLE_CUSTOMERS",
                    "message": "Selected invoices belong to different customers.",
                }
            )
        if len(eway_bills) > 1:
            warnings.append(
                {
                    "code": "MULTIPLE_EWAY_BILLS",
                    "message": "Selected invoices have different e-way bills.",
                }
            )
        return warnings

    @staticmethod
    def _join_unique(values):
        result = []
        for value in values:
            value = str(value or "").strip()
            if value and value not in result:
                result.append(value)
        return ", ".join(result)

    @staticmethod
    def _sum_documents(documents, key, places="0.001"):
        values = [decimal_or_none(document.get(key), places) for document in documents]
        values = [value for value in values if value is not None]
        if not values:
            return None
        return sum(values, Decimal("0"))

    def _header_snapshot(self, documents):
        primary = documents[0]
        snapshot = self._document_snapshot(primary)
        snapshot["sap_doc_num"] = self._join_unique(document.get("doc_num", "") for document in documents)
        snapshot["sap_doc_total"] = self._sum_documents(documents, "doc_total", "0.01")
        snapshot["sap_reference"] = self._join_unique(
            document.get("sap_reference") or document.get("base_refs", "")
            for document in documents
        )
        snapshot["customer_code"] = self._join_unique(document.get("card_code", "") for document in documents)
        snapshot["customer_name"] = self._join_unique(document.get("card_name", "") for document in documents)
        snapshot["eway_bill"] = self._join_unique(document.get("eway_bill", "") for document in documents)
        snapshot["warehouses"] = self._join_unique(document.get("warehouses", "") for document in documents)
        snapshot["item_summary"] = " | ".join(
            document.get("item_summary", "")
            for document in documents
            if document.get("item_summary", "")
        )
        snapshot["base_refs"] = self._join_unique(document.get("base_refs", "") for document in documents)
        snapshot["total_quantity"] = self._sum_documents(documents, "total_quantity")
        snapshot["total_litres"] = self._sum_documents(documents, "total_litres")
        snapshot["total_boxes"] = self._sum_documents(documents, "total_boxes")
        snapshot["total_weight"] = self._sum_documents(documents, "total_weight")
        return snapshot

    @staticmethod
    def _document_snapshot(document):
        return {
            "document_type": document["document_type"],
            "sap_doc_entry": document["doc_entry"],
            "sap_doc_num": document.get("doc_num", ""),
            "sap_doc_date": document.get("doc_date"),
            "sap_doc_total": decimal_or_none(document.get("doc_total"), "0.01"),
            "sap_branch_id": document.get("branch_id"),
            "sap_branch_name": document.get("branch_name", ""),
            "sap_reference": document.get("sap_reference") or document.get("base_refs", ""),
            "sap_comments": document.get("sap_comments", ""),
            "customer_code": document.get("card_code", ""),
            "customer_name": document.get("card_name", ""),
            "ship_to_code": document.get("ship_to_code", ""),
            "ship_to_address": document.get("ship_to_address", ""),
            "place_of_supply": document.get("place_of_supply", ""),
            "bp_gstin": document.get("bp_gstin", ""),
            "eway_bill": document.get("eway_bill", ""),
            "from_warehouse": document.get("from_warehouse", ""),
            "to_warehouse": document.get("to_warehouse", ""),
            "warehouses": document.get("warehouses", ""),
            "item_summary": document.get("item_summary", ""),
            "base_refs": document.get("base_refs", ""),
            "total_quantity": decimal_or_none(document.get("total_quantity")),
            "total_litres": decimal_or_none(document.get("total_litres")),
            "total_boxes": decimal_or_none(document.get("total_boxes")),
            "total_weight": decimal_or_none(document.get("total_weight")),
        }

    @staticmethod
    def _transport_snapshot(vehicle, driver, transporter):
        return {
            "vehicle_no": vehicle.vehicle_number,
            "transporter_name": transporter.name if transporter else "",
            "transporter_gstin": transporter.gstin if transporter else "",
            "transporter_contact_person": transporter.contact_person if transporter else "",
            "transporter_mobile_no": transporter.mobile_no if transporter else "",
            "driver_name": driver.name,
            "driver_mobile_no": driver.mobile_no,
            "driver_license_no": driver.license_no,
            "driver_id_proof_type": driver.id_proof_type,
            "driver_id_proof_number": driver.id_proof_number,
        }

    @staticmethod
    def _create_items(entry, document_row, document, user, start_line_num=0):
        items = []
        for index, item in enumerate(SalesDispatchDocumentService.iter_items(document)):
            item["line_num"] = start_line_num + index
            items.append(
                SalesDispatchGateOutItem(
                    sales_dispatch=entry,
                    document=document_row,
                    created_by=user,
                    updated_by=user,
                    **item,
                )
            )
        SalesDispatchGateOutItem.objects.bulk_create(items)
        return start_line_num + len(items)


class SalesDispatchGateOutDetailView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = {
        "GET": "gate_core.can_view_sales_dispatch_out",
        "PATCH": "gate_core.can_edit_sales_dispatch_out",
    }

    def get(self, request, entry_id):
        entry = get_sales_dispatch_or_404(request.company.company, entry_id)
        return Response(SalesDispatchGateOutSerializer(entry).data)

    def patch(self, request, entry_id):
        entry = get_sales_dispatch_or_404(request.company.company, entry_id)
        if not can_edit(entry):
            return Response(
                {"detail": "This Docking entry cannot be edited in its current status."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = SalesDispatchGateOutUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        for field, value in serializer.validated_data.items():
            setattr(entry, field, value)
        entry.updated_by = request.user
        entry.save()
        return Response(SalesDispatchGateOutSerializer(entry).data)


class SalesDispatchGateOutByVehicleEntryView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "gate_core.can_view_sales_dispatch_out"

    def get(self, request, vehicle_entry_id):
        entry = sales_dispatch_queryset(request.company.company).filter(
            vehicle_entry_id=vehicle_entry_id,
        ).order_by("-created_at").first()
        if not entry:
            raise NotFound("Docking entry not found for this vehicle entry.")
        return Response(SalesDispatchGateOutSerializer(entry).data)


class SalesDispatchAttachmentListCreateView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = {
        "GET": "gate_core.can_view_sales_dispatch_out",
        "POST": "gate_core.can_upload_sales_dispatch_photo",
    }

    def get(self, request, entry_id):
        entry = get_sales_dispatch_or_404(request.company.company, entry_id)
        return Response(SalesDispatchAttachmentSerializer(entry.attachments.all(), many=True).data)

    def post(self, request, entry_id):
        entry = get_sales_dispatch_or_404(request.company.company, entry_id)
        if entry.status in (
            SalesDispatchGateOutStatus.PRINT_COMMITTED,
            SalesDispatchGateOutStatus.DISPATCHED,
            SalesDispatchGateOutStatus.CANCELLED,
            SalesDispatchGateOutStatus.REJECTED,
        ):
            return Response(
                {"detail": "Attachments cannot be changed in this Docking status."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = SalesDispatchAttachmentUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        attachment = SalesDispatchAttachment.objects.create(
            sales_dispatch=entry,
            attachment_type=data["attachment_type"],
            file=data["file"],
            original_filename=getattr(data["file"], "name", ""),
            latitude=data.get("latitude"),
            longitude=data.get("longitude"),
            notes=data.get("notes", ""),
            uploaded_by=request.user,
        )

        if data["attachment_type"] == SalesDispatchAttachmentType.TRUCK_PHOTO:
            entry.truck_photo = attachment.file
            entry.photo_latitude = data.get("latitude")
            entry.photo_longitude = data.get("longitude")
            entry.photo_uploaded_by = request.user
            entry.photo_uploaded_at = timezone.now()
            if entry.status == SalesDispatchGateOutStatus.DOCKED:
                entry.status = SalesDispatchGateOutStatus.PHOTO_ATTACHED
            entry.updated_by = request.user
            entry.save(
                update_fields=[
                    "truck_photo",
                    "photo_latitude",
                    "photo_longitude",
                    "photo_uploaded_by",
                    "photo_uploaded_at",
                    "status",
                    "updated_by",
                    "updated_at",
                ]
            )

        return Response(
            SalesDispatchAttachmentSerializer(attachment).data,
            status=status.HTTP_201_CREATED,
        )


class SalesDispatchBoxScanListCreateView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = {
        "GET": "gate_core.can_view_sales_dispatch_out",
    }

    def get(self, request, entry_id):
        entry = get_sales_dispatch_or_404(request.company.company, entry_id)
        scans = (
            entry.box_scans
            .filter(is_active=True)
            .select_related("box", "scan_log", "scanned_by")
        )
        return Response(SalesDispatchBoxScanSerializer(scans, many=True).data)

    def post(self, request, entry_id):
        ensure_sales_dispatch_scan_permission(request.user)
        entry = get_sales_dispatch_or_404(request.company.company, entry_id)
        if not can_edit(entry):
            return Response(
                {"detail": "Box scans cannot be changed in this Docking status."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = SalesDispatchBoxScanCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        barcode_raw = serializer.validated_data["barcode_raw"]

        scan_service = ScanService(company_code=request.company.company.code)
        scan_result = scan_service.process_scan(
            barcode_raw=barcode_raw,
            scan_type="SHIP",
            context_ref_type="SALES_DISPATCH",
            context_ref_id=entry.id,
            user=request.user,
            device_info=request.META.get("HTTP_USER_AGENT", "")[:500],
        )

        if scan_result["result"] != ScanResult.SUCCESS:
            return Response(
                {"detail": "Box barcode was not found.", "scan": scan_result},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if scan_result["entity_type"] != EntityType.BOX:
            return Response(
                {"detail": "Only box barcodes can be scanned for Docking.", "scan": scan_result},
                status=status.HTTP_400_BAD_REQUEST,
            )

        box = get_object_or_404(
            Box.objects.select_related("pallet"),
            id=scan_result["entity_id"],
            company=request.company.company,
        )
        if box.status not in (BoxStatus.ACTIVE, BoxStatus.PARTIAL):
            return Response(
                {"detail": f"Box {box.box_barcode} is {box.status} and cannot be dispatched."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        scan, created = SalesDispatchBoxScan.objects.get_or_create(
            sales_dispatch=entry,
            box_barcode=box.box_barcode,
            defaults={
                "company": request.company.company,
                "box": box,
                "scan_log_id": scan_result["scan_id"],
                "barcode_raw": barcode_raw,
                "item_code": box.item_code,
                "item_name": box.item_name,
                "batch_number": box.batch_number,
                "quantity": box.qty,
                "uom": box.uom,
                "box_status": box.status,
                "warehouse_code": box.current_warehouse,
                "pallet_code": box.pallet.pallet_id if box.pallet else "",
                "scanned_by": request.user,
                "created_by": request.user,
                "updated_by": request.user,
            },
        )
        if not created and not scan.is_active:
            scan.is_active = True
            scan.box = box
            scan.scan_log_id = scan_result["scan_id"]
            scan.barcode_raw = barcode_raw
            scan.item_code = box.item_code
            scan.item_name = box.item_name
            scan.batch_number = box.batch_number
            scan.quantity = box.qty
            scan.uom = box.uom
            scan.box_status = box.status
            scan.warehouse_code = box.current_warehouse
            scan.pallet_code = box.pallet.pallet_id if box.pallet else ""
            scan.scanned_by = request.user
            scan.scanned_at = timezone.now()
            scan.updated_by = request.user
            scan.save()
            created = True

        response_data = SalesDispatchBoxScanSerializer(scan).data
        response_data["duplicate"] = not created
        return Response(
            response_data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class SalesDispatchBoxScanDetailView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = {}

    def delete(self, request, entry_id, scan_id):
        ensure_sales_dispatch_scan_permission(request.user)
        entry = get_sales_dispatch_or_404(request.company.company, entry_id)
        if not can_edit(entry):
            return Response(
                {"detail": "Box scans cannot be changed in this Docking status."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        scan = get_object_or_404(
            SalesDispatchBoxScan,
            id=scan_id,
            sales_dispatch=entry,
            company=request.company.company,
            is_active=True,
        )
        scan.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SalesDispatchGatepassPreviewView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "gate_core.can_print_sales_dispatch_gatepass"

    def post(self, request, entry_id):
        entry = get_sales_dispatch_or_404(request.company.company, entry_id)
        readiness = get_gatepass_readiness(entry)
        if readiness["ready"] and entry.status == SalesDispatchGateOutStatus.PHOTO_ATTACHED:
            entry.status = SalesDispatchGateOutStatus.READY_FOR_GATEPASS
            entry.updated_by = request.user
            entry.save(update_fields=["status", "updated_by", "updated_at"])
        data = SalesDispatchGateOutSerializer(entry).data
        data["gatepass_readiness"] = readiness
        return Response(data)


class SalesDispatchGatepassPrintView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "gate_core.can_print_sales_dispatch_gatepass"

    def post(self, request, entry_id):
        locked_response = sales_dispatch_locked_response(request.company.company)
        if locked_response:
            return locked_response

        serializer = SalesDispatchGatepassPrintSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            entry = get_sales_dispatch_for_update_or_404(request.company.company, entry_id)
            if (
                entry.gatepass_no
                or entry.printed_at
                or entry.gatepass_print_logs.filter(
                    print_type=SalesDispatchGatepassPrintType.ORIGINAL,
                ).exists()
            ):
                return Response(
                    {
                        "detail": (
                            "Original gatepass print is already recorded. "
                            "Use the audited reprint workflow."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if entry.status in (
                SalesDispatchGateOutStatus.GATEPASS_PRINTED,
                SalesDispatchGateOutStatus.PRINT_COMMITTED,
                SalesDispatchGateOutStatus.DISPATCHED,
                SalesDispatchGateOutStatus.CANCELLED,
                SalesDispatchGateOutStatus.REJECTED,
            ):
                return Response(
                    {"detail": "Gatepass cannot be printed in this Docking status."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            try:
                ensure_gatepass_ready(entry)
            except ValueError as exc:
                return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

            for field in (
                "uom",
                "physical_quantity",
                "seal_number",
                "pgi_reference",
                "eway_bill",
            ):
                value = serializer.validated_data.get(field)
                if value not in (None, ""):
                    setattr(entry, field, value)
            entry.updated_by = request.user
            entry.save()
            entry.assign_gatepass(request.user)
            SalesDispatchGatepassPrintLog.record_print(
                sales_dispatch=entry,
                print_type=SalesDispatchGatepassPrintType.ORIGINAL,
                user=request.user,
                printer_name=serializer.validated_data.get("printer_name", ""),
                **print_request_context(request),
            )
            getattr(entry, "_prefetched_objects_cache", {}).pop("gatepass_print_logs", None)

        return Response(SalesDispatchGateOutSerializer(entry).data)


class SalesDispatchGatepassReprintView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "gate_core.can_reprint_sales_dispatch_gatepass"

    def post(self, request, entry_id):
        locked_response = sales_dispatch_locked_response(request.company.company)
        if locked_response:
            return locked_response

        serializer = SalesDispatchGatepassReprintSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            entry = get_sales_dispatch_for_update_or_404(request.company.company, entry_id)
            if not entry.gatepass_no or not entry.printed_at:
                return Response(
                    {"detail": "Original gatepass must be printed before a reprint."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if entry.status in (
                SalesDispatchGateOutStatus.CANCELLED,
                SalesDispatchGateOutStatus.REJECTED,
            ):
                return Response(
                    {"detail": "Gatepass cannot be reprinted in this Docking status."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            SalesDispatchGatepassPrintLog.record_print(
                sales_dispatch=entry,
                print_type=SalesDispatchGatepassPrintType.REPRINT,
                user=request.user,
                reprint_reason=serializer.validated_data["reprint_reason"],
                printer_name=serializer.validated_data.get("printer_name", ""),
                **print_request_context(request),
            )
            entry.updated_by = request.user
            entry.save(update_fields=["updated_by", "updated_at"])
            getattr(entry, "_prefetched_objects_cache", {}).pop("gatepass_print_logs", None)

        return Response(SalesDispatchGateOutSerializer(entry).data)


class SalesDispatchGatepassPrintHistoryView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "gate_core.can_view_sales_dispatch_out"

    def get(self, request, entry_id):
        entry = get_sales_dispatch_or_404(request.company.company, entry_id)
        return Response(
            SalesDispatchGatepassPrintLogSerializer(
                entry.gatepass_print_logs.select_related("printed_by"),
                many=True,
            ).data
        )


class SalesDispatchGatepassPdfView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "gate_core.can_print_sales_dispatch_gatepass"

    def get(self, request, entry_id):
        entry = get_sales_dispatch_or_404(request.company.company, entry_id)
        if not entry.gatepass_no or not entry.printed_at:
            return Response(
                {"detail": "Original gatepass must be printed before PDF generation."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = render_sales_dispatch_gatepass_pdf(entry)
        except GatepassPdfError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        response = HttpResponse(result.pdf, content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="{result.filename}"'
        response["X-Gatepass-Renderer"] = result.renderer
        response["X-Crystal-DocKey"] = str(result.parameters.get("DocKey@", ""))
        response["X-Crystal-ObjectId"] = str(result.parameters.get("ObjectId@", ""))
        return response


class SalesDispatchCommitPrintView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "gate_core.can_commit_sales_dispatch_print"

    def post(self, request, entry_id):
        locked_response = sales_dispatch_locked_response(request.company.company)
        if locked_response:
            return locked_response

        entry = get_sales_dispatch_or_404(request.company.company, entry_id)
        if entry.status != SalesDispatchGateOutStatus.GATEPASS_PRINTED:
            return Response(
                {"detail": "Gatepass must be printed before final print commit."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        entry.status = SalesDispatchGateOutStatus.PRINT_COMMITTED
        entry.print_committed_by = request.user
        entry.print_committed_at = timezone.now()
        entry.updated_by = request.user
        entry.save(
            update_fields=[
                "status",
                "print_committed_by",
                "print_committed_at",
                "updated_by",
                "updated_at",
            ]
        )
        return Response(SalesDispatchGateOutSerializer(entry).data)


class SalesDispatchMarkDispatchedView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "gate_core.can_dispatch_sales_dispatch_out"

    def post(self, request, entry_id):
        entry = get_sales_dispatch_or_404(request.company.company, entry_id)
        if entry.status != SalesDispatchGateOutStatus.PRINT_COMMITTED:
            return Response(
                {"detail": "Print must be committed before marking Docking as dispatched."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not entry.gatepass_no or not entry.print_committed_at:
            return Response(
                {
                    "detail": (
                        "Gatepass number and final print commit timestamp are required "
                        "before marking Docking as dispatched."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        weight_error = get_sales_dispatch_dispatch_weight_error(entry)
        if weight_error:
            return Response({"detail": weight_error}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            entry.status = SalesDispatchGateOutStatus.DISPATCHED
            entry.gate_out_date = timezone.localdate()
            entry.out_time = timezone.localtime().time().replace(microsecond=0)
            entry.dispatched_by = request.user
            entry.dispatched_at = timezone.now()
            entry.updated_by = request.user
            entry.save(
                update_fields=[
                    "status",
                    "gate_out_date",
                    "out_time",
                    "dispatched_by",
                    "dispatched_at",
                    "updated_by",
                    "updated_at",
                ]
            )
            entry.vehicle_entry.status = "COMPLETED"
            entry.vehicle_entry.updated_by = request.user
            entry.vehicle_entry.save(update_fields=["status", "updated_by", "updated_at"])
            dispatch_plans = list(
                DispatchPlan.objects
                .filter(sales_dispatch_gate_out_documents__sales_dispatch=entry)
                .distinct()
            )
            if entry.dispatch_plan_id:
                dispatch_plans.append(entry.dispatch_plan)
            seen_plan_ids = set()
            for dispatch_plan in dispatch_plans:
                if dispatch_plan.id in seen_plan_ids:
                    continue
                seen_plan_ids.add(dispatch_plan.id)
                dispatch_plan.booking_status = DispatchPlanStatus.DISPATCHED
                dispatch_plan.updated_by = request.user
                dispatch_plan.save(update_fields=["booking_status", "updated_by", "updated_at"])
        return Response(SalesDispatchGateOutSerializer(entry).data)


class SalesDispatchRejectView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "gate_core.can_reject_sales_dispatch_out"

    def post(self, request, entry_id):
        entry = get_sales_dispatch_or_404(request.company.company, entry_id)
        serializer = SalesDispatchReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if entry.status == SalesDispatchGateOutStatus.DISPATCHED:
            return Response(
                {"detail": "Dispatched Docking entries cannot be rejected."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        entry.status = SalesDispatchGateOutStatus.REJECTED
        entry.reject_reason = serializer.validated_data["reason"]
        entry.rejected_by = request.user
        entry.rejected_at = timezone.now()
        entry.updated_by = request.user
        entry.save(
            update_fields=[
                "status",
                "reject_reason",
                "rejected_by",
                "rejected_at",
                "updated_by",
                "updated_at",
            ]
        )
        return Response(SalesDispatchGateOutSerializer(entry).data)


class SalesDispatchCancelView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "gate_core.can_cancel_sales_dispatch_out"

    def post(self, request, entry_id):
        entry = get_sales_dispatch_or_404(request.company.company, entry_id)
        serializer = SalesDispatchReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if entry.status in (
            SalesDispatchGateOutStatus.PRINT_COMMITTED,
            SalesDispatchGateOutStatus.DISPATCHED,
        ):
            return Response(
                {"detail": "Docking entries cannot be cancelled after final print commit."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        entry.status = SalesDispatchGateOutStatus.CANCELLED
        entry.cancel_reason = serializer.validated_data["reason"]
        entry.cancelled_by = request.user
        entry.cancelled_at = timezone.now()
        entry.updated_by = request.user
        entry.vehicle_entry.status = "CANCELLED"
        with transaction.atomic():
            entry.save(
                update_fields=[
                    "status",
                    "cancel_reason",
                    "cancelled_by",
                    "cancelled_at",
                    "updated_by",
                    "updated_at",
                ]
            )
            entry.vehicle_entry.updated_by = request.user
            entry.vehicle_entry.save(update_fields=["status", "updated_by", "updated_at"])
        return Response(SalesDispatchGateOutSerializer(entry).data)


def decimal_or_none(value, places="0.001"):
    if value in (None, ""):
        return None
    return Decimal(str(value)).quantize(Decimal(places))
