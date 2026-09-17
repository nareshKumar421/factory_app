"""Late dispatch gate-in approvals: dispatch raises, an admin decides.

Past the evening cutoff a DISPATCH empty-vehicle gate-in is refused until an
approval exists (see ``gate_core.services.late_dispatch_gate_in``). These are the
three endpoints around that:

* dispatch raises a request from Dispatch > Vehicle Linking, against a truck it
  has already linked bills to and before that truck arrives,
* either side reads back where a truck stands,
* an approver clears or refuses it from Admin > Late Dispatch Gate-In Approvals.

Raising is gated on ``dispatch_plans.can_link_dispatch_vehicle`` -- the right to
plan a truck onto a load is the right to ask for that truck to be let in late,
and it is precisely the right the Vehicle Linking page already runs on. The gate
holds no such right and has no endpoint of its own: it reads the answer and obeys
it. Seeing the queue and deciding on it are their own Django permissions.
"""

from django.db import IntegrityError
from django.shortcuts import get_object_or_404
from django.utils import timezone
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
# Raising the request is a dispatch act, done on the Vehicle Linking page and
# carrying that page's own right rather than a permission of its own.
PERM_REQUEST = "dispatch_plans.can_link_dispatch_vehicle"


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
    """Dispatch-side payload. The load is resolved server-side from the truck's plans.

    No arrival time is asked for. The request is raised before the truck turns up,
    so there is no arrival to state; the hour it actually came in is stamped on the
    approval later, by the gate-in that spends it.
    """

    vehicle_id = serializers.IntegerField()
    # The day the truck is expected. Defaults to today, which is the ordinary case:
    # dispatch can see the truck is running late and asks for tonight.
    gate_in_date = serializers.DateField(required=False)
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
    POST /api/v1/gate-core/late-dispatch-approvals/   -> dispatch asks to let a truck in late
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]

    def required_permissions(self, request):
        """Asking is dispatch's; reading is either side's; deciding is elsewhere.

        POST carries the Vehicle Linking right. GET is the approver's queue, but
        dispatch has to be able to read back what it raised -- Vehicle Linking
        badges every expected truck from this one call rather than asking per
        truck -- so either right is enough to look. Neither one decides anything;
        that is the approve/reject endpoints, and they want ``PERM_APPROVE``.
        """
        if request.method != "GET":
            return [PERM_REQUEST]
        return [] if request.user.has_perm(PERM_REQUEST) else [PERM_VIEW]

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
        gate_in_date = data.get("gate_in_date") or timezone.localdate()

        # Deliberately not refused for being raised before the cutoff: the whole
        # point of moving this to dispatch is that they ask in the afternoon, while
        # there is still somebody around to answer, rather than at 8 PM with a truck
        # idling at the gate. An approval that turns out not to have been needed --
        # the truck arrives at four after all -- simply goes unspent.
        cleared = usable_approval(vehicle, gate_in_date, company_ids)
        if cleared is not None:
            # Already allowed and not yet spent -- nothing to ask.
            return Response(
                LateDispatchGateInApprovalSerializer(cleared).data,
                status=status.HTTP_200_OK,
            )

        existing = latest_approval(vehicle, gate_in_date, company_ids)
        if existing is not None and existing.is_pending:
            # Asking twice for the same truck asks the same question twice.
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
                gate_in_date=gate_in_date,
                reason=data["reason"],
                requested_by=request.user,
                created_by=request.user,
                updated_by=request.user,
                **load_snapshot(plans),
            )
        except IntegrityError:
            # Two dispatch users raced on the same truck; the one already in wins.
            existing = latest_approval(vehicle, gate_in_date, company_ids)
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

    Read by both sides of the rule, for different halves of the answer:

    * the Empty Vehicle In board asks the moment "Start Entry" is clicked, and reads
      ``requires_approval`` -- whether to walk on to the entry form or stop dead;
    * Vehicle Linking asks for each expected truck and reads ``approval`` -- whether
      dispatch has already asked for this truck, and what came back.

    Lateness is judged here rather than in either client: the cutoff is server
    configuration, and three copies of the rule would drift.

    Deliberately not gated on the approver's permissions -- neither reader is
    deciding anything, they are both looking up where a truck stands.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, vehicle_id):
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
        # The entry has not been typed yet, so the clock is the arrival time. Note
        # this is false all afternoon, which is exactly when dispatch raises the
        # request -- the linking page must not read it as "nothing to ask for".
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
