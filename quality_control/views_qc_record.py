# quality_control/views_qc_record.py
"""APIs for the QC "Documents" screen -- fillable record forms."""

from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from .models import (
    QCRecord,
    RecordStatus,
    RecordTemplate,
    RecordTemplateParameter,
    RecordTimeSlot,
    RecordValue,
)
from .serializers_qc_record import (
    QCRecordListSerializer,
    QCRecordSerializer,
    RecordTemplateListSerializer,
    RecordTemplateSerializer,
    RecordValuesWriteSerializer,
)


class CanViewRecords(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("quality_control.can_view_qc_records")


class CanFillRecords(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("quality_control.can_fill_qc_records")


class CanApproveRecords(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("quality_control.can_approve_qc_records")


def _company(request):
    return request.company.company


def _templates(company):
    """Shared forms plus any kept private to this company."""
    return RecordTemplate.objects.filter(is_active=True).filter(
        Q(company=company) | Q(company__isnull=True)
    )


def _records(company):
    return QCRecord.objects.filter(company=company, is_active=True)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


class RecordTemplateListCreateAPI(APIView):
    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAuthenticated(), HasCompanyContext(), CanApproveRecords()]
        return [IsAuthenticated(), HasCompanyContext(), CanViewRecords()]

    def get(self, request):
        queryset = (
            _templates(_company(request))
            .annotate(
                parameter_count=Count("sections__parameters", distinct=True),
                record_count=Count(
                    "records", filter=Q(records__is_active=True), distinct=True
                ),
            )
            .order_by("title")
        )
        return Response(RecordTemplateListSerializer(queryset, many=True).data)

    def post(self, request):
        company = _company(request)
        serializer = RecordTemplateSerializer(
            data=request.data, context={"company": company, "request": request}
        )
        serializer.is_valid(raise_exception=True)
        # Shared, so the form is usable from every company.
        template = serializer.save(
            company=None, created_by=request.user, updated_by=request.user
        )
        return Response(
            RecordTemplateSerializer(template).data, status=status.HTTP_201_CREATED
        )


class RecordTemplateDetailAPI(APIView):
    def get_permissions(self):
        if self.request.method == "GET":
            return [IsAuthenticated(), HasCompanyContext(), CanViewRecords()]
        return [IsAuthenticated(), HasCompanyContext(), CanApproveRecords()]

    def _get(self, request, template_id):
        return get_object_or_404(
            _templates(_company(request)).prefetch_related("sections__parameters"),
            id=template_id,
        )

    def get(self, request, template_id):
        return Response(RecordTemplateSerializer(self._get(request, template_id)).data)

    def put(self, request, template_id):
        template = self._get(request, template_id)
        serializer = RecordTemplateSerializer(
            template,
            data=request.data,
            partial=True,
            context={"company": _company(request), "request": request},
        )
        serializer.is_valid(raise_exception=True)
        template = serializer.save(updated_by=request.user)
        return Response(RecordTemplateSerializer(template).data)

    def delete(self, request, template_id):
        template = self._get(request, template_id)
        template.is_active = False
        template.updated_by = request.user
        template.save(update_fields=["is_active", "updated_by", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


class QCRecordListCreateAPI(APIView):
    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAuthenticated(), HasCompanyContext(), CanFillRecords()]
        return [IsAuthenticated(), HasCompanyContext(), CanViewRecords()]

    def get(self, request):
        queryset = _records(_company(request)).annotate(
            slot_count=Count("time_slots", distinct=True),
            filled_count=Count(
                "values", filter=~Q(values__value=""), distinct=True
            ),
        )

        template_id = request.query_params.get("template")
        if template_id:
            queryset = queryset.filter(template_id=template_id)
        status_filter = request.query_params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        date_from = request.query_params.get("date_from")
        if date_from:
            queryset = queryset.filter(record_date__gte=date_from)
        date_to = request.query_params.get("date_to")
        if date_to:
            queryset = queryset.filter(record_date__lte=date_to)

        return Response(QCRecordListSerializer(queryset, many=True).data)

    @transaction.atomic
    def post(self, request):
        """Open a sheet for a template and date.

        Returns the existing sheet with 200 if one is already open for that
        day -- the operator pressing "new" twice should land on the same sheet,
        not hit a uniqueness error.
        """
        company = _company(request)
        serializer = QCRecordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        slots = data.pop("time_slots", [])

        template = get_object_or_404(_templates(company), id=data.pop("template").id)
        existing = _records(company).filter(
            template=template,
            record_date=data.get("record_date"),
            shift=data.get("shift", ""),
        ).first()
        if existing:
            return Response(QCRecordSerializer(existing).data)

        record = QCRecord.objects.create(
            company=company,
            template=template,
            created_by=request.user,
            updated_by=request.user,
            **data,
        )
        for index, slot in enumerate(slots):
            RecordTimeSlot.objects.create(
                record=record,
                sequence=slot.get("sequence", index),
                slot_time=slot["slot_time"],
                created_by=request.user,
            )
        return Response(
            QCRecordSerializer(record).data, status=status.HTTP_201_CREATED
        )


class QCRecordDetailAPI(APIView):
    def get_permissions(self):
        if self.request.method == "GET":
            return [IsAuthenticated(), HasCompanyContext(), CanViewRecords()]
        return [IsAuthenticated(), HasCompanyContext(), CanFillRecords()]

    def _get(self, request, record_id):
        return get_object_or_404(
            _records(_company(request)).select_related("template").prefetch_related(
                "time_slots", "values", "template__sections__parameters"
            ),
            id=record_id,
        )

    def get(self, request, record_id):
        return Response(QCRecordSerializer(self._get(request, record_id)).data)

    def put(self, request, record_id):
        record = self._get(request, record_id)
        if record.status == RecordStatus.APPROVED:
            return Response(
                {"detail": "An approved record cannot be edited."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        for field in ("shift", "remarks"):
            if field in request.data:
                setattr(record, field, request.data[field])
        record.updated_by = request.user
        record.save()
        return Response(QCRecordSerializer(record).data)

    def delete(self, request, record_id):
        record = self._get(request, record_id)
        record.is_active = False
        record.updated_by = request.user
        record.save(update_fields=["is_active", "updated_by", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class QCRecordValuesAPI(APIView):
    """Bulk-save cells. Time columns are created on demand."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanFillRecords]

    @transaction.atomic
    def post(self, request, record_id):
        record = get_object_or_404(_records(_company(request)), id=record_id)
        if record.status == RecordStatus.APPROVED:
            return Response(
                {"detail": "An approved record cannot be edited."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = RecordValuesWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        cells = serializer.validated_data["cells"]

        # Only parameters belonging to this record's own form may be written.
        valid_parameters = set(
            RecordTemplateParameter.objects.filter(
                section__template_id=record.template_id
            ).values_list("id", flat=True)
        )
        unknown = {c["parameter"] for c in cells} - valid_parameters
        if unknown:
            return Response(
                {"detail": f"Parameters {sorted(unknown)} are not on this form."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        slots = {slot.slot_time: slot for slot in record.time_slots.all()}
        for cell in cells:
            slot = slots.get(cell["slot_time"])
            if slot is None:
                slot = RecordTimeSlot.objects.create(
                    record=record,
                    sequence=len(slots),
                    slot_time=cell["slot_time"],
                    created_by=request.user,
                )
                slots[cell["slot_time"]] = slot

            RecordValue.objects.update_or_create(
                time_slot=slot,
                parameter_id=cell["parameter"],
                defaults={
                    "record": record,
                    "value": cell["value"],
                    "updated_by": request.user,
                },
            )

        record.updated_by = request.user
        record.save(update_fields=["updated_by", "updated_at"])
        record.refresh_from_db()
        return Response(QCRecordSerializer(record).data)


class QCRecordSubmitAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanFillRecords]

    def post(self, request, record_id):
        record = get_object_or_404(_records(_company(request)), id=record_id)
        if record.status not in (RecordStatus.DRAFT, RecordStatus.REJECTED):
            return Response(
                {"detail": f"A {record.get_status_display()} record cannot be submitted."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        record.status = RecordStatus.SUBMITTED
        record.submitted_by = request.user
        record.submitted_at = timezone.now()
        record.updated_by = request.user
        record.save()
        return Response(QCRecordSerializer(record).data)


class QCRecordApproveAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveRecords]

    def post(self, request, record_id):
        record = get_object_or_404(_records(_company(request)), id=record_id)
        if record.status != RecordStatus.SUBMITTED:
            return Response(
                {"detail": "Only a submitted record can be approved or rejected."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        decision = (request.data.get("decision") or "APPROVE").upper()
        record.approval_remarks = request.data.get("remarks", "")
        record.status = (
            RecordStatus.APPROVED if decision == "APPROVE" else RecordStatus.REJECTED
        )
        record.approved_by = request.user
        record.approved_at = timezone.now()
        record.updated_by = request.user
        record.save()
        return Response(QCRecordSerializer(record).data)
