"""One open ``VehicleArrival`` per physical truck.

Adds the partial-unique backstop behind ``resolve_open_arrival_for_vehicle`` so a
single truck trip can never split into multiple arrival entries. Because the bug
this fixes may already have minted duplicate open arrivals in the wild, a data
step first *merges* each vehicle's extra open arrivals into the earliest one
(re-pointing its gate-ins and dockings, carrying over a gatepass if the survivor
has none) and cancels the emptied duplicates -- so adding the constraint can't
fail on existing dirty data.
"""

from django.db import migrations, models


OPEN_STATUSES = ["INSIDE", "LOADING"]


def merge_duplicate_open_arrivals(apps, schema_editor):
    VehicleArrival = apps.get_model("gate_core", "VehicleArrival")
    EmptyVehicleGateIn = apps.get_model("gate_core", "EmptyVehicleGateIn")
    SalesDispatchGateOut = apps.get_model("gate_core", "SalesDispatchGateOut")

    open_arrivals = (
        VehicleArrival.objects.filter(is_active=True, status__in=OPEN_STATUSES)
        .order_by("vehicle_id", "created_at", "id")
    )
    by_vehicle = {}
    for arrival in open_arrivals:
        by_vehicle.setdefault(arrival.vehicle_id, []).append(arrival)

    for vehicle_id, arrivals in by_vehicle.items():
        if len(arrivals) < 2:
            continue
        survivor = arrivals[0]  # earliest-created keeps the trip
        for dup in arrivals[1:]:
            EmptyVehicleGateIn.objects.filter(arrival_id=dup.id).update(
                arrival_id=survivor.id
            )
            SalesDispatchGateOut.objects.filter(arrival_id=dup.id).update(
                arrival_id=survivor.id
            )
            if not survivor.gatepass_no and dup.gatepass_no:
                survivor.gatepass_no = dup.gatepass_no
                survivor.gatepass_random_code = dup.gatepass_random_code
                survivor.gatepass_printed_at = dup.gatepass_printed_at
                survivor.gatepass_printed_by_id = dup.gatepass_printed_by_id
                survivor.gatepass_committed_at = dup.gatepass_committed_at
                survivor.gatepass_committed_by_id = dup.gatepass_committed_by_id
                # Free the unique gatepass_no on the duplicate before saving it here.
                dup.gatepass_no = None
                dup.save(update_fields=["gatepass_no"])
                survivor.save(
                    update_fields=[
                        "gatepass_no",
                        "gatepass_random_code",
                        "gatepass_printed_at",
                        "gatepass_printed_by",
                        "gatepass_committed_at",
                        "gatepass_committed_by",
                    ]
                )
            dup.status = "CANCELLED"
            dup.cancel_reason = "Merged into arrival that shared this truck trip."
            dup.save(update_fields=["status", "cancel_reason"])


def noop_reverse(apps, schema_editor):
    # Merging is not reversible; dropping the constraint is enough to roll back.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("gate_core", "0049_alter_emptyvehiclegatein_retired_reason"),
    ]

    operations = [
        migrations.RunPython(merge_duplicate_open_arrivals, noop_reverse),
        migrations.AddConstraint(
            model_name="vehiclearrival",
            constraint=models.UniqueConstraint(
                fields=["vehicle"],
                condition=models.Q(is_active=True, status__in=["INSIDE", "LOADING"]),
                name="uniq_open_arrival_per_vehicle",
            ),
        ),
    ]
