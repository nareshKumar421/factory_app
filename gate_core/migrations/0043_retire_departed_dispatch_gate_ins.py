"""Retire dispatch empty-ins whose truck has already left.

Retirement is new (0039), so every pre-existing empty-in has ``retired_at`` null.
The "vehicle is already inside" guard treats a live (non-retired, COMPLETED,
not-departed) dispatch gate-in as the truck still being in -- which means every
old gate-in whose truck already dispatched would wrongly block that vehicle from
a new entry. After the cover backfill (0040), a dispatch gate-in with no active,
unconsumed cover carries no live bill: its truck is gone. Retire those so the
guard only blocks trucks that are genuinely inside. Idempotent; reversible to a
no-op.
"""

from django.db import migrations


def retire_departed(apps, schema_editor):
    from django.utils import timezone

    EmptyVehicleGateIn = apps.get_model("gate_core", "EmptyVehicleGateIn")
    EmptyVehicleGateInCover = apps.get_model("gate_core", "EmptyVehicleGateInCover")

    now = timezone.now()
    targets = []
    gate_ins = EmptyVehicleGateIn.objects.filter(
        is_active=True,
        reason="DISPATCH",
        retired_at__isnull=True,
        vehicle_entry__status="COMPLETED",
    )
    for gate_in in gate_ins.iterator():
        has_live_cover = EmptyVehicleGateInCover.objects.filter(
            empty_vehicle_gate_in_id=gate_in.id,
            is_active=True,
            consumed_at__isnull=True,
        ).exists()
        if not has_live_cover:
            targets.append(gate_in.id)

    if targets:
        EmptyVehicleGateIn.objects.filter(id__in=targets).update(
            retired_at=now, retired_reason="DISPATCHED"
        )


class Migration(migrations.Migration):

    dependencies = [
        ("gate_core", "0042_salesdispatchgateoutitem_dispatched_quantity_and_more"),
    ]

    operations = [
        migrations.RunPython(retire_departed, migrations.RunPython.noop),
    ]
