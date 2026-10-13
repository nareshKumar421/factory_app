"""
The cash book -> SAP API, mounted at ``/api/v1/cash-sap/``.

One screen sits on most of it: a bunch's SAP page. ``GET bunches/<id>/`` is the
plan; ``post/`` freezes and sends one document; the browser then loops
``send-next/`` (five-minute client timeout, one SAP write per call) until
``remaining`` is 0. The worker sends whatever the browser leaves.

Everything is scoped to the company on the request header: bunches, entries,
branches, batches and GRPOs are loaded with ``company=request.company.company``
(batches and GRPOs through their bunch), so another company's ids are 404s.

Errors: services raise DRF ``ValidationError`` -> 400, with ``code`` set for
``CASH_SAP_DISABLED`` and ``BUNCH_CHANGED``. SAP exceptions that reach a view
(re-check with HANA down) go to the project's ``sap_client.drf`` handler: 503.
"""

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from cash_book.models import CashBranch, CashBunch, CashEntry
from company.permissions import HasCompanyContext

from . import flags, services
from .models import GLItemMap, ImprestBatch, ImprestGRPO, ImprestVoucherBill
from .permissions import (
    POST_PERMISSION,
    CanPostCashToSap,
    CanSearchSapItems,
    CashSapReadOrPost,
    CashSapSettingsPermission,
    VoucherInfoPermission,
)
from .plan import build_plan
from .serializers import (
    AcceptChangesSerializer,
    BillSerializer,
    BranchRoutingSerializer,
    BranchRoutingWriteSerializer,
    GLItemMapSerializer,
    GLItemMapWriteSerializer,
    GrpoRedateSerializer,
    LeaveOutSerializer,
    PostBunchSerializer,
    SendNextSerializer,
    StopSerializer,
    VoucherInfoSerializer,
    VoucherOverrideSerializer,
)

#: The most bunch ids one status request may name.
MAX_STATUS_IDS = 200


def _company(request):
    return request.company.company


def _bunch(request, bunch_id) -> CashBunch:
    return get_object_or_404(CashBunch.objects.select_related("company"), pk=bunch_id,
                             company=_company(request))


def _entry(request, entry_id) -> CashEntry:
    return get_object_or_404(CashEntry, pk=entry_id, company=_company(request))


def _plan_response(request, bunch, *, extra=None, status_code=status.HTTP_200_OK):
    data = build_plan(bunch).as_dict()
    if not request.user.has_perm(POST_PERMISSION):
        data["can_post"] = False
        for group in data.get("groups", []):
            group["can_post"] = False
    if extra:
        data.update(extra)
    return Response(data, status=status_code)


def _with_plan_gate(request, payload: dict) -> dict:
    """``{sent, remaining, plan}`` with ``can_post`` ANDed with the caller's right."""
    plan = payload.get("plan")
    if plan and not request.user.has_perm(POST_PERMISSION):
        plan["can_post"] = False
    return payload


class CashSapOptionsAPI(APIView):
    """GET the lists and switches the SAP screens need."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CashSapReadOrPost]

    def get(self, request):
        return Response(services.options(_company(request), request.user))


class BunchSapStatusListAPI(APIView):
    """GET ``?ids=1,2,3`` -- the SAP badge for each bunch on the bunches page."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CashSapReadOrPost]

    def get(self, request):
        raw = request.query_params.get("ids", "")
        try:
            ids = [int(part) for part in raw.split(",") if part.strip()]
        except ValueError:
            raise ValidationError({"ids": "Give bunch ids as numbers separated by commas."})
        if len(ids) > MAX_STATUS_IDS:
            raise ValidationError({"ids": f"At most {MAX_STATUS_IDS} bunches at a time."})
        return Response({"results": services.bunch_status_summaries(_company(request), ids)})


class BunchSapPlanAPI(APIView):
    """GET the bunch's SAP plan - DELETE to reset it (nothing in SAP yet)."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CashSapReadOrPost]

    def get(self, request, bunch_id):
        return _plan_response(request, _bunch(request, bunch_id))

    def delete(self, request, bunch_id):
        bunch = _bunch(request, bunch_id)
        services.reset_bunch(user=request.user, bunch=bunch)
        return _plan_response(request, bunch)


class BunchSapPostAPI(APIView):
    """POST: freeze the bunch, queue its documents, send one."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanPostCashToSap]

    def post(self, request, bunch_id):
        serializer = PostBunchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        bunch = _bunch(request, bunch_id)
        result = services.post_bunch(
            user=request.user,
            bunch=bunch,
            sap_company_code=serializer.validated_data.get("sap_company_code") or None,
        )
        return Response(_with_plan_gate(request, result))


class BunchSapSendNextAPI(APIView):
    """POST: send the next document (at most one SAP write)."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanPostCashToSap]

    def post(self, request, bunch_id):
        serializer = SendNextSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = services.send_next(
            user=request.user,
            bunch=_bunch(request, bunch_id),
            include_failed=serializer.validated_data["include_failed"],
            skip_posting_ids=serializer.validated_data["skip_posting_ids"],
        )
        return Response(_with_plan_gate(request, result))


class BunchSapStopAPI(APIView):
    """POST ``{reason}``: stop sending; waiting and refused postings are cancelled."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanPostCashToSap]

    def post(self, request, bunch_id):
        serializer = StopSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = services.stop_bunch(user=request.user, bunch=_bunch(request, bunch_id),
                                     reason=serializer.validated_data["reason"])
        plan = result.pop("plan")
        plan.update(result)
        return Response(plan)


class BunchSapRecheckAPI(APIView):
    """POST: read SAP back (cancelled documents, deleted or added A/P drafts)."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanPostCashToSap]

    def post(self, request, bunch_id):
        return Response(services.recheck_bunch(user=request.user, bunch=_bunch(request, bunch_id)))


class BunchSapAcceptChangesAPI(APIView):
    """POST ``{note}``: accept that the bunch changed in the cash book after freezing."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanPostCashToSap]

    def post(self, request, bunch_id):
        serializer = AcceptChangesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(services.accept_bunch_changes(
            user=request.user, bunch=_bunch(request, bunch_id), note=serializer.validated_data["note"]))


class BatchClosePOAPI(APIView):
    """POST: close the batch's PO in SAP after vouchers were left out."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanPostCashToSap]

    def post(self, request, bunch_id, batch_id):
        bunch = _bunch(request, bunch_id)
        batch = get_object_or_404(ImprestBatch, pk=batch_id, bunch=bunch)
        return Response(_with_plan_gate(request, services.request_po_close(
            user=request.user, bunch=bunch, batch=batch)))


class VoucherSapOverrideAPI(APIView):
    """PATCH how one voucher of the bunch posts (and its bill facts)."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanPostCashToSap]

    def patch(self, request, bunch_id, entry_id):
        bunch = _bunch(request, bunch_id)
        entry = get_object_or_404(CashEntry, pk=entry_id, company=_company(request), bunch=bunch)
        serializer = VoucherOverrideSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.update_voucher_override(user=request.user, bunch=bunch, entry=entry,
                                         data=serializer.validated_data)
        return _plan_response(request, bunch)

    put = patch


class VoucherLeaveOutAPI(APIView):
    """POST ``{reason}``: leave a goods voucher out after its PO is in SAP."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanPostCashToSap]

    def post(self, request, bunch_id, entry_id):
        bunch = _bunch(request, bunch_id)
        entry = get_object_or_404(CashEntry, pk=entry_id, company=_company(request), bunch=bunch)
        serializer = LeaveOutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(services.leave_out_after_po(
            user=request.user, bunch=bunch, entry=entry, reason=serializer.validated_data["reason"]))


class GrpoRedateAPI(APIView):
    """PATCH a GRPO's posting date or G.no. (PO in SAP, GRPO not yet)."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanPostCashToSap]

    def patch(self, request, bunch_id, grpo_id):
        bunch = _bunch(request, bunch_id)
        grpo = get_object_or_404(ImprestGRPO, pk=grpo_id, batch__bunch=bunch)
        serializer = GrpoRedateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.redate_grpo(user=request.user, bunch=bunch, grpo=grpo,
                             posting_date=serializer.validated_data.get("posting_date"),
                             gate_entry_no=serializer.validated_data.get("gate_entry_no"))
        return _plan_response(request, bunch)


class VoucherInfoAPI(APIView):
    """GET / PATCH a voucher's bill date, G.no. and bill no. (with date warnings)."""

    permission_classes = [IsAuthenticated, HasCompanyContext, VoucherInfoPermission]

    def get(self, request, entry_id):
        return Response(services.voucher_info(_entry(request, entry_id)))

    def patch(self, request, entry_id):
        serializer = VoucherInfoSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(services.update_voucher_info(
            user=request.user, entry=_entry(request, entry_id), data=serializer.validated_data))

    put = patch


class VoucherBillAPI(APIView):
    """POST (multipart ``file``): a posting bill for a voucher."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanPostCashToSap]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request, entry_id):
        entry = _entry(request, entry_id)
        bill = services.attach_bill(user=request.user, entry=entry, upload=request.FILES.get("file"))
        return Response(BillSerializer(bill, context={"request": request}).data,
                        status=status.HTTP_201_CREATED)


class VoucherBillDetailAPI(APIView):
    """DELETE a posting bill that is not in SAP yet."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanPostCashToSap]

    def delete(self, request, bill_id):
        bill = get_object_or_404(
            ImprestVoucherBill.objects.select_related("voucher", "voucher__grpo"),
            pk=bill_id, voucher__entry__company=_company(request),
        )
        services.remove_bill(user=request.user, bill=bill)
        return Response(status=status.HTTP_204_NO_CONTENT)


class BranchRoutingListAPI(APIView):
    """GET every branch of the company (retired too) with its SAP routing."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CashSapSettingsPermission]

    def get(self, request):
        rows = services.routing_rows(_company(request))
        return Response(BranchRoutingSerializer(rows, many=True).data)


class BranchRoutingDetailAPI(APIView):
    """PUT / PATCH one branch's routing."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CashSapSettingsPermission]

    def patch(self, request, branch_id):
        branch = get_object_or_404(CashBranch, pk=branch_id, company=_company(request))
        serializer = BranchRoutingWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        row = services.save_routing(user=request.user, company=_company(request), branch=branch,
                                    data=serializer.validated_data)
        return Response(BranchRoutingSerializer((branch, row)).data)

    put = patch


class GLItemMapListCreateAPI(APIView):
    """GET the G/L item map (``?sap_company_code=&include_retired=true``) - POST a row."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CashSapSettingsPermission]

    def get(self, request):
        rows = services.gl_map_rows(
            sap_company_code=request.query_params.get("sap_company_code") or None,
            include_retired=request.query_params.get("include_retired") == "true",
        )
        return Response(GLItemMapSerializer(rows, many=True).data)

    def post(self, request):
        serializer = GLItemMapWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        row, warnings = services.save_gl_map_row(user=request.user, data=serializer.validated_data)
        return Response({"row": GLItemMapSerializer(row).data, "warnings": warnings},
                        status=status.HTTP_201_CREATED)


class GLItemMapDetailAPI(APIView):
    """PATCH a mapping - DELETE retires it."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CashSapSettingsPermission]

    def patch(self, request, pk):
        row = get_object_or_404(GLItemMap, pk=pk)
        serializer = GLItemMapWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        row, warnings = services.save_gl_map_row(user=request.user, row=row, data=serializer.validated_data)
        return Response({"row": GLItemMapSerializer(row).data, "warnings": warnings})

    put = patch

    def delete(self, request, pk):
        row = get_object_or_404(GLItemMap, pk=pk)
        return Response(GLItemMapSerializer(services.retire_gl_map_row(user=request.user, row=row)).data)


class SapItemSearchAPI(APIView):
    """GET ``?sap_company_code=&search=`` -- SAP items for the map and the override."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanSearchSapItems]

    def get(self, request):
        from sap_client.client import SAPClient

        code = (request.query_params.get("sap_company_code") or "").strip()
        if not flags.is_known_company(code):
            raise ValidationError({"sap_company_code": f"{code or '(blank)'} is not a SAP company this app knows."})
        search = (request.query_params.get("search") or "").strip()
        return Response({"results": SAPClient(code).lookup_items(search, 20)})
