"""Cross-company physical-truck arrival endpoints.

These deliberately operate across *all* of the requesting user's companies (via
``UserCompany``) rather than the single ``Company-Code`` header, so one physical
truck carrying bills for several companies is gated in once and departs once.
"""

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.models import UserCompany
from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
from driver_management.models import Driver
from gate_core.models import VehicleArrival, VehicleArrivalStatus
from gate_core.serializers_arrival import (
    VehicleArrivalCreateSerializer,
    VehicleArrivalSerializer,
)
from gate_core.services.empty_vehicle_dispatch import create_vehicle_arrival
from vehicle_management.models import Vehicle

_OPEN_ARRIVAL_STATUSES = [VehicleArrivalStatus.INSIDE, VehicleArrivalStatus.LOADING]


def _user_company_ids(user):
    return list(
        UserCompany.objects.filter(user=user, is_active=True).values_list(
            "company_id", flat=True
        )
    )


class VehicleArrivalExpectedView(APIView):
    """Bills booked to a vehicle across the user's companies, grouped by company."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        vehicle_id = request.query_params.get("vehicle_id")
        if not vehicle_id:
            return Response(
                {"detail": "vehicle_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        plans = (
            DispatchPlan.objects.filter(
                company_id__in=_user_company_ids(request.user),
                vehicle_id=vehicle_id,
                booking_status=DispatchPlanStatus.BOOKED,
                linked_vehicle_entry__isnull=True,
                is_active=True,
            )
            .select_related("company")
            .order_by("company__code", "dispatch_date", "sap_invoice_doc_entry")
        )
        groups = {}
        for plan in plans:
            group = groups.setdefault(
                plan.company_id,
                {
                    "company_id": plan.company_id,
                    "company_code": plan.company.code,
                    "company_name": plan.company.name,
                    "bills": [],
                },
            )
            group["bills"].append(
                {
                    "dispatch_plan_id": plan.id,
                    "sap_invoice_doc_entry": plan.sap_invoice_doc_entry,
                    "sap_invoice_doc_num": plan.sap_invoice_doc_num,
                    "invoice_number": plan.invoice_number,
                    "invoice_weight": plan.invoice_weight,
                    "total_litres": plan.total_litres,
                    "place_of_supply": plan.place_of_supply,
                    "dispatch_date": plan.dispatch_date,
                }
            )
        return Response(
            {"vehicle_id": int(vehicle_id), "companies": list(groups.values())}
        )


class VehicleArrivalListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = (
            VehicleArrival.objects.filter(is_active=True)
            .select_related("vehicle", "driver")
            .prefetch_related("gate_ins__company")
        )
        if request.query_params.get("open_only") in ("1", "true", "True", "yes"):
            qs = qs.filter(status__in=_OPEN_ARRIVAL_STATUSES)
        return Response(VehicleArrivalSerializer(qs[:200], many=True).data)

    def post(self, request):
        serializer = VehicleArrivalCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        vehicle = get_object_or_404(Vehicle, id=data["vehicle_id"])
        driver = get_object_or_404(Driver, id=data["driver_id"])

        if VehicleArrival.objects.filter(
            vehicle=vehicle, is_active=True, status__in=_OPEN_ARRIVAL_STATUSES
        ).exists():
            return Response(
                {"detail": "This vehicle already has an open arrival."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        arrival = create_vehicle_arrival(
            vehicle=vehicle,
            driver=driver,
            company_ids=_user_company_ids(request.user),
            gate_in_date=data["gate_in_date"],
            in_time=data["in_time"],
            tare_weight=data.get("tare_weight"),
            weighbridge_slip_no=data.get("weighbridge_slip_no", ""),
            security_name=data.get("security_name", ""),
            remarks=data.get("remarks", ""),
            user=request.user,
        )
        if arrival is None:
            return Response(
                {"detail": "No booked bills for this vehicle in your companies."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            VehicleArrivalSerializer(arrival).data, status=status.HTTP_201_CREATED
        )


class VehicleArrivalDepartView(APIView):
    """Record the single physical exit once every company chain is dispatched."""

    permission_classes = [IsAuthenticated]

    def post(self, request, arrival_id):
        arrival = get_object_or_404(VehicleArrival, id=arrival_id, is_active=True)
        if arrival.status == VehicleArrivalStatus.DEPARTED:
            return Response(
                {"detail": "Arrival already departed."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if arrival.status == VehicleArrivalStatus.CANCELLED:
            return Response(
                {"detail": "Cancelled arrival cannot depart."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if arrival.gate_ins.filter(is_active=True, retired_at__isnull=True).exists():
            return Response(
                {
                    "detail": (
                        "All companies must be dispatched before the truck can depart."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        arrival.status = VehicleArrivalStatus.DEPARTED
        arrival.gate_out_date = timezone.localdate()
        arrival.out_time = timezone.localtime().time().replace(microsecond=0)
        arrival.exit_security_name = request.data.get("security_name", "")
        arrival.departed_at = timezone.now()
        arrival.departed_by = request.user
        arrival.updated_by = request.user
        arrival.save(
            update_fields=[
                "status",
                "gate_out_date",
                "out_time",
                "exit_security_name",
                "departed_at",
                "departed_by",
                "updated_by",
                "updated_at",
            ]
        )
        return Response(VehicleArrivalSerializer(arrival).data)


class VehicleArrivalEmptyOutView(APIView):
    """Reset the whole physical trip across all companies (the correction path)."""

    permission_classes = [IsAuthenticated]

    def post(self, request, arrival_id):
        from gate_core.views import release_dispatch_plans_for_empty_out

        arrival = get_object_or_404(VehicleArrival, id=arrival_id, is_active=True)
        if arrival.status in (
            VehicleArrivalStatus.DEPARTED,
            VehicleArrivalStatus.CANCELLED,
        ):
            return Response(
                {"detail": "Arrival is already closed."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        with transaction.atomic():
            for gate_in in arrival.gate_ins.filter(is_active=True).select_related(
                "vehicle_entry"
            ):
                release_dispatch_plans_for_empty_out(gate_in.vehicle_entry, request.user)
            arrival.status = VehicleArrivalStatus.CANCELLED
            arrival.cancel_reason = request.data.get(
                "reason", "Reset via arrival empty-out."
            )
            arrival.cancelled_at = timezone.now()
            arrival.cancelled_by = request.user
            arrival.updated_by = request.user
            arrival.save(
                update_fields=[
                    "status",
                    "cancel_reason",
                    "cancelled_at",
                    "cancelled_by",
                    "updated_by",
                    "updated_at",
                ]
            )
        return Response(VehicleArrivalSerializer(arrival).data)
