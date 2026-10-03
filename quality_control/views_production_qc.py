# quality_control/views_production_qc.py
"""QA Reports API (still under `production-qc/`): entries, their approval, and the report types."""

from datetime import date

from django.db import transaction
from django.db.models import Count, Exists, Min, OuterRef, Prefetch, Q
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
    ProductionParameterDefaultValue,
    ProductionParameterType,
    ProductionParameterTypeDefault,
    ProductionQCEntry,
    ProductionQCResult,
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
    ProductionParameterTypeDefaultSerializer,
    ProductionParameterTypeDefaultWriteSerializer,
    ProductionParameterTypeSerializer,
    ProductionParameterTypeWriteSerializer,
    ProductionParameterWriteSerializer,
    ProductionQCDecisionSerializer,
    ProductionQCEntryCreateSerializer,
    ProductionQCEntryDetailSerializer,
    ProductionQCEntryListSerializer,
    ProductionQCEntryUpdateSerializer,
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
            ),
            active_default_count=Count(
                "defaults", filter=Q(defaults__is_active=True), distinct=True
            ),
        )
        .prefetch_related("print_documents")
    )


def _entries(company):
    return (
        ProductionQCEntry.objects.filter(company=company, is_active=True)
        .select_related(
            "parameter_type", "submission",
            "submitted_by", "approved_by", "sent_back_by",
        )
        # The entries sent with each one, for its `submission_entry_ids`.
        .prefetch_related(
            Prefetch(
                "submission__entries",
                queryset=ProductionQCEntry.objects.only("id", "submission_id", "is_active"),
            )
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
        parameter_type_id = params.get("parameter_type_id")
        if parameter_type_id:
            qs = qs.filter(parameter_type_id=parameter_type_id)

        search = (params.get("search") or "").strip()
        if search:
            # A report's header (product, batch, line...) is among its
            # readings, so a search looks there too.
            match = (
                Q(parameter_type__name__icontains=search)
                | Q(parameter_type__code__icontains=search)
                | Q(default_name__icontains=search)
                | Exists(
                    ProductionQCResult.objects.filter(
                        entry=OuterRef("pk"), result_value__icontains=search
                    )
                )
            )
            if search.isdigit():
                match |= Q(pk=int(search))
            qs = qs.filter(match)

        submission_id = params.get("submission_id")
        if submission_id:
            # The entries sent together — corrected together — whatever their date.
            qs = qs.filter(submission_id=submission_id).order_by("id")
        elif day:
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
            entries = service.create_entries(
                request.company.company,
                request.user,
                parameter_type_id=data["parameter_type_id"],
                default_id=data.get("default_id"),
                samples=serializer.samples_readings(),
                remarks=data.get("remarks", ""),
            )
        except service.ProductionQCError as exc:
            return _error(exc)
        # The first of them; its `submission_entry_ids` name the rest.
        entry = _entries(request.company.company).get(pk=entries[0].pk)
        return Response(
            ProductionQCEntryDetailSerializer(entry).data, status=status.HTTP_201_CREATED
        )


class ProductionQCEntryCountsAPI(APIView):
    """With `date`, that day's counts, and what is still waiting on other days.

    Without it: waiting and sent-back cover every date; approved covers the
    picked range, or today (the sidebar badge reads `pending` this way).

    Samples sent together are one entry to the people using it, so a set counts
    once: these count submissions, not sample rows.
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

            def sets(status_):
                return Count("submission", distinct=True, filter=on_day & Q(status=status_))

            counts = qs.aggregate(
                pending=sets(ProductionQCStatus.PENDING),
                sent_back=sets(ProductionQCStatus.SENT_BACK),
                approved=sets(ProductionQCStatus.APPROVED),
            )
            # A per-day page must not hide a check still waiting on another day.
            elsewhere = (
                qs.filter(status__in=UNFINISHED)
                .exclude(on_day)
                .annotate(day=TruncDate("checked_at"))
                .aggregate(count=Count("submission", distinct=True), first=Min("day"))
            )
            counts["waiting_elsewhere"] = elsewhere["count"]
            counts["waiting_elsewhere_first_date"] = elsewhere["first"]
            return Response(counts)

        today = timezone.localdate()
        from_date = request.query_params.get("from_date") or today
        to_date = request.query_params.get("to_date") or today
        counts = qs.aggregate(
            pending=Count("submission", distinct=True, filter=Q(status=ProductionQCStatus.PENDING)),
            sent_back=Count(
                "submission", distinct=True, filter=Q(status=ProductionQCStatus.SENT_BACK)
            ),
            approved=Count(
                "submission",
                distinct=True,
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
            service.update_entries(
                entry,
                request.user,
                readings_by_entry=serializer.readings_by_entry(entry),
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


# ==================== Defaults ====================


def _defaults(parameter_type):
    return parameter_type.defaults.filter(is_active=True).prefetch_related("values")


def _check_default(parameter_type, data, default=None):
    """A 400 for a name the report already has or a parameter it does not; else None."""
    taken = parameter_type.defaults.filter(is_active=True, name__iexact=data["name"])
    if default is not None:
        taken = taken.exclude(pk=default.pk)
    if taken.exists():
        return Response(
            {"name": ["This report already has a default with this name."]},
            status=status.HTTP_400_BAD_REQUEST,
        )
    own = set(parameter_type.parameters.filter(is_active=True).values_list("pk", flat=True))
    if any(row["parameter_id"] not in own for row in data["values"]):
        return Response(
            {"values": ["A value was sent for a parameter not in this report."]},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return None


def _replace_values(default, rows):
    default.values.all().delete()
    ProductionParameterDefaultValue.objects.bulk_create([
        ProductionParameterDefaultValue(default=default, **row) for row in rows
    ])


class ProductionParameterTypeDefaultListCreateAPI(APIView):
    """A report's defaults — one per SKU, say: anyone who fills reads them; managers keep them."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanReadOrManageProductionQCParameters]

    def get(self, request, type_id):
        parameter_type = get_object_or_404(
            ProductionParameterType, company=request.company.company, pk=type_id
        )
        return Response(ProductionParameterTypeDefaultSerializer(_defaults(parameter_type), many=True).data)

    def post(self, request, type_id):
        parameter_type = get_object_or_404(
            ProductionParameterType, company=request.company.company, is_active=True, pk=type_id
        )
        serializer = ProductionParameterTypeDefaultWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        error = _check_default(parameter_type, data)
        if error:
            return error
        with transaction.atomic():
            default = ProductionParameterTypeDefault.objects.create(
                parameter_type=parameter_type, name=data["name"],
                created_by=request.user, updated_by=request.user,
            )
            _replace_values(default, data["values"])
        default = _defaults(parameter_type).get(pk=default.pk)
        return Response(
            ProductionParameterTypeDefaultSerializer(default).data, status=status.HTTP_201_CREATED
        )


class ProductionParameterTypeDefaultDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanReadOrManageProductionQCParameters]

    def _get(self, request, default_id):
        return get_object_or_404(
            ProductionParameterTypeDefault.objects.select_related("parameter_type").prefetch_related("values"),
            parameter_type__company=request.company.company,
            is_active=True,
            pk=default_id,
        )

    def get(self, request, default_id):
        return Response(ProductionParameterTypeDefaultSerializer(self._get(request, default_id)).data)

    def put(self, request, default_id):
        """Replace the name and every value: the editor sends the whole default."""
        default = self._get(request, default_id)
        serializer = ProductionParameterTypeDefaultWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        error = _check_default(default.parameter_type, data, default)
        if error:
            return error
        with transaction.atomic():
            default.name = data["name"]
            default.updated_by = request.user
            default.save(update_fields=["name", "updated_by", "updated_at"])
            _replace_values(default, data["values"])
        return Response(ProductionParameterTypeDefaultSerializer(self._get(request, default_id)).data)

    def delete(self, request, default_id):
        # Soft: entries made with it keep its name and the standards they were judged on.
        default = self._get(request, default_id)
        default.is_active = False
        default.updated_by = request.user
        default.save(update_fields=["is_active", "updated_by", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)
