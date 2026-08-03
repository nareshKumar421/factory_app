import json
import logging
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser

from company.permissions import HasCompanyContext
from quality_control.models import RawMaterialInspection
from quality_control.serializers import RawMaterialInspectionSerializer
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError

from .notifications import notify_material_grpo_failed, notify_service_grpo_failed
from .pagination import (
    get_page_params,
    get_month_params,
    paginate_list,
    paginate_queryset,
    build_page,
)
from .services import GRPOService
from .serializers import (
    GRPOPreviewSerializer,
    GRPOPostRequestSerializer,
    GRPOPostingSerializer,
    GRPOPostResponseSerializer,
    GRPOAttachmentSerializer,
    GRPOAttachmentUploadSerializer,
    AllGRPOEntrySerializer,
    GRPODashboardSummarySerializer,
    ServiceGRPOPendingEntrySerializer,
    ServiceGRPOPreviewSerializer,
    ServiceGRPOPostRequestSerializer,
    ServiceGRPOOptionsSerializer,
    ServiceGRPOPostingSerializer,
    ServiceGRPOPostResponseSerializer,
)
from .permissions import (
    CanViewPendingGRPO,
    CanPreviewGRPO,
    CanCreateGRPOPosting,
    CanViewGRPOHistory,
    CanViewGRPOPosting,
    CanManageGRPOAttachments,
)
# Bilty Service GRPO submodule permissions. These are OR-checks that accept the
# dispatch-owned ``can_post_bilty_service_grpo`` OR the material-GRPO perms, so
# the dedicated "Service GRPO" group (which holds only the bilty permission)
# and legacy material-GRPO users both work. The Service GRPO endpoints below
# must gate on these, NOT the material-only classes.
from dispatch_plans.permissions import (
    CanViewBiltyServiceGRPOQueue,
    CanPreviewBiltyServiceGRPO,
    CanPostBiltyServiceGRPO,
    CanViewBiltyServiceGRPOHistory,
    CanViewBiltyServiceGRPODetail,
)

logger = logging.getLogger(__name__)


def _parse_grpo_multipart(request):
    """Extract (parsed_data, attachments) from a GRPO request.

    Multipart: JSON fields in a "data" part, files in "attachments" parts.
    JSON body: the body itself, with no attachments. Raises ``json.JSONDecodeError``
    on a malformed "data" part so callers can return a 400.
    """
    if request.content_type and 'multipart' in request.content_type:
        raw_data = request.data.get("data", "{}")
        parsed_data = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
        attachments = request.FILES.getlist("attachments")
    else:
        parsed_data = request.data
        attachments = []
    return parsed_data, attachments


class GRPODashboardSummaryAPI(APIView):
    """
    Returns material GRPO dashboard insight totals.

    GET /api/grpo/summary/
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewPendingGRPO]

    def get(self, request):
        service = GRPOService(
            company_code=request.company.company.code,
            entry_type=getattr(self, "entry_type", "RAW_MATERIAL"),
        )
        summary = service.get_grpo_dashboard_summary()
        return Response(GRPODashboardSummarySerializer(summary).data)


class AllGRPOEntriesListAPI(APIView):
    """
    Returns every RAW_MATERIAL gate entry visible to a GRPO operator,
    including entries still at gate or in QC. Each entry carries a `phase`
    (GATE / QC / DONE / CANCELLED) and a friendly `status_label`.

    GET /api/grpo/all-entries/
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewPendingGRPO]

    def get(self, request):
        from collections import defaultdict
        from gate_core.enums import GateEntryStatus, get_entry_phase

        service = GRPOService(
            company_code=request.company.company.code,
            entry_type=getattr(self, "entry_type", "RAW_MATERIAL"),
        )
        year, month = get_month_params(request)
        page, page_size = get_page_params(request)
        phase_filter = (request.GET.get("phase") or "").strip().upper()
        search = (request.GET.get("search") or "").strip().lower()

        entries = service.get_all_grpo_visible_entries(year=year, month=month)

        status_labels = dict(GateEntryStatus.choices)
        result = []

        for entry in entries:
            po_receipts = list(entry.po_receipts.all())
            total_count = len(po_receipts)

            posted_po_ids = set()
            for grpo in entry.grpo_postings.filter(status="POSTED"):
                posted_po_ids.update(
                    grpo.po_receipts.values_list("id", flat=True)
                )
                if grpo.po_receipt_id:
                    posted_po_ids.add(grpo.po_receipt_id)

            pending_count = sum(1 for po in po_receipts if po.id not in posted_po_ids)
            posted_count = total_count - pending_count

            supplier_groups = defaultdict(list)
            for po in po_receipts:
                supplier_groups[po.supplier_code].append(po)

            suppliers = [
                {
                    "supplier_code": code,
                    "supplier_name": pos[0].supplier_name,
                    "po_count": len(pos),
                }
                for code, pos in supplier_groups.items()
            ]

            result.append({
                "vehicle_entry_id": entry.id,
                "entry_no": entry.entry_no,
                "status": entry.status,
                "status_label": status_labels.get(entry.status, entry.status),
                "phase": get_entry_phase(entry.status),
                "is_ready_for_grpo": service.is_entry_ready_for_grpo(entry),
                "is_fully_posted": total_count > 0 and pending_count == 0,
                "entry_time": entry.entry_time,
                "total_po_count": total_count,
                "posted_po_count": posted_count,
                "pending_po_count": pending_count,
                "suppliers": suppliers,
                "po_numbers": [po.po_number for po in po_receipts],
                "po_receipts": service.get_entry_qc_breakdown(entry, posted_po_ids),
            })

        # Phase counts for the tab badges, over the whole month (before the
        # phase/search filter narrows the visible rows).
        counts = {"ALL": len(result), "GATE": 0, "QC": 0, "DONE": 0}
        for row in result:
            if row["phase"] in counts:
                counts[row["phase"]] += 1

        if search:
            def _matches(row):
                haystack = " ".join([
                    row["entry_no"] or "",
                    row["status_label"] or "",
                    " ".join(row["po_numbers"]),
                    " ".join(s["supplier_name"] or "" for s in row["suppliers"]),
                ]).lower()
                return search in haystack
            result = [row for row in result if _matches(row)]

        if phase_filter in ("GATE", "QC", "DONE"):
            result = [row for row in result if row["phase"] == phase_filter]

        page_rows, meta = paginate_list(result, page, page_size)
        serializer = AllGRPOEntrySerializer(page_rows, many=True)
        return Response({**build_page(serializer.data, meta), "counts": counts})


class PendingGRPOListAPI(APIView):
    """
    Returns list of completed gate entries pending GRPO posting.
    Groups PO receipts by supplier for merged GRPO selection.

    GET /api/grpo/pending/
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewPendingGRPO]

    def get(self, request):
        from collections import defaultdict
        from .models import GRPOPosting, GRPOStatus

        service = GRPOService(
            company_code=request.company.company.code,
            entry_type=getattr(self, "entry_type", "RAW_MATERIAL"),
        )
        year, month = get_month_params(request)
        page, page_size = get_page_params(request)
        search = (request.GET.get("search") or "").strip().lower()

        entries = service.get_pending_grpo_entries(year=year, month=month)

        result = []
        for entry in entries:
            po_receipts = list(entry.po_receipts.all())
            total_count = len(po_receipts)

            # Find which POs are already posted (via M2M or legacy FK)
            posted_po_ids = set()
            for grpo in entry.grpo_postings.filter(status="POSTED"):
                # Check M2M
                posted_po_ids.update(
                    grpo.po_receipts.values_list("id", flat=True)
                )
                # Check legacy FK
                if grpo.po_receipt_id:
                    posted_po_ids.add(grpo.po_receipt_id)

            pending_pos = [po for po in po_receipts if po.id not in posted_po_ids]
            pending_count = len(pending_pos)

            if pending_count == 0:
                continue

            # Group pending POs by supplier for merge selection
            supplier_groups = defaultdict(list)
            for po in pending_pos:
                po_date = service.resolve_po_date(po)
                supplier_groups[po.supplier_code].append({
                    "po_receipt_id": po.id,
                    "po_number": po.po_number,
                    "supplier_code": po.supplier_code,
                    "supplier_name": po.supplier_name,
                    "branch_id": po.branch_id,
                    "item_count": po.items.count(),
                    "po_date": po_date,
                })

            suppliers = []
            for supplier_code, pos in supplier_groups.items():
                suppliers.append({
                    "supplier_code": supplier_code,
                    "supplier_name": pos[0]["supplier_name"],
                    "po_count": len(pos),
                    "can_merge": len(pos) > 1,
                    "po_receipts": pos,
                })

            pending_po_dates = [
                pr["po_date"]
                for group in supplier_groups.values()
                for pr in group
                if pr["po_date"]
            ]
            earliest_po_date = min(pending_po_dates) if pending_po_dates else None

            result.append({
                "vehicle_entry_id": entry.id,
                "entry_no": entry.entry_no,
                "status": entry.status,
                "entry_time": entry.entry_time,
                "po_date": earliest_po_date,
                "total_po_count": total_count,
                "posted_po_count": total_count - pending_count,
                "pending_po_count": pending_count,
                "is_fully_posted": False,
                "suppliers": suppliers,
            })

        if search:
            def _matches(row):
                haystack = " ".join([
                    row["entry_no"] or "",
                    " ".join(
                        pr["po_number"] or ""
                        for s in row["suppliers"]
                        for pr in s["po_receipts"]
                    ),
                    " ".join(s["supplier_name"] or "" for s in row["suppliers"]),
                ]).lower()
                return search in haystack
            result = [row for row in result if _matches(row)]

        page_rows, meta = paginate_list(result, page, page_size)
        return Response(build_page(page_rows, meta))


class GRPOPreviewAPI(APIView):
    """
    Returns all data required for GRPO posting for a specific gate entry.
    Shows PO details, items, QC status, and accepted quantities.

    GET /api/grpo/preview/<vehicle_entry_id>/
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanPreviewGRPO]

    def get(self, request, vehicle_entry_id):
        service = GRPOService(company_code=request.company.company.code)

        # Optional: filter by specific PO receipt IDs for merged preview
        po_receipt_ids_param = request.GET.get("po_receipt_ids")
        po_receipt_ids = None
        if po_receipt_ids_param:
            try:
                po_receipt_ids = [int(x) for x in po_receipt_ids_param.split(",")]
            except ValueError:
                return Response(
                    {"detail": "Invalid po_receipt_ids format. Use comma-separated integers."},
                    status=status.HTTP_400_BAD_REQUEST
                )

        try:
            preview_data = service.get_grpo_preview_data(
                vehicle_entry_id, po_receipt_ids=po_receipt_ids
            )
        except ValueError as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_404_NOT_FOUND
            )

        serializer = GRPOPreviewSerializer(preview_data, many=True)
        return Response(serializer.data)


class GRPOInspectionReportAPI(APIView):
    """
    Returns the QC inspection report payload for direct printing inside GRPO.

    GET /api/grpo/inspection-report/<arrival_slip_id>/
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanPreviewGRPO]

    def get(self, request, arrival_slip_id):
        inspection = get_object_or_404(
            RawMaterialInspection.objects.select_related(
                "arrival_slip",
                "arrival_slip__po_item_receipt",
                "arrival_slip__po_item_receipt__po_receipt",
                "arrival_slip__po_item_receipt__po_receipt__vehicle_entry",
                "material_type",
                "qa_chemist",
                "qam",
                "rejected_by",
            ).prefetch_related(
                "parameter_results__parameter_master",
                "arrival_slip__attachments",
            ),
            arrival_slip_id=arrival_slip_id,
            arrival_slip__po_item_receipt__po_receipt__vehicle_entry__company=request.company.company,
        )
        serializer = RawMaterialInspectionSerializer(inspection, context={"request": request})
        return Response(serializer.data)


class PostGRPOAPI(APIView):
    """
    Post GRPO to SAP for a specific PO receipt.
    Supports multipart/form-data to include attachments during posting.
    Attachments are uploaded to SAP first and included in the GRPO document
    creation, avoiding the approval re-trigger on PATCH.

    Supports two content types:
    1. application/json — JSON body (no attachments)
    2. multipart/form-data — JSON in 'data' field + files in 'attachments' field(s)

    SAP requires attachments at GRPO creation time. When attachments are provided,
    they are uploaded to SAP Attachments2 first, and the resulting AttachmentEntry
    is included in the GRPO payload.

    POST /api/grpo/post/

    For multipart/form-data:
      - Send JSON fields as a "data" part (JSON string)
      - Send files as "attachments" parts

    For application/json (no attachments):
      - Send JSON body as before (backward compatible)
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateGRPOPosting]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        # Handle multipart form data: JSON in 'data' field + files in 'attachments'
        if request.content_type and 'multipart' in request.content_type:
            try:
                raw_data = request.data.get("data", "{}")
                if isinstance(raw_data, str):
                    parsed_data = json.loads(raw_data)
                else:
                    parsed_data = raw_data
            except json.JSONDecodeError:
                return Response(
                    {"detail": "Invalid JSON in 'data' field"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            attachments = request.FILES.getlist("attachments")
        else:
            parsed_data = request.data
            attachments = []

        if not attachments:
            return Response(
                {"detail": "At least one attachment is required for material GRPO."},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = GRPOPostRequestSerializer(data=parsed_data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid request data", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )

        service = GRPOService(
            company_code=request.company.company.code,
            entry_type=getattr(self, "entry_type", "RAW_MATERIAL"),
        )

        def record_failure(error_message):
            """Persist the failed attempt so it surfaces in History → Failed.
            Never let a bookkeeping error mask the real posting error."""
            try:
                service.record_material_grpo_failure(
                    vehicle_entry_id=serializer.validated_data.get("vehicle_entry_id"),
                    po_receipt_ids=serializer.validated_data.get("po_receipt_ids") or [],
                    error_message=error_message,
                    user=request.user,
                )
            except Exception:
                logger.exception("Failed to record FAILED GRPO posting")

        try:
            grpo_posting = service.post_grpo(
                vehicle_entry_id=serializer.validated_data["vehicle_entry_id"],
                po_receipt_ids=serializer.validated_data["po_receipt_ids"],
                user=request.user,
                items=serializer.validated_data["items"],
                branch_id=serializer.validated_data["branch_id"],
                warehouse_code=serializer.validated_data.get("warehouse_code"),
                comments=serializer.validated_data.get("comments"),
                vendor_ref=serializer.validated_data.get("vendor_ref"),
                tare_weight=serializer.validated_data.get("tare_weight"),
                extra_charges=serializer.validated_data.get("extra_charges"),
                attachments=attachments,
                doc_date=serializer.validated_data.get("doc_date"),
                doc_due_date=serializer.validated_data.get("doc_due_date"),
                tax_date=serializer.validated_data.get("tax_date"),
                should_roundoff=serializer.validated_data.get("should_roundoff", False),
            )

            response_data = {
                "success": True,
                "grpo_posting_id": grpo_posting.id,
                "sap_doc_entry": grpo_posting.sap_doc_entry,
                "sap_doc_num": grpo_posting.sap_doc_num,
                "sap_doc_total": grpo_posting.sap_doc_total,
                "message": f"GRPO posted successfully. SAP Doc Num: {grpo_posting.sap_doc_num}",
                "attachments": grpo_posting.attachments.all(),
            }

            return Response(
                GRPOPostResponseSerializer(
                    response_data, context={"request": request}
                ).data,
                status=status.HTTP_201_CREATED
            )

        except ValueError as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

        except SAPValidationError as e:
            record_failure(str(e))
            notify_material_grpo_failed(
                company=request.company.company,
                user=request.user,
                error_message=str(e),
                vehicle_entry_id=serializer.validated_data.get("vehicle_entry_id"),
            )
            return Response(
                {"detail": f"SAP validation error: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST
            )

        except SAPConnectionError:
            record_failure("SAP system unavailable")
            notify_material_grpo_failed(
                company=request.company.company,
                user=request.user,
                error_message="SAP system unavailable",
                vehicle_entry_id=serializer.validated_data.get("vehicle_entry_id"),
            )
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )

        except SAPDataError as e:
            record_failure(str(e))
            notify_material_grpo_failed(
                company=request.company.company,
                user=request.user,
                error_message=str(e),
                vehicle_entry_id=serializer.validated_data.get("vehicle_entry_id"),
            )
            return Response(
                {"detail": f"SAP error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY
            )

        except Exception as e:
            logger.exception("Unexpected error posting GRPO")
            record_failure(f"Unexpected error: {e}")
            notify_material_grpo_failed(
                company=request.company.company,
                user=request.user,
                error_message=str(e),
                vehicle_entry_id=serializer.validated_data.get("vehicle_entry_id"),
            )
            return Response(
                {"detail": "Unexpected error while posting GRPO."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class GRPODraftAPI(APIView):
    """
    Save / edit / discard a material GRPO draft (no SAP posting).

    POST   /api/grpo/draft/              Create a draft.
    GET    /api/grpo/draft/<id>/         Load a draft (for hydration / retry).
    PATCH  /api/grpo/draft/<id>/         Replace saved data; append new attachments.
    DELETE /api/grpo/draft/<id>/         Discard an unposted draft/failed posting.

    Multipart: JSON fields in a "data" part, files in "attachments" parts.
    Attachments are optional when saving but required before posting.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateGRPOPosting]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def _save(self, request, posting_id):
        try:
            parsed_data, attachments = _parse_grpo_multipart(request)
        except json.JSONDecodeError:
            return Response(
                {"detail": "Invalid JSON in 'data' field"},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = GRPOPostRequestSerializer(data=parsed_data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid request data", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )

        service = GRPOService(company_code=request.company.company.code)
        try:
            draft = service.save_grpo_draft(
                vehicle_entry_id=serializer.validated_data["vehicle_entry_id"],
                po_receipt_ids=serializer.validated_data["po_receipt_ids"],
                user=request.user,
                request_payload=parsed_data,
                attachments=attachments,
                grpo_posting_id=posting_id,
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            GRPOPostingSerializer(draft, context={"request": request}).data,
            status=status.HTTP_201_CREATED if posting_id is None else status.HTTP_200_OK,
        )

    def post(self, request):
        return self._save(request, posting_id=None)

    def patch(self, request, posting_id):
        return self._save(request, posting_id=posting_id)

    def get(self, request, posting_id):
        from .models import GRPOPosting

        try:
            posting = GRPOPosting.objects.select_related(
                "vehicle_entry", "po_receipt", "posted_by"
            ).prefetch_related(
                "lines__po_item_receipt__arrival_slip__inspection",
                "attachments",
                "po_receipts",
            ).get(id=posting_id)
        except GRPOPosting.DoesNotExist:
            return Response(
                {"detail": "GRPO posting not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        return Response(
            GRPOPostingSerializer(posting, context={"request": request}).data
        )

    def delete(self, request, posting_id):
        from .models import GRPOPosting, GRPOStatus

        try:
            draft = GRPOPosting.objects.prefetch_related("attachments").get(
                id=posting_id
            )
        except GRPOPosting.DoesNotExist:
            return Response(
                {"detail": "GRPO posting not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        if draft.status not in (GRPOStatus.DRAFT, GRPOStatus.FAILED) or draft.sap_doc_entry:
            return Response(
                {"detail": "Only unposted draft/failed GRPOs can be discarded."},
                status=status.HTTP_400_BAD_REQUEST
            )

        for att in draft.attachments.all():
            if att.file:
                att.file.delete(save=False)
        draft.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PostSavedGRPOAPI(APIView):
    """
    Post a previously-saved GRPO draft to SAP.

    POST /api/grpo/draft/<posting_id>/post/

    On failure the draft is kept (status FAILED) with its saved payload and
    attachments intact, so it can be edited and retried.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateGRPOPosting]

    def post(self, request, posting_id):
        from .models import GRPOPosting, GRPOStatus

        try:
            draft = GRPOPosting.objects.prefetch_related("attachments").get(
                id=posting_id
            )
        except GRPOPosting.DoesNotExist:
            return Response(
                {"detail": "GRPO posting not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        if draft.status not in (GRPOStatus.DRAFT, GRPOStatus.FAILED):
            return Response(
                {"detail": f"Only draft or failed GRPOs can be posted. "
                           f"This posting is '{draft.status}'."},
                status=status.HTTP_400_BAD_REQUEST
            )
        if not draft.attachments.exists():
            return Response(
                {"detail": "At least one attachment is required before posting."},
                status=status.HTTP_400_BAD_REQUEST
            )

        service = GRPOService(company_code=request.company.company.code)
        vehicle_entry_id = draft.vehicle_entry_id

        def notify(error_message):
            notify_material_grpo_failed(
                company=request.company.company,
                user=request.user,
                error_message=error_message,
                vehicle_entry_id=vehicle_entry_id,
            )

        try:
            posting = service.post_saved_grpo(
                grpo_posting_id=posting_id, user=request.user
            )
        except ValueError as e:
            # Draft may already be marked FAILED by the service (e.g. SAP rejected
            # a value surfaced as ValueError); surface the message to the operator.
            notify(str(e))
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except SAPValidationError as e:
            notify(str(e))
            return Response(
                {"detail": f"SAP validation error: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST
            )
        except SAPConnectionError:
            notify("SAP system unavailable")
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        except SAPDataError as e:
            notify(str(e))
            return Response(
                {"detail": f"SAP error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY
            )
        except Exception as e:
            logger.exception("Unexpected error posting saved GRPO")
            # post_saved_grpo only marks FAILED for known errors; guard the rest
            # so the draft still survives an unexpected failure.
            GRPOPosting.objects.filter(id=posting_id).update(
                status=GRPOStatus.FAILED,
                error_message=f"Unexpected error: {e}",
            )
            notify(str(e))
            return Response(
                {"detail": "Unexpected error while posting GRPO."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        response_data = {
            "success": True,
            "grpo_posting_id": posting.id,
            "sap_doc_entry": posting.sap_doc_entry,
            "sap_doc_num": posting.sap_doc_num,
            "sap_doc_total": posting.sap_doc_total,
            "message": f"GRPO posted successfully. SAP Doc Num: {posting.sap_doc_num}",
            "attachments": posting.attachments.all(),
        }
        return Response(
            GRPOPostResponseSerializer(
                response_data, context={"request": request}
            ).data,
            status=status.HTTP_201_CREATED
        )


class PendingServiceGRPOListAPI(APIView):
    """
    Returns dispatch plans pending transport service GRPO posting.

    Paginated (``page`` / ``page_size``), month-filtered (``year`` / ``month``,
    defaulting to the current month for the dispatched backlog), and searchable
    (``search`` over bill no / bilty / vehicle / driver / transporter / entry).

    GET /api/grpo/service/pending/
    """
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

        # Build lightweight rows (no SAP calls yet) so we can search + paginate
        # cheaply, then fetch bill snapshots for the current page only.
        rows = []
        for plan in dispatch_plans:
            rows.append({
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
            })

        if search:
            def _matches(row):
                haystack = " ".join(str(row.get(field) or "") for field in (
                    "sap_invoice_doc_num", "bilty_no", "vehicle_no",
                    "driver_name", "transporter_name", "linked_vehicle_entry_no",
                )).lower()
                return search in haystack
            rows = [row for row in rows if _matches(row)]

        page_rows, meta = paginate_list(rows, page, page_size)

        # The SAP bill-header snapshot is the expensive per-row call. Fetching it
        # for the CURRENT PAGE only (<= page_size live reads) instead of every
        # dispatched bill in the backlog is what keeps this page from timing out.
        page_plans = [plans_by_id[row["dispatch_plan_id"]] for row in page_rows]
        bill_snapshots = service.get_dispatch_bill_snapshots(page_plans)
        for row in page_rows:
            snapshot = bill_snapshots.get(row["dispatch_plan_id"], {})
            row["source_state"] = snapshot.get("state", "") or row["source_state"]

        serializer = ServiceGRPOPendingEntrySerializer(page_rows, many=True)
        return Response(build_page(serializer.data, meta))


class ServiceGRPOOptionsAPI(APIView):
    """
    Returns SAP options for service GRPO selectable fields.

    GET /api/grpo/service/options/
    """
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


class ServiceGRPOPreviewAPI(APIView):
    """
    Returns dispatch booking data required for service GRPO posting.

    GET /api/grpo/service/preview/<dispatch_plan_id>/
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanPreviewBiltyServiceGRPO]

    def get(self, request, dispatch_plan_id):
        service = GRPOService(company_code=request.company.company.code)
        try:
            preview_data = service.get_service_grpo_preview_data(dispatch_plan_id)
        except ValueError as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = ServiceGRPOPreviewSerializer(preview_data)
        return Response(serializer.data)


class PostServiceGRPOAPI(APIView):
    """
    Post a transport service GRPO to SAP for a booked dispatch plan.

    POST /api/grpo/service/post/
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanPostBiltyServiceGRPO]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        if request.content_type and "multipart" in request.content_type:
            try:
                raw_data = request.data.get("data", "{}")
                if isinstance(raw_data, str):
                    parsed_data = json.loads(raw_data)
                else:
                    parsed_data = raw_data
            except json.JSONDecodeError:
                return Response(
                    {"detail": "Invalid JSON in 'data' field"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            attachments = request.FILES.getlist("attachments")
        else:
            parsed_data = request.data
            attachments = []

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
                budget_delivery_point=serializer.validated_data.get("budget_delivery_point"),
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
                bilty_no=serializer.validated_data.get("bilty_no"),
                bilty_date=serializer.validated_data.get("bilty_date"),
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

        except ValueError as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        except SAPValidationError as e:
            notify_service_grpo_failed(
                company=request.company.company,
                user=request.user,
                error_message=str(e),
                dispatch_plan_id=serializer.validated_data.get("dispatch_plan_id"),
            )
            return Response(
                {"detail": f"SAP validation error: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        except SAPConnectionError:
            notify_service_grpo_failed(
                company=request.company.company,
                user=request.user,
                error_message="SAP system unavailable",
                dispatch_plan_id=serializer.validated_data.get("dispatch_plan_id"),
            )
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        except SAPDataError as e:
            notify_service_grpo_failed(
                company=request.company.company,
                user=request.user,
                error_message=str(e),
                dispatch_plan_id=serializer.validated_data.get("dispatch_plan_id"),
            )
            return Response(
                {"detail": f"SAP error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )


class ServiceGRPOPostingHistoryAPI(APIView):
    """
    Returns transport service GRPO posting history.

    Paginated (``page`` / ``page_size``), month-filtered (``year`` / ``month``
    by posting date), status-filtered (``status``) and searchable (``search``).

    GET /api/grpo/service/history/
    GET /api/grpo/service/history/?dispatch_plan_id=123
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBiltyServiceGRPOHistory]

    def get(self, request):
        from django.db.models import Q
        from .models import GRPOStatus

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


class ServiceGRPOPostingDetailAPI(APIView):
    """
    Returns details of a specific service GRPO posting.

    GET /api/grpo/service/<posting_id>/
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBiltyServiceGRPODetail]

    def get(self, request, posting_id):
        from .models import ServiceGRPOPosting

        try:
            posting = ServiceGRPOPosting.objects.select_related(
                "dispatch_plan",
                "dispatch_plan__vehicle",
                "dispatch_plan__vehicle__transporter",
                "dispatch_plan__transporter",
                "dispatch_plan__linked_vehicle_entry",
                "dispatch_plan__linked_vehicle_entry__vehicle",
                "dispatch_plan__linked_vehicle_entry__vehicle__transporter",
                "dispatch_plan__linked_vehicle_entry__driver",
                "posted_by",
            ).prefetch_related("lines", "attachments").get(id=posting_id)
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


class GRPOPostingHistoryAPI(APIView):
    """
    Returns GRPO posting history.

    GET /api/grpo/history/
    GET /api/grpo/history/?vehicle_entry_id=123
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewGRPOHistory]

    def get(self, request):
        from django.db.models import Q
        from .models import GRPOStatus

        vehicle_entry_id = request.GET.get("vehicle_entry_id")
        year, month = get_month_params(request)
        page, page_size = get_page_params(request)
        status_filter = (request.GET.get("status") or "").strip().upper()
        search = (request.GET.get("search") or "").strip()

        service = GRPOService(
            company_code=request.company.company.code,
            entry_type=getattr(self, "entry_type", "RAW_MATERIAL"),
        )
        postings = service.get_grpo_posting_history(
            vehicle_entry_id=int(vehicle_entry_id) if vehicle_entry_id else None,
            year=year,
            month=month,
        )
        if status_filter and status_filter in GRPOStatus.values:
            postings = postings.filter(status=status_filter)
        if search:
            postings = postings.filter(
                Q(vehicle_entry__entry_no__icontains=search)
                | Q(sap_doc_num__icontains=search)
                | Q(po_receipts__po_number__icontains=search)
                | Q(po_receipt__po_number__icontains=search)
                | Q(po_receipts__supplier_name__icontains=search)
            ).distinct()

        # Detect failures that a later successful posting resolved, so the client
        # can hide them from the Failed list. Computed over the FULL filtered set
        # (before slicing) so a page boundary can't miss a superseding posting.
        posted_po_ids, posted_vehicle_entry_ids = service.build_posted_supersede_sets(
            postings
        )

        page_qs, _total, meta = paginate_queryset(postings, page, page_size)
        serializer = GRPOPostingSerializer(
            page_qs,
            many=True,
            context={
                "request": request,
                "posted_po_ids": posted_po_ids,
                "posted_vehicle_entry_ids": posted_vehicle_entry_ids,
            },
        )
        return Response(build_page(serializer.data, meta))


class GRPOPostingDetailAPI(APIView):
    """
    Returns details of a specific GRPO posting.

    GET /api/grpo/<posting_id>/
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewGRPOPosting]

    def get(self, request, posting_id):
        from .models import GRPOPosting

        try:
            posting = GRPOPosting.objects.select_related(
                "vehicle_entry",
                "po_receipt",
                "posted_by"
            ).prefetch_related(
                "lines__po_item_receipt__arrival_slip__inspection",
                "attachments",
                "po_receipts",
            ).get(id=posting_id)
        except GRPOPosting.DoesNotExist:
            return Response(
                {"detail": "GRPO posting not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        serializer = GRPOPostingSerializer(posting,context={"request": request})
        return Response(serializer.data)


class GRPOAttachmentListCreateAPI(APIView):
    """
    List and upload attachments for a GRPO posting.

    GET  /api/grpo/<posting_id>/attachments/
    POST /api/grpo/<posting_id>/attachments/  (multipart/form-data)
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageGRPOAttachments]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request, posting_id):
        from .models import GRPOAttachment

        attachments = GRPOAttachment.objects.filter(
            grpo_posting_id=posting_id
        ).order_by("-uploaded_at")

        serializer = GRPOAttachmentSerializer(
            attachments, many=True, context={"request": request}
        )
        return Response(serializer.data)

    def post(self, request, posting_id):
        serializer = GRPOAttachmentUploadSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid file upload", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )

        service = GRPOService(company_code=request.company.company.code)

        try:
            attachment = service.upload_grpo_attachment(
                grpo_posting_id=posting_id,
                file=serializer.validated_data["file"],
                user=request.user,
            )

            response_serializer = GRPOAttachmentSerializer(
                attachment, context={"request": request}
            )

            return Response(
                response_serializer.data,
                status=status.HTTP_201_CREATED
            )

        except ValueError as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )


class GRPOAttachmentDeleteAPI(APIView):
    """
    Delete a GRPO attachment.

    DELETE /api/grpo/<posting_id>/attachments/<attachment_id>/
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageGRPOAttachments]

    def delete(self, request, posting_id, attachment_id):
        from .models import GRPOAttachment

        try:
            attachment = GRPOAttachment.objects.get(
                id=attachment_id,
                grpo_posting_id=posting_id,
            )
        except GRPOAttachment.DoesNotExist:
            return Response(
                {"detail": "Attachment not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        if attachment.file:
            attachment.file.delete(save=False)

        attachment.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)


class GRPOAttachmentRetryAPI(APIView):
    """
    Retry uploading a FAILED attachment to SAP.

    POST /api/grpo/<posting_id>/attachments/<attachment_id>/retry/
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageGRPOAttachments]

    def post(self, request, posting_id, attachment_id):
        from .models import GRPOAttachment

        if not GRPOAttachment.objects.filter(
            id=attachment_id, grpo_posting_id=posting_id
        ).exists():
            return Response(
                {"detail": "Attachment not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        service = GRPOService(company_code=request.company.company.code)

        try:
            attachment = service.retry_attachment_upload(
                attachment_id=attachment_id
            )

            serializer = GRPOAttachmentSerializer(
                attachment, context={"request": request}
            )
            return Response(serializer.data)

        except ValueError as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )


# ---------------------------------------------------------------------------
# Finished-goods (traded FG purchasing) GRPO surfaces.
#
# Finished goods bought from vendors flow through the exact same material GRPO
# machinery as raw materials (PO -> PurchaseDeliveryNotes, BaseType=22), but on
# gate entries of entry_type "FINISHED_GOODS" and with no QC arrival slip. These
# thin subclasses just re-scope the GRPOService to that entry type; the parent
# views read `self.entry_type`, and the service skips the QC gate for it.
# Detail / attachment endpoints are entry-type agnostic and are reused as-is.
# ---------------------------------------------------------------------------

class FGGRPODashboardSummaryAPI(GRPODashboardSummaryAPI):
    entry_type = "FINISHED_GOODS"


class FGAllGRPOEntriesListAPI(AllGRPOEntriesListAPI):
    entry_type = "FINISHED_GOODS"


class FGPendingGRPOListAPI(PendingGRPOListAPI):
    entry_type = "FINISHED_GOODS"


class FGGRPOPreviewAPI(GRPOPreviewAPI):
    entry_type = "FINISHED_GOODS"


class FGPostGRPOAPI(PostGRPOAPI):
    entry_type = "FINISHED_GOODS"


class FGGRPOPostingHistoryAPI(GRPOPostingHistoryAPI):
    entry_type = "FINISHED_GOODS"
