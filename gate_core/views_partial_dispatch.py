"""Partial-dispatch endpoints (Part C).

C1 removes a whole bill from an in-progress docking (operational reschedule).
C2 captures a partial-item dispatch: held quantities + an approval that, once
approved, requires a credit note before the gatepass can print.
"""

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from dispatch_plans.models import DispatchPlanStatus
from gate_core.models import (
    EmptyVehicleGateInCover,
    PartialDispatchApproval,
    PartialDispatchApprovalStatus,
    SalesDispatchGateOutDocument,
    SalesDispatchGateOutItem,
)
from gate_core.permissions import HasRequiredDjangoPermission
from gate_core.serializers_sales_dispatch import SalesDispatchGateOutSerializer
from gate_core.views_sales_dispatch import get_sales_dispatch_or_404


class PartialDispatchApprovalSerializer(serializers.ModelSerializer):
    class Meta:
        model = PartialDispatchApproval
        fields = [
            "id",
            "sales_dispatch",
            "document",
            "reason",
            "status",
            "credit_note_no",
            "requested_by",
            "requested_at",
            "decided_by",
            "decided_at",
        ]


class SalesDispatchRemoveDocumentView(APIView):
    """C1: drop a whole bill from a docking — it reschedules onto a future trip."""

    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "gate_core.can_edit_sales_dispatch_out"

    def post(self, request, entry_id, document_id):
        entry = get_sales_dispatch_or_404(request.company.company, entry_id)
        document = get_object_or_404(
            SalesDispatchGateOutDocument,
            id=document_id,
            sales_dispatch=entry,
            is_active=True,
        )
        if entry.documents.filter(is_active=True).count() <= 1:
            return Response(
                {
                    "detail": (
                        "Cannot remove the only bill on this docking — cancel the "
                        "docking instead."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        now = timezone.now()
        with transaction.atomic():
            plan = document.dispatch_plan
            SalesDispatchGateOutItem.objects.filter(
                sales_dispatch=entry, document=document, is_active=True
            ).update(is_active=False, updated_by=request.user, updated_at=now)
            document.is_active = False
            document.updated_by = request.user
            document.save(update_fields=["is_active", "updated_by", "updated_at"])
            if plan is not None:
                # Void the cover and return the bill to Expected Dispatch.
                EmptyVehicleGateInCover.objects.filter(
                    dispatch_plan=plan, is_active=True
                ).update(is_active=False, updated_by=request.user, updated_at=now)
                plan.linked_vehicle_entry = None
                plan.booking_status = DispatchPlanStatus.BOOKED
                plan.updated_by = request.user
                plan.save(
                    update_fields=[
                        "linked_vehicle_entry",
                        "booking_status",
                        "updated_by",
                        "updated_at",
                    ]
                )
        return Response(SalesDispatchGateOutSerializer(entry).data)


class SalesDispatchPartialApprovalRequestView(APIView):
    """C2: record held item quantities and open a partial-dispatch approval."""

    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "gate_core.can_edit_sales_dispatch_out"

    def post(self, request, entry_id):
        entry = get_sales_dispatch_or_404(request.company.company, entry_id)
        document = get_object_or_404(
            SalesDispatchGateOutDocument,
            id=request.data.get("document_id"),
            sales_dispatch=entry,
            is_active=True,
        )
        if PartialDispatchApproval.objects.filter(document=document, is_active=True).exists():
            return Response(
                {"detail": "A partial-dispatch approval already exists for this bill."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        now = timezone.now()
        with transaction.atomic():
            for row in request.data.get("items", []):
                SalesDispatchGateOutItem.objects.filter(
                    id=row.get("item_id"),
                    document=document,
                    sales_dispatch=entry,
                    is_active=True,
                ).update(
                    dispatched_quantity=row.get("dispatched_quantity"),
                    updated_by=request.user,
                    updated_at=now,
                )
            approval = PartialDispatchApproval.objects.create(
                company=request.company.company,
                sales_dispatch=entry,
                document=document,
                reason=request.data.get("reason", ""),
                status=PartialDispatchApprovalStatus.PENDING,
                requested_by=request.user,
                created_by=request.user,
                updated_by=request.user,
            )
        return Response(
            PartialDispatchApprovalSerializer(approval).data,
            status=status.HTTP_201_CREATED,
        )


class SalesDispatchPartialApprovalDecideView(APIView):
    """C2: approve/reject a partial dispatch (separate authoriser permission)."""

    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "gate_core.can_approve_partial_sales_dispatch"

    def post(self, request, approval_id):
        approval = get_object_or_404(
            PartialDispatchApproval,
            id=approval_id,
            company=request.company.company,
            is_active=True,
        )
        decision = (request.data.get("decision") or "").upper()
        if decision not in (
            PartialDispatchApprovalStatus.APPROVED,
            PartialDispatchApprovalStatus.REJECTED,
        ):
            return Response(
                {"detail": "decision must be APPROVED or REJECTED."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        approval.status = decision
        if decision == PartialDispatchApprovalStatus.APPROVED:
            approval.credit_note_no = (
                request.data.get("credit_note_no", "") or approval.credit_note_no
            )
        approval.decided_by = request.user
        approval.decided_at = timezone.now()
        approval.updated_by = request.user
        approval.save(
            update_fields=[
                "status",
                "credit_note_no",
                "decided_by",
                "decided_at",
                "updated_by",
                "updated_at",
            ]
        )
        return Response(PartialDispatchApprovalSerializer(approval).data)
