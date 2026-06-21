"""Cross-company physical-truck arrival endpoints.

These deliberately operate across *all* of the requesting user's companies (via
``UserCompany``) rather than the single ``Company-Code`` header, so one physical
truck carrying bills for several companies is gated in once and departs once.
"""

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
from driver_management.models import Driver
from gate_core.models import (
    SalesDispatchGateOutStatus,
    VehicleArrival,
    VehicleArrivalStatus,
)
from gate_core.serializers_arrival import (
    VehicleArrivalCreateSerializer,
    VehicleArrivalSerializer,
)
from gate_core.services.arrival_gatepass import (
    arrival_dockings,
    assign_arrival_gatepass,
    commit_arrival_gatepass,
    get_arrival_gatepass_readiness,
    locked_companies,
    reprint_arrival_gatepass,
)
from gate_core.services.empty_vehicle_dispatch import create_vehicle_arrival
from gate_core.services.sales_dispatch_dispatch import mark_docking_dispatched
from gate_core.services.user_scope import user_company_ids
from vehicle_management.models import Vehicle

_OPEN_ARRIVAL_STATUSES = [VehicleArrivalStatus.INSIDE, VehicleArrivalStatus.LOADING]


def _print_request_context(request):
    return {
        "ip_address": request.META.get("REMOTE_ADDR") or None,
        "user_agent": request.META.get("HTTP_USER_AGENT", ""),
    }


def _locked_response(locked_codes):
    return Response(
        {
            "detail": "Gatepass printing is locked for: " + ", ".join(locked_codes) + ".",
            "locked_companies": locked_codes,
        },
        status=423,
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
                company_id__in=user_company_ids(request),
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
            .prefetch_related("gate_ins__company", "gate_outs__company")
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
            company_ids=user_company_ids(request),
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


class _ArrivalGatepassBaseView(APIView):
    """Shared scope guard for the combined-gatepass endpoints.

    Authorisation follows the records, not the active header: every company on the
    arrival (gate-ins + dockings) must be one the user belongs to.
    """

    permission_classes = [IsAuthenticated]

    def get_arrival(self, request, arrival_id):
        arrival = get_object_or_404(
            VehicleArrival.objects.prefetch_related(
                "gate_ins__company", "gate_outs__company"
            ),
            id=arrival_id,
            is_active=True,
        )
        scope = set(user_company_ids(request))
        involved = {d.company_id for d in arrival.gate_outs.all()} | set(
            arrival.company_ids
        )
        if involved - scope:
            raise PermissionDenied(
                "This arrival includes a company outside your access."
            )
        return arrival


class VehicleArrivalGatepassReadinessView(_ArrivalGatepassBaseView):
    """Per-company readiness + whether the combined gatepass can be printed."""

    def get(self, request, arrival_id):
        arrival = self.get_arrival(request, arrival_id)
        return Response(get_arrival_gatepass_readiness(arrival))


class VehicleArrivalGatepassPrintView(_ArrivalGatepassBaseView):
    """Assign the combined ARV/... number and print every company's docking."""

    def post(self, request, arrival_id):
        arrival = self.get_arrival(request, arrival_id)
        dockings = arrival_dockings(arrival)
        if not dockings:
            return Response(
                {"detail": "No dockings to print on this arrival."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        locked = locked_companies(dockings)
        if locked:
            return _locked_response(locked)
        try:
            assign_arrival_gatepass(
                arrival,
                request.user,
                printer_name=request.data.get("printer_name", ""),
                **_print_request_context(request),
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(VehicleArrivalSerializer(arrival).data)


class VehicleArrivalGatepassCommitView(_ArrivalGatepassBaseView):
    """Commit every docking's print on the combined gatepass."""

    def post(self, request, arrival_id):
        arrival = self.get_arrival(request, arrival_id)
        try:
            commit_arrival_gatepass(arrival, request.user)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(VehicleArrivalSerializer(arrival).data)


class VehicleArrivalGatepassReprintView(_ArrivalGatepassBaseView):
    """Log an audited reprint for every printed docking on the combined gatepass."""

    def post(self, request, arrival_id):
        arrival = self.get_arrival(request, arrival_id)
        reason = (request.data.get("reprint_reason") or "").strip()
        if not reason:
            return Response(
                {"detail": "A reprint reason is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        locked = locked_companies(arrival_dockings(arrival))
        if locked:
            return _locked_response(locked)
        try:
            reprint_arrival_gatepass(
                arrival,
                request.user,
                reason,
                printer_name=request.data.get("printer_name", ""),
                **_print_request_context(request),
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(VehicleArrivalSerializer(arrival).data)


class VehicleArrivalDispatchView(_ArrivalGatepassBaseView):
    """Dispatch every company's docking on the truck in one action.

    Each docking must be PRINT_COMMITTED with a valid weight; the loop runs in one
    transaction so a not-yet-ready company rolls the whole dispatch back (no partial
    dispatch). Already-dispatched dockings are skipped so the action is idempotent.
    The physical exit stays a separate step (``VehicleArrivalDepartView``).
    """

    def post(self, request, arrival_id):
        arrival = self.get_arrival(request, arrival_id)
        dockings = arrival_dockings(arrival)
        if not dockings:
            return Response(
                {"detail": "No dockings to dispatch on this arrival."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            with transaction.atomic():
                for docking in dockings:
                    if docking.status == SalesDispatchGateOutStatus.DISPATCHED:
                        continue
                    mark_docking_dispatched(docking, request.user)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        arrival.refresh_from_db()
        return Response(VehicleArrivalSerializer(arrival).data)
