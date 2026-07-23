import json
import logging
from datetime import datetime, timedelta, timezone as datetime_timezone

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Count, Prefetch, Q
from django.utils import timezone

# Aware sentinel used to sort cards whose stage timestamp is missing to the end.
_OLDEST_DATETIME = datetime(1, 1, 1, tzinfo=datetime_timezone.utc)
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.models import Company
from company.permissions import HasCompanyContext
from gate_core.services.user_scope import user_company_ids, wants_all_companies
from grpo.serializers import (
    ServiceGRPOOptionsSerializer,
    ServiceGRPOPendingEntrySerializer,
    ServiceGRPOPostRequestSerializer,
    ServiceGRPOPostResponseSerializer,
    ServiceGRPOPostingSerializer,
    ServiceGRPOPreviewSerializer,
)
from grpo.services import GRPOService
from grpo.pagination import (
    get_page_params,
    get_month_params,
    paginate_list,
    paginate_queryset,
    build_page,
)
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError

from .invoice_services import DispatchInvoiceService
from .models import DispatchPlan, DispatchPlanStatus
from .permissions import (
    CanPreviewBiltyServiceGRPO,
    CanPostBiltyServiceGRPO,
    CanPostTransporterAPInvoice,
    CanEditDispatchPlansOrLinkDispatchVehicle,
    CanLookupDispatchBill,
    CanSelectDispatchBills,
    CanViewDispatchPipeline,
    CanViewDispatchSchedule,
    CanViewOpenBiltiesOrPostTransporterAPInvoice,
    CanViewBiltyServiceGRPODetail,
    CanViewBiltyServiceGRPOHistory,
    CanViewBiltyServiceGRPOQueue,
    CanViewDispatchPlansOrLinkDispatchVehicle,
    CanViewTransporterAPInvoice,
)
from .serializers import (
    DispatchBillDetailSerializer,
    DispatchBillFilterSerializer,
    DispatchBillLineSerializer,
    DispatchBillSelectionSerializer,
    DispatchBillListResponseSerializer,
    DispatchPipelineCardSerializer,
    DispatchPipelineFilterSerializer,
    DispatchPlanSerializer,
    DispatchPlanUpdateSerializer,
    DispatchScheduleFilterSerializer,
    DispatchScheduleSerializer,
    OpenBiltySerializer,
    TransporterAPInvoicePostRequestSerializer,
    TransporterAPInvoicePostResponseSerializer,
    TransporterAPInvoicePostingSerializer,
    TransporterAPInvoicePreviewRequestSerializer,
    TransporterAPInvoicePreviewSerializer,
    TransporterAPInvoiceSAPPostRequestSerializer,
    TransporterAPInvoiceSubmitRequestSerializer,
)
from .services import (
    DispatchPlansService,
    PIPELINE_STAGE_LABELS,
    PIPELINE_STAGE_ORDER,
    compute_pipeline_stage,
    pipeline_module_status,
)

logger = logging.getLogger(__name__)


class DispatchBillListAPI(APIView):
    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanViewDispatchPlansOrLinkDispatchVehicle,
    ]

    def get(self, request):
        filter_serializer = DispatchBillFilterSerializer(data=request.query_params)
        if not filter_serializer.is_valid():
            return Response(
                {
                    "detail": "Invalid query parameters.",
                    "errors": filter_serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        filters = filter_serializer.validated_data
        try:
            if wants_all_companies(request):
                # The gate's "expected dispatch" view is cross-company: fan the SAP
                # read out over every company the user belongs to and merge, so the
                # company selector is a decorator (each company is its own HANA schema).
                result = self._get_bills_all_companies(request, filters)
            else:
                result = DispatchPlansService(
                    company_code=request.company.company.code
                ).get_bills(filters)
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError as e:
            return Response(
                {"detail": f"SAP data error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(DispatchBillListResponseSerializer(result).data)

    @staticmethod
    def _get_bills_all_companies(request, filters):
        codes = list(
            Company.objects.filter(id__in=user_company_ids(request))
            .order_by("code")
            .values_list("code", flat=True)
        )
        merged = []
        for code in codes:
            rows = DispatchPlansService(company_code=code).get_bills(filters)["data"]
            # Tag each row with its owning company so cross-company consumers (the
            # Inside Vehicle Manager) can scope bills per vehicle's company.
            for row in rows:
                row["company_code"] = code
            merged.extend(rows)
        # The panel keys on the scheduled dispatch date; keep newest first across companies.
        merged.sort(key=lambda row: (row.get("plan") or {}).get("dispatch_date") or "", reverse=True)
        return {"data": merged, "meta": DispatchPlansService._build_meta(merged)}


class DispatchBillSelectionAPI(APIView):
    """Submit the Bill Selection page — mark which bills enter dispatch planning.

    Reconciles only the shown bills (company-wide, shared selection), so submitting
    one date window never affects bills selected in another.
    """

    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanSelectDispatchBills,
    ]

    def post(self, request):
        serializer = DispatchBillSelectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = DispatchPlansService(
            company_code=request.company.company.code
        ).reconcile_selection(
            shown_doc_entries=serializer.validated_data["shown_doc_entries"],
            selected_doc_entries=serializer.validated_data["selected_doc_entries"],
            user=request.user,
        )
        return Response(result)


class DispatchPipelineView(APIView):
    """Read-only Dispatch Pipeline board.

    Returns every booked outbound dispatch as a card placed in its current
    stage, from vehicle linking (BOOKED) through sales-dispatch-out
    (DISPATCHED). The journey hangs off DispatchPlan, so the whole board is one
    DB query (no SAP). Dates filter on ``dispatch_date``; without an explicit
    range a recent-plus-upcoming window is used.
    """

    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanViewDispatchPipeline,
    ]

    DEFAULT_DAYS_BACK = 3
    DEFAULT_DAYS_AHEAD = 14

    def get(self, request):
        filter_serializer = DispatchPipelineFilterSerializer(data=request.query_params)
        if not filter_serializer.is_valid():
            return Response(
                {
                    "detail": "Invalid query parameters.",
                    "errors": filter_serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        data = filter_serializer.validated_data
        # Cross-company: the docking board's "expected" section aggregates across
        # every company the user belongs to when all_companies is set, so the
        # selector is a decorator there too (each plan's stage still hangs off its
        # own gate-outs).
        company_filter = (
            {"company_id__in": user_company_ids(request)}
            if wants_all_companies(request)
            else {"company": request.company.company}
        )
        today = timezone.localdate()
        date_from = data.get("date_from") or (today - timedelta(days=self.DEFAULT_DAYS_BACK))
        date_to = data.get("date_to") or (today + timedelta(days=self.DEFAULT_DAYS_AHEAD))
        stage_filter = (data.get("stage") or "").strip().upper()
        stage_set = {
            part.strip().upper()
            for part in (data.get("stages") or "").split(",")
            if part.strip()
        }
        search = (data.get("search") or "").strip()

        from gate_core.models.sales_dispatch import SalesDispatchGateOut

        plans = (
            DispatchPlan.objects.filter(
                **company_filter,
                is_active=True,
                booking_status__in=[
                    DispatchPlanStatus.BOOKED,
                    DispatchPlanStatus.DISPATCHED,
                ],
                dispatch_date__range=(date_from, date_to),
            )
            .select_related(
                "vehicle",
                "transporter",
                "driver",
                "linked_vehicle_entry",
                "linked_vehicle_entry__empty_vehicle_gate_in",
            )
            .prefetch_related(
                Prefetch(
                    "sales_dispatch_gate_outs",
                    queryset=SalesDispatchGateOut.objects.order_by("-created_at").annotate(
                        box_scan_count=Count("box_scans")
                    ),
                )
            )
        )

        cards = []
        for plan in plans:
            stage, gate_out, stage_at = compute_pipeline_stage(plan)
            if stage_filter and stage != stage_filter:
                continue
            if stage_set and stage not in stage_set:
                continue
            card = self._build_card(plan, stage, gate_out, stage_at)
            if search and not self._matches_search(card, search):
                continue
            cards.append(card)

        # Within each column the most recently-updated cards float to the top.
        cards.sort(key=lambda card: card["stage_at"] or _OLDEST_DATETIME, reverse=True)

        counts = {stage: 0 for stage in PIPELINE_STAGE_ORDER}
        for card in cards:
            counts[card["stage"]] = counts.get(card["stage"], 0) + 1

        columns = [
            {
                "stage": stage,
                "label": PIPELINE_STAGE_LABELS[stage],
                "count": counts.get(stage, 0),
            }
            for stage in PIPELINE_STAGE_ORDER
        ]

        return Response(
            {
                "columns": columns,
                "cards": DispatchPipelineCardSerializer(cards, many=True).data,
                "meta": {
                    "date_from": date_from,
                    "date_to": date_to,
                    "total": len(cards),
                },
            }
        )

    @staticmethod
    def _build_card(plan, stage, gate_out, stage_at):
        vehicle_entry = plan.linked_vehicle_entry
        empty_gate_in = None
        if vehicle_entry is not None:
            try:
                empty_gate_in = vehicle_entry.empty_vehicle_gate_in
            except ObjectDoesNotExist:
                empty_gate_in = None

        module_info = pipeline_module_status(
            stage,
            gate_out,
            box_scan_count=getattr(gate_out, "box_scan_count", None) if gate_out else None,
        )

        return {
            "plan_id": plan.id,
            "stage": stage,
            "stage_label": PIPELINE_STAGE_LABELS.get(stage, stage),
            "stage_at": stage_at,
            "module": module_info["module"],
            "module_status": module_info["module_status"],
            "module_label": module_info["module_label"],
            "sap_invoice_doc_entry": plan.sap_invoice_doc_entry,
            "sap_doc_num": plan.sap_invoice_doc_num or "",
            "invoice_number": plan.sap_invoice_doc_num or "",
            "vehicle_no": plan.vehicle_no
            or (plan.vehicle.vehicle_number if plan.vehicle else ""),
            "vehicle_id": plan.vehicle_id,
            "transporter_name": plan.transporter_name or "",
            "driver_name": plan.driver_name or "",
            "driver_mobile_no": plan.driver_mobile_no or "",
            "dispatch_date": plan.dispatch_date,
            "place_of_supply": plan.place_of_supply or "",
            "customer_name": (gate_out.customer_name if gate_out else "") or "",
            "empty_gate_in_entry_no": empty_gate_in.entry_no if empty_gate_in else None,
            "gate_out_id": gate_out.id if gate_out else None,
            "gate_out_entry_no": gate_out.entry_no if gate_out else None,
            "gate_out_status": gate_out.status if gate_out else None,
            "gate_out_vehicle_entry_id": gate_out.vehicle_entry_id if gate_out else None,
        }

    @staticmethod
    def _matches_search(card, search):
        needle = search.lower()
        haystack = " ".join(
            filter(
                None,
                [
                    card["vehicle_no"],
                    card["invoice_number"],
                    card["sap_doc_num"],
                    card["transporter_name"],
                    card["driver_name"],
                    card["customer_name"],
                ],
            )
        ).lower()
        return needle in haystack


class DispatchScheduleListAPI(APIView):
    """Read-only dispatch schedule for warehouse staff.

    Lists dispatch plans that have a dispatch date set (what is going out today,
    tomorrow, and the days ahead). By default it returns upcoming plans plus any
    overdue plans that have not yet been dispatched; an explicit date range
    overrides that.

    Each plan is enriched with the SAP invoice's item summary, source
    warehouse(s) and box/litre/weight totals (one SAP query) so the warehouse
    knows what to issue and from where. If SAP is unavailable the schedule still
    loads with ``sap_available: false`` and the item fields left blank.
    """

    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanViewDispatchSchedule,
    ]

    def get(self, request):
        filter_serializer = DispatchScheduleFilterSerializer(data=request.query_params)
        if not filter_serializer.is_valid():
            return Response(
                {
                    "detail": "Invalid query parameters.",
                    "errors": filter_serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        data = filter_serializer.validated_data
        today = timezone.localdate()
        company = request.company.company

        plans = DispatchPlan.objects.filter(
            company=company,
            dispatch_date__isnull=False,
        )

        booking_status = data.get("booking_status", "all")
        if booking_status and booking_status != "all":
            plans = plans.filter(booking_status=booking_status)
        else:
            plans = plans.exclude(booking_status=DispatchPlanStatus.CANCELLED)

        date_from = data.get("date_from")
        date_to = data.get("date_to")
        if date_from:
            plans = plans.filter(dispatch_date__gte=date_from)
        if date_to:
            plans = plans.filter(dispatch_date__lte=date_to)
        if not date_from and not date_to:
            # Default: upcoming plans, plus overdue ones not yet dispatched.
            plans = plans.filter(
                Q(dispatch_date__gte=today)
                | (
                    Q(dispatch_date__lt=today)
                    & ~Q(booking_status=DispatchPlanStatus.DISPATCHED)
                )
            )

        search = data.get("search")
        if search:
            plans = plans.filter(
                Q(sap_invoice_doc_num__icontains=search)
                | Q(place_of_supply__icontains=search)
                | Q(vehicle__vehicle_number__icontains=search)
                | Q(transporter__name__icontains=search)
            )

        plans = plans.order_by("dispatch_date", "-updated_at")

        serialized = DispatchScheduleSerializer(plans, many=True).data

        # Enrich with SAP item data (what to issue + from where). One SAP query;
        # degrade gracefully so the schedule still loads if SAP is down.
        doc_entries = [
            row["sap_invoice_doc_entry"]
            for row in serialized
            if row.get("sap_invoice_doc_entry")
        ]
        sap_available = True
        enrichment = {}
        if doc_entries:
            try:
                service = DispatchPlansService(company_code=company.code)
                enrichment = service.get_schedule_enrichment(doc_entries)
            except (SAPConnectionError, SAPDataError):
                sap_available = False

        for row in serialized:
            extra = enrichment.get(row.get("sap_invoice_doc_entry")) or {}
            row["item_summary"] = extra.get("item_summary", "")
            row["warehouses"] = extra.get("warehouses", "")
            row["line_count"] = extra.get("line_count", 0)
            row["total_boxes"] = extra.get("total_boxes", 0)
            row["sap_total_litres"] = extra.get("total_litres", 0)
            row["sap_total_weight"] = extra.get("total_weight", 0)

        return Response(
            {
                "data": serialized,
                "meta": {
                    "total": len(serialized),
                    "today": today.isoformat(),
                    "today_count": sum(
                        1 for plan in serialized if plan["dispatch_date"] == today.isoformat()
                    ),
                    "sap_available": sap_available,
                    "fetched_at": timezone.now().isoformat(),
                },
            }
        )


class DispatchScheduleItemsAPI(APIView):
    """Full SAP line items for one scheduled invoice, loaded on demand when the
    warehouse expands a row in the dispatch schedule."""

    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanViewDispatchSchedule,
    ]

    def get(self, request, doc_entry: int):
        service = DispatchPlansService(company_code=request.company.company.code)
        try:
            items = service.get_schedule_line_items(doc_entry)
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError as exc:
            return Response(
                {"detail": f"SAP data error: {str(exc)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(DispatchBillLineSerializer(items, many=True).data)


class DispatchBillByNumberAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanLookupDispatchBill]

    def get(self, request, invoice_number: str):
        invoice_number = (invoice_number or "").strip()
        if not invoice_number:
            return Response(
                {"detail": "Invoice number is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        service = DispatchPlansService(company_code=request.company.company.code)

        try:
            bill = service.get_bill_by_number(invoice_number)
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError as e:
            return Response(
                {"detail": f"SAP data error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if not bill:
            return Response(
                {"detail": f"SAP invoice {invoice_number} was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(DispatchBillDetailSerializer(bill).data)


class DispatchPlanUpdateAPI(APIView):
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanEditDispatchPlansOrLinkDispatchVehicle,
    ]

    def patch(self, request, sap_invoice_doc_entry: int):
        serializer = DispatchPlanUpdateSerializer(data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid dispatch plan details.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        service = DispatchPlansService(company_code=request.company.company.code)
        try:
            plan = service.update_plan(
                sap_invoice_doc_entry=sap_invoice_doc_entry,
                data=dict(serializer.validated_data),
                user=request.user,
            )
        except ValueError as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(DispatchPlanSerializer(plan).data)


class DispatchPendingBiltyGRPOListAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBiltyServiceGRPOQueue]

    def get(self, request):
        service = GRPOService(company_code=request.company.company.code)
        year, month = get_month_params(request)
        page, page_size = get_page_params(request)
        search = (request.GET.get("search") or "").strip().lower()

        dispatch_plans = service.get_pending_service_grpo_entries(
            year=year, month=month
        )
        plans_by_id = {plan.id: plan for plan in dispatch_plans}

        # Build lightweight rows (no SAP calls yet) so search + pagination stay
        # cheap; the SAP bill snapshot is fetched for the current page only.
        rows = []
        for plan in dispatch_plans:
            rows.append(
                {
                    "dispatch_plan_id": plan.id,
                    "sap_invoice_doc_entry": plan.sap_invoice_doc_entry,
                    "sap_invoice_doc_num": plan.sap_invoice_doc_num,
                    "booking_status": plan.booking_status,
                    "dispatch_date": plan.dispatch_date,
                    "linked_vehicle_entry_id": plan.linked_vehicle_entry_id,
                    "linked_vehicle_entry_no": service.get_service_display_linked_entry_no(plan),
                    "vehicle_no": service.get_service_display_vehicle_no(plan),
                    "driver_name": service.get_service_display_driver_name(plan),
                    "transporter_name": service._dispatch_transporter_name(plan),
                    "transporter_gstin": service._dispatch_transporter_gstin(plan),
                    "source_state": plan.place_of_supply,
                    "bilty_no": plan.bilty_no,
                    "bilty_date": plan.bilty_date,
                    "freight": plan.freight,
                    "total_freight": plan.total_freight,
                    "invoice_count": getattr(plan, "_service_group_invoice_count", 1),
                    "created_at": plan.created_at,
                    "updated_at": plan.updated_at,
                }
            )

        if search:
            def _matches(row):
                haystack = " ".join(str(row.get(field) or "") for field in (
                    "sap_invoice_doc_num", "bilty_no", "vehicle_no",
                    "driver_name", "transporter_name", "linked_vehicle_entry_no",
                )).lower()
                return search in haystack
            rows = [row for row in rows if _matches(row)]

        page_rows, meta = paginate_list(rows, page, page_size)

        # One batched SAP query for the current page only (was a per-plan live
        # read for every dispatched bill in the backlog -- the N+1 that hung the
        # page).
        page_plans = [plans_by_id[row["dispatch_plan_id"]] for row in page_rows]
        bill_snapshots = service.get_dispatch_bill_snapshots(page_plans)
        for row in page_rows:
            snapshot = bill_snapshots.get(row["dispatch_plan_id"], {})
            row["source_state"] = snapshot.get("state", "") or row["source_state"]

        serializer = ServiceGRPOPendingEntrySerializer(page_rows, many=True)
        return Response(build_page(serializer.data, meta))


class DispatchBiltyGRPOOptionsAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanPreviewBiltyServiceGRPO]

    def get(self, request):
        service = GRPOService(company_code=request.company.company.code)
        try:
            options = service.get_service_grpo_options()
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError as e:
            return Response(
                {"detail": f"SAP data error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(ServiceGRPOOptionsSerializer(options).data)


class DispatchBiltyGRPOPreviewAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanPreviewBiltyServiceGRPO]

    def get(self, request, dispatch_plan_id):
        service = GRPOService(company_code=request.company.company.code)
        try:
            preview_data = service.get_service_grpo_preview_data(dispatch_plan_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)

        return Response(ServiceGRPOPreviewSerializer(preview_data).data)


class DispatchBiltyServiceGRPOPostAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanPostBiltyServiceGRPO]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        parsed_data, attachments, error_response = self._parse_payload(request)
        if error_response:
            return error_response

        serializer = ServiceGRPOPostRequestSerializer(data=parsed_data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid request data", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        service = GRPOService(company_code=request.company.company.code)

        try:
            grpo_posting = service.post_service_grpo(
                dispatch_plan_id=serializer.validated_data["dispatch_plan_id"],
                user=request.user,
                vendor_code=serializer.validated_data["vendor_code"],
                branch_id=serializer.validated_data["branch_id"],
                service_description=serializer.validated_data["service_description"],
                amount=serializer.validated_data["amount"],
                tax_code=serializer.validated_data.get("tax_code"),
                gl_account=serializer.validated_data.get("gl_account"),
                unit_price=serializer.validated_data.get("unit_price"),
                place_of_supply=serializer.validated_data.get("place_of_supply"),
                effective_month=serializer.validated_data.get("effective_month"),
                budget_delivery_point=serializer.validated_data.get(
                    "budget_delivery_point"
                ),
                sub_account=serializer.validated_data.get("sub_account"),
                location_code=serializer.validated_data.get("location_code"),
                location_name=serializer.validated_data.get("location_name"),
                sac_entry=serializer.validated_data.get("sac_entry"),
                sac_code=serializer.validated_data.get("sac_code"),
                product_variety=serializer.validated_data.get("product_variety"),
                total_litres=serializer.validated_data.get("total_litres"),
                invoice_number=serializer.validated_data.get("invoice_number"),
                eway_bill=serializer.validated_data.get("eway_bill"),
                invoice_weight=serializer.validated_data.get("invoice_weight"),
                invoice_amount=serializer.validated_data.get("invoice_amount"),
                comments=serializer.validated_data.get("comments"),
                vendor_ref=serializer.validated_data.get("vendor_ref"),
                extra_charges=serializer.validated_data.get("extra_charges"),
                attachments=attachments,
                include_bilty_attachment=serializer.validated_data.get(
                    "include_bilty_attachment", True
                ),
                doc_date=serializer.validated_data.get("doc_date"),
                doc_due_date=serializer.validated_data.get("doc_due_date"),
                tax_date=serializer.validated_data.get("tax_date"),
                should_roundoff=serializer.validated_data.get("should_roundoff", False),
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except SAPValidationError as e:
            return Response(
                {"detail": f"SAP validation error: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError as e:
            return Response(
                {"detail": f"SAP error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        response_data = {
            "success": True,
            "service_grpo_posting_id": grpo_posting.id,
            "sap_doc_entry": grpo_posting.sap_doc_entry,
            "sap_doc_num": grpo_posting.sap_doc_num,
            "sap_doc_total": grpo_posting.sap_doc_total,
            "message": (
                "Service GRPO posted successfully. "
                f"SAP Doc Num: {grpo_posting.sap_doc_num}"
            ),
            "attachments": grpo_posting.attachments.all(),
        }
        return Response(
            ServiceGRPOPostResponseSerializer(
                response_data, context={"request": request}
            ).data,
            status=status.HTTP_201_CREATED,
        )

    @staticmethod
    def _parse_payload(request):
        if request.content_type and "multipart" in request.content_type:
            try:
                raw_data = request.data.get("data", "{}")
                parsed_data = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
            except json.JSONDecodeError:
                return (
                    None,
                    [],
                    Response(
                        {"detail": "Invalid JSON in 'data' field"},
                        status=status.HTTP_400_BAD_REQUEST,
                    ),
                )
            attachments = request.FILES.getlist("attachments")
        else:
            parsed_data = request.data
            attachments = []
        return parsed_data, attachments, None


class DispatchBiltyGRPOPostingHistoryAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBiltyServiceGRPOHistory]

    def get(self, request):
        from grpo.models import GRPOStatus

        dispatch_plan_id = request.GET.get("dispatch_plan_id")
        year, month = get_month_params(request)
        page, page_size = get_page_params(request)
        status_filter = (request.GET.get("status") or "").strip().upper()
        search = (request.GET.get("search") or "").strip()

        service = GRPOService(company_code=request.company.company.code)
        postings = service.get_service_grpo_posting_history(
            dispatch_plan_id=int(dispatch_plan_id) if dispatch_plan_id else None,
            year=year,
            month=month,
        )
        if status_filter and status_filter in GRPOStatus.values:
            postings = postings.filter(status=status_filter)
        if search:
            postings = postings.filter(
                Q(dispatch_plan__bilty_no__icontains=search)
                | Q(sap_doc_num__icontains=search)
                | Q(dispatch_plan__sap_invoice_doc_num__icontains=search)
                | Q(dispatch_plan__vehicle__vehicle_number__icontains=search)
                | Q(dispatch_plan__transporter__name__icontains=search)
            )

        page_qs, _total, meta = paginate_queryset(postings, page, page_size)
        serializer = ServiceGRPOPostingSerializer(page_qs, many=True)
        return Response(build_page(serializer.data, meta))


class DispatchBiltyGRPOPostingDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBiltyServiceGRPODetail]

    def get(self, request, posting_id: int):
        from grpo.models import ServiceGRPOPosting

        try:
            posting = (
                ServiceGRPOPosting.objects.select_related(
                    "dispatch_plan",
                    "dispatch_plan__company",
                    "dispatch_plan__vehicle",
                    "dispatch_plan__vehicle__transporter",
                    "dispatch_plan__transporter",
                    "dispatch_plan__linked_vehicle_entry",
                    "dispatch_plan__linked_vehicle_entry__vehicle",
                    "dispatch_plan__linked_vehicle_entry__vehicle__transporter",
                    "dispatch_plan__linked_vehicle_entry__driver",
                    "posted_by",
                )
                .prefetch_related("lines", "attachments")
                .get(
                    id=posting_id,
                    dispatch_plan__company=request.company.company,
                )
            )
        except ServiceGRPOPosting.DoesNotExist:
            return Response(
                {"detail": "Service GRPO posting not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = ServiceGRPOPostingSerializer(
            posting,
            context={"request": request},
        )
        return Response(serializer.data)


class OpenBiltyListAPI(APIView):
    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanViewOpenBiltiesOrPostTransporterAPInvoice,
    ]

    def get(self, request):
        service = DispatchInvoiceService(company_code=request.company.company.code)
        try:
            open_bilties = service.get_open_bilties()
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError as e:
            return Response(
                {"detail": f"SAP data error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(OpenBiltySerializer(open_bilties, many=True).data)


class TransporterAPInvoicePreviewAPI(APIView):
    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanPostTransporterAPInvoice,
    ]

    def post(self, request):
        serializer = TransporterAPInvoicePreviewRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid request data", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        service = DispatchInvoiceService(company_code=request.company.company.code)
        try:
            preview = service.preview_ap_invoice(
                service_grpo_posting_ids=serializer.validated_data[
                    "service_grpo_posting_ids"
                ],
                vendor_code=serializer.validated_data.get("vendor_code"),
                branch_id=serializer.validated_data.get("branch_id"),
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError as e:
            return Response(
                {"detail": f"SAP data error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(TransporterAPInvoicePreviewSerializer(preview).data)


class TransporterAPInvoicePostAPI(APIView):
    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanPostTransporterAPInvoice,
    ]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request, posting_id: int | None = None):
        if posting_id is not None:
            serializer = TransporterAPInvoiceSAPPostRequestSerializer(data=request.data)
            if not serializer.is_valid():
                return Response(
                    {"detail": "Invalid request data", "errors": serializer.errors},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            service = DispatchInvoiceService(company_code=request.company.company.code)
            try:
                posting = service.post_submitted_ap_invoice(
                    posting_id=posting_id,
                    user=request.user,
                    doc_date=serializer.validated_data.get("doc_date"),
                    doc_due_date=serializer.validated_data.get("doc_due_date"),
                    tax_date=serializer.validated_data.get("tax_date"),
                    comments=serializer.validated_data.get("comments", ""),
                )
            except ValueError as e:
                return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
            except SAPValidationError as e:
                return Response(
                    {"detail": f"SAP validation error: {str(e)}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            except SAPConnectionError:
                return Response(
                    {"detail": "SAP system is currently unavailable. Please try again later."},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            except SAPDataError as e:
                return Response(
                    {"detail": f"SAP error: {str(e)}"},
                    status=status.HTTP_502_BAD_GATEWAY,
                )

            posting = service.get_ap_invoice(posting.id)
            response_data = {
                "success": True,
                "transporter_ap_invoice_posting_id": posting.id,
                "sap_doc_entry": posting.sap_doc_entry,
                "sap_doc_num": posting.sap_doc_num,
                "sap_doc_total": posting.sap_doc_total,
                "message": (
                    "A/P Invoice posted successfully. "
                    f"SAP Doc Num: {posting.sap_doc_num}"
                ),
                "posting": posting,
            }
            return Response(
                TransporterAPInvoicePostResponseSerializer(
                    response_data, context={"request": request}
                ).data
            )

        parsed_data, attachments, error_response = self._parse_payload(request)
        if error_response:
            return error_response

        serializer = TransporterAPInvoicePostRequestSerializer(data=parsed_data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid request data", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        service = DispatchInvoiceService(company_code=request.company.company.code)
        try:
            posting = service.post_ap_invoice(
                service_grpo_posting_ids=serializer.validated_data[
                    "service_grpo_posting_ids"
                ],
                user=request.user,
                invoice_number=serializer.validated_data["invoice_number"],
                invoice_amount=serializer.validated_data["invoice_amount"],
                attachments=attachments,
                invoice_date=serializer.validated_data.get("invoice_date"),
                doc_date=serializer.validated_data.get("doc_date"),
                doc_due_date=serializer.validated_data.get("doc_due_date"),
                tax_date=serializer.validated_data.get("tax_date"),
                vendor_code=serializer.validated_data.get("vendor_code"),
                branch_id=serializer.validated_data.get("branch_id"),
                comments=serializer.validated_data.get("comments", ""),
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except SAPValidationError as e:
            return Response(
                {"detail": f"SAP validation error: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError as e:
            return Response(
                {"detail": f"SAP error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        posting = service.get_ap_invoice(posting.id)
        response_data = {
            "success": True,
            "transporter_ap_invoice_posting_id": posting.id,
            "sap_doc_entry": posting.sap_doc_entry,
            "sap_doc_num": posting.sap_doc_num,
            "sap_doc_total": posting.sap_doc_total,
            "message": (
                "Transporter A/P Invoice posted successfully. "
                f"SAP Doc Num: {posting.sap_doc_num}"
            ),
            "posting": posting,
        }
        return Response(
            TransporterAPInvoicePostResponseSerializer(
                response_data, context={"request": request}
            ).data,
            status=status.HTTP_201_CREATED,
        )

    @staticmethod
    def _parse_payload(request):
        if request.content_type and "multipart" in request.content_type:
            try:
                raw_data = request.data.get("data", "{}")
                parsed_data = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
            except json.JSONDecodeError:
                return (
                    None,
                    [],
                    Response(
                        {"detail": "Invalid JSON in 'data' field"},
                        status=status.HTTP_400_BAD_REQUEST,
                    ),
                )
            attachments = request.FILES.getlist("attachments")
        else:
            parsed_data = request.data
            attachments = []
        return parsed_data, attachments, None


class TransporterAPInvoiceSubmitAPI(APIView):
    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanPostTransporterAPInvoice,
    ]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        parsed_data, attachments, error_response = TransporterAPInvoicePostAPI._parse_payload(
            request
        )
        if error_response:
            return error_response

        serializer = TransporterAPInvoiceSubmitRequestSerializer(data=parsed_data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid request data", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        service = DispatchInvoiceService(company_code=request.company.company.code)
        try:
            posting = service.submit_ap_invoice(
                service_grpo_posting_ids=serializer.validated_data[
                    "service_grpo_posting_ids"
                ],
                user=request.user,
                invoice_number=serializer.validated_data["invoice_number"],
                invoice_amount=serializer.validated_data["invoice_amount"],
                attachments=attachments,
                invoice_date=serializer.validated_data.get("invoice_date"),
                vendor_code=serializer.validated_data.get("vendor_code"),
                branch_id=serializer.validated_data.get("branch_id"),
                comments=serializer.validated_data.get("comments", ""),
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError as e:
            return Response(
                {"detail": f"SAP data error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        posting = service.get_ap_invoice(posting.id)
        response_data = {
            "success": True,
            "transporter_ap_invoice_posting_id": posting.id,
            "sap_doc_entry": posting.sap_doc_entry,
            "sap_doc_num": posting.sap_doc_num,
            "sap_doc_total": posting.sap_doc_total,
            "message": "Transporter invoice submitted for A/P Invoice posting.",
            "posting": posting,
        }
        return Response(
            TransporterAPInvoicePostResponseSerializer(
                response_data, context={"request": request}
            ).data,
            status=status.HTTP_201_CREATED,
        )


class TransporterAPInvoiceHistoryAPI(APIView):
    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanViewTransporterAPInvoice,
    ]

    def get(self, request):
        service = DispatchInvoiceService(company_code=request.company.company.code)
        postings = service.get_ap_invoice_history()
        serializer = TransporterAPInvoicePostingSerializer(
            postings,
            many=True,
            context={"request": request},
        )
        return Response(serializer.data)


class TransporterAPInvoiceDetailAPI(APIView):
    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanViewTransporterAPInvoice,
    ]

    def get(self, request, posting_id: int):
        service = DispatchInvoiceService(company_code=request.company.company.code)
        try:
            posting = service.get_ap_invoice(posting_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)

        serializer = TransporterAPInvoicePostingSerializer(
            posting,
            context={"request": request},
        )
        return Response(serializer.data)
