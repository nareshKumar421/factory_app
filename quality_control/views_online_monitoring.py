"""Online Quality Monitoring API views."""

import os

from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from production_execution.models import ProductionLine

from .models.online_monitoring import (
    OnlineQualityRecord,
    OnlineQualityReading,
    OnlineQualityReadingAttachment,
    OnlineQualitySpec,
    OnlineRecordStatus,
)
from .permissions import (
    CanViewOnlineMonitoring,
    CanCreateOnlineMonitoring,
    CanSubmitOnlineMonitoring,
    CanApproveOnlineMonitoring,
)
from .serializers_online_monitoring import (
    OnlineQualityApprovalSerializer,
    OnlineQualityReadingAttachmentSerializer,
    OnlineQualityReadingSerializer,
    OnlineQualityRecordCreateSerializer,
    OnlineQualityRecordListSerializer,
    OnlineQualityRecordSerializer,
    OnlineQualitySpecSerializer,
)

# Attachments accepted on a reading: images + PDF, capped at 10 MB.
ALLOWED_ATTACHMENT_TYPES = {
    "image/jpeg", "image/png", "image/webp", "image/gif", "application/pdf",
}
ALLOWED_ATTACHMENT_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".pdf"}
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


def _validate_attachment(f):
    """Return ``(ok, error_message)`` for an uploaded reading attachment."""
    if f.size > MAX_ATTACHMENT_BYTES:
        return False, "File too large (max 10 MB)."
    content_type = (getattr(f, "content_type", "") or "").lower()
    ext = os.path.splitext(f.name or "")[1].lower()
    if content_type not in ALLOWED_ATTACHMENT_TYPES and ext not in ALLOWED_ATTACHMENT_EXTS:
        return False, "Only images (JPG, PNG, WEBP, GIF) and PDF files are allowed."
    return True, ""


def _company(request):
    return request.company.company


def _record_qs(request):
    return OnlineQualityRecord.objects.filter(
        company=_company(request), is_active=True
    ).select_related("production_line", "created_by")


def _draft_or_403(record):
    """Return None if editable (DRAFT), else a 400 Response."""
    if record.status != OnlineRecordStatus.DRAFT:
        return Response(
            {"detail": f"Record is {record.status} and can no longer be edited."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return None


class OnlineMonitoringListCreateAPI(APIView):
    """GET list (with filters) · POST create a record header."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewOnlineMonitoring]

    def get(self, request):
        qs = _record_qs(request).prefetch_related("readings")
        g = request.GET
        if g.get("status"):
            qs = qs.filter(status=g["status"])
        if g.get("production_line"):
            qs = qs.filter(production_line_id=g["production_line"])
        if g.get("sku"):
            qs = qs.filter(sku__icontains=g["sku"])
        if g.get("shift"):
            qs = qs.filter(shift=g["shift"])
        if g.get("batch"):
            qs = qs.filter(batch_no__icontains=g["batch"])
        if g.get("date"):
            qs = qs.filter(date=g["date"])
        if g.get("date_from"):
            qs = qs.filter(date__gte=g["date_from"])
        if g.get("date_to"):
            qs = qs.filter(date__lte=g["date_to"])
        return Response(OnlineQualityRecordListSerializer(qs, many=True).data)

    def post(self, request):
        if not request.user.has_perm("quality_control.can_create_online_monitoring"):
            return Response({"detail": "Not allowed to create records."}, status=403)
        ser = OnlineQualityRecordCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        company = _company(request)
        line = get_object_or_404(
            ProductionLine, id=data["production_line_id"], company=company
        )
        record = OnlineQualityRecord.objects.create(
            company=company,
            production_line=line,
            date=data["date"],
            sku=data.get("sku", ""),
            product_name=data.get("product_name", ""),
            flavour=data.get("flavour", ""),
            shift=data.get("shift", ""),
            batch_no=data.get("batch_no", ""),
            remarks=data.get("remarks", ""),
            created_by=request.user,
            updated_by=request.user,
        )
        return Response(
            OnlineQualityRecordSerializer(record).data, status=status.HTTP_201_CREATED
        )


class OnlineMonitoringDetailAPI(APIView):
    """GET record (nested) · PATCH header (draft) · DELETE (draft)."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewOnlineMonitoring]

    def get(self, request, record_id):
        record = get_object_or_404(
            _record_qs(request).prefetch_related(
                "readings__torque_heads", "readings__attachments",
            ),
            id=record_id,
        )
        return Response(
            OnlineQualityRecordSerializer(record, context={"request": request}).data
        )

    def patch(self, request, record_id):
        if not request.user.has_perm("quality_control.can_create_online_monitoring"):
            return Response({"detail": "Not allowed."}, status=403)
        record = get_object_or_404(_record_qs(request), id=record_id)
        blocked = _draft_or_403(record)
        if blocked:
            return blocked
        ser = OnlineQualityRecordSerializer(record, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save(updated_by=request.user)
        return Response(ser.data)

    def delete(self, request, record_id):
        if not request.user.has_perm("quality_control.can_create_online_monitoring"):
            return Response({"detail": "Not allowed."}, status=403)
        record = get_object_or_404(_record_qs(request), id=record_id)
        blocked = _draft_or_403(record)
        if blocked:
            return blocked
        record.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class OnlineMonitoringReadingCreateAPI(APIView):
    """POST add a reading (with torque heads) to a draft record."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateOnlineMonitoring]

    def post(self, request, record_id):
        record = get_object_or_404(_record_qs(request), id=record_id)
        blocked = _draft_or_403(record)
        if blocked:
            return blocked
        ser = OnlineQualityReadingSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        ser.save(record=record, created_by=request.user, updated_by=request.user)
        return Response(ser.data, status=status.HTTP_201_CREATED)


class OnlineMonitoringReadingDetailAPI(APIView):
    """PATCH / DELETE a reading (draft record only)."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateOnlineMonitoring]

    def _get(self, request, record_id, reading_id):
        record = get_object_or_404(_record_qs(request), id=record_id)
        reading = get_object_or_404(
            OnlineQualityReading, id=reading_id, record=record
        )
        return record, reading

    def patch(self, request, record_id, reading_id):
        record, reading = self._get(request, record_id, reading_id)
        blocked = _draft_or_403(record)
        if blocked:
            return blocked
        ser = OnlineQualityReadingSerializer(reading, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save(updated_by=request.user)
        return Response(ser.data)

    def delete(self, request, record_id, reading_id):
        record, reading = self._get(request, record_id, reading_id)
        blocked = _draft_or_403(record)
        if blocked:
            return blocked
        reading.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class OnlineMonitoringReadingAttachmentAPI(APIView):
    """POST upload a photo/PDF to a time-interval reading (draft record only)."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateOnlineMonitoring]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, record_id, reading_id):
        record = get_object_or_404(_record_qs(request), id=record_id)
        blocked = _draft_or_403(record)
        if blocked:
            return blocked
        reading = get_object_or_404(OnlineQualityReading, id=reading_id, record=record)
        f = request.FILES.get("file")
        if not f:
            return Response({"detail": "No file uploaded (field 'file')."}, status=400)
        ok, err = _validate_attachment(f)
        if not ok:
            return Response({"detail": err}, status=400)
        att = OnlineQualityReadingAttachment.objects.create(
            reading=reading,
            file=f,
            original_name=(f.name or "")[:255],
            content_type=(getattr(f, "content_type", "") or "")[:100],
            created_by=request.user,
            updated_by=request.user,
        )
        return Response(
            OnlineQualityReadingAttachmentSerializer(att, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class OnlineMonitoringReadingAttachmentDetailAPI(APIView):
    """DELETE an attachment from a reading (draft record only)."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateOnlineMonitoring]

    def delete(self, request, record_id, reading_id, attachment_id):
        record = get_object_or_404(_record_qs(request), id=record_id)
        blocked = _draft_or_403(record)
        if blocked:
            return blocked
        att = get_object_or_404(
            OnlineQualityReadingAttachment,
            id=attachment_id, reading_id=reading_id, reading__record=record,
        )
        att.file.delete(save=False)  # remove the stored file, then the row
        att.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class OnlineMonitoringSubmitAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanSubmitOnlineMonitoring]

    def post(self, request, record_id):
        record = get_object_or_404(_record_qs(request), id=record_id)
        if record.status != OnlineRecordStatus.DRAFT:
            return Response({"detail": "Only a draft can be submitted."}, status=400)
        if not record.readings.exists():
            return Response({"detail": "Add at least one reading before submitting."}, status=400)
        record.status = OnlineRecordStatus.SUBMITTED
        record.submitted_by = request.user
        record.submitted_at = timezone.now()
        record.updated_by = request.user
        record.save()
        return Response(OnlineQualityRecordSerializer(record).data)


class OnlineMonitoringApproveAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveOnlineMonitoring]

    def post(self, request, record_id):
        record = get_object_or_404(_record_qs(request), id=record_id)
        if record.status != OnlineRecordStatus.SUBMITTED:
            return Response({"detail": "Only a submitted record can be approved."}, status=400)
        ser = OnlineQualityApprovalSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        record.status = OnlineRecordStatus.APPROVED
        record.approved_by = request.user
        record.approved_at = timezone.now()
        record.approval_remarks = ser.validated_data.get("remarks", "")
        record.updated_by = request.user
        record.save()
        return Response(OnlineQualityRecordSerializer(record).data)


class OnlineMonitoringRejectAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveOnlineMonitoring]

    def post(self, request, record_id):
        record = get_object_or_404(_record_qs(request), id=record_id)
        if record.status != OnlineRecordStatus.SUBMITTED:
            return Response({"detail": "Only a submitted record can be rejected."}, status=400)
        ser = OnlineQualityApprovalSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        record.status = OnlineRecordStatus.REJECTED
        record.rejected_by = request.user
        record.rejected_at = timezone.now()
        record.rejection_remarks = ser.validated_data.get("remarks", "")
        record.updated_by = request.user
        record.save()
        return Response(OnlineQualityRecordSerializer(record).data)


class OnlineMonitoringReopenAPI(APIView):
    """Send a rejected record back to DRAFT so it can be corrected and resubmitted."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanSubmitOnlineMonitoring]

    def post(self, request, record_id):
        record = get_object_or_404(_record_qs(request), id=record_id)
        if record.status != OnlineRecordStatus.REJECTED:
            return Response({"detail": "Only a rejected record can be revised."}, status=400)
        record.status = OnlineRecordStatus.DRAFT
        record.submitted_by = None
        record.submitted_at = None
        record.updated_by = request.user
        record.save()  # rejection_remarks kept so the operator can see what to fix
        return Response(OnlineQualityRecordSerializer(record).data)


class OnlineMonitoringLinesAPI(APIView):
    """Active production lines for the company — for the create screen's picker."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewOnlineMonitoring]

    def get(self, request):
        lines = ProductionLine.objects.filter(
            company=_company(request), is_active=True
        ).order_by("name").values("id", "name")
        return Response(list(lines))


class OnlineMonitoringSpecListAPI(APIView):
    """GET specs (this company + global) · POST create a spec."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewOnlineMonitoring]

    def get(self, request):
        company = _company(request)
        specs = OnlineQualitySpec.objects.filter(
            Q(company=company) | Q(company__isnull=True), is_active=True
        )
        return Response(OnlineQualitySpecSerializer(specs, many=True).data)

    def post(self, request):
        if not request.user.has_perm("quality_control.can_approve_online_monitoring"):
            return Response({"detail": "Only QA can manage specifications."}, status=403)
        ser = OnlineQualitySpecSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        ser.save(company=_company(request), created_by=request.user, updated_by=request.user)
        return Response(ser.data, status=status.HTTP_201_CREATED)


class OnlineMonitoringSpecDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveOnlineMonitoring]

    def patch(self, request, spec_id):
        """Edit a spec's range/validation.

        Editing a *global default* is copy-on-write: it creates (or updates) a
        company-specific override, leaving the shared default untouched for other
        companies. Editing a company override updates it in place.
        """
        company = _company(request)
        spec = get_object_or_404(
            OnlineQualitySpec.objects.filter(Q(company=company) | Q(company__isnull=True)),
            id=spec_id, is_active=True,
        )
        if spec.company_id is None:
            payload = {**OnlineQualitySpecSerializer(spec).data, **request.data}
            payload.pop("id", None)
            payload["parameter_key"] = spec.parameter_key
            override = OnlineQualitySpec.objects.filter(
                company=company, parameter_key=spec.parameter_key
            ).first()
            ser = (
                OnlineQualitySpecSerializer(override, data=payload, partial=True)
                if override else OnlineQualitySpecSerializer(data=payload)
            )
            ser.is_valid(raise_exception=True)
            saved = ser.save(company=company, is_active=True, updated_by=request.user)
            if override is None:
                saved.created_by = request.user
                saved.save(update_fields=["created_by"])
            return Response(OnlineQualitySpecSerializer(saved).data)

        ser = OnlineQualitySpecSerializer(spec, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save(updated_by=request.user)
        return Response(ser.data)

    def delete(self, request, spec_id):
        """Reset a spec to its global default by removing the company override."""
        company = _company(request)
        spec = get_object_or_404(OnlineQualitySpec, id=spec_id)
        if spec.company_id is None:
            return Response(
                {"detail": "Global default specifications cannot be deleted."}, status=400
            )
        if spec.company_id != company.id:
            return Response(status=status.HTTP_404_NOT_FOUND)
        spec.is_active = False
        spec.updated_by = request.user
        spec.save(update_fields=["is_active", "updated_by", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)
