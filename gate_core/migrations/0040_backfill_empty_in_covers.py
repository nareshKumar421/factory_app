"""Backfill cover rows for existing in-flight dispatch empty-ins.

Bill-accurate docking eligibility (see ``pending_dispatch_plan_queryset``) keys a
plan's dockability on it having an *unconsumed cover* on its linked, non-retired
empty-in. Existing COMPLETED empty-ins predate covers, so without this backfill
their currently-linked BOOKED bills would silently stop being dockable. For every
active, non-retired, COMPLETED dispatch empty-in we materialise one cover per
still-BOOKED plan linked to its vehicle entry. Additive and idempotent.
"""

from django.db import migrations


def backfill_covers(apps, schema_editor):
    EmptyVehicleGateIn = apps.get_model("gate_core", "EmptyVehicleGateIn")
    EmptyVehicleGateInCover = apps.get_model("gate_core", "EmptyVehicleGateInCover")
    DispatchPlan = apps.get_model("dispatch_plans", "DispatchPlan")

    gate_ins = EmptyVehicleGateIn.objects.filter(
        is_active=True,
        reason="DISPATCH",
        retired_at__isnull=True,
        vehicle_entry__status="COMPLETED",
    )

    new_covers = []
    for gate_in in gate_ins.iterator():
        already = set(
            EmptyVehicleGateInCover.objects.filter(
                empty_vehicle_gate_in_id=gate_in.id
            ).values_list("sap_doc_entry", flat=True)
        )
        plans = DispatchPlan.objects.filter(
            company_id=gate_in.company_id,
            is_active=True,
            booking_status="BOOKED",
            linked_vehicle_entry_id=gate_in.vehicle_entry_id,
        )
        for plan in plans:
            if plan.sap_invoice_doc_entry in already:
                continue
            already.add(plan.sap_invoice_doc_entry)
            new_covers.append(
                EmptyVehicleGateInCover(
                    empty_vehicle_gate_in_id=gate_in.id,
                    dispatch_plan_id=plan.id,
                    sap_doc_entry=plan.sap_invoice_doc_entry,
                    sap_doc_num=plan.sap_invoice_doc_num or "",
                    is_active=True,
                )
            )

    if new_covers:
        EmptyVehicleGateInCover.objects.bulk_create(new_covers, batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ("gate_core", "0039_emptyvehiclegatein_retired_at_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_covers, migrations.RunPython.noop),
    ]
