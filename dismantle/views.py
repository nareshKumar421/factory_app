import logging

from django.core.exceptions import PermissionDenied

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from gate_core.services.user_scope import user_company_ids, wants_all_companies
from sap_client.exceptions import (
    SAPConnectionError,
    SAPDataError,
    SAPValidationError,
)

from .guards import DismantleGuardError
from .permissions import (
    CanCreateDismantle,
    CanEditDismantle,
    CanPostDismantle,
    CanViewDismantle,
)
from .serializers import (
    DismantleBulkCreateSerializer,
    DismantleComponentsSaveSerializer,
    DismantleCreateSerializer,
    DismantleDetailSerializer,
    DismantleHeaderPatchSerializer,
    DismantleListSerializer,
)
from .services import DismantleService

logger = logging.getLogger(__name__)


def _service(request):
    return DismantleService(company=request.company.company)


def _allowed_ids(request):
    return user_company_ids(request)


def _validation_error(serializer):
    return Response(
        {"detail": "Invalid data.", "errors": serializer.errors},
        status=status.HTTP_400_BAD_REQUEST,
    )


def _detail(record):
    return Response(DismantleDetailSerializer(record).data)


def _handle(fn):
    """Run a service call, turning its refusals into the right HTTP answer.

    A guard refusal and a SAP refusal are both 400 and both carry their own
    wording — the messages name the SAP error they came from, and flattening them
    into "something went wrong" would throw away the only thing that tells the
    operator what to fix.
    """
    try:
        return None, fn()
    except PermissionDenied as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN), None
    except (DismantleGuardError, ValueError, SAPValidationError) as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST), None
    except (SAPConnectionError, SAPDataError) as exc:
        logger.error("SAP unavailable during a dismantle: %s", exc)
        return (
            Response(
                {"detail": f"SAP is not reachable right now: {exc}"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            ),
            None,
        )


class DismantleListCreateAPI(APIView):
    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAuthenticated(), HasCompanyContext(), CanCreateDismantle()]
        return [IsAuthenticated(), HasCompanyContext(), CanViewDismantle()]

    def get(self, request):
        if wants_all_companies(request):
            company_ids = user_company_ids(request)
        else:
            company_ids = [request.company.company_id]
        qs = _service(request).list_dismantles(
            company_ids,
            status=request.GET.get("status") or None,
            search=request.GET.get("search") or None,
        )
        return Response(DismantleListSerializer(qs, many=True).data)

    def post(self, request):
        serializer = DismantleCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        error, record = _handle(
            lambda: _service(request).create(serializer.validated_data, request.user)
        )
        if error:
            return error
        return Response(
            DismantleDetailSerializer(record).data, status=status.HTTP_201_CREATED
        )


class DismantleBulkCreateAPI(APIView):
    """Start several dismantles at once — one record per item, all or nothing."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateDismantle]

    def post(self, request):
        serializer = DismantleBulkCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        error, records = _handle(
            lambda: _service(request).create_many(
                serializer.validated_data["items"], request.user
            )
        )
        if error:
            return error
        return Response(
            DismantleListSerializer(records, many=True).data,
            status=status.HTTP_201_CREATED,
        )


class DismantleDetailAPI(APIView):
    def get_permissions(self):
        if self.request.method == "GET":
            return [IsAuthenticated(), HasCompanyContext(), CanViewDismantle()]
        return [IsAuthenticated(), HasCompanyContext(), CanEditDismantle()]

    def delete(self, request, pk):
        """Soft-delete a draft: it leaves every screen, the record stays on file."""
        error, _record = _handle(
            lambda: _service(request).delete_draft(pk, request.user, _allowed_ids(request))
        )
        if error:
            return error
        return Response(status=status.HTTP_204_NO_CONTENT)

    def get(self, request, pk):
        error, record = _handle(
            lambda: _service(request).get_dismantle(pk, _allowed_ids(request))
        )
        if error:
            return error
        return _detail(record)

    def patch(self, request, pk):
        serializer = DismantleHeaderPatchSerializer(data=request.data, partial=True)
        if not serializer.is_valid():
            return _validation_error(serializer)
        error, record = _handle(
            lambda: _service(request).update_header(
                pk, serializer.validated_data, request.user, _allowed_ids(request)
            )
        )
        if error:
            return error
        return _detail(record)


class DismantleComponentsAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanEditDismantle]

    def put(self, request, pk):
        serializer = DismantleComponentsSaveSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        error, record = _handle(
            lambda: _service(request).save_components(
                pk,
                serializer.validated_data["components"],
                request.user,
                _allowed_ids(request),
            )
        )
        if error:
            return error
        return _detail(record)


class DismantleRebuildComponentsAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanEditDismantle]

    def post(self, request, pk):
        error, record = _handle(
            lambda: _service(request).rebuild_components(
                pk, request.user, _allowed_ids(request)
            )
        )
        if error:
            return error
        return _detail(record)


class DismantlePreviewAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewDismantle]

    def get(self, request, pk):
        error, result = _handle(
            lambda: _service(request).preview(pk, _allowed_ids(request))
        )
        if error:
            return error
        return Response(result)


class DismantlePostAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanPostDismantle]

    def post(self, request, pk):
        error, record = _handle(
            lambda: _service(request).post(pk, request.user, _allowed_ids(request))
        )
        if error:
            return error
        data = DismantleDetailSerializer(record).data
        # A run SAP stopped part-way through is reported as the partial success it
        # is: 207, with the documents that did post on the record and the refusal
        # in ``posting_error``. A 400 would read as "nothing happened", which is
        # the one thing that is not true.
        if getattr(record, "posting_error", ""):
            return Response(data, status=status.HTTP_207_MULTI_STATUS)
        return Response(data)


class DismantleReturnedLinesAPI(APIView):
    """The returned stock waiting to be dealt with — the primary source."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewDismantle]

    def get(self, request):
        if wants_all_companies(request):
            company_ids = user_company_ids(request)
        else:
            company_ids = [request.company.company_id]
        error, rows = _handle(
            lambda: _service(request).returned_lines(
                company_ids,
                search=request.GET.get("search") or None,
                limit=int(request.GET.get("limit") or 200),
            )
        )
        if error:
            return error
        return Response(rows)


class DismantleStockAPI(APIView):
    """Warehouse stock that has a BOM — the fallback source.

    Covers the returns the accounts team keyed straight into SAP, which the app
    has no record of, and any other stock that has to come apart.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewDismantle]

    def get(self, request):
        error, rows = _handle(
            lambda: _service(request).dismantlable_stock(
                request.GET.get("warehouse_code") or "",
                search=request.GET.get("search") or "",
                limit=int(request.GET.get("limit") or 50),
            )
        )
        if error:
            return error
        return Response(rows)


class DismantleBatchesAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewDismantle]

    def get(self, request):
        error, rows = _handle(
            lambda: _service(request).available_batches(
                request.GET.get("item_code") or "",
                request.GET.get("warehouse_code") or "",
            )
        )
        if error:
            return error
        return Response(rows)


class DismantleWarehousesAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewDismantle]

    def get(self, request):
        error, rows = _handle(lambda: _service(request).warehouses())
        if error:
            return error
        return Response(rows)
