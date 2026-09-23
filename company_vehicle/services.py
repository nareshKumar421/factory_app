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
from .models import DailyReading, FleetVehicle, FuelEntry, ServiceEntry, VehicleDocument

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
    """Only an approved workshop bill is spend. A pending one is never totalled.

    Fuel does not go through here: a filling has no approval and counts the
    moment it is recorded.
    """
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
    month_fuel = FuelEntry.objects.filter(
        entry_date__gte=month_start, entry_date__lte=today
    ).aggregate(total=Sum("amount"), quantity=Sum("quantity"))
    month_service = _approved(
        ServiceEntry.objects.filter(entry_date__gte=month_start, entry_date__lte=today)
    ).aggregate(total=Sum("total_amount"))

    # Service alone: a filling has no approval to wait for.
    pending_service = ServiceEntry.objects.filter(approval_status=ApprovalStatus.PENDING).count()
    pending = {"service": pending_service, "total": pending_service}

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

    # Every filling counts; only the workshop bills are filtered.
    approved_fuel = fuel
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


# ---------------------------------------------------------------- running log


def _readings_by_date(vehicle: FleetVehicle, upto: date | None = None) -> dict[date, int]:
    """Every meter reading known for a vehicle, one per day, highest wins.

    Three tables hold readings -- the daily log, the fillings and the service
    bills -- because a person at a pump or a workshop is already writing the
    meter down. Merging them here is what stops anyone typing it twice, and
    taking the highest of a day's readings is what makes the day's closing
    figure the closing figure.
    """
    readings: dict[date, int] = {}

    def offer(on: date, value):
        if value is None:
            return
        if on not in readings or value > readings[on]:
            readings[on] = value

    daily = DailyReading.objects.filter(vehicle=vehicle)
    fuel = FuelEntry.objects.filter(vehicle=vehicle)
    service = ServiceEntry.objects.filter(vehicle=vehicle).exclude(odometer__isnull=True)
    if upto:
        daily = daily.filter(reading_date__lte=upto)
        fuel = fuel.filter(entry_date__lte=upto)
        service = service.filter(entry_date__lte=upto)

    for on, value in daily.values_list("reading_date", "odometer"):
        offer(on, value)
    for on, value in fuel.values_list("entry_date", "odometer"):
        offer(on, value)
    for on, value in service.values_list("entry_date", "odometer"):
        offer(on, value)
    return readings


def running_log(vehicle: FleetVehicle, date_from: date, date_to: date) -> dict:
    """One row per day: what the meter read, how far it ran, what it cost.

    Distance is only claimed where two readings bracket it. Where the previous
    reading is older than the day before, the row says so in ``covers_days``
    rather than pretending the whole distance belongs to this day -- a truck
    that was not read on Sunday did not do 600 km on Monday.

    A day with no reading is returned as a row all the same, with nulls in it.
    The gaps are the point: they are what tells a supervisor the log is not
    being kept.
    """
    readings = _readings_by_date(vehicle, upto=date_to)

    fuel_rows = FuelEntry.objects.filter(
        vehicle=vehicle, entry_date__gte=date_from, entry_date__lte=date_to
    )
    service_rows = ServiceEntry.objects.filter(
        vehicle=vehicle, entry_date__gte=date_from, entry_date__lte=date_to
    )
    notes = {
        row.reading_date: row.remarks
        for row in DailyReading.objects.filter(
            vehicle=vehicle, reading_date__gte=date_from, reading_date__lte=date_to
        )
        if row.remarks
    }

    fuel_by_date: dict[date, dict] = {}
    for row in fuel_rows:
        day = fuel_by_date.setdefault(
            row.entry_date, {"quantity": Decimal("0"), "cost": Decimal("0"), "fills": 0, "units": set()}
        )
        day["quantity"] += row.quantity or Decimal("0")
        day["cost"] += row.amount or Decimal("0")
        day["fills"] += 1
        day["units"].add(row.unit)

    service_by_date: dict[date, Decimal] = {}
    for row in _approved(service_rows).values_list("entry_date", "total_amount"):
        service_by_date[row[0]] = service_by_date.get(row[0], Decimal("0")) + (
            row[1] or Decimal("0")
        )

    # The last reading before the window opens, so the first day in it can
    # still report a distance.
    earlier = [on for on in readings if on < date_from]
    previous_date = max(earlier) if earlier else None
    previous_value = readings[previous_date] if previous_date else None

    rows = []
    totals = {
        "distance_km": 0,
        "fuel_quantity": Decimal("0"),
        "fuel_cost": Decimal("0"),
        "service_cost": Decimal("0"),
        "days_with_reading": 0,
    }

    day = date_from
    while day <= date_to:
        reading = readings.get(day)
        fuel = fuel_by_date.get(day)
        service_cost = service_by_date.get(day, Decimal("0"))

        distance = None
        covers_days = None
        if reading is not None and previous_value is not None and reading >= previous_value:
            distance = reading - previous_value
            covers_days = (day - previous_date).days if previous_date else None

        rows.append(
            {
                "date": day,
                "odometer": reading,
                "distance_km": distance,
                # 1 means "since yesterday". More means the distance is the
                # whole stretch since the last reading, not one day's running.
                "covers_days": covers_days,
                "fuel_quantity": fuel["quantity"] if fuel else None,
                "fuel_cost": fuel["cost"] if fuel else None,
                "fuel_fills": fuel["fills"] if fuel else 0,
                "fuel_unit": "/".join(sorted(fuel["units"])) if fuel else "",
                "service_cost": service_cost or None,
                "remarks": notes.get(day, ""),
            }
        )

        if reading is not None:
            totals["days_with_reading"] += 1
            previous_date, previous_value = day, reading
        if distance:
            totals["distance_km"] += distance
        if fuel:
            totals["fuel_quantity"] += fuel["quantity"]
            totals["fuel_cost"] += fuel["cost"]
        totals["service_cost"] += service_cost

        day += timedelta(days=1)

    totals["total_cost"] = totals["fuel_cost"] + totals["service_cost"]
    totals["cost_per_km"] = (
        (totals["total_cost"] / Decimal(totals["distance_km"])).quantize(TWO_PLACES)
        if totals["distance_km"]
        else None
    )
    totals["days_in_range"] = (date_to - date_from).days + 1
    totals["days_missing"] = totals["days_in_range"] - totals["days_with_reading"]

    return {"vehicle": vehicle, "rows": rows, "totals": totals}


def running_log_by_vehicle(date_from: date, date_to: date) -> list[dict]:
    """One row per vehicle: how far it ran in the window, and what that cost.

    The fleet-wide answer to "which vehicle ran how much", built from the same
    readings as the day-wise log so the two can never disagree.
    """
    rows = []
    for vehicle in FleetVehicle.objects.filter(is_active=True).order_by("vehicle_number"):
        log = running_log(vehicle, date_from, date_to)
        totals = log["totals"]
        rows.append(
            {
                "vehicle_id": vehicle.id,
                "vehicle_number": vehicle.vehicle_number,
                "nickname": vehicle.nickname,
                "category": vehicle.category,
                "fuel_unit": vehicle.fuel_unit,
                "last_odometer": vehicle.last_odometer,
                **totals,
            }
        )
    return rows
