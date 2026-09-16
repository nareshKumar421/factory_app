"""Late dispatch gate-in approvals: the gate raises, an admin decides.

Past the evening cutoff a DISPATCH empty-vehicle gate-in is refused until an
approval exists (see ``gate_core.services.late_dispatch_gate_in``). These are the
three endpoints around that:

* the gate raises a request from the Empty Vehicle In board,
* the gate reads back the truck's current request to know where it stands,
* an approver clears or refuses it from Admin > Late Dispatch Gate-In Approvals.

Raising needs no special right -- anyone who can start an empty-vehicle gate-in
can ask for one to be allowed. Seeing the queue and deciding on it are their own
Django permissions.
"""

from django.db import IntegrityError
from django.shortcuts import get_object_or_404
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from vehicle_management.models import Vehicle

from .models import LateDispatchGateInApproval, LateDispatchGateInApprovalStatus
from .permissions import HasRequiredDjangoPermission
from .serializers_sales_dispatch import user_display_name
from .services.late_dispatch_gate_in import (
    booked_plans_for_vehicle,
    cutoff_time,
    is_late_dispatch_gate_in,
    latest_approval,
    load_snapshot,
    notify_approvers_of_new_request,
    notify_requester_of_review,
    resolve_dispatch_company,
    usable_approval,
)
from .services.user_scope import user_company_ids, wants_all_companies

PERM_VIEW = "gate_core.can_view_late_dispatch_gate_in"
PERM_APPROVE = "gate_core.can_approve_late_dispatch_gate_in"


class LateDispatchGateInApprovalSerializer(serializers.ModelSerializer):
    """Read serializer, enriched with the truck/load context the approver needs."""

    vehicle_no = serializers.SerializerMethodField()
    transporter_name = serializers.SerializerMethodField()
    company_code = serializers.SerializerMethodField()
    company_name = serializers.SerializerMethodField()
    requested_by_name = serializers.SerializerMethodField()
    reviewed_by_name = serializers.SerializerMethodField()
    gate_in_entry_no = serializers.SerializerMethodField()

    class Meta:
        model = LateDispatchGateInApproval
        fields = [
            "id",
            "company",
            "company_code",
            "company_name",
            "vehicle",
            "vehicle_no",
            "transporter_name",
            "gate_in_date",
            "in_time",
            "bill_doc_nums",
            "customer_names",
            "bill_count",
            "reason",
            "status",
            "requested_by",
            "requested_by_name",
            "requested_at",
            "reviewed_by",
            "reviewed_by_name",
            "reviewed_at",
            "review_notes",
            "empty_vehicle_gate_in",
            "gate_in_entry_no",
            "consumed_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_vehicle_no(self, obj):
        return getattr(obj.vehicle, "vehicle_number", "")

    def get_transporter_name(self, obj):
        transporter = getattr(obj.vehicle, "transporter", None)
        return getattr(transporter, "name", "") if transporter else ""

    def get_company_code(self, obj):
        return getattr(obj.company, "code", "")

    def get_company_name(self, obj):
        return getattr(obj.company, "name", "")

    def get_requested_by_name(self, obj):
        return user_display_name(obj.requested_by)

    def get_reviewed_by_name(self, obj):
        return user_display_name(obj.reviewed_by)

    def get_gate_in_entry_no(self, obj):
        return getattr(obj.empty_vehicle_gate_in, "entry_no", "")


class LateDispatchGateInApprovalCreateSerializer(serializers.Serializer):
    """Gate-side payload. The load is resolved server-side from the truck's plans."""

    vehicle_id = serializers.IntegerField()
    gate_in_date = serializers.DateField()
    in_time = serializers.TimeField()
    reason = serializers.CharField(trim_whitespace=True)

    def validate_reason(self, value):
        if not value.strip():
            raise serializers.ValidationError(
                "A reason is required to ask for a late dispatch gate-in."
            )
        return value.strip()


class LateDispatchGateInReviewSerializer(serializers.Serializer):
    """Approve/reject payload. Notes optional on approve, required on reject."""

    notes = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=True, default=""
    )


def approval_queryset(company_ids):
    return LateDispatchGateInApproval.objects.filter(
        company_id__in=company_ids, is_active=True
    ).select_related(
        "company",
        "vehicle",
        "vehicle__transporter",
        "requested_by",
        "reviewed_by",
        "empty_vehicle_gate_in",
    )


class LateDispatchGateInApprovalListCreateView(APIView):
    """
    GET  /api/v1/gate-core/late-dispatch-approvals/   -> approver queue (?status=&all_companies=)
    POST /api/v1/gate-core/late-dispatch-approvals/   -> the gate asks to let a truck in late
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    # POST is open to anyone who can work the gate: raising the question is not the
    # privilege, answering it is. GET is the approver's queue and needs the right.
    required_permissions = {"GET": PERM_VIEW}

    def get(self, request):
        # The gate is one physical place for all of a user's companies and a truck
        # carries whichever company's bills it was booked for, so an approval can be
        # filed in a company other than the reader's active header. The queue spans
        # every company the user belongs to when asked to -- otherwise a request
        # lands where nobody is watching and the truck waits at the gate.
        if wants_all_companies(request):
            company_ids = user_company_ids(request)
        else:
            company_ids = [request.company.company.id]
        queryset = approval_queryset(company_ids)

        status_filter = request.query_params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter.upper())

        vehicle = request.query_params.get("vehicle")
        if vehicle:
            queryset = queryset.filter(vehicle_id=vehicle)

        gate_in_date = request.query_params.get("gate_in_date")
        if gate_in_date:
            queryset = queryset.filter(gate_in_date=gate_in_date)

        serializer = LateDispatchGateInApprovalSerializer(queryset, many=True)
        return Response(serializer.data)

    def post(self, request):
        serializer = LateDispatchGateInApprovalCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        vehicle = get_object_or_404(Vehicle, id=data["vehicle_id"])
        company_ids = user_company_ids(request)

        if not is_late_dispatch_gate_in(data["gate_in_date"], data["in_time"]):
            return Response(
                {
                    "detail": (
                        f"This entry is before the {cutoff_time().strftime('%H:%M')} "
                        "cutoff — no approval is needed. Start the entry directly."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        cleared = usable_approval(vehicle, data["gate_in_date"], company_ids)
        if cleared is not None:
            # Already allowed and not yet spent -- nothing to ask.
            return Response(
                LateDispatchGateInApprovalSerializer(cleared).data,
                status=status.HTTP_200_OK,
            )

        existing = latest_approval(vehicle, data["gate_in_date"], company_ids)
        if existing is not None and existing.is_pending:
            # Clicking "Start Entry" twice asks the same question twice.
            return Response(
                LateDispatchGateInApprovalSerializer(existing).data,
                status=status.HTTP_200_OK,
            )

        plans = booked_plans_for_vehicle(vehicle, company_ids)
        # Filed in the company whose bills the truck carries, the same company the
        # gate-in itself will be created under, so the approval and the entry it
        # unlocks are never split across two companies' queues.
        company = resolve_dispatch_company(
            company_ids, request.company.company, vehicle
        )

        try:
            approval = LateDispatchGateInApproval.objects.create(
                company=company,
                vehicle=vehicle,
                gate_in_date=data["gate_in_date"],
                in_time=data["in_time"],
                reason=data["reason"],
                requested_by=request.user,
                created_by=request.user,
                updated_by=request.user,
                **load_snapshot(plans),
            )
        except IntegrityError:
            # Two gate terminals raced on the same truck; the one already in wins.
            existing = latest_approval(vehicle, data["gate_in_date"], company_ids)
            if existing is None:
                raise
            return Response(
                LateDispatchGateInApprovalSerializer(existing).data,
                status=status.HTTP_200_OK,
            )

        notify_approvers_of_new_request(approval)
        return Response(
            LateDispatchGateInApprovalSerializer(approval).data,
            status=status.HTTP_201_CREATED,
        )


class LateDispatchGateInApprovalForVehicleView(APIView):
    """
    GET /api/v1/gate-core/late-dispatch-approvals/by-vehicle/<vehicle_id>/?gate_in_date=
    Where this truck stands on the cutoff right now, and the request behind it.

    Asked by the Empty Vehicle In board the moment "Start Entry" is clicked, so the
    board knows whether to walk on to the entry form or stop and offer to send the
    vehicle for approval. Lateness is judged here rather than in the client: the
    cutoff is server configuration, and two copies of the rule would drift.

    Deliberately not gated on the approver's permissions -- this is the gate looking
    at its own request.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, vehicle_id):
        from django.utils import timezone
        from django.utils.dateparse import parse_date

        vehicle = get_object_or_404(Vehicle, id=vehicle_id)
        raw_date = request.query_params.get("gate_in_date")
        gate_in_date = parse_date(raw_date) if raw_date else timezone.localdate()
        if gate_in_date is None:
            return Response(
                {"detail": "gate_in_date must be an ISO date (YYYY-MM-DD)."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        now = timezone.localtime()
        company_ids = user_company_ids(request)
        # The entry has not been typed yet, so the clock is the arrival time.
        is_late = is_late_dispatch_gate_in(gate_in_date, now.time(), now=now)
        approval = latest_approval(vehicle, gate_in_date, company_ids)
        cleared = (
            usable_approval(vehicle, gate_in_date, company_ids) is not None
            if is_late
            else False
        )

        return Response(
            {
                "vehicle": vehicle.id,
                "vehicle_no": vehicle.vehicle_number,
                "gate_in_date": gate_in_date,
                "cutoff": cutoff_time().isoformat(timespec="minutes"),
                "is_late": is_late,
                # What the board branches on: late, and nothing already cleared it.
                "requires_approval": is_late and not cleared,
                "approval": (
                    LateDispatchGateInApprovalSerializer(approval).data
                    if approval is not None
                    else None
                ),
            }
        )


class LateDispatchGateInApprovalReviewBaseView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = {"POST": PERM_APPROVE}
    target_status = None

    def post(self, request, pk):
        # Resolved across the user's companies, like the queue: on a mixed-company
        # gate the request may be filed under a company other than the header.
        approval = get_object_or_404(
            approval_queryset(user_company_ids(request)), pk=pk
        )

        if not approval.is_pending:
            return Response(
                {
                    "detail": (
                        f"This request is already "
                        f"{approval.get_status_display().lower()}."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = LateDispatchGateInReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        notes = serializer.validated_data.get("notes", "").strip()

        if self.target_status == LateDispatchGateInApprovalStatus.REJECTED and not notes:
            return Response(
                {"notes": ["A note is required when rejecting a request."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        approval.mark_reviewed(
            status=self.target_status, reviewer=request.user, notes=notes
        )
        notify_requester_of_review(approval)
        return Response(LateDispatchGateInApprovalSerializer(approval).data)


class LateDispatchGateInApprovalApproveView(LateDispatchGateInApprovalReviewBaseView):
    """POST /api/v1/gate-core/late-dispatch-approvals/<pk>/approve/"""

    target_status = LateDispatchGateInApprovalStatus.APPROVED


class LateDispatchGateInApprovalRejectView(LateDispatchGateInApprovalReviewBaseView):
    """POST /api/v1/gate-core/late-dispatch-approvals/<pk>/reject/"""

    target_status = LateDispatchGateInApprovalStatus.REJECTED
