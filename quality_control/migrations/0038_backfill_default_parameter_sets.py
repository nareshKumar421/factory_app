# quality_control/migrations/0038_backfill_default_parameter_sets.py
"""Move existing QC parameters onto a default parameter set per material type.

Before vendor-wise parameters, every parameter hung directly off its material
type and applied to all vendors — which is exactly what the default set means,
so the backfill is a straight move with no behaviour change. Result rows also
get the rest of the parameter definition copied onto them, so historical
reports stop reading limits live from a master that is now vendor-specific and
gets edited more often.
"""

from django.db import migrations


SNAPSHOT_FIELDS = [
    "parameter_code",
    "parameter_type",
    "min_value",
    "max_value",
    "uom",
    "sequence",
    "is_mandatory",
]


def create_default_sets(apps, schema_editor):
    MaterialType = apps.get_model("quality_control", "MaterialType")
    QCParameterSet = apps.get_model("quality_control", "QCParameterSet")
    QCParameterMaster = apps.get_model("quality_control", "QCParameterMaster")

    default_set_by_material_type = {}
    for material_type in MaterialType.objects.all().iterator(chunk_size=500):
        parameter_set, _ = QCParameterSet.objects.get_or_create(
            material_type=material_type,
            vendor_code="",
            defaults={
                "vendor_name": "",
                "is_active": True,
            },
        )
        default_set_by_material_type[material_type.id] = parameter_set.id

    batch = []
    for parameter in QCParameterMaster.objects.filter(
        parameter_set__isnull=True
    ).iterator(chunk_size=500):
        set_id = default_set_by_material_type.get(parameter.material_type_id)
        if set_id is None:
            continue
        parameter.parameter_set_id = set_id
        batch.append(parameter)
        if len(batch) >= 500:
            QCParameterMaster.objects.bulk_update(batch, ["parameter_set"])
            batch = []
    if batch:
        QCParameterMaster.objects.bulk_update(batch, ["parameter_set"])

    # A parameter with no material type can't be placed on a set and would
    # block the non-null constraint in the next migration. There should be
    # none (the column was non-null until now), but drop any stragglers rather
    # than fail the deploy.
    QCParameterMaster.objects.filter(parameter_set__isnull=True).delete()


def snapshot_result_definitions(apps, schema_editor):
    for model_name in ("InspectionParameterResult", "ProductionQCResult"):
        model = apps.get_model("quality_control", model_name)
        batch = []
        rows = model.objects.filter(
            parameter_code=""
        ).select_related("parameter_master").iterator(chunk_size=500)
        for row in rows:
            parameter = row.parameter_master
            if parameter is None:
                continue
            for field in SNAPSHOT_FIELDS:
                setattr(row, field, getattr(parameter, field))
            batch.append(row)
            if len(batch) >= 500:
                model.objects.bulk_update(batch, SNAPSHOT_FIELDS)
                batch = []
        if batch:
            model.objects.bulk_update(batch, SNAPSHOT_FIELDS)


def link_inspections_to_default_sets(apps, schema_editor):
    """Record which set (and vendor) each existing inspection ran against.

    Everything inspected before this change used the material type's only
    parameter list, which is now its default set. The vendor is taken from the
    PO the inspection came through, so vendor-wise reporting works on history
    too.
    """
    RawMaterialInspection = apps.get_model("quality_control", "RawMaterialInspection")
    QCParameterSet = apps.get_model("quality_control", "QCParameterSet")

    default_sets = {
        row["material_type_id"]: row["id"]
        for row in QCParameterSet.objects.filter(vendor_code="").values(
            "id", "material_type_id"
        )
    }

    batch = []
    inspections = RawMaterialInspection.objects.filter(
        material_type__isnull=False, parameter_set__isnull=True
    ).select_related(
        "arrival_slip__po_item_receipt__po_receipt"
    ).iterator(chunk_size=500)

    for inspection in inspections:
        set_id = default_sets.get(inspection.material_type_id)
        if set_id is None:
            continue
        inspection.parameter_set_id = set_id

        slip = inspection.arrival_slip
        po_receipt = getattr(
            getattr(slip, "po_item_receipt", None), "po_receipt", None
        )
        if po_receipt is not None:
            inspection.vendor_code = (po_receipt.supplier_code or "").strip().upper()[:30]
            inspection.vendor_name = (po_receipt.supplier_name or "").strip()[:150]

        batch.append(inspection)
        if len(batch) >= 500:
            RawMaterialInspection.objects.bulk_update(
                batch, ["parameter_set", "vendor_code", "vendor_name"]
            )
            batch = []
    if batch:
        RawMaterialInspection.objects.bulk_update(
            batch, ["parameter_set", "vendor_code", "vendor_name"]
        )


def noop(apps, schema_editor):
    """Reverse is a no-op: the next migration restores material_type itself."""


class Migration(migrations.Migration):

    dependencies = [
        ("quality_control", "0037_qc_parameter_sets"),
    ]

    operations = [
        migrations.RunPython(create_default_sets, noop),
        migrations.RunPython(snapshot_result_definitions, noop),
        migrations.RunPython(link_inspections_to_default_sets, noop),
    ]
