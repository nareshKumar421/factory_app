import logging
from datetime import timedelta

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Department
from company.permissions import HasCompanyContext

from .models import (
    LabourRequest,
    LabourRequestAction,
    LabourRequestAudit,
    LabourRequestStatus,
)
from .permissions import (
    CanDecideLabourRequest,
    CanRaiseLabourRequest,
    CanViewLabourRequest,
)
from .serializers import (
    UNDO_WINDOW_MINUTES,
    DecisionSerializer,
    LabourRequestAuditSerializer,
    LabourRequestSerializer,
    RaiseRequestSerializer,
    UpdateRequestSerializer,
)

logger = logging.getLogger(__name__)


def _record_audit(req, action, request, *, detail="", old_value=None, new_value=None):
    """Append one immutable row to a request's audit trail."""
    LabourRequestAudit.objects.create(
        company=req.company,
        request=req,
        action=action,
        detail=detail,
        old_value=old_value,
        new_value=new_value,
        performed_by=request.user
        if request.user and request.user.is_authenticated
        else None,
    )


def _request_for_company(pk, request):
    return get_object_or_404(
        LabourRequest.objects.select_related("department"),
        id=pk,
        company=request.company.company,
    )


class LabourRequestDayAPI(APIView):
    """GET ?date=YYYY-MM-DD : every department's request for that day.

    Both shifts come back in one call -- the screen switches between them
    locally, and the day's grand total is the two added up.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewLabourRequest]

    def get(self, request):
        work_date = parse_date(request.query_params.get("date", "") or "")
        if not work_date:
            return Response(
                {"detail": "A valid ?date=YYYY-MM-DD is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        qs = LabourRequest.objects.filter(
            company=request.company.company, work_date=work_date
        ).select_related(
            "department", "created_by", "updated_by", "decided_by", "deleted_by"
        )
        return Response(LabourRequestSerializer(qs, many=True).data)


class LabourRequestRaiseAPI(APIView):
    """POST {department, work_date, shift, requested_count, note} : raise or
    revise a department's ask for one shift.

    One row per (department, day, shift), so re-posting the same combination
    revises the existing ask rather than stacking a second one. Revising a
    request that was already decided sends it back to PENDING: the approver
    agreed to a number, and a different number has to be agreed again.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanRaiseLabourRequest]

    @transaction.atomic
    def post(self, request):
        serializer = RaiseRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        company = request.company.company
        department = get_object_or_404(Department, id=data["department"])

        req, created = LabourRequest.objects.get_or_create(
            company=company,
            department=department,
            work_date=data["work_date"],
            shift=data["shift"],
            defaults={
                "requested_count": data["requested_count"],
                "note": data["note"],
                "created_by": request.user,
            },
        )
        if created:
            _record_audit(
                req,
                LabourRequestAction.CREATE,
                request,
                new_value=req.requested_count,
                detail=f"Requested {req.requested_count} for {department.name}",
            )
        else:
            old_count = req.requested_count
            was_deleted = not req.is_active
            was_decided = req.is_decided

            req.requested_count = data["requested_count"]
            req.note = data["note"]
            req.updated_by = request.user
            update_fields = ["requested_count", "note", "updated_at", "updated_by"]

            # Re-raising a soft-deleted ask revives the row in place: the unique
            # (department, day, shift) slot is still occupied by it.
            if was_deleted:
                req.is_active = True
                req.deleted_at = None
                req.deleted_by = None
                update_fields += ["is_active", "deleted_at", "deleted_by"]

            # A revised ask is an undecided ask.
            if was_decided and old_count != req.requested_count:
                req.status = LabourRequestStatus.PENDING
                req.approved_count = None
                req.decision_note = ""
                req.decided_at = None
                req.decided_by = None
                update_fields += [
                    "status",
                    "approved_count",
                    "decision_note",
                    "decided_at",
                    "decided_by",
                ]

            req.save(update_fields=update_fields)

            if was_deleted:
                _record_audit(
                    req,
                    LabourRequestAction.RESTORE,
                    request,
                    new_value=req.requested_count,
                    detail=f"Re-raised; ask set to {req.requested_count}",
                )
            elif old_count != req.requested_count:
                detail = f"Ask {old_count} -> {req.requested_count}"
                if was_decided:
                    detail += "; sent back for approval"
                _record_audit(
                    req,
                    LabourRequestAction.UPDATE,
                    request,
                    old_value=old_count,
                    new_value=req.requested_count,
                    detail=detail,
                )

        logger.info(
            f"Labour request {'raised' if created else 'revised'}: "
            f"department={department.id} date={data['work_date']} "
            f"shift={data['shift']} count={data['requested_count']} by {request.user}"
        )
        return Response(
            LabourRequestSerializer(req).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class LabourRequestDetailAPI(APIView):
    """PATCH {requested_count, note} edit, DELETE remove a request."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanRaiseLabourRequest]

    @transaction.atomic
    def patch(self, request, pk):
        req = _request_for_company(pk, request)
        if not req.is_active:
            return Response(
                {"detail": "This request is deleted. Restore it before editing."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = UpdateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        old_count = req.requested_count
        new_count = data.get("requested_count", old_count)
        note_changed = "note" in data and data["note"] != req.note

        req.requested_count = new_count
        if "note" in data:
            req.note = data["note"]
        req.updated_by = request.user
        update_fields = ["requested_count", "note", "updated_at", "updated_by"]

        # Same rule as re-raising: changing the number un-decides the request.
        was_decided = req.is_decided
        if was_decided and old_count != new_count:
            req.status = LabourRequestStatus.PENDING
            req.approved_count = None
            req.decision_note = ""
            req.decided_at = None
            req.decided_by = None
            update_fields += [
                "status",
                "approved_count",
                "decision_note",
                "decided_at",
                "decided_by",
            ]
        req.save(update_fields=update_fields)

        if old_count != new_count or note_changed:
            detail = (
                f"Ask {old_count} -> {new_count}"
                if old_count != new_count
                else "Note updated"
            )
            if was_decided and old_count != new_count:
                detail += "; sent back for approval"
            _record_audit(
                req,
                LabourRequestAction.UPDATE,
                request,
                old_value=old_count,
                new_value=new_count,
                detail=detail,
            )
        return Response(LabourRequestSerializer(req).data)

    @transaction.atomic
    def delete(self, request, pk):
        # Soft delete: the row and its trail stay, the number stops counting.
        req = _request_for_company(pk, request)
        if not req.is_active:
            return Response(
                {"detail": "Request is already deleted."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        req.is_active = False
        req.deleted_at = timezone.now()
        req.deleted_by = request.user
        req.updated_by = request.user
        req.save(
            update_fields=[
                "is_active",
                "deleted_at",
                "deleted_by",
                "updated_at",
                "updated_by",
            ]
        )
        _record_audit(req, LabourRequestAction.DELETE, request, detail="Request deleted")
        logger.info(f"Labour request {req.id} soft-deleted by {request.user}")
        return Response(LabourRequestSerializer(req).data)


class LabourRequestRestoreAPI(APIView):
    """POST : undo a soft-delete within the grace window."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanRaiseLabourRequest]

    @transaction.atomic
    def post(self, request, pk):
        req = _request_for_company(pk, request)
        if req.is_active:
            return Response(
                {"detail": "Request is not deleted."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not req.deleted_at or (
            timezone.now() - req.deleted_at > timedelta(minutes=UNDO_WINDOW_MINUTES)
        ):
            return Response(
                {
                    "detail": (
                        f"This request was deleted over {UNDO_WINDOW_MINUTES} minutes "
                        "ago and can no longer be restored."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )
        req.is_active = True
        req.deleted_at = None
        req.deleted_by = None
        req.updated_by = request.user
        req.save(
            update_fields=[
                "is_active",
                "deleted_at",
                "deleted_by",
                "updated_at",
                "updated_by",
            ]
        )
        _record_audit(
            req, LabourRequestAction.RESTORE, request, detail="Request restored"
        )
        logger.info(f"Labour request {req.id} restored by {request.user}")
        return Response(LabourRequestSerializer(req).data)


class LabourRequestDecisionAPI(APIView):
    """POST {decision, approved_count, note} : approve or reject one request.

    An approval may grant fewer people than were asked for but never more --
    granting more is a different request, and letting a decision inflate the ask
    would hide who actually asked for the extra heads.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanDecideLabourRequest]

    @transaction.atomic
    def post(self, request, pk):
        req = _request_for_company(pk, request)
        if not req.is_active:
            return Response(
                {"detail": "This request is deleted and cannot be decided."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = DecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        decision = data["decision"]

        if decision == LabourRequestStatus.APPROVED:
            approved = data.get("approved_count", req.requested_count)
            if approved > req.requested_count:
                return Response(
                    {
                        "detail": (
                            f"Cannot approve {approved}: only {req.requested_count} "
                            "were requested."
                        ),
                        "requested_count": req.requested_count,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            req.status = LabourRequestStatus.APPROVED
            req.approved_count = approved
            action = LabourRequestAction.APPROVE
            detail = (
                f"Approved {approved} of {req.requested_count}"
                if approved != req.requested_count
                else f"Approved {approved}"
            )
            new_value = approved
        else:
            req.status = LabourRequestStatus.REJECTED
            req.approved_count = 0
            action = LabourRequestAction.REJECT
            detail = f"Rejected (asked {req.requested_count})"
            new_value = 0

        req.decision_note = data["note"]
        req.decided_at = timezone.now()
        req.decided_by = request.user
        req.updated_by = request.user
        req.save(
            update_fields=[
                "status",
                "approved_count",
                "decision_note",
                "decided_at",
                "decided_by",
                "updated_at",
                "updated_by",
            ]
        )
        if data["note"]:
            detail = f"{detail} - {data['note']}"
        _record_audit(
            req,
            action,
            request,
            old_value=req.requested_count,
            new_value=new_value,
            detail=detail,
        )
        logger.info(
            f"Labour request {req.id} {req.status.lower()} by {request.user} "
            f"(asked {req.requested_count}, granted {req.approved_count})"
        )
        return Response(LabourRequestSerializer(req).data)


class LabourRequestReopenAPI(APIView):
    """POST : take a decision back and put the request in PENDING again."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanDecideLabourRequest]

    @transaction.atomic
    def post(self, request, pk):
        req = _request_for_company(pk, request)
        if not req.is_decided:
            return Response(
                {"detail": "This request has not been decided yet."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        previous = req.get_status_display()
        req.status = LabourRequestStatus.PENDING
        req.approved_count = None
        req.decision_note = ""
        req.decided_at = None
        req.decided_by = None
        req.updated_by = request.user
        req.save(
            update_fields=[
                "status",
                "approved_count",
                "decision_note",
                "decided_at",
                "decided_by",
                "updated_at",
                "updated_by",
            ]
        )
        _record_audit(
            req,
            LabourRequestAction.REOPEN,
            request,
            detail=f"Reopened (was {previous})",
        )
        return Response(LabourRequestSerializer(req).data)


class LabourRequestAuditAPI(APIView):
    """GET : the full audit trail for one request."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewLabourRequest]

    def get(self, request, pk):
        req = _request_for_company(pk, request)
        logs = req.audit_logs.select_related("performed_by").all()
        return Response(LabourRequestAuditSerializer(logs, many=True).data)
