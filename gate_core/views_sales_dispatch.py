from decimal import Decimal

from django.db import transaction
from django.db.models import Prefetch, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.http import HttpResponse
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
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
    SalesDispatchAdditionalWeight,
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
    VehicleArrivalStatus,
)
from gate_core.serializers_sales_dispatch import (
    SalesDispatchAdditionalWeightSerializer,
    SalesDispatchAdditionalWeightSetSerializer,
    SalesDispatchAttachmentSerializer,
    SalesDispatchAttachmentUploadSerializer,
    SalesDispatchBoxScanBatchCreateSerializer,
    SalesDispatchBoxScanCreateSerializer,
    SalesDispatchBoxScanSerializer,
    SalesDispatchChallanWeightSerializer,
    SalesDispatchDocumentSerializer,
    SalesDispatchGateOutCreateSerializer,
    SalesDispatchGateOutListSerializer,
    SalesDispatchGateOutSerializer,
    SalesDispatchGateOutUpdateSerializer,
    SalesDispatchGatepassPrintLogSerializer,
    SalesDispatchGatepassPrintSerializer,
    SalesDispatchGatepassReprintSerializer,
    SalesDispatchLockSerializer,
    SalesDispatchLockUpdateSerializer,
    SalesDispatchReasonSerializer,
)
from gate_core.services import sales_dispatch_docking as docking_builder
from gate_core.services.sales_dispatch_dispatch import (
    dispatch_arrival,
    mark_docking_dispatched,
)
from gate_core.services.user_scope import (
    assert_company_in_scope,
    user_company_ids,
    wants_all_companies,
)
from gate_core.services.sales_dispatch_box_match import (
    document_for_dispatch_session,
    document_invoices_item,
    remaining_invoiced_qty,
    resolve_scan_document,
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


def _sales_dispatch_base_queryset(**company_filter):
    # NOTE: the serializer and ``get_gatepass_readiness`` read these relations per row.
    # Anything they touch must be prefetched/joined here, and the consuming code must use
    # ``.all()`` (cache-friendly) rather than ``.filter()/.exists()/.count()`` (which
    # re-query and defeat the prefetch). The filtered Prefetches below let the consumers
    # read active-only rows straight from cache.
    return (
        SalesDispatchGateOut.objects
        .filter(is_active=True, **company_filter)
        .select_related(
            "company",
            "vehicle_entry",
            "vehicle_entry__weighment",  # readiness + weighment serializer fields (reverse O2O)
            "dispatch_plan",
            "vehicle",
            "vehicle__vehicle_type",
            "vehicle__transporter",
            "transporter",
            "driver",
            "arrival",
        )
        .prefetch_related(
            Prefetch(
                "documents",
                queryset=SalesDispatchGateOutDocument.objects
                .select_related("dispatch_plan")
                .prefetch_related(
                    Prefetch(
                        "items",
                        queryset=SalesDispatchGateOutItem.objects.select_related("document"),
                    )
                ),
            ),
            Prefetch(
                "items",
                queryset=SalesDispatchGateOutItem.objects.select_related("document"),
            ),
            # ``select_related`` the user FK each nested serializer reads for its
            # ``*_by_name`` field; otherwise serializing a load with N attachments /
            # weights / print logs fires N ``accounts_user`` queries (a 555-box load
            # was 569 queries — one user lookup per row).
            Prefetch(
                "attachments",
                queryset=SalesDispatchAttachment.objects.select_related("uploaded_by"),
            ),
            # Box scans are the high-cardinality relation (hundreds per load). The
            # detail payload uses ``SalesDispatchBoxScanDetailSerializer``, which drops
            # ``scanned_by_name`` — so we deliberately DON'T join ``accounts_user`` here
            # (that join pulled a full user row per scan). We DO select_related the
            # per-bill ``document`` so ``document_sap_doc_num`` doesn't re-query per scan.
            Prefetch(
                "box_scans",
                queryset=SalesDispatchBoxScan.objects.filter(is_active=True).select_related(
                    "document"
                ),
            ),
            Prefetch(
                "additional_weights",
                queryset=SalesDispatchAdditionalWeight.objects
                .filter(is_active=True)
                .select_related("created_by"),
            ),
            "scan_skip_requests",
            "partial_scan_requests",
            Prefetch(
                "gatepass_print_logs",
                queryset=SalesDispatchGatepassPrintLog.objects.select_related("printed_by"),
            ),
            "arrival__gate_ins",
        )
    )


def sales_dispatch_queryset(company):
    return _sales_dispatch_base_queryset(company=company)


def sales_dispatch_queryset_for_companies(company_ids):
    """Cross-company docking list (the user's companies aggregated)."""
    return _sales_dispatch_base_queryset(company_id__in=company_ids)


def _sales_dispatch_list_queryset(**company_filter):
    """Lean queryset for the dashboard *list* endpoint.

    Pairs with ``SalesDispatchGateOutListSerializer``: only the relations that slim
    serializer reads are joined/prefetched. The heavy relations the detail serializer
    needs (box scans, attachments, scan-skip / partial-scan requests, print logs,
    additional weights) are deliberately left off so the list neither loads nor
    serializes them. Single-object reads keep ``_sales_dispatch_base_queryset``.
    """
    return (
        SalesDispatchGateOut.objects
        .filter(is_active=True, **company_filter)
        .select_related(
            "company",
            "vehicle_entry",
            "vehicle_entry__weighment",  # gross/tare/net weight serializer fields
            "dispatch_plan",  # dispatch_date
            "vehicle",
            "transporter",
            "driver",
            "arrival",  # arrival_no grouping key on the list serializer
        )
        .prefetch_related(
            "documents",
            Prefetch(
                "items",
                queryset=SalesDispatchGateOutItem.objects.select_related("document"),
            ),
        )
    )


def sales_dispatch_list_queryset(company):
    return _sales_dispatch_list_queryset(company=company)


def sales_dispatch_list_queryset_for_companies(company_ids):
    """Cross-company docking dashboard list (the user's companies aggregated)."""
    return _sales_dispatch_list_queryset(company_id__in=company_ids)


def get_sales_dispatch_or_404(request, entry_id):
    # Resolve across all the user's companies (not the active Company-Code), so the
    # aggregated UI can act on any company's docking. Out-of-scope -> 404.
    return get_object_or_404(
        sales_dispatch_queryset_for_companies(user_company_ids(request)), id=entry_id
    )


def get_sales_dispatch_for_update_or_404(request, entry_id):
    return get_object_or_404(
        SalesDispatchGateOut.objects.select_for_update().filter(
            company_id__in=user_company_ids(request),
            is_active=True,
        ),
        id=entry_id,
    )


def _sales_dispatch_scan_queryset(**company_filter):
    """Minimal queryset for the box-scan POST hot path.

    A scan only needs the docking's status (``can_edit``), its company, and its
    bills + invoiced items to attribute the box and enforce the invoice cap (see
    ``resolve_scan_document`` / ``remaining_invoiced_qty``). It deliberately omits
    every heavy relation the detail queryset loads — crucially ``box_scans``,
    which grows with each scan, so loading it per scan makes the endpoint re-read
    all prior scans (O(n) per scan, O(n^2) across a truckload). The match helpers
    read box-scan totals via ``.filter()`` (a fresh query that ignores any
    prefetch cache anyway), so nothing is lost by dropping the prefetch. The scan
    endpoint serializes the created ``SalesDispatchBoxScan``, not the docking.
    """
    return (
        SalesDispatchGateOut.objects
        .filter(is_active=True, **company_filter)
        .select_related("company")
        .prefetch_related(
            "documents",
            Prefetch(
                "items",
                queryset=SalesDispatchGateOutItem.objects.select_related("document"),
            ),
        )
    )


def get_sales_dispatch_for_scan_or_404(request, entry_id):
    """Load a docking for the scan hot path with a minimal, scan-scoped queryset."""
    return get_object_or_404(
        _sales_dispatch_scan_queryset(company_id__in=user_company_ids(request)),
        id=entry_id,
    )


def get_sales_dispatch_dispatch_plans(entry):
    plans = []
    seen = set()
    if entry.dispatch_plan_id:
        plans.append(entry.dispatch_plan)
        seen.add(entry.dispatch_plan_id)

    for document in entry.documents.select_related("dispatch_plan").all():
        plan = document.dispatch_plan
        if not plan or plan.id in seen:
            continue
        plans.append(plan)
        seen.add(plan.id)
    return plans


def sync_sales_dispatch_transport_to_plans(entry, data, user):
    fields = {
        "eway_bill",
        "bilty_no",
        "bilty_date",
        "freight",
        "total_freight",
    }
    if not fields.intersection(data):
        return

    plans = get_sales_dispatch_dispatch_plans(entry)
    if not plans:
        return

    total_amount = data.get("total_freight") if "total_freight" in data else None
    if total_amount is None and "freight" in data:
        total_amount = data.get("freight")
    allocations = allocate_freight_to_dispatch_plans(plans, total_amount)

    for plan in plans:
        update_fields = ["updated_by", "updated_at"]
        for field in ("eway_bill", "bilty_no", "bilty_date"):
            if field in data:
                setattr(plan, field, data.get(field) or ("" if field != "bilty_date" else None))
                update_fields.append(field)

        if allocations is not None:
            allocated_amount = allocations.get(plan.id)
            plan.freight = allocated_amount
            plan.total_freight = allocated_amount
            update_fields.extend(["freight", "total_freight"])
        else:
            for field in ("freight", "total_freight"):
                if field in data:
                    setattr(plan, field, data.get(field))
                    update_fields.append(field)

        plan.updated_by = user
        plan.save(update_fields=list(dict.fromkeys(update_fields)))


def allocate_freight_to_dispatch_plans(plans, total_amount):
    if total_amount in (None, ""):
        return None

    total_amount = Decimal(str(total_amount)).quantize(Decimal("0.01"))
    if len(plans) == 1:
        return {plans[0].id: total_amount}

    weights = {}
    for plan in plans:
        weight = (
            positive_decimal(plan.total_litres)
            or positive_decimal(plan.invoice_weight)
            or positive_decimal(plan.invoice_amount)
            or Decimal("1")
        )
        weights[plan.id] = weight

    total_weight = sum(weights.values(), Decimal("0")) or Decimal(len(plans))
    remaining = total_amount
    allocations = {}
    for plan in plans[:-1]:
        amount = (total_amount * weights[plan.id] / total_weight).quantize(Decimal("0.01"))
        allocations[plan.id] = amount
        remaining -= amount
    allocations[plans[-1].id] = remaining.quantize(Decimal("0.01"))
    return allocations


def positive_decimal(value):
    if value in (None, ""):
        return None
    decimal_value = Decimal(str(value))
    return decimal_value if decimal_value > 0 else None


def sync_sales_dispatch_bilty_attachment_to_plans(entry, attachment, user):
    if attachment.attachment_type != SalesDispatchAttachmentType.BILTY:
        return
    for plan in get_sales_dispatch_dispatch_plans(entry):
        plan.bilty_attachment = attachment.file
        plan.bilty_attachment_name = attachment.original_filename or attachment.file.name
        plan.updated_by = user
        plan.save(
            update_fields=[
                "bilty_attachment",
                "bilty_attachment_name",
                "updated_by",
                "updated_at",
            ]
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


def _pending_dispatch_plan_base(company_filter):
    # ``company_filter`` is {"company": c} for one company or {"company_id__in": ids}
    # for the aggregated cross-company view. It is applied to ALL the exclusion
    # subqueries too, so the cross-company exclusion stays bill-accurate.
    active_plan_ids = SalesDispatchGateOut.objects.filter(
        **company_filter,
        is_active=True,
        dispatch_plan_id__isnull=False,
        status__in=SALES_DISPATCH_ACTIVE_STATUSES,
    ).values_list("dispatch_plan_id", flat=True)
    active_document_plan_ids = SalesDispatchGateOutDocument.objects.filter(
        **company_filter,
        is_active=True,
        dispatch_plan_id__isnull=False,
        sales_dispatch__is_active=True,
        sales_dispatch__status__in=SALES_DISPATCH_ACTIVE_STATUSES,
    ).values_list("dispatch_plan_id", flat=True)
    active_document_doc_entries = SalesDispatchGateOutDocument.objects.filter(
        **company_filter,
        is_active=True,
        document_type=SalesDispatchDocumentType.INVOICE,
        sales_dispatch__is_active=True,
        sales_dispatch__status__in=SALES_DISPATCH_ACTIVE_STATUSES,
    ).values_list("sap_doc_entry", flat=True)
    return (
        DispatchPlan.objects
        .filter(
            **company_filter,
            is_active=True,
            booking_status=DispatchPlanStatus.BOOKED,
            # Bill-accurate: the plan must hold an unconsumed cover on a live
            # (non-retired, COMPLETED) dispatch gate-in. A returning truck's new
            # bill has no such cover until its own fresh empty-vehicle-in, so it
            # stays an expected dispatch vehicle instead of jumping to the board.
            empty_in_covers__is_active=True,
            empty_in_covers__consumed_at__isnull=True,
            empty_in_covers__empty_vehicle_gate_in__is_active=True,
            empty_in_covers__empty_vehicle_gate_in__retired_at__isnull=True,
            empty_in_covers__empty_vehicle_gate_in__vehicle_entry__status="COMPLETED",
        )
        .exclude(id__in=active_plan_ids)
        .exclude(id__in=active_document_plan_ids)
        .exclude(sap_invoice_doc_entry__in=active_document_doc_entries)
        .distinct()
        .select_related(
            "company",
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


def pending_dispatch_plan_queryset(company):
    return _pending_dispatch_plan_base({"company": company})


def pending_dispatch_plan_queryset_for_companies(company_ids):
    """Cross-company expected-dispatch list (the user's companies aggregated)."""
    return _pending_dispatch_plan_base({"company_id__in": company_ids})


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
        "company": primary.company_id,
        "company_code": primary.company.code,
        "company_name": primary.company.name,
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
        "customer_code": join_unique(plan.customer_code for plan in plans),
        "customer_name": join_unique(plan.customer_name for plan in plans),
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
        base = (
            pending_dispatch_plan_queryset_for_companies(user_company_ids(request))
            if wants_all_companies(request)
            else pending_dispatch_plan_queryset(request.company.company)
        )
        qs = apply_pending_dispatch_plan_filters(base, request.query_params)
        limit = min(int(request.query_params.get("limit") or 200), 1000)
        groups = serialize_pending_booking_groups(qs[:limit])
        return Response(groups)


class SalesDispatchDocumentListView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "gate_core.can_create_sales_dispatch_out"

    def get(self, request):
        document_type = request.query_params.get("document_type", "ALL")
        filters = {
            "search": request.query_params.get("search", ""),
            "from_date": request.query_params.get("from_date"),
            "to_date": request.query_params.get("to_date"),
            "branch": request.query_params.get("branch", ""),
            "booking_status": request.query_params.get("booking_status", "all"),
            "limit": request.query_params.get("limit", 100),
        }
        try:
            if wants_all_companies(request):
                # Cross-company manual search: each company is its own HANA schema,
                # so fan the SAP read out over the user's companies and merge (the
                # company selector is a decorator). A booked invoice carries its
                # plan, so docking it still resolves the company from the record.
                from company.models import Company

                documents = []
                for company in Company.objects.filter(
                    id__in=user_company_ids(request)
                ).order_by("code"):
                    documents.extend(
                        SalesDispatchDocumentService(company).list_documents(
                            document_type, filters
                        )
                    )
            else:
                documents = SalesDispatchDocumentService(
                    request.company.company
                ).list_documents(document_type, filters)
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
        # The manual SAP search is cross-company, so a picked invoice may belong to
        # a sibling company (a different HANA schema). Resolve the document from the
        # company that actually has it -- active company first, then the user's other
        # companies -- instead of always the active header. Keying only off the
        # header would 404 the sibling invoice, or (worse) return the active
        # company's DIFFERENT bill that happens to share the doc_entry.
        from company.models import Company

        active = request.company.company
        companies = [active] + list(
            Company.objects.filter(id__in=user_company_ids(request))
            .exclude(id=active.id)
            .order_by("code")
        )
        try:
            document = None
            for company in companies:
                document = SalesDispatchDocumentService(company).get_document(
                    document_type, doc_entry
                )
                if document:
                    break
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
        base = (
            sales_dispatch_list_queryset_for_companies(user_company_ids(request))
            if wants_all_companies(request)
            else sales_dispatch_list_queryset(request.company.company)
        )
        qs = apply_sales_dispatch_filters(base, request.query_params)
        return Response(SalesDispatchGateOutListSerializer(qs, many=True).data)

    def post(self, request):
        serializer = SalesDispatchGateOutCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        document_inputs = data["documents"]
        # The cross-company docking board can start a docking for a sibling
        # company's booked bills, so the owning company is resolved from the
        # plans being docked (not the active Company-Code header) -- the SAP
        # invoice lives in that company's schema and the records belong to it.
        company = self._resolve_docking_company(request, document_inputs)
        service = SalesDispatchDocumentService(company)
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

        validation_error = self._validate_document_set(company, documents)
        if validation_error:
            return validation_error

        duplicate_response = self._duplicate_response(company, documents)
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
                company=company,
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

        # De-fragment: a vehicle+company keeps ONE open docking. If this truck
        # already has an open (DOCKED, pre-photo-lock) docking for the same
        # gate-in, append these bills to it instead of creating a second
        # docking -- the same append primitive as the late-link auto-merge, so
        # box scans stay intact. Only invoice loads where every document
        # resolved a plan qualify; stock-transfer / manual-SAP / branch-mismatch
        # loads fall through to a fresh docking (guards live in
        # ``add_plan_to_open_docking``).
        reused = self._append_to_open_docking(
            dispatch_plan,
            primary_document,
            documents,
            dispatch_plans_by_doc_entry,
            service,
            request.user,
        )
        if reused is not None:
            response_data = SalesDispatchGateOutSerializer(reused).data
            response_data["warnings"] = warnings
            return Response(response_data, status=status.HTTP_200_OK)

        with transaction.atomic():
            header_snapshot = docking_builder.header_snapshot(documents)
            if data.get("eway_bill"):
                header_snapshot["eway_bill"] = data.get("eway_bill")

            vehicle_entry = VehicleEntry.objects.create(
                entry_no=SalesDispatchGateOut.generate_vehicle_entry_no(),
                company=company,
                vehicle=vehicle,
                driver=driver,
                entry_type="SALES_DISPATCH",
                status="IN_PROGRESS",
                remarks=data.get("remarks", ""),
                created_by=request.user,
                updated_by=request.user,
            )
            source_entry = getattr(dispatch_plan, "linked_vehicle_entry", None)
            self._copy_empty_vehicle_tare_weighment(
                source_entry=source_entry,
                target_entry=vehicle_entry,
                user=request.user,
            )
            # Thread this docking onto the physical truck trip (cross-company arrival).
            arrival = None
            if source_entry is not None and hasattr(source_entry, "empty_vehicle_gate_in"):
                arrival = source_entry.empty_vehicle_gate_in.arrival
            entry = SalesDispatchGateOut.objects.create(
                company=company,
                entry_no=SalesDispatchGateOut.generate_entry_no(),
                vehicle_entry=vehicle_entry,
                dispatch_plan=dispatch_plan,
                arrival=arrival,
                vehicle=vehicle,
                transporter=transporter,
                driver=driver,
                **header_snapshot,
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
                    company=company,
                    dispatch_plan=dispatch_plans_by_doc_entry.get(document["doc_entry"]),
                    created_by=request.user,
                    updated_by=request.user,
                    **docking_builder.document_snapshot(document),
                )
                next_line_num = docking_builder.create_items(
                    entry,
                    document_row,
                    document,
                    request.user,
                    next_line_num,
                )
            transport_data = {
                field: data[field]
                for field in (
                    "eway_bill",
                    "bilty_no",
                    "bilty_date",
                    "freight",
                    "total_freight",
                )
                if field in request.data and field in data
            }
            sync_sales_dispatch_transport_to_plans(entry, transport_data, request.user)

            # The physical truck is now being loaded for this trip.
            if arrival is not None and arrival.status == VehicleArrivalStatus.INSIDE:
                arrival.status = VehicleArrivalStatus.LOADING
                arrival.updated_by = request.user
                arrival.save(update_fields=["status", "updated_by", "updated_at"])

        response_data = SalesDispatchGateOutSerializer(entry).data
        response_data["warnings"] = warnings
        return Response(response_data, status=status.HTTP_201_CREATED)

    def _append_to_open_docking(
        self,
        dispatch_plan,
        primary_document,
        documents,
        dispatch_plans_by_doc_entry,
        document_service,
        user,
    ):
        """Append these bills to the truck's existing open docking, or ``None``.

        Keeps one open docking per vehicle+company: when the primary bill's truck
        already has an open (``DOCKED``) docking for its gate-in, fold every
        requested invoice into it (reusing ``add_plan_to_open_docking``) and
        return that docking. Returns ``None`` -- so the caller creates a fresh
        docking -- when there is no such open docking, any document is not an
        invoice with a resolved dispatch plan, or the primary bill can't be
        merged (branch mismatch / already docked / no longer ``DOCKED``).
        """
        if dispatch_plan is None:
            return None
        if any(
            document["document_type"] != SalesDispatchDocumentType.INVOICE
            or dispatch_plans_by_doc_entry.get(document["doc_entry"]) is None
            for document in documents
        ):
            return None

        existing = docking_builder.find_open_docking_for_plan(dispatch_plan)
        if existing is None:
            return None

        with transaction.atomic():
            merged = docking_builder.add_plan_to_open_docking(
                existing, dispatch_plan, user, document_service=document_service
            )
            if merged is None:
                return None
            for document in documents:
                if document["doc_entry"] == primary_document["doc_entry"]:
                    continue
                docking_builder.add_plan_to_open_docking(
                    existing,
                    dispatch_plans_by_doc_entry[document["doc_entry"]],
                    user,
                    document_service=document_service,
                )
            existing.refresh_from_db()
            return existing

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

    def _resolve_docking_company(self, request, document_inputs):
        """The company that owns this docking.

        The cross-company docking board can start a docking for a sibling
        company's booked bills, so resolve the owning company from the dispatch
        plans being docked (asserting the user belongs to it) instead of the
        active ``Company-Code`` header. A manual SAP-search docking carries no
        plan id and stays on the active company.
        """
        plan_ids = [
            d["dispatch_plan_id"] for d in document_inputs if d.get("dispatch_plan_id")
        ]
        if not plan_ids:
            return request.company.company
        companies = {
            plan.company_id: plan.company
            for plan in DispatchPlan.objects.filter(
                id__in=plan_ids, is_active=True
            ).select_related("company")
        }
        if not companies:
            return request.company.company
        if len(companies) > 1:
            raise ValidationError(
                "All bills in one docking must belong to the same company."
            )
        ((company_id, company),) = companies.items()
        assert_company_in_scope(request, company_id)
        return company

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


class SalesDispatchGateOutDetailView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = {
        "GET": "gate_core.can_view_sales_dispatch_out",
        "PATCH": "gate_core.can_edit_sales_dispatch_out",
    }

    def get(self, request, entry_id):
        entry = get_sales_dispatch_or_404(request, entry_id)
        return Response(SalesDispatchGateOutSerializer(entry).data)

    def patch(self, request, entry_id):
        serializer = SalesDispatchGateOutUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            entry = get_sales_dispatch_for_update_or_404(request, entry_id)
            if not can_edit(entry):
                return Response(
                    {"detail": "This Docking entry cannot be edited in its current status."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            for field, value in serializer.validated_data.items():
                setattr(entry, field, value)
            entry.updated_by = request.user
            entry.save()
            sync_sales_dispatch_transport_to_plans(entry, serializer.validated_data, request.user)

        entry = get_sales_dispatch_or_404(request, entry_id)
        return Response(SalesDispatchGateOutSerializer(entry).data)


class SalesDispatchGateOutByVehicleEntryView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "gate_core.can_view_sales_dispatch_out"

    def get(self, request, vehicle_entry_id):
        # Cross-company: the scan / gatepass / weighment / attachments pages load
        # a docking by its vehicle-entry id, and the board can open a sibling
        # company's docking while another company is active. Resolve across the
        # user's companies (a vehicle entry belongs to one company) rather than
        # the active header, or those pages 404 to a blank screen.
        entry = (
            sales_dispatch_queryset_for_companies(user_company_ids(request))
            .filter(vehicle_entry_id=vehicle_entry_id)
            .order_by("-created_at")
            .first()
        )
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
        entry = get_sales_dispatch_or_404(request, entry_id)
        return Response(SalesDispatchAttachmentSerializer(entry.attachments.all(), many=True).data)

    def post(self, request, entry_id):
        entry = get_sales_dispatch_or_404(request, entry_id)
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
        sync_sales_dispatch_bilty_attachment_to_plans(entry, attachment, request.user)

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
        entry = get_sales_dispatch_or_404(request, entry_id)
        scans = (
            entry.box_scans
            .filter(is_active=True)
            .select_related("box", "scan_log", "scanned_by", "document")
        )
        return Response(SalesDispatchBoxScanSerializer(scans, many=True).data)

    def post(self, request, entry_id):
        ensure_sales_dispatch_scan_permission(request.user)
        # Scan hot path: load only company + bills + invoiced items, never the
        # box_scans/attachments/print-log relations (which grow per scan). See
        # get_sales_dispatch_for_scan_or_404.
        entry = get_sales_dispatch_for_scan_or_404(request, entry_id)
        if not can_edit(entry):
            return Response(
                {"detail": "Box scans cannot be changed in this Docking status."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = SalesDispatchBoxScanCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        barcode_raw = serializer.validated_data["barcode_raw"]

        scan_service = ScanService(company_code=entry.company.code)
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
            # dispatch_session is read per scan by resolve_scan_document (origin-bill
            # attribution); join it here so it isn't a lazy query on every scan.
            Box.objects.select_related("pallet", "dispatch_session"),
            id=scan_result["entity_id"],
            company=entry.company,
        )
        if box.status not in (BoxStatus.ACTIVE, BoxStatus.PARTIAL):
            return Response(
                {"detail": f"Box {box.box_barcode} is {box.status} and cannot be dispatched."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Resolve the one bill (document) this box is scanned against. When the
        # operator scans into a specific bill we honour that bill (and reject a box
        # whose item the bill doesn't invoice); otherwise auto-resolve so a box whose
        # item appears on several bills isn't counted against all of them.
        document_id = serializer.validated_data.get("document")
        if document_id is not None:
            document = get_object_or_404(
                SalesDispatchGateOutDocument,
                id=document_id,
                sales_dispatch=entry,
                is_active=True,
            )
            if not document_invoices_item(entry, document.id, box.item_code):
                return Response(
                    {
                        "detail": (
                            f"Box {box.box_barcode} (item {box.item_code}) is not on "
                            f"bill {document.sap_doc_num}."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            document = resolve_scan_document(entry, item_code=box.item_code, box=box)

        # A box already scanned on this docking is a duplicate, not a new dispatch —
        # report it without re-checking the invoice cap (it already counts once).
        existing = (
            SalesDispatchBoxScan.objects
            .filter(sales_dispatch=entry, box_barcode=box.box_barcode)
            .first()
        )
        if existing and existing.is_active:
            response_data = SalesDispatchBoxScanSerializer(existing).data
            response_data["duplicate"] = True
            return Response(response_data, status=status.HTTP_200_OK)

        # Never scan more than the bill's invoiced quantity for this item.
        if (
            document is not None
            and remaining_invoiced_qty(entry, document.id, box.item_code) <= 0
        ):
            return Response(
                {
                    "detail": (
                        f"Bill {document.sap_doc_num} already has the full invoiced "
                        f"quantity of {box.item_code} scanned."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        scan, created = SalesDispatchBoxScan.objects.get_or_create(
            sales_dispatch=entry,
            box_barcode=box.box_barcode,
            defaults={
                "company": entry.company,
                "document": document,
                "box": box,
                "scan_log_id": scan_result["scan_id"],
                "barcode_raw": barcode_raw,
                "item_code": box.item_code,
                "item_name": box.item_name,
                "batch_number": box.batch_number,
                "quantity": box.qty,
                "uom": box.uom,
                "net_weight": box.n_weight,
                "gross_weight": box.g_weight,
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
            scan.document = document
            scan.box = box
            scan.scan_log_id = scan_result["scan_id"]
            scan.barcode_raw = barcode_raw
            scan.item_code = box.item_code
            scan.item_name = box.item_name
            scan.batch_number = box.batch_number
            scan.quantity = box.qty
            scan.uom = box.uom
            scan.net_weight = box.n_weight
            scan.gross_weight = box.g_weight
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


class SalesDispatchBoxScanBatchView(APIView):
    """Submit a batch of locally-scanned box barcodes in a single request.

    The client scans all boxes into local state and submits them here at once.
    Each barcode is validated independently with the same rules as the single
    scan endpoint. Valid boxes are saved; invalid ones are returned in ``failed``
    with a machine-readable ``reason`` and a human-readable ``detail`` so the
    operator can fix or drop them and re-submit only the remaining entries
    through this same endpoint.

    Always returns 200 with both ``saved`` and ``failed`` (partial success);
    a 4xx is reserved for whole-request problems (permission, status, bad body).
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = {}

    def post(self, request, entry_id):
        ensure_sales_dispatch_scan_permission(request.user)
        entry = get_sales_dispatch_or_404(request, entry_id)
        if not can_edit(entry):
            return Response(
                {"detail": "Box scans cannot be changed in this Docking status."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = SalesDispatchBoxScanBatchCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        barcodes = serializer.validated_data["barcodes"]

        scan_service = ScanService(company_code=entry.company.code)
        device_info = request.META.get("HTTP_USER_AGENT", "")[:500]

        # Barcodes already saved (active) on this entry, plus those resolved
        # earlier in this same batch — both count as duplicates.
        existing_barcodes = set(
            entry.box_scans.filter(is_active=True).values_list("box_barcode", flat=True)
        )
        seen_in_batch = set()

        saved_scans = []
        failed = []

        def fail(barcode_raw, reason, detail):
            failed.append({"barcode_raw": barcode_raw, "reason": reason, "detail": detail})

        with transaction.atomic():
            for raw in barcodes:
                barcode_raw = (raw or "").strip()
                if not barcode_raw:
                    fail(raw, "EMPTY", "Empty barcode.")
                    continue

                scan_result = scan_service.process_scan(
                    barcode_raw=barcode_raw,
                    scan_type="SHIP",
                    context_ref_type="SALES_DISPATCH",
                    context_ref_id=entry.id,
                    user=request.user,
                    device_info=device_info,
                )

                if scan_result["result"] != ScanResult.SUCCESS:
                    fail(barcode_raw, "UNKNOWN_BARCODE", "Box barcode was not found.")
                    continue
                if scan_result["entity_type"] != EntityType.BOX:
                    fail(
                        barcode_raw,
                        "NOT_A_BOX",
                        "Only box barcodes can be scanned for Docking.",
                    )
                    continue

                box = (
                    Box.objects.select_related("pallet")
                    .filter(id=scan_result["entity_id"], company=entry.company)
                    .first()
                )
                if box is None:
                    fail(barcode_raw, "UNKNOWN_BARCODE", "Box barcode was not found.")
                    continue
                if box.status not in (BoxStatus.ACTIVE, BoxStatus.PARTIAL):
                    fail(
                        barcode_raw,
                        "INVALID_STATUS",
                        f"Box {box.box_barcode} is {box.status} and cannot be dispatched.",
                    )
                    continue
                if box.box_barcode in existing_barcodes or box.box_barcode in seen_in_batch:
                    fail(
                        barcode_raw,
                        "DUPLICATE",
                        f"Box {box.box_barcode} is already scanned for this Docking entry.",
                    )
                    continue

                fields = {
                    "company": entry.company,
                    "box": box,
                    "scan_log_id": scan_result["scan_id"],
                    "barcode_raw": barcode_raw,
                    "item_code": box.item_code,
                    "item_name": box.item_name,
                    "batch_number": box.batch_number,
                    "quantity": box.qty,
                    "uom": box.uom,
                    "net_weight": box.n_weight,
                    "gross_weight": box.g_weight,
                    "box_status": box.status,
                    "warehouse_code": box.current_warehouse,
                    "pallet_code": box.pallet.pallet_id if box.pallet else "",
                    "scanned_by": request.user,
                    "updated_by": request.user,
                }
                # get_or_create handles a previously soft-deleted scan: the unique
                # constraint is on (sales_dispatch, box_barcode) regardless of
                # is_active, so reactivate rather than hit an IntegrityError.
                scan, created = SalesDispatchBoxScan.objects.get_or_create(
                    sales_dispatch=entry,
                    box_barcode=box.box_barcode,
                    defaults={**fields, "created_by": request.user},
                )
                if not created:
                    if scan.is_active:
                        # Raced with another save within the request lifetime.
                        fail(
                            barcode_raw,
                            "DUPLICATE",
                            f"Box {box.box_barcode} is already scanned for this Docking entry.",
                        )
                        continue
                    for field, value in fields.items():
                        setattr(scan, field, value)
                    scan.is_active = True
                    scan.scanned_at = timezone.now()
                    scan.save()

                seen_in_batch.add(box.box_barcode)
                saved_scans.append(scan)

        return Response(
            {
                "saved": SalesDispatchBoxScanSerializer(saved_scans, many=True).data,
                "saved_count": len(saved_scans),
                "failed": failed,
                "failed_count": len(failed),
                "total": len(barcodes),
            }
        )


class SalesDispatchBoxScanDetailView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = {}

    def delete(self, request, entry_id, scan_id):
        ensure_sales_dispatch_scan_permission(request.user)
        entry = get_sales_dispatch_or_404(request, entry_id)
        if not can_edit(entry):
            return Response(
                {"detail": "Box scans cannot be changed in this Docking status."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        scan = get_object_or_404(
            SalesDispatchBoxScan,
            id=scan_id,
            sales_dispatch=entry,
            company=entry.company,
            is_active=True,
        )
        scan.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


def _sales_dispatch_doc_keys(entry):
    """Collect the SAP document identifiers carried by a docking entry.

    Returns (doc_nums, doc_entries) as sets of trimmed strings, gathered from the
    entry header and each linked document, so we can match a barcode dispatch
    session that was scanned for the same SAP bill/invoice.
    """
    doc_nums = set()
    doc_entries = set()

    def add(num, ent):
        if num:
            doc_nums.add(str(num).strip())
        if ent not in (None, "", 0, "0"):
            doc_entries.add(str(ent).strip())

    add(entry.sap_doc_num, entry.sap_doc_entry)
    for document in entry.documents.all():
        add(document.sap_doc_num, document.sap_doc_entry)
    return doc_nums, doc_entries


def find_barcode_dispatch_sessions(company, entry):
    """Find barcode-module dispatch sessions for the same company + SAP document.

    Excludes cancelled sessions. Matches on SAP doc number, bill number, or SAP
    doc entry so it works regardless of which identifier each side captured.
    """
    from barcode.models import DispatchSession, DispatchSessionStatus

    doc_nums, doc_entries = _sales_dispatch_doc_keys(entry)
    if not doc_nums and not doc_entries:
        return DispatchSession.objects.none()

    match = Q()
    if doc_nums:
        match |= Q(sap_doc_num__in=doc_nums) | Q(bill_number__in=doc_nums)
    if doc_entries:
        match |= Q(sap_doc_entry__in=doc_entries)

    return (
        DispatchSession.objects
        .filter(company=company)
        .filter(match)
        .exclude(status=DispatchSessionStatus.CANCELLED)
        .order_by("-updated_at")
    )


def _serialize_barcode_dispatch_session(session):
    from barcode.models import DispatchScannedUnitStatus

    units = (
        session.scanned_units
        .exclude(scan_status=DispatchScannedUnitStatus.REMOVED)
        .select_related("box")
        .order_by("created_at")
    )
    boxes = [
        {
            "id": unit.id,
            "barcode": unit.barcode_value,
            "entity_type": unit.entity_type,
            "item_code": unit.material_code,
            "item_name": unit.box.item_name if unit.box else "",
            "batch_number": unit.batch_number,
            "quantity": str(unit.qty),
            "uom": unit.uom,
            "scan_status": unit.scan_status,
            "box_status": unit.box.status if unit.box else "",
            "scanned_at": unit.created_at,
        }
        for unit in units
    ]
    return {
        "session_id": session.id,
        "bill_number": session.bill_number,
        "sap_doc_num": session.sap_doc_num,
        "status": session.status,
        "customer_code": session.customer_code,
        "customer_name": session.customer_name,
        "total_scanned_qty": str(session.total_scanned_qty),
        "scanned_at": session.updated_at,
        "box_count": len(boxes),
        "boxes": boxes,
    }


class SalesDispatchBarcodeScansView(APIView):
    """Surface boxes already scanned in the barcode module's dispatch flow.

    Some operators still scan dispatches in the old barcode module before the
    docking module existed. This lets a docking entry look up — by the shared SAP
    document — whether the same bill was already scanned there, so the operator
    can review those boxes instead of re-scanning. Read-only; creates nothing.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = {"GET": "gate_core.can_view_sales_dispatch_out"}

    def get(self, request, entry_id):
        entry = get_sales_dispatch_or_404(request, entry_id)
        sessions = find_barcode_dispatch_sessions(entry.company, entry)
        data = [_serialize_barcode_dispatch_session(session) for session in sessions]
        return Response(
            {
                "matched": bool(data),
                "session_count": len(data),
                "box_count": sum(item["box_count"] for item in data),
                "sessions": data,
            }
        )


def _box_scan_fields_from_box(box, company, user, barcode_raw="", document=None):
    """Field map for a SalesDispatchBoxScan built from a barcode Box (no created_by)."""
    return {
        "company": company,
        "document": document,
        "box": box,
        "barcode_raw": barcode_raw or box.box_barcode,
        "item_code": box.item_code,
        "item_name": box.item_name,
        "batch_number": box.batch_number,
        "quantity": box.qty,
        "uom": box.uom,
        "net_weight": box.n_weight,
        "gross_weight": box.g_weight,
        "box_status": box.status,
        "warehouse_code": box.current_warehouse,
        "pallet_code": box.pallet.pallet_id if box.pallet else "",
        "scanned_by": user,
        "updated_by": user,
    }


class SalesDispatchBarcodeScansImportView(APIView):
    """Import boxes from one or more matched barcode dispatch sessions into this
    docking entry, so operators who scanned in the barcode module don't re-scan.

    Only sessions that match this entry's SAP document are importable. Each box
    becomes a SalesDispatchBoxScan (deduped by barcode). Boxes with status
    ACTIVE/PARTIAL/DISPATCHED are imported (DISPATCHED is expected here since
    completing the barcode-module session marks its boxes dispatched); boxes that
    are unlinked, in another non-dispatch status, or already on the entry are skipped.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = {"POST": "gate_core.can_edit_sales_dispatch_out"}

    def post(self, request, entry_id):
        from barcode.models import DispatchScanEntityType, DispatchScannedUnitStatus

        ensure_sales_dispatch_scan_permission(request.user)
        entry = get_sales_dispatch_or_404(request, entry_id)
        if not can_edit(entry):
            return Response(
                {"detail": "Box scans cannot be changed in this Docking status."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        session_ids = request.data.get("session_ids")
        if not isinstance(session_ids, list) or not session_ids:
            return Response(
                {"detail": "Provide session_ids (a non-empty list) to import."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            session_ids = [int(value) for value in session_ids]
        except (TypeError, ValueError):
            return Response(
                {"detail": "session_ids must be integers."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Only sessions that actually match this entry's SAP document are allowed.
        sessions = list(
            find_barcode_dispatch_sessions(entry.company, entry).filter(
                id__in=session_ids
            )
        )
        if not sessions:
            return Response(
                {"detail": "No matching barcode sessions found for this entry."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        documents = list(entry.documents.all())
        imported = 0
        skipped = 0
        with transaction.atomic():
            for session in sessions:
                # A barcode session is scanned against one bill, so every box it
                # imports belongs to that bill's document on the load.
                session_document = document_for_dispatch_session(documents, session)
                units = (
                    session.scanned_units
                    .filter(entity_type=DispatchScanEntityType.BOX)
                    .exclude(scan_status=DispatchScannedUnitStatus.REMOVED)
                    .select_related("box", "box__pallet")
                )
                for unit in units:
                    box = unit.box
                    if box is None or box.status not in (
                        BoxStatus.ACTIVE,
                        BoxStatus.PARTIAL,
                        BoxStatus.DISPATCHED,
                    ):
                        skipped += 1
                        continue
                    fields = _box_scan_fields_from_box(
                        box,
                        entry.company,
                        request.user,
                        unit.barcode_value,
                        document=session_document,
                    )
                    scan, created = SalesDispatchBoxScan.objects.get_or_create(
                        sales_dispatch=entry,
                        box_barcode=box.box_barcode,
                        defaults={**fields, "created_by": request.user},
                    )
                    if created:
                        imported += 1
                    elif not scan.is_active:
                        for field, value in fields.items():
                            setattr(scan, field, value)
                        scan.is_active = True
                        scan.scanned_at = timezone.now()
                        scan.save()
                        imported += 1
                    else:
                        skipped += 1

        return Response(
            {
                "imported": imported,
                "skipped": skipped,
                "total": entry.box_scans.filter(is_active=True).count(),
            }
        )


class SalesDispatchGatepassPreviewView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "gate_core.can_print_sales_dispatch_gatepass"

    def post(self, request, entry_id):
        entry = get_sales_dispatch_or_404(request, entry_id)
        readiness = get_gatepass_readiness(entry)
        if readiness["ready"] and entry.status == SalesDispatchGateOutStatus.PHOTO_ATTACHED:
            entry.status = SalesDispatchGateOutStatus.READY_FOR_GATEPASS
            entry.updated_by = request.user
            entry.save(update_fields=["status", "updated_by", "updated_at"])
        data = SalesDispatchGateOutSerializer(entry).data
        data["gatepass_readiness"] = readiness
        return Response(data)


def ensure_partial_dispatch_cleared(entry):
    """Block gatepass printing while a bill on the load is partially dispatched
    without an approval + credit note."""
    from gate_core.models import (
        PartialDispatchApproval,
        PartialDispatchApprovalStatus,
        SalesDispatchAttachmentType,
    )

    approvals = PartialDispatchApproval.objects.filter(sales_dispatch=entry, is_active=True)
    if approvals.filter(status=PartialDispatchApprovalStatus.PENDING).exists():
        raise ValueError(
            "A partial-dispatch approval is still pending for a bill on this load."
        )
    if approvals.filter(status=PartialDispatchApprovalStatus.APPROVED).exists():
        has_credit_note = entry.attachments.filter(
            attachment_type=SalesDispatchAttachmentType.CREDIT_NOTE
        ).exists()
        missing_number = approvals.filter(
            status=PartialDispatchApprovalStatus.APPROVED, credit_note_no=""
        ).exists()
        if not has_credit_note or missing_number:
            raise ValueError(
                "A credit note (attachment + number) is required before printing the "
                "gatepass for a partial dispatch."
            )


class SalesDispatchGatepassPrintView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "gate_core.can_print_sales_dispatch_gatepass"

    def post(self, request, entry_id):
        serializer = SalesDispatchGatepassPrintSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            entry = get_sales_dispatch_for_update_or_404(request, entry_id)
            # Lock follows the docking's company, not the active header.
            locked_response = sales_dispatch_locked_response(entry.company)
            if locked_response:
                return locked_response
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
                ensure_partial_dispatch_cleared(entry)
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
        serializer = SalesDispatchGatepassReprintSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            entry = get_sales_dispatch_for_update_or_404(request, entry_id)
            locked_response = sales_dispatch_locked_response(entry.company)
            if locked_response:
                return locked_response
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
        entry = get_sales_dispatch_or_404(request, entry_id)
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
        entry = get_sales_dispatch_or_404(request, entry_id)
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
        entry = get_sales_dispatch_or_404(request, entry_id)
        locked_response = sales_dispatch_locked_response(entry.company)
        if locked_response:
            return locked_response
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


class SalesDispatchChallanWeightView(APIView):
    """Set or clear the operator-entered challan weight.

    The SAP document weight (``total_weight``) is often missing or wrong, so the gate
    operator records a reliable challan weight to compare the loaded net weight against.
    Allowed any time before dispatch — including after gatepass print/commit, which is when
    the operator is actually at the weighbridge — but not after the entry is finalised.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "gate_core.can_edit_sales_dispatch_out"

    def post(self, request, entry_id):
        serializer = SalesDispatchChallanWeightSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        challan_weight = serializer.validated_data["challan_weight"]

        with transaction.atomic():
            entry = get_sales_dispatch_for_update_or_404(request, entry_id)
            if entry.status in (
                SalesDispatchGateOutStatus.DISPATCHED,
                SalesDispatchGateOutStatus.REJECTED,
                SalesDispatchGateOutStatus.CANCELLED,
            ):
                return Response(
                    {
                        "detail": (
                            "Challan weight cannot be changed after the Docking entry is "
                            "dispatched, rejected, or cancelled."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            entry.challan_weight = challan_weight
            if challan_weight is None:
                entry.challan_weight_by = None
                entry.challan_weight_at = None
            else:
                entry.challan_weight_by = request.user
                entry.challan_weight_at = timezone.now()
            entry.updated_by = request.user
            entry.save(
                update_fields=[
                    "challan_weight",
                    "challan_weight_by",
                    "challan_weight_at",
                    "updated_by",
                    "updated_at",
                ]
            )

        entry = get_sales_dispatch_or_404(request, entry_id)
        return Response(SalesDispatchGateOutSerializer(entry).data)


class SalesDispatchAdditionalWeightView(APIView):
    """List and replace the additional-weight line items for a Docking entry.

    These are operator-entered weights of non-goods items loaded on the truck
    (packaging, cardboard, dunnage, securing material). The gate user subtracts
    their total from the net loaded weight (gross - tare) to estimate the actual
    goods weight and reconcile it against the invoice/challan weight. Allowed any
    time before dispatch (including after gatepass print/commit, when the operator
    is at the weighbridge). Never affects the weighment or gross/net figures.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = {
        "GET": "gate_core.can_view_sales_dispatch_out",
        "PUT": "gate_core.can_edit_sales_dispatch_out",
    }

    def get(self, request, entry_id):
        entry = get_sales_dispatch_or_404(request, entry_id)
        return Response(
            SalesDispatchAdditionalWeightSerializer(
                entry.additional_weights.filter(is_active=True),
                many=True,
            ).data
        )

    def put(self, request, entry_id):
        serializer = SalesDispatchAdditionalWeightSetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        items = serializer.validated_data["items"]

        with transaction.atomic():
            entry = get_sales_dispatch_for_update_or_404(request, entry_id)
            if entry.status in (
                SalesDispatchGateOutStatus.DISPATCHED,
                SalesDispatchGateOutStatus.REJECTED,
                SalesDispatchGateOutStatus.CANCELLED,
            ):
                return Response(
                    {
                        "detail": (
                            "Additional weights cannot be changed after the Docking entry "
                            "is dispatched, rejected, or cancelled."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            entry.additional_weights.all().delete()
            SalesDispatchAdditionalWeight.objects.bulk_create(
                [
                    SalesDispatchAdditionalWeight(
                        company=entry.company,
                        sales_dispatch=entry,
                        name=item["name"],
                        weight=item["weight"],
                        created_by=request.user,
                        updated_by=request.user,
                    )
                    for item in items
                ]
            )
            entry.updated_by = request.user
            entry.save(update_fields=["updated_by", "updated_at"])

        return Response(
            SalesDispatchAdditionalWeightSerializer(
                entry.additional_weights.filter(is_active=True),
                many=True,
            ).data
        )


class SalesDispatchMarkDispatchedView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "gate_core.can_dispatch_sales_dispatch_out"

    def post(self, request, entry_id):
        entry = get_sales_dispatch_or_404(request, entry_id)
        arrival = entry.arrival
        try:
            if arrival is not None and len(arrival.company_ids) > 1:
                # One physical truck, one exit: dispatching this docking dispatches
                # the WHOLE truck (every company at once, in one atomic step). This
                # happens in place -- no separate page -- and rolls back naming the
                # blocking company if any sibling docking isn't ready yet.
                dispatch_arrival(arrival, request.user)
                entry.refresh_from_db()
            else:
                mark_docking_dispatched(entry, request.user)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(SalesDispatchGateOutSerializer(entry).data)


class SalesDispatchRejectView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "gate_core.can_reject_sales_dispatch_out"

    def post(self, request, entry_id):
        entry = get_sales_dispatch_or_404(request, entry_id)
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
        entry = get_sales_dispatch_or_404(request, entry_id)
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
