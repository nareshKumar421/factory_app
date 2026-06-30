import logging

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from company.permissions import HasCompanyContext
from accounts.models import Department
from person_gatein.models import Contractor

from django.db.models import Sum

from .models import (
    LabourCountSheet,
    LabourCountItem,
    LabourOutBatch,
    LabourVerification,
    SheetStatus,
    LabourShift,
)
from .serializers import (
    LabourCountSheetSerializer,
    EnsureSheetSerializer,
    ItemsUpsertSerializer,
    MarkOutSerializer,
    FinalizeSerializer,
)
from .services import lock
from .permissions import CanViewLabourCount, CanSubmitLabourCount, CanVerifyLabourCount


def _recompute_out(sheet):
    """Set the sheet's running gate_counted to the sum of its out batches."""
    sheet.gate_counted = sheet.out_batches.aggregate(t=Sum("count"))["t"] or 0
    return sheet.gate_counted

logger = logging.getLogger(__name__)


def _sheet_for_company(pk, request):
    return get_object_or_404(LabourCountSheet, id=pk, company=request.company.company)


class LabourSheetEnsureAPI(APIView):
    """
    POST: Get-or-create the DRAFT sheet for a (department, work_date, shift)
    and return it with its items. This is what the department page loads.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanSubmitLabourCount]

    def post(self, request):
        serializer = EnsureSheetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        company = request.company.company
        department = get_object_or_404(Department, id=data["department"])

        sheet, created = LabourCountSheet.objects.get_or_create(
            company=company,
            department=department,
            work_date=data["work_date"],
            shift=data["shift"],
            defaults={
                "lock_at": lock.compute_lock_at(company, data["work_date"], data["shift"]),
                "created_by": request.user,
            },
        )
        if created:
            logger.info(
                f"Labour sheet created. Dept: {department.id}, date: {data['work_date']}, "
                f"shift: {data['shift']}, user: {request.user}"
            )
        return Response(
            LabourCountSheetSerializer(sheet).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class LabourSheetDetailAPI(APIView):
    """GET a single sheet by id."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewLabourCount]

    def get(self, request, pk):
        sheet = _sheet_for_company(pk, request)
        return Response(LabourCountSheetSerializer(sheet).data)


class LabourSheetItemsAPI(APIView):
    """PUT: replace the per-contractor counts on a DRAFT sheet."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanSubmitLabourCount]

    @transaction.atomic
    def put(self, request, pk):
        sheet = _sheet_for_company(pk, request)

        if not lock.is_editable(sheet):
            return Response(
                {"detail": "Sheet is locked or already submitted. Pull it back to edit."},
                status=status.HTTP_409_CONFLICT,
            )

        serializer = ItemsUpsertSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        items = serializer.validated_data["items"]

        # Validate contractors exist & are active
        contractor_ids = [it["contractor"] for it in items if it["count"] > 0]
        valid_ids = set(
            Contractor.objects.filter(id__in=contractor_ids, is_active=True).values_list(
                "id", flat=True
            )
        )
        invalid = [cid for cid in contractor_ids if cid not in valid_ids]
        if invalid:
            return Response(
                {"detail": f"Unknown or inactive contractor id(s): {invalid}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        sheet.items.all().delete()
        # Keep only non-zero counts; the grid re-derives zeros from the roster.
        for it in items:
            if it["count"] > 0:
                LabourCountItem.objects.create(
                    sheet=sheet,
                    contractor_id=it["contractor"],
                    count=it["count"],
                    created_by=request.user,
                )
        # Entering counts clears a previously-marked holiday.
        sheet.is_holiday = False
        sheet.updated_by = request.user
        sheet.save(update_fields=["is_holiday", "updated_at", "updated_by"])

        return Response(LabourCountSheetSerializer(sheet).data)


class LabourSheetSubmitAPI(APIView):
    """POST: submit a DRAFT sheet so the gate can see it."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanSubmitLabourCount]

    def post(self, request, pk):
        sheet = _sheet_for_company(pk, request)

        if sheet.status != SheetStatus.DRAFT:
            return Response(
                {"detail": "Only a draft sheet can be submitted."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not sheet.is_holiday and not sheet.items.exists():
            return Response(
                {"detail": "Add at least one count or mark the day as a holiday."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        sheet.status = SheetStatus.SUBMITTED
        sheet.submitted_at = timezone.now()
        sheet.submitted_by = request.user
        sheet.auto_submitted = False
        sheet.updated_by = request.user
        sheet.save()
        logger.info(f"Labour sheet {sheet.id} submitted by {request.user}")
        return Response(LabourCountSheetSerializer(sheet).data)


class LabourSheetPullBackAPI(APIView):
    """POST: pull a submitted sheet back to DRAFT (only before the lock time)."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanSubmitLabourCount]

    def post(self, request, pk):
        sheet = _sheet_for_company(pk, request)

        if not lock.can_pull_back(sheet):
            return Response(
                {"detail": "Cannot pull back: not submitted, already verified, or past the lock time."},
                status=status.HTTP_409_CONFLICT,
            )

        sheet.status = SheetStatus.DRAFT
        sheet.submitted_at = None
        sheet.submitted_by = None
        sheet.auto_submitted = False
        sheet.updated_by = request.user
        sheet.save()
        logger.info(f"Labour sheet {sheet.id} pulled back by {request.user}")
        return Response(LabourCountSheetSerializer(sheet).data)


class LabourSheetHolidayAPI(APIView):
    """POST: mark the day as a holiday / no-labour and submit it."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanSubmitLabourCount]

    @transaction.atomic
    def post(self, request, pk):
        sheet = _sheet_for_company(pk, request)

        if sheet.status == SheetStatus.VERIFIED:
            return Response(
                {"detail": "Sheet already verified."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        sheet.items.all().delete()
        sheet.is_holiday = True
        sheet.status = SheetStatus.SUBMITTED
        sheet.submitted_at = timezone.now()
        sheet.submitted_by = request.user
        sheet.auto_submitted = False
        sheet.updated_by = request.user
        sheet.save()
        logger.info(f"Labour sheet {sheet.id} marked holiday by {request.user}")
        return Response(LabourCountSheetSerializer(sheet).data)


class LabourSheetHistoryAPI(APIView):
    """GET ?department=&from=&to=&shift= : list of sheets (for the dashboard trend)."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewLabourCount]

    def get(self, request):
        company = request.company.company
        qs = LabourCountSheet.objects.filter(company=company).select_related("department")

        department = request.query_params.get("department")
        if department:
            qs = qs.filter(department_id=department)
        shift = request.query_params.get("shift")
        if shift in LabourShift.values:
            qs = qs.filter(shift=shift)
        from_date = parse_date(request.query_params.get("from", "") or "")
        if from_date:
            qs = qs.filter(work_date__gte=from_date)
        to_date = parse_date(request.query_params.get("to", "") or "")
        if to_date:
            qs = qs.filter(work_date__lte=to_date)

        qs = qs.prefetch_related("items")[:200]
        return Response(LabourCountSheetSerializer(qs, many=True).data)


# ---------------------------------------------------------------------------
# Gate side
# ---------------------------------------------------------------------------

class LabourGateBoardAPI(APIView):
    """
    GET ?work_date=&shift=&department=&contractor= :
    The submitted (and verified) sheets for a shift, for the gate to tally.
    The frontend builds the by-department / by-contractor / grand-total matrix.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanVerifyLabourCount]

    def get(self, request):
        company = request.company.company
        work_date = parse_date(request.query_params.get("work_date", "") or "")
        shift = request.query_params.get("shift")
        if not work_date or shift not in LabourShift.values:
            return Response(
                {"detail": "work_date and a valid shift are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        qs = LabourCountSheet.objects.filter(
            company=company,
            work_date=work_date,
            shift=shift,
            status__in=[SheetStatus.SUBMITTED, SheetStatus.VERIFIED],
        ).select_related("department").prefetch_related("items__contractor")

        department = request.query_params.get("department")
        if department:
            qs = qs.filter(department_id=department)
        contractor = request.query_params.get("contractor")
        if contractor:
            qs = qs.filter(items__contractor_id=contractor).distinct()

        return Response(LabourCountSheetSerializer(qs, many=True).data)


class LabourGateMarkOutAPI(APIView):
    """
    POST {sheet, count}: mark one batch of people OUT at the gate. Adds to the
    sheet's running ``gate_counted``. Labour leaves incrementally, so this is
    called once per batch as each group exits.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanVerifyLabourCount]

    @transaction.atomic
    def post(self, request):
        serializer = MarkOutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        sheet = _sheet_for_company(data["sheet"], request)
        if sheet.status == SheetStatus.DRAFT:
            return Response(
                {"detail": "This sheet has not been submitted yet."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if sheet.status == SheetStatus.VERIFIED:
            return Response(
                {"detail": "Sheet is finalized. Re-open it to mark more out."},
                status=status.HTTP_409_CONFLICT,
            )

        LabourOutBatch.objects.create(
            sheet=sheet, count=data["count"], created_by=request.user
        )
        _recompute_out(sheet)
        sheet.updated_by = request.user
        sheet.save(update_fields=["gate_counted", "updated_at", "updated_by"])
        logger.info(
            f"Labour sheet {sheet.id}: +{data['count']} out (running {sheet.gate_counted}) by {request.user}"
        )
        return Response(LabourCountSheetSerializer(sheet).data)


class LabourGateUndoOutAPI(APIView):
    """POST {sheet}: remove the most recent out batch (fix a mis-count)."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanVerifyLabourCount]

    @transaction.atomic
    def post(self, request):
        sheet_id = request.data.get("sheet")
        if not sheet_id:
            return Response({"detail": "sheet is required."}, status=status.HTTP_400_BAD_REQUEST)
        sheet = _sheet_for_company(sheet_id, request)
        if sheet.status == SheetStatus.VERIFIED:
            return Response(
                {"detail": "Sheet is finalized. Re-open it first."},
                status=status.HTTP_409_CONFLICT,
            )
        last = sheet.out_batches.order_by("-created_at", "-id").first()
        if not last:
            return Response(
                {"detail": "No out batches to undo."}, status=status.HTTP_400_BAD_REQUEST
            )
        last.delete()
        _recompute_out(sheet)
        sheet.updated_by = request.user
        sheet.save(update_fields=["gate_counted", "updated_at", "updated_by"])
        return Response(LabourCountSheetSerializer(sheet).data)


class LabourGateFinalizeAPI(APIView):
    """
    POST {sheet, remark}: close a department sheet at the gate. Locks the running
    out count and records the final variance.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanVerifyLabourCount]

    @transaction.atomic
    def post(self, request):
        serializer = FinalizeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        sheet = _sheet_for_company(data["sheet"], request)
        if sheet.status != SheetStatus.SUBMITTED:
            return Response(
                {"detail": "Only a submitted sheet can be finalized."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        out = sheet.gate_counted or 0
        submitted_total = sheet.total_count
        now = timezone.now()

        sheet.status = SheetStatus.VERIFIED
        sheet.verified_at = now
        sheet.verified_by = request.user
        sheet.verify_basis = "DEPARTMENT"
        sheet.verify_remark = data.get("remark", "")
        sheet.updated_by = request.user
        sheet.save()

        LabourVerification.objects.create(
            company=sheet.company,
            work_date=sheet.work_date,
            shift=sheet.shift,
            basis="DEPARTMENT",
            department=sheet.department,
            submitted_total=submitted_total,
            gate_counted=out,
            variance=out - submitted_total,
            remark=data.get("remark", ""),
            created_by=request.user,
        )
        logger.info(
            f"Labour sheet {sheet.id} finalized: out={out} submitted={submitted_total} "
            f"var={out - submitted_total} by {request.user}"
        )
        return Response(LabourCountSheetSerializer(sheet).data)


class LabourGateReopenAPI(APIView):
    """POST: re-open a finalized sheet so the gate can mark more out / correct it.
    Keeps the running out count and batches."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanVerifyLabourCount]

    def post(self, request, pk):
        sheet = _sheet_for_company(pk, request)
        if sheet.status != SheetStatus.VERIFIED:
            return Response(
                {"detail": "Only a finalized sheet can be re-opened."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        sheet.status = SheetStatus.SUBMITTED
        sheet.verified_at = None
        sheet.verified_by = None
        sheet.verify_remark = ""
        sheet.updated_by = request.user
        sheet.save()
        logger.info(f"Labour sheet {sheet.id} re-opened by {request.user}")
        return Response(LabourCountSheetSerializer(sheet).data)
