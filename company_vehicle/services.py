"""Working out what the numbers mean.

Two jobs live here. The first is mileage: it cannot be computed on one row in
isolation, because a filling only tells you anything next to the filling
before it. The second is the summaries the dashboard and the vehicle page read.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, Max, Min, Sum
from django.utils import timezone

from .constants import ApprovalStatus, DOCUMENT_WARNING_DAYS
from .models import FleetVehicle, FuelEntry, ServiceEntry, VehicleDocument

TWO_PLACES = Decimal("0.01")


def recalculate_fuel_metrics(vehicle: FleetVehicle) -> None:
    """Rewrite ``distance_km`` and ``mileage`` on every fuel entry of a vehicle.

    Called after any create, edit or delete, because all three move the row
    that the entries after them are measured against. Cheap by design: a
    company fleet is tens of vehicles with hundreds of fillings, not millions.

    Two rules decide the numbers:

    * **Streams.** A dual-fuel vehicle's petrol fillings and CNG fillings are
      measured separately -- mixing km per litre with km per kg would be
      meaningless. Each fuel is its own chain.
    * **Full tank to full tank.** Mileage is the distance between two brimmed
      tanks divided by everything put in after the first of them. A part fill
      gets no mileage of its own but its litres still count towards the next
      full tank's figure, which is what makes that figure honest.

    Entries are walked in date order, not meter order: a replaced or wrapped
    odometer would otherwise reorder history. Where the meter goes backwards
    the distance is simply left blank.
    """
    entries = list(
        FuelEntry.objects.filter(vehicle=vehicle).order_by("entry_date", "odometer", "id")
    )

    # Per fuel: the previous entry, the previous FULL entry, and the quantity
    # accumulated since that full one.
    previous: dict[str, FuelEntry] = {}
    previous_full: dict[str, FuelEntry] = {}
    since_full: dict[str, Decimal] = {}

    changed = []
    for entry in entries:
        fuel = entry.fuel_type
        distance = None
        mileage = None

        last = previous.get(fuel)
        if last is not None and entry.odometer > last.odometer:
            distance = entry.odometer - last.odometer

        since_full[fuel] = since_full.get(fuel, Decimal("0")) + (entry.quantity or Decimal("0"))

        if entry.is_tank_full:
            anchor = previous_full.get(fuel)
            if anchor is not None and entry.odometer > anchor.odometer:
                consumed = since_full[fuel]
                if consumed > 0:
                    run = Decimal(entry.odometer - anchor.odometer)
                    mileage = (run / consumed).quantize(TWO_PLACES)
            previous_full[fuel] = entry
            since_full[fuel] = Decimal("0")

        if entry.distance_km != distance or entry.mileage != mileage:
            entry.distance_km = distance
            entry.mileage = mileage
            changed.append(entry)

        previous[fuel] = entry

    if changed:
        FuelEntry.objects.bulk_update(changed, ["distance_km", "mileage"])


def _approved(queryset):
    """Only approved money is spend. Pending bills are shown, never totalled."""
    return queryset.filter(approval_status=ApprovalStatus.APPROVED)


def _month_start(on: date) -> date:
    return on.replace(day=1)


def expiring_documents(within_days: int = DOCUMENT_WARNING_DAYS):
    """Documents already expired or expiring inside the window, soonest first."""
    today = timezone.localdate()
    return (
        VehicleDocument.objects.filter(
            is_active=True, expiry_date__lte=today + timedelta(days=within_days)
        )
        .select_related("vehicle")
        .order_by("expiry_date")
    )


def fleet_summary() -> dict:
    """The dashboard's five numbers, plus what needs someone's attention."""
    today = timezone.localdate()
    month_start = _month_start(today)

    vehicles = FleetVehicle.objects.filter(is_active=True)
    month_fuel = _approved(
        FuelEntry.objects.filter(entry_date__gte=month_start, entry_date__lte=today)
    ).aggregate(total=Sum("amount"), quantity=Sum("quantity"))
    month_service = _approved(
        ServiceEntry.objects.filter(entry_date__gte=month_start, entry_date__lte=today)
    ).aggregate(total=Sum("total_amount"))

    pending = {
        "fuel": FuelEntry.objects.filter(approval_status=ApprovalStatus.PENDING).count(),
        "service": ServiceEntry.objects.filter(approval_status=ApprovalStatus.PENDING).count(),
    }
    pending["total"] = pending["fuel"] + pending["service"]

    documents = expiring_documents()
    return {
        "month": month_start.isoformat(),
        "vehicle_count": vehicles.count(),
        "by_status": {
            row["status"]: row["n"]
            for row in vehicles.values("status").annotate(n=Count("id"))
        },
        "month_fuel_cost": month_fuel["total"] or Decimal("0"),
        "month_fuel_quantity": month_fuel["quantity"] or Decimal("0"),
        "month_service_cost": month_service["total"] or Decimal("0"),
        "month_total_cost": (month_fuel["total"] or Decimal("0"))
        + (month_service["total"] or Decimal("0")),
        "pending_approvals": pending,
        "expiring_documents": documents.count(),
        "expired_documents": documents.filter(expiry_date__lt=today).count(),
    }


def vehicle_summary(vehicle: FleetVehicle, date_from: date | None, date_to: date | None) -> dict:
    """One vehicle's running cost over a window, with its monthly series.

    ``cost_per_km`` is the figure the whole module exists to produce. It is
    left null rather than guessed when the window holds too few fillings to
    know the distance -- one fill tells you what was spent but not how far it
    went.
    """
    fuel = FuelEntry.objects.filter(vehicle=vehicle)
    service = ServiceEntry.objects.filter(vehicle=vehicle)
    if date_from:
        fuel = fuel.filter(entry_date__gte=date_from)
        service = service.filter(entry_date__gte=date_from)
    if date_to:
        fuel = fuel.filter(entry_date__lte=date_to)
        service = service.filter(entry_date__lte=date_to)

    approved_fuel = _approved(fuel)
    approved_service = _approved(service)

    fuel_totals = approved_fuel.aggregate(
        cost=Sum("amount"),
        quantity=Sum("quantity"),
        fills=Count("id"),
        first_odo=Min("odometer"),
        last_odo=Max("odometer"),
    )
    service_totals = approved_service.aggregate(cost=Sum("total_amount"), jobs=Count("id"))

    fuel_cost = fuel_totals["cost"] or Decimal("0")
    service_cost = service_totals["cost"] or Decimal("0")

    # Distance covered by the window is the span between its first and last
    # meter readings, not the sum of the per-entry distances: the first entry
    # in a window has no predecessor inside it.
    distance = None
    if (
        fuel_totals["first_odo"] is not None
        and fuel_totals["last_odo"] is not None
        and fuel_totals["last_odo"] > fuel_totals["first_odo"]
    ):
        distance = fuel_totals["last_odo"] - fuel_totals["first_odo"]

    cost_per_km = None
    if distance:
        cost_per_km = ((fuel_cost + service_cost) / Decimal(distance)).quantize(TWO_PLACES)

    # Mileage is averaged over the measured fills only -- the ones that sit
    # between two full tanks.
    measured = approved_fuel.exclude(mileage__isnull=True)
    mileage_by_fuel = {
        row["fuel_type"]: row["avg"]
        for row in measured.values("fuel_type").annotate(avg=Sum("mileage") / Count("id"))
    }

    months: dict[str, dict] = {}
    for row in (
        approved_fuel.values("entry_date__year", "entry_date__month")
        .annotate(cost=Sum("amount"), quantity=Sum("quantity"))
        .order_by("entry_date__year", "entry_date__month")
    ):
        key = f"{row['entry_date__year']:04d}-{row['entry_date__month']:02d}"
        months.setdefault(key, {"month": key, "fuel": Decimal("0"), "service": Decimal("0")})
        months[key]["fuel"] = row["cost"] or Decimal("0")
        months[key]["quantity"] = row["quantity"] or Decimal("0")
    for row in (
        approved_service.values("entry_date__year", "entry_date__month")
        .annotate(cost=Sum("total_amount"))
        .order_by("entry_date__year", "entry_date__month")
    ):
        key = f"{row['entry_date__year']:04d}-{row['entry_date__month']:02d}"
        months.setdefault(key, {"month": key, "fuel": Decimal("0"), "service": Decimal("0")})
        months[key]["service"] = row["cost"] or Decimal("0")

    series = []
    for key in sorted(months):
        row = months[key]
        row["total"] = row["fuel"] + row["service"]
        series.append(row)

    return {
        "fuel_cost": fuel_cost,
        "fuel_quantity": fuel_totals["quantity"] or Decimal("0"),
        "fill_count": fuel_totals["fills"] or 0,
        "service_cost": service_cost,
        "service_count": service_totals["jobs"] or 0,
        "total_cost": fuel_cost + service_cost,
        "distance_km": distance,
        "cost_per_km": cost_per_km,
        "mileage_by_fuel": mileage_by_fuel,
        "pending_fuel": fuel.filter(approval_status=ApprovalStatus.PENDING).count(),
        "pending_service": service.filter(approval_status=ApprovalStatus.PENDING).count(),
        "monthly": series,
    }


def fleet_cost_rows(date_from: date | None, date_to: date | None) -> list[dict]:
    """One row per vehicle for the cost report and its Excel export."""
    rows = []
    for vehicle in FleetVehicle.objects.filter(is_active=True).order_by("vehicle_number"):
        summary = vehicle_summary(vehicle, date_from, date_to)
        rows.append(
            {
                "vehicle_id": vehicle.id,
                "vehicle_number": vehicle.vehicle_number,
                "nickname": vehicle.nickname,
                "category": vehicle.category,
                "fuel_type": vehicle.fuel_type,
                "fuel_cost": summary["fuel_cost"],
                "fuel_quantity": summary["fuel_quantity"],
                "service_cost": summary["service_cost"],
                "total_cost": summary["total_cost"],
                "distance_km": summary["distance_km"],
                "cost_per_km": summary["cost_per_km"],
                "mileage_by_fuel": summary["mileage_by_fuel"],
            }
        )
    return rows
