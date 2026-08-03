"""Post-dispatch truck tracking endpoints.

A dispatched truck is a ``VehicleArrival`` carrying at least one DISPATCHED
docking. These list such trucks with their current post-dispatch status and let
operators append status events (in transit, delivered, returned, ...). Scoped
across every company the user belongs to, since a truck is cross-company.
"""
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from gate_core.models import (
    SalesDispatchGateOutStatus,
    TERMINAL_DISPATCH_STATUSES,
    TruckDispatchStatus,
    TruckDispatchUpdate,
    VehicleArrival,
)
from gate_core.permissions import HasRequiredDjangoPermission
from gate_core.serializers_dispatch_tracking import (
    DispatchTrackingTruckSerializer,
    TruckDispatchUpdateCreateSerializer,
    TruckDispatchUpdateSerializer,
)
from gate_core.services.user_scope import user_company_ids


DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100


def _dispatched_trucks_qs(request):
    """Arrivals with a live DISPATCHED docking in one of the user's companies."""
    return (
        VehicleArrival.objects.filter(
            is_active=True,
            gate_outs__is_active=True,
            gate_outs__status=SalesDispatchGateOutStatus.DISPATCHED,
            gate_outs__company_id__in=list(user_company_ids(request)),
        )
        .distinct()
    )


def _parse_positive_int(value, default):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _row_matches_search(row, query):
    """True if any of the row's searchable fields contains ``query`` (lowercased)."""
    parts = [
        row.get("vehicle_number"),
        row.get("arrival_no"),
        row.get("gatepass_no"),
        row.get("driver_name"),
        row.get("current_status_display"),
    ]
    parts.extend(row.get("companies") or [])
    parts.extend(row.get("documents") or [])
    parts.extend(row.get("customers") or [])
    return any(query in str(part or "").lower() for part in parts)


class DispatchTrackingListView(APIView):
    """List dispatched trucks with their current post-dispatch status.

    Supports ``search``, ``status`` (comma-separated, matched against the
    truck's current derived status), ``from_date``/``to_date`` (on the dispatch
    date), and ``page``/``page_size`` — returning the standard paginated
    envelope. ``current_status`` is derived from the latest update, so status
    and text search run in Python over the serialized rows; the date filter
    narrows at the DB level first so counts stay accurate at scale.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "gate_core.can_view_dispatch_tracking"

    def get(self, request):
        qs = (
            _dispatched_trucks_qs(request)
            .select_related("vehicle", "driver")
            .prefetch_related(
                "gate_outs__company",
                "gate_outs__documents",
                "dispatch_updates",
            )
        )

        # Date filter on the dispatch date: departed_at, falling back to a
        # dispatched docking's dispatched_at when the truck has no gate-out
        # time yet — mirroring the serializer's get_dispatched_at().
        from_date = parse_date(request.query_params.get("from_date") or "")
        to_date = parse_date(request.query_params.get("to_date") or "")
        if from_date:
            qs = qs.filter(
                Q(departed_at__date__gte=from_date)
                | Q(departed_at__isnull=True, gate_outs__dispatched_at__date__gte=from_date)
            )
        if to_date:
            qs = qs.filter(
                Q(departed_at__date__lte=to_date)
                | Q(departed_at__isnull=True, gate_outs__dispatched_at__date__lte=to_date)
            )

        arrivals = qs.order_by("-departed_at", "-id").distinct()
        rows = DispatchTrackingTruckSerializer(arrivals, many=True).data

        status_param = request.query_params.get("status") or ""
        wanted_statuses = {s.strip() for s in status_param.split(",") if s.strip()}
        if wanted_statuses:
            rows = [row for row in rows if row["current_status"] in wanted_statuses]

        search = (request.query_params.get("search") or "").strip().lower()
        if search:
            rows = [row for row in rows if _row_matches_search(row, search)]

        page = _parse_positive_int(request.query_params.get("page"), 1)
        page_size = min(
            _parse_positive_int(request.query_params.get("page_size"), DEFAULT_PAGE_SIZE),
            MAX_PAGE_SIZE,
        )
        total_count = len(rows)
        total_pages = max((total_count + page_size - 1) // page_size, 1)
        page = min(page, total_pages)
        start = (page - 1) * page_size
        end = start + page_size

        return Response(
            {
                "results": rows[start:end],
                "count": total_count,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "next": page < total_pages,
                "previous": page > 1,
            }
        )


class DispatchTrackingUpdatesView(APIView):
    """A truck's post-dispatch status timeline; POST appends a new event."""

    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = {
        "GET": "gate_core.can_view_dispatch_tracking",
        "POST": "gate_core.can_update_dispatch_tracking",
    }

    def _get_arrival(self, request, arrival_id):
        return get_object_or_404(_dispatched_trucks_qs(request), id=arrival_id)

    def get(self, request, arrival_id):
        arrival = self._get_arrival(request, arrival_id)
        updates = arrival.dispatch_updates.filter(is_active=True).select_related("created_by")
        return Response(
            TruckDispatchUpdateSerializer(
                updates, many=True, context={"request": request}
            ).data
        )

    def post(self, request, arrival_id):
        arrival = self._get_arrival(request, arrival_id)
        serializer = TruckDispatchUpdateCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        update = TruckDispatchUpdate.objects.create(
            arrival=arrival,
            status=data["status"],
            occurred_at=data.get("occurred_at") or timezone.now(),
            expected_reach_date=data.get("expected_reach_date"),
            location=data.get("location", ""),
            remarks=data.get("remarks", ""),
            proof=data.get("proof"),
            created_by=request.user,
            updated_by=request.user,
        )
        return Response(
            TruckDispatchUpdateSerializer(update, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


# Status-value sets used by the summary aggregation (string values, so they compare
# cleanly against the CharField ``status`` stored on updates / the "DISPATCHED" start).
_IN_TRANSIT_VALUES = {TruckDispatchStatus.IN_TRANSIT.value, TruckDispatchStatus.DELAYED.value}
_DELIVERED_VALUES = {TruckDispatchStatus.DELIVERED.value, TruckDispatchStatus.PARTIALLY_DELIVERED.value}
_REACHED_OR_BEYOND_VALUES = {
    TruckDispatchStatus.REACHED_DESTINATION.value,
    TruckDispatchStatus.UNLOADING.value,
} | _DELIVERED_VALUES
_TERMINAL_VALUES = {s.value for s in TERMINAL_DISPATCH_STATUSES}


def _summary_dispatched_at(arrival):
    """The truck's dispatch time — departed_at, else its latest dispatched docking's
    dispatched_at. Mirrors the tracking serializer so KPIs line up with the board."""
    if arrival.departed_at:
        return arrival.departed_at
    times = [
        d.dispatched_at
        for d in arrival.gate_outs.all()
        if d.is_active
        and d.status == SalesDispatchGateOutStatus.DISPATCHED
        and d.dispatched_at
    ]
    return max(times) if times else None


class DispatchTrackingSummaryView(APIView):
    """Aggregate insight over dispatched trucks, for the tracking dashboard.

    Returns status-wise counts, a Dispatched -> In transit -> Reached -> Delivered
    funnel, the late/overdue trucks, and delivery KPIs (on-time rate, average transit
    days, delivered-today). Filtered on the dispatch date via ``from_date`` /
    ``to_date`` (defaulting to the current month) and scoped to the user's companies.
    The truck's current status is derived from its latest update — the same rule the
    list board uses — so numbers agree across the two screens.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "gate_core.can_view_dispatch_tracking"

    def get(self, request):
        today = timezone.localdate()
        from_date = parse_date(request.query_params.get("from_date") or "") or today.replace(day=1)
        to_date = parse_date(request.query_params.get("to_date") or "") or today

        qs = (
            _dispatched_trucks_qs(request)
            .select_related("vehicle")
            .prefetch_related("gate_outs", "dispatch_updates")
            .filter(
                Q(departed_at__date__gte=from_date, departed_at__date__lte=to_date)
                | Q(
                    departed_at__isnull=True,
                    gate_outs__dispatched_at__date__gte=from_date,
                    gate_outs__dispatched_at__date__lte=to_date,
                )
            )
            .distinct()
        )

        # Every status starts at zero so the dashboard always renders the full set.
        counts = {"DISPATCHED": 0}
        counts.update({s.value: 0 for s in TruckDispatchStatus})

        late_trucks = []
        transit_days = []
        delivered_with_eta = on_time = delivered_today = no_update_yet = 0

        for arrival in qs:
            updates = sorted(
                arrival.dispatch_updates.all(),
                key=lambda u: (u.occurred_at, u.id),
                reverse=True,
            )
            current = updates[0].status if updates else "DISPATCHED"
            counts[current] = counts.get(current, 0) + 1
            if not updates:
                no_update_yet += 1

            eta = next((u.expected_reach_date for u in updates if u.expected_reach_date), None)

            # Late / overdue: still on the road (in transit / delayed) past its reach-by date.
            if eta and current in _IN_TRANSIT_VALUES and eta < today:
                late_trucks.append({
                    "arrival": arrival.id,
                    "arrival_no": arrival.arrival_no,
                    "vehicle_number": (
                        getattr(arrival.vehicle, "vehicle_number", "") if arrival.vehicle_id else ""
                    ),
                    "expected_reach_date": eta,
                    "days_overdue": (today - eta).days,
                })

            # Delivery KPIs over trucks that have reached a delivered state.
            if current in _DELIVERED_VALUES:
                delivered_update = next((u for u in updates if u.status in _DELIVERED_VALUES), None)
                dispatched_at = _summary_dispatched_at(arrival)
                if delivered_update and dispatched_at:
                    days = (delivered_update.occurred_at - dispatched_at).days
                    if days >= 0:
                        transit_days.append(days)
                if delivered_update and delivered_update.occurred_at.date() == today:
                    delivered_today += 1
                if eta and delivered_update:
                    delivered_with_eta += 1
                    if delivered_update.occurred_at.date() <= eta:
                        on_time += 1

        total = sum(counts.values())
        active = sum(v for k, v in counts.items() if k not in _TERMINAL_VALUES)
        completed = total - active

        funnel = [
            {"stage": "Dispatched", "count": total},
            {"stage": "In transit", "count": total - counts["DISPATCHED"]},
            {"stage": "Reached", "count": sum(v for k, v in counts.items() if k in _REACHED_OR_BEYOND_VALUES)},
            {"stage": "Delivered", "count": sum(v for k, v in counts.items() if k in _DELIVERED_VALUES)},
        ]

        late_trucks.sort(key=lambda t: t["days_overdue"], reverse=True)

        return Response({
            "range": {"from": from_date, "to": to_date},
            "total_dispatched": total,
            "status_counts": counts,
            "active": active,
            "completed": completed,
            "no_update_yet": no_update_yet,
            "funnel": funnel,
            "late": {"count": len(late_trucks), "trucks": late_trucks[:20]},
            "delivered_today": delivered_today,
            "avg_transit_days": (
                round(sum(transit_days) / len(transit_days), 1) if transit_days else None
            ),
            "on_time_rate": round(on_time / delivered_with_eta, 2) if delivered_with_eta else None,
        })
