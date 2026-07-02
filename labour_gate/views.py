import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Sum
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

from .models import LabourAuditAction, LabourGateAudit, LabourGateEntry, LabourGateOutBatch
from .serializers import (
    LabourGateAuditSerializer,
    LabourGateEntrySerializer,
    LabourInSerializer,
    UpdateInSerializer,
    OutBatchSerializer,
    UNDO_WINDOW_MINUTES,
)
from .permissions import CanViewLabourGate, CanRecordLabourIn, CanRecordLabourOut

logger = logging.getLogger(__name__)


def _record_audit(entry, action, request, *, detail="", old_value=None, new_value=None):
    """Append one immutable row to the labour-gate audit trail for an entry."""
    LabourGateAudit.objects.create(
        company=entry.company,
        entry=entry,
        action=action,
        detail=detail,
        old_value=old_value,
        new_value=new_value,
        performed_by=request.user if request.user and request.user.is_authenticated else None,
    )


def _entry_for_company(pk, request):
    # No prefetch here: these views mutate out_batches and then serialize the
    # result, so the count_out/remaining must be read fresh (a prefetched cache
    # would be stale after add/undo). The list view does its own prefetch.
    return get_object_or_404(
        LabourGateEntry, id=pk, company=request.company.company
    )


class LabourGateDayAPI(APIView):
    """
    GET ?date=YYYY-MM-DD : every contractor's labour in/out tally for the day.
    Feeds both the Labour In and Labour Out screens.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewLabourGate]

    def get(self, request):
        company = request.company.company
        work_date = parse_date(request.query_params.get("date", "") or "")
        if not work_date:
            return Response(
                {"detail": "A valid ?date=YYYY-MM-DD is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        qs = (
            LabourGateEntry.objects.filter(company=company, work_date=work_date)
            .select_related("contractor")
            .prefetch_related("out_batches")
        )
        return Response(LabourGateEntrySerializer(qs, many=True).data)


class LabourInAPI(APIView):
    """POST {department, contractor, work_date, count_in} : record/update how many
    labourers a contractor brought into a department on a day (one row per
    department+contractor+day)."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanRecordLabourIn]

    @transaction.atomic
    def post(self, request):
        serializer = LabourInSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        company = request.company.company
        department = None
        if data.get("department"):
            department = get_object_or_404(Department, id=data["department"])
        contractor = get_object_or_404(
            Contractor, id=data["contractor"], is_active=True
        )

        # Labour-module split: the per-department allocation can never exceed the
        # contractor's gate intake. (used = other departments; entered = gate count_in)
        if department is not None:
            gate = (
                LabourGateEntry.objects.filter(
                    company=company,
                    contractor=contractor,
                    work_date=data["work_date"],
                    department__isnull=True,
                    is_active=True,
                )
                .only("count_in")
                .first()
            )
            entered = gate.count_in if gate else 0
            used = (
                LabourGateEntry.objects.filter(
                    company=company,
                    contractor=contractor,
                    work_date=data["work_date"],
                    department__isnull=False,
                    is_active=True,
                )
                .exclude(department=department)
                .aggregate(s=Sum("count_in"))["s"]
                or 0
            )
            available = entered - used
            if data["count_in"] > available:
                return Response(
                    {
                        "detail": (
                            f"{contractor.contractor_name}: {entered} entered, {used} used, "
                            f"{available} left to allocate."
                        ),
                        "entered": entered,
                        "used": used,
                        "left": available,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        entry, created = LabourGateEntry.objects.get_or_create(
            company=company,
            department=department,
            contractor=contractor,
            work_date=data["work_date"],
            defaults={"count_in": data["count_in"], "created_by": request.user},
        )
        if created:
            _record_audit(
                entry,
                LabourAuditAction.CREATE_IN,
                request,
                new_value=entry.count_in,
                detail=f"Recorded {entry.count_in} in",
            )
        else:
            old_count = entry.count_in
            was_deleted = not entry.is_active
            if data["count_in"] < entry.total_out:
                return Response(
                    {"detail": f"Labour in cannot be less than the {entry.total_out} already marked out."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            entry.count_in = data["count_in"]
            entry.updated_by = request.user
            update_fields = ["count_in", "updated_at", "updated_by"]
            # Re-adding a soft-deleted (dept, contractor, day) reactivates it —
            # the unique row still occupies the slot, so we revive it in place.
            if not entry.is_active:
                entry.is_active = True
                entry.deleted_at = None
                entry.deleted_by = None
                update_fields += ["is_active", "deleted_at", "deleted_by"]
            entry.save(update_fields=update_fields)

            if was_deleted:
                _record_audit(
                    entry,
                    LabourAuditAction.RESTORE,
                    request,
                    new_value=entry.count_in,
                    detail=f"Re-added; labour-in set to {entry.count_in}",
                )
            elif old_count != entry.count_in:
                _record_audit(
                    entry,
                    LabourAuditAction.UPDATE_IN,
                    request,
                    old_value=old_count,
                    new_value=entry.count_in,
                    detail=f"Labour-in {old_count} → {entry.count_in}",
                )

        logger.info(
            f"Labour in {'created' if created else 'updated'}: contractor={contractor.id} "
            f"date={data['work_date']} count_in={data['count_in']} by {request.user}"
        )
        return Response(
            LabourGateEntrySerializer(entry).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class LabourEntryDetailAPI(APIView):
    """PATCH {count_in} edit, DELETE remove a labour-in entry."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanRecordLabourIn]

    def patch(self, request, pk):
        entry = _entry_for_company(pk, request)
        serializer = UpdateInSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        count_in = serializer.validated_data["count_in"]

        if count_in < entry.total_out:
            return Response(
                {"detail": f"Labour in cannot be less than the {entry.total_out} already marked out."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        old_count = entry.count_in
        entry.count_in = count_in
        entry.updated_by = request.user
        entry.save(update_fields=["count_in", "updated_at", "updated_by"])
        if old_count != count_in:
            _record_audit(
                entry,
                LabourAuditAction.UPDATE_IN,
                request,
                old_value=old_count,
                new_value=count_in,
                detail=f"Labour-in {old_count} → {count_in}",
            )
        return Response(LabourGateEntrySerializer(entry).data)

    def delete(self, request, pk):
        # Soft delete: keep the row (is_active=False) so the audit trail and the
        # released-labour history survive. The frontend lists these separately.
        entry = _entry_for_company(pk, request)
        if entry.out_batches.exists():
            return Response(
                {"detail": "Cannot delete: labour has already been marked out for this contractor."},
                status=status.HTTP_409_CONFLICT,
            )
        if not entry.is_active:
            return Response(
                {"detail": "Entry is already deleted."}, status=status.HTTP_400_BAD_REQUEST
            )
        entry.is_active = False
        entry.deleted_at = timezone.now()
        entry.deleted_by = request.user
        entry.updated_by = request.user
        entry.save(
            update_fields=["is_active", "deleted_at", "deleted_by", "updated_at", "updated_by"]
        )
        _record_audit(entry, LabourAuditAction.DELETE, request, detail="Entry deleted")
        logger.info(f"Labour entry {entry.id} soft-deleted by {request.user}")
        return Response(LabourGateEntrySerializer(entry).data)


class LabourRestoreAPI(APIView):
    """POST : undo a soft-delete (restore the row) within the grace window."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanRecordLabourIn]

    def post(self, request, pk):
        entry = _entry_for_company(pk, request)
        if entry.is_active:
            return Response(
                {"detail": "Entry is not deleted."}, status=status.HTTP_400_BAD_REQUEST
            )
        if not entry.deleted_at or (
            timezone.now() - entry.deleted_at > timedelta(minutes=UNDO_WINDOW_MINUTES)
        ):
            return Response(
                {
                    "detail": (
                        f"This entry was deleted over {UNDO_WINDOW_MINUTES} minutes ago "
                        "and can no longer be restored."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )
        entry.is_active = True
        entry.deleted_at = None
        entry.deleted_by = None
        entry.updated_by = request.user
        entry.save(
            update_fields=["is_active", "deleted_at", "deleted_by", "updated_at", "updated_by"]
        )
        _record_audit(entry, LabourAuditAction.RESTORE, request, detail="Entry restored")
        logger.info(f"Labour entry {entry.id} restored by {request.user}")
        return Response(LabourGateEntrySerializer(entry).data)


class LabourOutAPI(APIView):
    """POST {count} : mark one batch of people OUT against an entry."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanRecordLabourOut]

    @transaction.atomic
    def post(self, request, pk):
        entry = _entry_for_company(pk, request)
        serializer = OutBatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        count = serializer.validated_data["count"]

        if count > entry.remaining:
            return Response(
                {"detail": f"Only {entry.remaining} labour remaining inside for this contractor."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        LabourGateOutBatch.objects.create(entry=entry, count=count, created_by=request.user)
        entry.updated_by = request.user
        entry.save(update_fields=["updated_at", "updated_by"])
        _record_audit(
            entry,
            LabourAuditAction.MARK_OUT,
            request,
            new_value=count,
            detail=f"{count} marked out ({entry.remaining} remaining)",
        )
        logger.info(
            f"Labour out: entry={entry.id} +{count} (remaining {entry.remaining}) by {request.user}"
        )
        return Response(LabourGateEntrySerializer(entry).data)


class LabourOutUndoAPI(APIView):
    """POST : remove the most recent out batch for an entry (fix a mis-count)."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanRecordLabourOut]

    @transaction.atomic
    def post(self, request, pk):
        entry = _entry_for_company(pk, request)
        last = entry.out_batches.order_by("-created_at", "-id").first()
        if not last:
            return Response(
                {"detail": "No out batches to undo."}, status=status.HTTP_400_BAD_REQUEST
            )
        if timezone.now() - last.created_at > timedelta(minutes=UNDO_WINDOW_MINUTES):
            return Response(
                {
                    "detail": (
                        f"This batch was marked out over {UNDO_WINDOW_MINUTES} minutes ago "
                        "and can no longer be undone."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )
        removed = last.count
        last.delete()
        entry.updated_by = request.user
        entry.save(update_fields=["updated_at", "updated_by"])
        _record_audit(
            entry,
            LabourAuditAction.UNDO_OUT,
            request,
            old_value=removed,
            detail=f"Undid out batch of {removed}",
        )
        return Response(LabourGateEntrySerializer(entry).data)


class LabourEntryAuditAPI(APIView):
    """GET : the full audit trail (who/what/when) for one labour entry."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewLabourGate]

    def get(self, request, pk):
        entry = _entry_for_company(pk, request)
        logs = entry.audit_logs.select_related("performed_by").all()
        return Response(LabourGateAuditSerializer(logs, many=True).data)
