"""Fixed Asset GRPO endpoints, mounted at ``api/v1/fixed-asset-grpo/``.

Every view is ``[IsAuthenticated, HasCompanyContext, <this app's right>]`` and
every lookup is scoped to ``request.company.company`` **and**
``entry_type="FIXED_ASSET"``: an id from another company or another gate type
is a 404, never someone else's data. None of grpo's rights opens anything
here, and none of grpo's views is changed.

LISTS. summary, all-entries, pending (a PO is linked and not posted),
needs-po (completed, no PO yet), history, history detail, print.

ONE ENTRY. detail; po-receipts (GET / POST / PUT / DELETE: the late PO link);
open POs of a supplier; preview; post; discard a draft; close / reopen ("done
in SAP by hand"); discard a simulated post.

POST, AND WHY IT ANSWERS 201, 202 OR 400. ``post/`` saves the GRPO as a
draft first (nothing from SAP is needed for that) and then sends it through
the SAP posting queue with kind ``fixed_asset_grpo.post``, the way grpo's
material post does: 201 posted (or simulated, ``simulated: true``), 202
waiting for SAP (``code: SAP_QUEUED``; the worker posts it once SAP is back),
400/502 refused (the draft stays FAILED with its data and files, for a
corrected retry with ``grpo_posting_id``), 409 when the same POs are already
on their way. A request makes at most one SAP document write, so the browser
waits at most one writer timeout (180 s); the screen sends a 5-minute timeout.
"""

import json
import logging

from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from driver_management.models import VehicleEntry
from grpo.models import GRPOPosting, GRPOStatus
from grpo.pagination import (
    build_page,
    get_month_params,
    get_page_params,
    paginate_list,
    paginate_queryset,
)
from grpo.serializers import GRPOPostRequestSerializer, GRPOPreviewSerializer
from grpo.views import GRPOPrintAPI
from raw_material_gatein.models import POReceipt
from raw_material_gatein.services import is_over_receipt_enforced
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from sap_postings import services as sap_postings
from sap_postings.models import SapPostingOutcome

from . import flags
from . import sap_posting as fa_sap_posting
from .constants import FA_ENTRY_TYPE
from .notifications import notify_fixed_asset_grpo_failed
from .permissions import (
    CanLinkFixedAssetPO,
    CanPostFixedAssetGRPO,
    CanViewFixedAssetGRPO,
)
from .serializers import (
    FAAllEntrySerializer,
    FAClosureSerializer,
    FAGRPODashboardSummarySerializer,
    FAGRPOPostingSerializer,
    FAGRPOPostResponseSerializer,
    FANeedsPOEntrySerializer,
    FAOpenPOSerializer,
    FAPOLinkRequestSerializer,
)
from .services import (
    UNSENT_STATUSES,
    FixedAssetGRPOService,
    active_source_ids,
    close_fa_entry,
    discard_fa_draft,
    discard_simulated_grpo,
    entry_state,
    fa_detail_of,
    fa_entry_detail,
    gate_attachment_files,
    grpo_on_its_way,
    needs_po_entries,
    needs_po_row,
    receive_fa_po,
    remove_fa_po_receipt,
    reopen_fa_entry,
    reopen_stranded,
    serialize_fa_po_receipt,
    update_fa_po_receipt,
)

logger = logging.getLogger(__name__)

VIEW = [IsAuthenticated, HasCompanyContext, CanViewFixedAssetGRPO]
LINK = [IsAuthenticated, HasCompanyContext, CanLinkFixedAssetPO]
POST = [IsAuthenticated, HasCompanyContext, CanPostFixedAssetGRPO]


def _company(request):
    return request.company.company


def _fa_entry(request, vehicle_entry_id):
    """The FA gate entry ``vehicle_entry_id`` of the request's company, or 404."""
    return get_object_or_404(
        VehicleEntry.objects.select_related("company"),
        id=vehicle_entry_id,
        company=_company(request),
        entry_type=FA_ENTRY_TYPE,
        is_active=True,
    )


def _fa_posting_queryset(request):
    return GRPOPosting.objects.select_related(
        "vehicle_entry", "vehicle_entry__company", "po_receipt", "posted_by", "fa_record",
    ).prefetch_related(
        "lines__po_item_receipt", "attachments", "po_receipts",
    ).filter(
        vehicle_entry__company=_company(request),
        vehicle_entry__entry_type=FA_ENTRY_TYPE,
    )


def _fa_posting(request, posting_id):
    return get_object_or_404(_fa_posting_queryset(request), id=posting_id)


def _parse_multipart(request):
    """``(data, files)``: JSON in a "data" part and files in "attachments" parts,
    or a plain JSON body with no files (grpo's request shape). Raises
    ``json.JSONDecodeError`` on a malformed "data" part."""
    if request.content_type and "multipart" in request.content_type:
        raw = request.data.get("data", "{}")
        data = json.loads(raw) if isinstance(raw, str) else raw
        files = request.FILES.getlist("attachments")
    else:
        data = request.data
        files = []
    return dict(data or {}), list(files)


def _bad_request(detail, code=None, **extra):
    body = {"detail": detail, **extra}
    if code:
        body["code"] = code
    return Response(body, status=status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# lists
# ---------------------------------------------------------------------------

class FAGRPODashboardSummaryAPI(APIView):
    """GET summary/ -- the FA dashboard numbers, counted over FA entries only."""

    permission_classes = VIEW

    def get(self, request):
        summary = FixedAssetGRPOService(_company(request).code).get_grpo_dashboard_summary()
        return Response(FAGRPODashboardSummarySerializer(summary).data)


class FAAllEntriesListAPI(APIView):
    """GET all-entries/ -- every FA gate entry in scope, with where it stands.

    Query: ``year`` + ``month``, ``search`` (entry no, supplier, PO, work
    order), ``state`` (AT_GATE, NEEDS_PO, READY, SENDING, SIMULATED, POSTED,
    CLOSED), ``include_closed=1`` to show entries done in SAP by hand.
    """

    permission_classes = VIEW

    def get(self, request):
        service = FixedAssetGRPOService(_company(request).code)
        year, month = get_month_params(request)
        page, page_size = get_page_params(request)
        search = (request.GET.get("search") or "").strip().lower()
        state_filter = (request.GET.get("state") or "").strip().upper()
        include_closed = (request.GET.get("include_closed") or "").lower() in ("1", "true", "yes")

        entries = service.get_all_grpo_visible_entries(
            year=year, month=month, include_closed=include_closed
        ).select_related("fixed_asset_entry", "fa_grpo_closure")

        rows = []
        counts = {}
        for entry in entries:
            detail = fa_detail_of(entry)
            state = entry_state(entry)
            counts[state] = counts.get(state, 0) + 1
            rows.append({
                "vehicle_entry_id": entry.id,
                "entry_no": entry.entry_no,
                "entry_time": entry.entry_time,
                "status": entry.status,
                "state": state,
                "work_order_number": getattr(detail, "work_order_number", "") or "",
                "supplier_name": getattr(detail, "supplier_name", "") or "",
                "invoice_number": getattr(detail, "invoice_number", "") or "",
                "po_numbers": [po.po_number for po in entry.po_receipts.all()],
            })
        counts["ALL"] = len(rows)

        if search:
            rows = [
                row for row in rows
                if search in " ".join([
                    row["entry_no"] or "", row["supplier_name"], row["work_order_number"],
                    row["invoice_number"], " ".join(row["po_numbers"]),
                ]).lower()
            ]
        if state_filter:
            rows = [row for row in rows if row["state"] == state_filter]

        page_rows, meta = paginate_list(rows, page, page_size)
        return Response({
            **build_page(FAAllEntrySerializer(page_rows, many=True).data, meta),
            "counts": counts,
        })


class FAPendingGRPOListAPI(APIView):
    """GET pending/ -- completed FA entries with a linked PO not yet posted ("Ready").

    The same row shape as grpo's ``pending/``, plus the FA detail's supplier
    and work order. Closed entries and entries before the cut-over are left out.
    """

    permission_classes = VIEW

    def get(self, request):
        from collections import defaultdict

        service = FixedAssetGRPOService(_company(request).code)
        year, month = get_month_params(request)
        page, page_size = get_page_params(request)
        search = (request.GET.get("search") or "").strip().lower()

        rows = []
        for entry in service.get_pending_grpo_entries(year=year, month=month):
            po_receipts = list(entry.po_receipts.all())
            posted_po_ids = set()
            for grpo in entry.grpo_postings.filter(status=GRPOStatus.POSTED):
                posted_po_ids.update(grpo.po_receipts.values_list("id", flat=True))
                if grpo.po_receipt_id:
                    posted_po_ids.add(grpo.po_receipt_id)
            pending = [po for po in po_receipts if po.id not in posted_po_ids]
            if not pending:
                continue

            groups = defaultdict(list)
            for po in pending:
                groups[po.supplier_code].append({
                    "po_receipt_id": po.id,
                    "po_number": po.po_number,
                    "supplier_code": po.supplier_code,
                    "supplier_name": po.supplier_name,
                    "branch_id": po.branch_id,
                    "item_count": po.items.count(),
                    "po_date": service.resolve_po_date(po),
                })
            suppliers = [
                {
                    "supplier_code": code,
                    "supplier_name": pos[0]["supplier_name"],
                    "po_count": len(pos),
                    "can_merge": len(pos) > 1,
                    "po_receipts": pos,
                }
                for code, pos in groups.items()
            ]
            dates = [pr["po_date"] for pos in groups.values() for pr in pos if pr["po_date"]]
            detail = fa_detail_of(entry)
            rows.append({
                "vehicle_entry_id": entry.id,
                "entry_no": entry.entry_no,
                "status": entry.status,
                "entry_time": entry.entry_time,
                "po_date": min(dates) if dates else None,
                "total_po_count": len(po_receipts),
                "posted_po_count": len(po_receipts) - len(pending),
                "pending_po_count": len(pending),
                "is_fully_posted": False,
                "work_order_number": getattr(detail, "work_order_number", "") or "",
                "fa_supplier_name": getattr(detail, "supplier_name", "") or "",
                "invoice_number": getattr(detail, "invoice_number", "") or "",
                "suppliers": suppliers,
            })

        if search:
            rows = [
                row for row in rows
                if search in " ".join([
                    row["entry_no"] or "",
                    row["fa_supplier_name"],
                    row["work_order_number"],
                    " ".join(pr["po_number"] or "" for s in row["suppliers"] for pr in s["po_receipts"]),
                    " ".join(s["supplier_name"] or "" for s in row["suppliers"]),
                ]).lower()
            ]
        page_rows, meta = paginate_list(rows, page, page_size)
        return Response(build_page(page_rows, meta))


class FANeedsPOListAPI(APIView):
    """GET needs-po/ -- completed FA entries with no PO linked yet."""

    permission_classes = VIEW

    def get(self, request):
        year, month = get_month_params(request)
        page, page_size = get_page_params(request)
        search = (request.GET.get("search") or "").strip().lower()
        rows = [needs_po_row(entry) for entry in needs_po_entries(
            _company(request).code, year=year, month=month
        )]
        if search:
            rows = [
                row for row in rows
                if search in " ".join([
                    row["entry_no"] or "", row["supplier_name"], row["work_order_number"],
                    row["invoice_number"],
                ]).lower()
            ]
        page_rows, meta = paginate_list(rows, page, page_size)
        return Response(build_page(FANeedsPOEntrySerializer(page_rows, many=True).data, meta))


class FAGRPOPostingHistoryAPI(APIView):
    """GET history/ -- FA GRPO postings (posted, simulated, failed, waiting, drafts).

    Query: ``vehicle_entry_id``, ``year`` + ``month``, ``status``, ``search``,
    ``simulated=true|false``.
    """

    permission_classes = VIEW

    def get(self, request):
        service = FixedAssetGRPOService(_company(request).code)
        vehicle_entry_id = request.GET.get("vehicle_entry_id")
        year, month = get_month_params(request)
        page, page_size = get_page_params(request)
        status_filter = (request.GET.get("status") or "").strip().upper()
        search = (request.GET.get("search") or "").strip()
        simulated = (request.GET.get("simulated") or "").strip().lower()

        try:
            entry_id = int(vehicle_entry_id) if vehicle_entry_id else None
        except ValueError:
            return _bad_request("vehicle_entry_id must be a number.")
        postings = service.get_grpo_posting_history(
            vehicle_entry_id=entry_id, year=year, month=month
        ).select_related("fa_record")
        if status_filter and status_filter in GRPOStatus.values:
            postings = postings.filter(status=status_filter)
        if simulated in ("true", "1", "yes"):
            postings = postings.filter(fa_record__simulated=True)
        elif simulated in ("false", "0", "no"):
            postings = postings.exclude(fa_record__simulated=True)
        if search:
            postings = postings.filter(
                Q(vehicle_entry__entry_no__icontains=search)
                | Q(sap_doc_num__icontains=search)
                | Q(po_receipts__po_number__icontains=search)
                | Q(po_receipt__po_number__icontains=search)
                | Q(po_receipts__supplier_name__icontains=search)
            ).distinct()

        # A simulated post resolves nothing: a real attempt that failed while it
        # existed is still a failure (often the "A simulated GRPO exists" one).
        posted_po_ids, posted_entry_ids = service.build_posted_supersede_sets(
            postings.exclude(fa_record__simulated=True)
        )
        page_qs, _total, meta = paginate_queryset(postings, page, page_size)
        data = FAGRPOPostingSerializer(
            page_qs,
            many=True,
            context={
                "request": request,
                "posted_po_ids": posted_po_ids,
                "posted_vehicle_entry_ids": posted_entry_ids,
            },
        ).data
        return Response(build_page(data, meta))


class FAGRPOPostingDetailAPI(APIView):
    """GET history/<posting_id>/ -- one FA GRPO posting of this company."""

    permission_classes = VIEW

    def get(self, request, posting_id):
        posting = _fa_posting(request, posting_id)
        return Response(FAGRPOPostingSerializer(posting, context={"request": request}).data)


class FAGRPOPrintAPI(GRPOPrintAPI):
    """GET history/<posting_id>/print/ -- SAP's goods receipt note for an FA GRPO.

    grpo's print view, behind this app's right and scoped to the company and
    to FA entries first (grpo's own looks the posting up by id alone). A
    simulated post has no SAP document, so it answers 404 as an unposted one.
    """

    permission_classes = VIEW

    def get(self, request, posting_id):
        _fa_posting(request, posting_id)
        return super().get(request, posting_id)


class FAGRPODiscardSimulatedAPI(APIView):
    """POST history/<posting_id>/discard-simulated/ -- delete a simulated post (204)."""

    permission_classes = POST

    def post(self, request, posting_id):
        posting = _fa_posting(request, posting_id)
        try:
            discard_simulated_grpo(posting=posting, user=request.user)
        except ValueError as exc:
            return _bad_request(str(exc))
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# one entry
# ---------------------------------------------------------------------------

class FAEntryDetailAPI(APIView):
    """GET entries/<vehicle_entry_id>/ -- the entry, its PO link, open draft, closure and SAP flags."""

    permission_classes = VIEW

    def get(self, request, vehicle_entry_id):
        entry = _fa_entry(request, vehicle_entry_id)
        return Response(fa_entry_detail(entry, _company(request).code))


class FAPOReceiptListCreateAPI(APIView):
    """The late PO link on one FA entry.

    GET  entries/<id>/po-receipts/  -- the linked PO(s) (view right).
    POST entries/<id>/po-receipts/  -- link an open SAP PO (link or post right).
         Body: ``{po_number, supplier_code, supplier_name, invoice_no?,
         items:[{line_num, po_item_code, item_name, received_qty, uom}]}``.
         201 ``{message, po_receipt, warnings}``.
    """

    def get_permissions(self):
        classes = VIEW if self.request.method == "GET" else LINK
        return [cls() for cls in classes]

    def get(self, request, vehicle_entry_id):
        entry = _fa_entry(request, vehicle_entry_id)
        receipts = entry.po_receipts.prefetch_related("items").order_by("id")
        return Response([serialize_fa_po_receipt(r) for r in receipts])

    def post(self, request, vehicle_entry_id):
        entry = _fa_entry(request, vehicle_entry_id)
        serializer = FAPOLinkRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, warnings = receive_fa_po(
            vehicle_entry=entry,
            data=serializer.validated_data,
            user=request.user,
            company_code=_company(request).code,
        )
        receipt = POReceipt.objects.prefetch_related("items").get(id=receipt.id)
        return Response(
            {
                "message": "The SAP PO is linked to this fixed asset entry.",
                "po_receipt": serialize_fa_po_receipt(receipt),
                "warnings": warnings,
            },
            status=status.HTTP_201_CREATED,
        )


class FAPOReceiptDetailAPI(APIView):
    """Change or remove the PO link.

    PUT    entries/<id>/po-receipts/<po_receipt_id>/  -- 200 ``{po_receipt, warnings}``.
    DELETE entries/<id>/po-receipts/<po_receipt_id>/  -- 204.

    Either first discards the entry's unsent GRPO drafts for this PO; a GRPO
    posted, simulated or on its way to SAP locks the link (400 with the reason).
    """

    permission_classes = LINK

    def _receipt(self, request, vehicle_entry_id, po_receipt_id):
        entry = _fa_entry(request, vehicle_entry_id)
        return get_object_or_404(
            POReceipt.objects.select_related("vehicle_entry").prefetch_related("items"),
            id=po_receipt_id,
            vehicle_entry=entry,
        )

    def put(self, request, vehicle_entry_id, po_receipt_id):
        receipt = self._receipt(request, vehicle_entry_id, po_receipt_id)
        serializer = FAPOLinkRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt, warnings = update_fa_po_receipt(
            po_receipt=receipt,
            data=serializer.validated_data,
            user=request.user,
            company_code=_company(request).code,
        )
        receipt = POReceipt.objects.prefetch_related("items").get(id=receipt.id)
        return Response({"po_receipt": serialize_fa_po_receipt(receipt), "warnings": warnings})

    def delete(self, request, vehicle_entry_id, po_receipt_id):
        receipt = self._receipt(request, vehicle_entry_id, po_receipt_id)
        remove_fa_po_receipt(po_receipt=receipt, user=request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class FAOpenPOListAPI(APIView):
    """GET entries/<id>/open-pos/?supplier_code= -- open FA PO lines of a supplier.

    Item lines of the supplier's open POs whose warehouse is in
    ``FIXED_ASSET_WAREHOUSES``, with the warehouse, tax code, G/L and variety
    grpo's PO serializer leaves out.
    """

    permission_classes = LINK

    def get(self, request, vehicle_entry_id):
        from sap_purchasing.fa_po_reader import get_open_fixed_asset_pos

        _fa_entry(request, vehicle_entry_id)
        supplier_code = (request.GET.get("supplier_code") or "").strip()
        if not supplier_code:
            return _bad_request("supplier_code is required.")
        code = _company(request).code
        try:
            pos = get_open_fixed_asset_pos(code, supplier_code)
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError as exc:
            return Response(
                {"detail": f"SAP data error: {exc}"}, status=status.HTTP_502_BAD_GATEWAY
            )
        except ValueError as exc:
            return _bad_request(str(exc))
        return Response({
            "results": FAOpenPOSerializer(pos, many=True).data,
            "warehouses": flags.warehouses(),
            "over_receipt_enforced": is_over_receipt_enforced(code),
        })


class FAGRPOPreviewAPI(APIView):
    """GET entries/<id>/preview/[?po_receipt_ids=1,2] -- grpo's preview of an FA entry."""

    permission_classes = VIEW

    def get(self, request, vehicle_entry_id):
        entry = _fa_entry(request, vehicle_entry_id)
        po_receipt_ids = None
        raw = request.GET.get("po_receipt_ids")
        if raw:
            try:
                po_receipt_ids = [int(x) for x in raw.split(",")]
            except ValueError:
                return _bad_request("Invalid po_receipt_ids format. Use comma-separated integers.")
        service = FixedAssetGRPOService(_company(request).code)
        try:
            data = service.get_grpo_preview_data(entry.id, po_receipt_ids=po_receipt_ids)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        return Response(GRPOPreviewSerializer(data, many=True).data)


class FAEntryCloseAPI(APIView):
    """"Done in SAP by hand".

    POST   entries/<id>/close/  ``{sap_doc_num?, note?}`` (one is needed) -- 201.
    DELETE entries/<id>/close/  -- reopen, 204.
    """

    permission_classes = POST

    def post(self, request, vehicle_entry_id):
        entry = _fa_entry(request, vehicle_entry_id)
        serializer = FAClosureSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            closure = close_fa_entry(
                vehicle_entry=entry,
                user=request.user,
                sap_doc_num=serializer.validated_data.get("sap_doc_num", ""),
                note=serializer.validated_data.get("note", ""),
            )
        except ValueError as exc:
            return _bad_request(str(exc))
        return Response(
            {
                "vehicle_entry_id": entry.id,
                "sap_doc_num": closure.sap_doc_num,
                "note": closure.note,
                "closed_at": closure.closed_at,
            },
            status=status.HTTP_201_CREATED,
        )

    def delete(self, request, vehicle_entry_id):
        entry = _fa_entry(request, vehicle_entry_id)
        try:
            reopen_fa_entry(vehicle_entry=entry, user=request.user)
        except ValueError as exc:
            return _bad_request(str(exc))
        return Response(status=status.HTTP_204_NO_CONTENT)


class FAGRPODraftAPI(APIView):
    """DELETE drafts/<posting_id>/ -- discard an unsent FA GRPO draft (204)."""

    permission_classes = POST

    def delete(self, request, posting_id):
        posting = _fa_posting(request, posting_id)
        try:
            discard_fa_draft(posting=posting, user=request.user)
        except ValueError as exc:
            return _bad_request(str(exc))
        return Response(status=status.HTTP_204_NO_CONTENT)


class FAPostGRPOAPI(APIView):
    """POST post/ -- save the FA GRPO and send it to SAP (or simulate it).

    Multipart: grpo's GRPO request as JSON in a "data" part (plus an optional
    ``grpo_posting_id`` to reuse an open DRAFT/FAILED draft of this entry, or a
    QUEUED one whose SAP postings row was cancelled), and
    files in "attachments" parts. No files: the gate entry's invoice photos
    are used instead. See the module docstring for the answers.
    """

    permission_classes = POST
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        if not flags.enabled():
            return _bad_request(
                "Fixed-asset GRPO posting is switched off.", code="FA_GRPO_DISABLED"
            )
        try:
            data, uploaded = _parse_multipart(request)
        except json.JSONDecodeError:
            return _bad_request("Invalid JSON in 'data' field")

        grpo_posting_id = data.pop("grpo_posting_id", None)
        if grpo_posting_id not in (None, ""):
            try:
                grpo_posting_id = int(grpo_posting_id)
            except (TypeError, ValueError):
                return _bad_request("grpo_posting_id must be a number.")
        else:
            grpo_posting_id = None

        serializer = GRPOPostRequestSerializer(data=data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid request data", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        v = serializer.validated_data
        entry = _fa_entry(request, v["vehicle_entry_id"])
        service = FixedAssetGRPOService(_company(request).code)

        try:
            service.assert_entry_postable(entry)
        except ValueError as exc:
            return _bad_request(str(exc))
        if v.get("doc_date") and v["doc_date"] > timezone.localdate():
            return _bad_request("The posting date cannot be in the future.")

        in_flight = grpo_on_its_way(entry.id, v["po_receipt_ids"])
        if in_flight is not None:
            return Response(
                {
                    "detail": (
                        "This entry's GRPO is already on its way to SAP: it is waiting "
                        "for SAP and will post by itself once SAP is back."
                    ),
                    "code": "SAP_QUEUED",
                    "grpo_posting_id": in_flight.id,
                },
                status=status.HTTP_409_CONFLICT,
            )

        draft = None
        if grpo_posting_id is not None:
            # DRAFT, FAILED, or a QUEUED one whose SAP postings row was
            # cancelled (stranded): never one the queue still holds.
            draft = GRPOPosting.objects.filter(
                id=grpo_posting_id,
                vehicle_entry=entry,
                status__in=UNSENT_STATUSES,
            ).first()
            if draft is None:
                return _bad_request(
                    "That saved GRPO is not an open draft of this entry; reload the page."
                )
            if draft.id in active_source_ids([draft.id]):
                return Response(
                    {
                        "detail": "That saved GRPO is already on its way to SAP.",
                        "code": "SAP_QUEUED",
                        "grpo_posting_id": draft.id,
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            # grpo's save_grpo_draft re-uses only DRAFT/FAILED drafts.
            reopen_stranded(draft)

        try:
            warehouse = service.lines_warehouse(v["po_receipt_ids"], v["items"])
        except ValueError as exc:
            return _bad_request(str(exc), code="FA_GRPO_MIXED_WAREHOUSES")
        if warehouse:
            data["warehouse_code"] = warehouse

        draft_has_files = draft is not None and draft.attachments.exists()
        attachments = uploaded or ([] if draft_has_files else gate_attachment_files(entry))
        if not attachments and not draft_has_files:
            return _bad_request("Attach the invoice — the gate entry has no photo either.")

        try:
            draft = service.save_grpo_draft(
                vehicle_entry_id=entry.id,
                po_receipt_ids=v["po_receipt_ids"],
                user=request.user,
                request_payload=data,
                attachments=attachments,
                grpo_posting_id=draft.id if draft is not None else None,
            )
        except ValueError as exc:
            return _bad_request(str(exc))
        return send_fa_draft_to_sap(request, draft, service)


def send_fa_draft_to_sap(request, draft, service):
    """Send a saved FA GRPO through the queue and answer as the screen expects."""
    receipts = service.draft_po_receipts(draft)
    try:
        queue_row, outcome = sap_postings.post_now(
            kind=fa_sap_posting.KIND,
            company=_company(request),
            source_id=draft.id,
            title=fa_sap_posting.title_for(draft, [pr.po_number for pr in receipts]),
            link=fa_sap_posting.draft_link(draft),
            user=request.user,
        )
    except sap_postings.PostingInProgress as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)

    if outcome.kind == SapPostingOutcome.WAITING:
        message = (
            "Saved. SAP is not answering, so this GRPO is waiting and will post "
            "by itself once SAP is back."
        )
        return Response(
            {
                "success": True,
                "queued": True,
                "code": "SAP_QUEUED",
                "grpo_posting_id": draft.id,
                "sap_posting_id": queue_row.pk,
                "message": message,
                "detail": outcome.message or message,
            },
            status=status.HTTP_202_ACCEPTED,
        )

    if outcome.kind == SapPostingOutcome.REJECTED:
        error = outcome.context.get("error")
        notify_fixed_asset_grpo_failed(
            company=_company(request),
            user=request.user,
            error_message=outcome.message,
            vehicle_entry_id=draft.vehicle_entry_id,
        )
        body = {"grpo_posting_id": draft.id}
        if isinstance(error, SAPDataError):
            return Response(
                {"detail": f"SAP error: {outcome.message}", **body},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        if isinstance(error, SAPValidationError):
            return Response(
                {"detail": f"SAP validation error: {outcome.message}", **body},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({"detail": outcome.message, **body}, status=status.HTTP_400_BAD_REQUEST)

    posting = outcome.context["posting"]
    simulated = bool(outcome.context.get("simulated"))
    message = (
        "Simulated: every check passed and nothing was sent to SAP. Discard it from "
        "history before posting for real."
        if simulated
        else f"GRPO posted successfully. SAP Doc Num: {posting.sap_doc_num}"
    )
    response = {
        "success": True,
        "grpo_posting_id": posting.id,
        "sap_doc_entry": posting.sap_doc_entry,
        "sap_doc_num": posting.sap_doc_num,
        "sap_doc_total": posting.sap_doc_total,
        "message": message,
        "attachments": posting.attachments.all(),
        "simulated": simulated,
    }
    return Response(
        FAGRPOPostResponseSerializer(response, context={"request": request}).data,
        status=status.HTTP_201_CREATED,
    )
