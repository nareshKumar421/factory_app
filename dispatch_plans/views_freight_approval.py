"""
dispatch_plans/views_freight_approval.py

Admin > Freight Approvals: the queue of trucks linked at a freight over their
benchmark, and the approver's decision.

The request is raised by entering the truck's freight at Vehicle Linking
(`TruckFreightAPI`), once for the whole truck after its bills are linked -- the
page links a truck one company at a time, and a freight is agreed per truck. The
server takes the truck's bills from its own booked plans, never from the client,
so the approver reads what the truck is actually carrying.

Seeing the queue and deciding on it are their own permissions. The queue spans
every company the reader belongs to when asked (`?all_companies=1`): the truck
carries whichever company's bills it was booked for, and a request filed where
nobody is looking leaves the truck waiting at the gate.
"""

from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from gate_core.permissions import HasRequiredDjangoPermission
from gate_core.services.user_scope import (
    assert_company_in_scope,
    user_company_ids,
    wants_all_companies,
)

from vehicle_management.models import Vehicle

from .freight_approval_service import (
    FreightApprovalError,
    record_truck_freight,
    review,
    truck_freight_board,
)
from .models_freight_approval import DispatchFreightApproval, FreightApprovalStatus
from .models_freight_benchmark import FreightDestination, FreightSlab

PERM_VIEW = "dispatch_plans.can_view_freight_approvals"
PERM_APPROVE = "dispatch_plans.can_approve_freight_approvals"
# Entering a truck's freight is part of linking it, so it carries that page's right.
PERM_LINK = "dispatch_plans.can_link_dispatch_vehicle"

# "All" is a look back, not an archive.
MAX_ROWS = 300


def _name(user) -> str:
    if user is None:
        return ""
    return user.full_name or user.email


class DispatchFreightApprovalSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company.code", read_only=True)
    company_name = serializers.CharField(source="company.name", read_only=True)
    vehicle_no = serializers.CharField(source="vehicle.vehicle_number", read_only=True)
    transporter_name = serializers.SerializerMethodField()
    suggested_slab_label = serializers.SerializerMethodField()
    requested_by_name = serializers.SerializerMethodField()
    reviewed_by_name = serializers.SerializerMethodField()
    rate_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, coerce_to_string=False, allow_null=True
    )
    load_kg = serializers.DecimalField(
        max_digits=18, decimal_places=3, coerce_to_string=False, allow_null=True
    )
    benchmark_freight = serializers.DecimalField(
        max_digits=18, decimal_places=2, coerce_to_string=False, allow_null=True
    )
    actual_freight = serializers.DecimalField(
        max_digits=18, decimal_places=2, coerce_to_string=False
    )
    excess = serializers.DecimalField(
        max_digits=18, decimal_places=2, coerce_to_string=False, allow_null=True
    )

    class Meta:
        model = DispatchFreightApproval
        fields = [
            "id",
            "company",
            "company_code",
            "company_name",
            "vehicle",
            "vehicle_no",
            "vehicle_capacity_kg",
            "transporter_name",
            "destination",
            "destination_label",
            "slab",
            "slab_label",
            "suggested_slab",
            "suggested_slab_label",
            "rate_basis",
            "rate_amount",
            "load_kg",
            "benchmark_freight",
            "actual_freight",
            "excess",
            "bill_doc_nums",
            "customer_names",
            "bill_count",
            "status",
            "reason",
            "requested_by_name",
            "requested_at",
            "reviewed_by_name",
            "reviewed_at",
            "review_notes",
        ]

    def get_transporter_name(self, obj) -> str:
        return obj.transporter.name if obj.transporter_id else ""

    def get_suggested_slab_label(self, obj) -> str:
        return obj.suggested_slab.label if obj.suggested_slab_id else ""

    def get_requested_by_name(self, obj) -> str:
        return _name(obj.requested_by)

    def get_reviewed_by_name(self, obj) -> str:
        return _name(obj.reviewed_by)


class BillRefSerializer(serializers.Serializer):
    company_code = serializers.CharField()
    doc_entry = serializers.IntegerField()


class TruckFreightSerializer(serializers.Serializer):
    vehicle_id = serializers.PrimaryKeyRelatedField(
        queryset=Vehicle.objects.all(), source="vehicle"
    )
    destination_id = serializers.PrimaryKeyRelatedField(
        queryset=FreightDestination.objects.all(), source="destination"
    )
    slab_id = serializers.PrimaryKeyRelatedField(
        queryset=FreightSlab.objects.all(), source="slab"
    )
    actual_freight = serializers.DecimalField(
        max_digits=18, decimal_places=2, min_value=0
    )
    reason = serializers.CharField(required=False, allow_blank=True, default="")
    # The bills the freight is for. With `extend`, also every bill the truck's
    # current freight already covers (the linking sheet, adding bills); without
    # it, exactly these (the truck card, showing the truck's bills).
    bills = BillRefSerializer(many=True, allow_empty=False)
    extend = serializers.BooleanField(default=False)


class FreightReviewSerializer(serializers.Serializer):
    notes = serializers.CharField(required=False, allow_blank=True, default="")


def approval_queryset(company_ids):
    return DispatchFreightApproval.objects.filter(company_id__in=company_ids).select_related(
        "company",
        "vehicle",
        "transporter",
        "suggested_slab",
        "requested_by",
        "reviewed_by",
    )


class DispatchFreightApprovalListAPI(APIView):
    """GET /api/v1/dispatch/freight-approvals/?status=&all_companies=

    `status` defaults to PENDING; `ALL` lifts it. SUPERSEDED rows are history
    and only come back when asked for by name.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]

    def required_permissions(self, request):
        return [] if request.user.has_perm(PERM_APPROVE) else [PERM_VIEW]

    def get(self, request):
        if wants_all_companies(request):
            company_ids = user_company_ids(request)
        else:
            company_ids = [request.company.company.id]
        queryset = approval_queryset(company_ids)

        wanted = (request.query_params.get("status") or "PENDING").upper()
        if wanted == "ALL":
            queryset = queryset.exclude(status=FreightApprovalStatus.SUPERSEDED)
        else:
            queryset = queryset.filter(status=wanted)

        return Response(
            DispatchFreightApprovalSerializer(queryset[:MAX_ROWS], many=True).data
        )


class _ReviewAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = {"POST": PERM_APPROVE}
    approve = True

    def post(self, request, pk):
        serializer = FreightReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            approval = get_object_or_404(
                DispatchFreightApproval.objects.select_for_update(), pk=pk
            )
            assert_company_in_scope(request, approval.company_id)
            try:
                review(
                    approval,
                    approve=self.approve,
                    reviewer=request.user,
                    notes=serializer.validated_data["notes"],
                )
            except FreightApprovalError as error:
                return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        fresh = approval_queryset([approval.company_id]).get(pk=approval.pk)
        return Response(DispatchFreightApprovalSerializer(fresh).data)


class DispatchFreightApproveAPI(_ReviewAPI):
    """POST /api/v1/dispatch/freight-approvals/<id>/approve/  {notes?}"""

    approve = True


class DispatchFreightRejectAPI(_ReviewAPI):
    """POST /api/v1/dispatch/freight-approvals/<id>/reject/  {notes}"""

    approve = False


class TruckFreightAPI(APIView):
    """POST /api/v1/dispatch/freight-approvals/truck/

    {vehicle_id, destination_id, slab_id, actual_freight, reason?, bills, extend?}
    -- the truck's freight, held against its benchmark and split over the bills
    it is for. Returns the approval it now stands on: WITHIN_BENCHMARK, or
    PENDING for Admin.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = {"POST": PERM_LINK}

    def post(self, request):
        serializer = TruckFreightSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            result = record_truck_freight(
                vehicle=data["vehicle"],
                company_ids=user_company_ids(request),
                bills=[(bill["company_code"], bill["doc_entry"]) for bill in data["bills"]],
                extend=data["extend"],
                destination=data["destination"],
                slab=data["slab"],
                actual_freight=data["actual_freight"],
                reason=data["reason"],
                user=request.user,
            )
        except FreightApprovalError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        approval = approval_queryset([result["approval"].company_id]).get(
            pk=result["approval"].pk
        )
        return Response(
            {
                "approval": DispatchFreightApprovalSerializer(approval).data,
                "shares": [
                    {
                        "plan_id": plan.pk,
                        "sap_invoice_doc_entry": plan.sap_invoice_doc_entry,
                        "company_code": plan.company.code,
                        "freight": float(plan.freight),
                    }
                    for plan in result["plans"]
                ],
            }
        )


class TruckFreightBoardAPI(APIView):
    """GET /api/v1/dispatch/freight-approvals/trucks/

    Every booked, not-yet-gated truck across the reader's companies, with the
    freight approval it stands on (or none), for the Vehicle Linking badges.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]

    def required_permissions(self, request):
        return [] if request.user.has_perm(PERM_LINK) else [PERM_VIEW]

    def get(self, request):
        board = truck_freight_board(user_company_ids(request))
        ids = [row["approval"].pk for row in board if row["approval"] is not None]
        approvals = {
            approval.pk: approval for approval in approval_queryset(user_company_ids(request)).filter(pk__in=ids)
        }
        return Response(
            [
                {
                    "vehicle_id": row["vehicle_id"],
                    "vehicle_no": row["vehicle_no"],
                    "covered_bills": row["covered_bills"],
                    "approval": (
                        DispatchFreightApprovalSerializer(approvals[row["approval"].pk]).data
                        if row["approval"] is not None and row["approval"].pk in approvals
                        else None
                    ),
                }
                for row in board
            ]
        )
