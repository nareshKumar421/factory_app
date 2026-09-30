# quality_control/views_production_qc.py
"""Production QC API: running lines, checks and their approval, and the masters."""

from datetime import date

from django.db.models import Count, Min, Q
from django.db.models.functions import TruncDate
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from .models import (
    QCPrintDocument,
    ProductionParameter,
    ProductionParameterType,
    ProductionQCEntry,
    ProductionQCStatus,
)
from .permissions import (
    CanApproveProductionQC,
    CanFillProductionQC,
    CanReadOrManageProductionQCParameters,
    CanViewProductionQC,
)
from .serializers_production_qc import (
    ProductionParameterSerializer,
    ProductionParameterTypeSerializer,
    ProductionParameterTypeWriteSerializer,
    ProductionParameterWriteSerializer,
    ProductionQCDecisionSerializer,
    ProductionQCEntryCreateSerializer,
    ProductionQCEntryDetailSerializer,
    ProductionQCEntryListSerializer,
    ProductionQCEntryUpdateSerializer,
    RunningLineSerializer,
)
from .services import production_qc as service

UNFINISHED = (ProductionQCStatus.PENDING, ProductionQCStatus.SENT_BACK)


def _error(exc):
    return Response(exc.as_response_data(), status=status.HTTP_400_BAD_REQUEST)


class _BadDay(ValueError):
    pass


def _day(request):
    """The `date` asked for (YYYY-MM-DD), a day in the factory's own time zone."""
    raw = (request.query_params.get("date") or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise _BadDay(raw) from exc


def _bad_day(raw):
    return Response({"date": [f"Not a date: {raw}"]}, status=status.HTTP_400_BAD_REQUEST)


def _types(company):
    return (
        ProductionParameterType.objects.filter(company=company)
        .annotate(
            active_parameter_count=Count(
                "parameters", filter=Q(parameters__is_active=True), distinct=True
            )
        )
        .prefetch_related("print_documents")
    )


def _entries(company):
    return (
        ProductionQCEntry.objects.filter(company=company, is_active=True)
        .select_related(
            "line", "production_run", "parameter_type",
            "submitted_by", "approved_by", "sent_back_by",
        )
        .annotate(out_of_spec=Count("results", filter=Q(results__is_within_spec=False)))
    )


def _set_form_number(parameter_type, number, user):
    """Keep the type's Print Documents row in step with the number given here.

    One row, two places to edit it: Master Data > Print Documents and the
    parameter type. Blank removes it, as deleting it in Print Documents does.
    """
    number = (number or "").strip()
    document = QCPrintDocument.objects.filter(
        company=parameter_type.company,
        document_key=QCPrintDocument.DocumentKey.PRODUCTION_QC_SHEET,
        production_parameter_type=parameter_type,
    ).first()
    if not number:
        if document and document.is_active:
            document.is_active = False
            document.updated_by = user
            document.save(update_fields=["is_active", "updated_by", "updated_at"])
        return
    if document:
        document.document_id = number
        document.is_active = True
        document.updated_by = user
        document.save()
    else:
        QCPrintDocument.objects.create(
            company=parameter_type.company,
            document_key=QCPrintDocument.DocumentKey.PRODUCTION_QC_SHEET,
            production_parameter_type=parameter_type,
            document_id=number,
            created_by=user,
            updated_by=user,
        )


# ==================== Running lines ====================


class ProductionQCRunningLinesAPI(APIView):
    """The lines a check can be made on."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanFillProductionQC]

    def get(self, request):
        lines = service.running_lines(request.company.company)
        return Response(RunningLineSerializer(lines, many=True).data)


# ==================== Entries ====================


class ProductionQCEntryListCreateAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get_permissions(self):
        extra = CanFillProductionQC if self.request.method == "POST" else CanViewProductionQC
        return [permission() for permission in self.permission_classes] + [extra()]

    def get(self, request):
        qs = _entries(request.company.company)
        params = request.query_params
        try:
            day = _day(request)
        except _BadDay as exc:
            return _bad_day(exc.args[0])

        status_filter = (params.get("status") or "").strip().upper()
        if status_filter:
            qs = qs.filter(status=status_filter)
        line_id = params.get("line_id")
        if line_id:
            qs = qs.filter(line_id=line_id)

        search = (params.get("search") or "").strip()
        if search:
            match = (
                Q(product__icontains=search)
                | Q(item_code__icontains=search)
                | Q(line__name__icontains=search)
                | Q(parameter_type__name__icontains=search)
                | Q(parameter_type__code__icontains=search)
            )
            if search.isdigit():
                match |= Q(pk=int(search))
            qs = qs.filter(match)

        if day:
            # One day's record, like the paper form: every status, that day only.
            qs = qs.filter(checked_at__date=day)
        elif not search and status_filter not in UNFINISHED:
            # Otherwise a search looks across every date, and so do entries still
            # waiting on someone: an unfinished check must not drop off with age.
            dated = Q()
            if params.get("from_date"):
                dated &= Q(checked_at__date__gte=params["from_date"])
            if params.get("to_date"):
                dated &= Q(checked_at__date__lte=params["to_date"])
            if status_filter:
                qs = qs.filter(dated)
            else:
                qs = qs.filter(dated | Q(status__in=UNFINISHED))

        # The sheet view lays each entry's readings out as a column.
        if params.get("include") == "results":
            qs = qs.prefetch_related("results")
            return Response(ProductionQCEntryDetailSerializer(qs, many=True).data)
        return Response(ProductionQCEntryListSerializer(qs, many=True).data)

    def post(self, request):
        serializer = ProductionQCEntryCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            entry = service.create_entry(
                request.company.company,
                request.user,
                run_id=data["run_id"],
                parameter_type_id=data["parameter_type_id"],
                readings=serializer.readings(),
                remarks=data.get("remarks", ""),
            )
        except service.ProductionQCError as exc:
            return _error(exc)
        entry = _entries(request.company.company).get(pk=entry.pk)
        return Response(
            ProductionQCEntryDetailSerializer(entry).data, status=status.HTTP_201_CREATED
        )


class ProductionQCEntryCountsAPI(APIView):
    """With `date`, that day's counts, and what is still waiting on other days.

    Without it: waiting and sent-back cover every date; approved covers the
    picked range, or today (the sidebar badge reads `pending` this way).
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewProductionQC]

    def get(self, request):
        qs = ProductionQCEntry.objects.filter(company=request.company.company, is_active=True)
        try:
            day = _day(request)
        except _BadDay as exc:
            return _bad_day(exc.args[0])
        if day:
            on_day = Q(checked_at__date=day)
            counts = qs.aggregate(
                pending=Count("id", filter=on_day & Q(status=ProductionQCStatus.PENDING)),
                sent_back=Count("id", filter=on_day & Q(status=ProductionQCStatus.SENT_BACK)),
                approved=Count("id", filter=on_day & Q(status=ProductionQCStatus.APPROVED)),
            )
            # A per-day page must not hide a check still waiting on another day.
            elsewhere = (
                qs.filter(status__in=UNFINISHED)
                .exclude(on_day)
                .annotate(day=TruncDate("checked_at"))
                .aggregate(count=Count("id"), first=Min("day"))
            )
            counts["waiting_elsewhere"] = elsewhere["count"]
            counts["waiting_elsewhere_first_date"] = elsewhere["first"]
            return Response(counts)

        today = timezone.localdate()
        from_date = request.query_params.get("from_date") or today
        to_date = request.query_params.get("to_date") or today
        counts = qs.aggregate(
            pending=Count("id", filter=Q(status=ProductionQCStatus.PENDING)),
            sent_back=Count("id", filter=Q(status=ProductionQCStatus.SENT_BACK)),
            approved=Count(
                "id",
                filter=Q(
                    status=ProductionQCStatus.APPROVED,
                    checked_at__date__gte=from_date,
                    checked_at__date__lte=to_date,
                ),
            ),
        )
        return Response(counts)


class ProductionQCEntryDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get_permissions(self):
        extra = CanFillProductionQC if self.request.method == "PATCH" else CanViewProductionQC
        return [permission() for permission in self.permission_classes] + [extra()]

    def _get(self, request, entry_id):
        return get_object_or_404(
            _entries(request.company.company).prefetch_related("results"), pk=entry_id
        )

    def get(self, request, entry_id):
        return Response(ProductionQCEntryDetailSerializer(self._get(request, entry_id)).data)

    def patch(self, request, entry_id):
        entry = self._get(request, entry_id)
        serializer = ProductionQCEntryUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            service.update_entry(
                entry,
                request.user,
                readings=serializer.readings(),
                remarks=serializer.validated_data.get("remarks", ""),
            )
        except service.ProductionQCError as exc:
            return _error(exc)
        return Response(ProductionQCEntryDetailSerializer(self._get(request, entry_id)).data)


class _DecisionAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveProductionQC]
    decide = None

    def post(self, request, entry_id):
        company = request.company.company
        entry = get_object_or_404(ProductionQCEntry, company=company, is_active=True, pk=entry_id)
        serializer = ProductionQCDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            self.decide(entry, request.user, serializer.validated_data.get("remarks", ""))
        except service.ProductionQCError as exc:
            return _error(exc)
        entry = _entries(company).prefetch_related("results").get(pk=entry_id)
        return Response(ProductionQCEntryDetailSerializer(entry).data)


class ProductionQCEntryApproveAPI(_DecisionAPI):
    decide = staticmethod(service.approve_entry)


class ProductionQCEntrySendBackAPI(_DecisionAPI):
    decide = staticmethod(service.send_back_entry)


# ==================== Masters ====================


class ProductionParameterTypeListCreateAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanReadOrManageProductionQCParameters]

    def get(self, request):
        qs = _types(request.company.company)
        if request.query_params.get("include_inactive") != "true":
            qs = qs.filter(is_active=True)
        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(Q(code__icontains=search) | Q(name__icontains=search))
        return Response(ProductionParameterTypeSerializer(qs, many=True).data)

    def post(self, request):
        serializer = ProductionParameterTypeWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        company = request.company.company
        data = dict(serializer.validated_data)
        form_number = data.pop("print_document_id", None)
        existing = ProductionParameterType.objects.filter(
            company=company, code__iexact=data["code"]
        ).first()
        if existing and existing.is_active:
            return Response(
                {"code": ["A parameter type with this code already exists."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if existing:
            # Reuse a removed type's row, as material types do, so the code stays unique.
            for key, value in data.items():
                setattr(existing, key, value)
            existing.is_active = True
            existing.updated_by = request.user
            existing.save()
            parameter_type = existing
        else:
            parameter_type = ProductionParameterType.objects.create(
                company=company, created_by=request.user, updated_by=request.user, **data
            )
        if form_number is not None:
            _set_form_number(parameter_type, form_number, request.user)
        parameter_type = _types(company).get(pk=parameter_type.pk)
        return Response(
            ProductionParameterTypeSerializer(parameter_type).data, status=status.HTTP_201_CREATED
        )


class ProductionParameterTypeDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanReadOrManageProductionQCParameters]

    def _get(self, request, type_id):
        return get_object_or_404(_types(request.company.company), pk=type_id)

    def get(self, request, type_id):
        return Response(ProductionParameterTypeSerializer(self._get(request, type_id)).data)

    def patch(self, request, type_id):
        parameter_type = self._get(request, type_id)
        serializer = ProductionParameterTypeWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        form_number = data.pop("print_document_id", None)
        if "code" in data and ProductionParameterType.objects.filter(
            company=parameter_type.company, code__iexact=data["code"]
        ).exclude(pk=parameter_type.pk).exists():
            return Response(
                {"code": ["A parameter type with this code already exists."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        for key, value in data.items():
            setattr(parameter_type, key, value)
        parameter_type.updated_by = request.user
        parameter_type.save()
        if form_number is not None:
            _set_form_number(parameter_type, form_number, request.user)
        return Response(ProductionParameterTypeSerializer(self._get(request, type_id)).data)

    def delete(self, request, type_id):
        parameter_type = self._get(request, type_id)
        parameter_type.is_active = False
        parameter_type.updated_by = request.user
        parameter_type.save(update_fields=["is_active", "updated_by", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class ProductionParameterListCreateAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanReadOrManageProductionQCParameters]

    def _type(self, request, type_id):
        return get_object_or_404(
            ProductionParameterType, company=request.company.company, pk=type_id
        )

    def get(self, request, type_id):
        parameters = self._type(request, type_id).parameters.filter(is_active=True)
        return Response(ProductionParameterSerializer(parameters, many=True).data)

    def post(self, request, type_id):
        parameter_type = self._type(request, type_id)
        serializer = ProductionParameterWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        existing = parameter_type.parameters.filter(
            parameter_code__iexact=data["parameter_code"]
        ).first()
        if existing and existing.is_active:
            return Response(
                {"parameter_code": ["This type already has a parameter with this code."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if existing:
            for key, value in data.items():
                setattr(existing, key, value)
            existing.is_active = True
            existing.updated_by = request.user
            existing.save()
            parameter = existing
        else:
            parameter = ProductionParameter.objects.create(
                parameter_type=parameter_type,
                created_by=request.user,
                updated_by=request.user,
                **data,
            )
        return Response(ProductionParameterSerializer(parameter).data, status=status.HTTP_201_CREATED)


class ProductionParameterDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanReadOrManageProductionQCParameters]

    def _get(self, request, parameter_id):
        return get_object_or_404(
            ProductionParameter,
            parameter_type__company=request.company.company,
            is_active=True,
            pk=parameter_id,
        )

    def patch(self, request, parameter_id):
        parameter = self._get(request, parameter_id)
        serializer = ProductionParameterWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if "parameter_code" in data and parameter.parameter_type.parameters.filter(
            parameter_code__iexact=data["parameter_code"]
        ).exclude(pk=parameter.pk).exists():
            return Response(
                {"parameter_code": ["This type already has a parameter with this code."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        low = data.get("min_value", parameter.min_value)
        high = data.get("max_value", parameter.max_value)
        if low is not None and high is not None and low > high:
            return Response(
                {"max_value": ["Max must not be below min."]}, status=status.HTTP_400_BAD_REQUEST
            )
        for key, value in data.items():
            setattr(parameter, key, value)
        parameter.updated_by = request.user
        parameter.save()
        return Response(ProductionParameterSerializer(parameter).data)

    def delete(self, request, parameter_id):
        # Soft: saved entries keep their snapshot and still point at the row.
        parameter = self._get(request, parameter_id)
        parameter.is_active = False
        parameter.updated_by = request.user
        parameter.save(update_fields=["is_active", "updated_by", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)

