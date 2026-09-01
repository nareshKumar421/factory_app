"""Backfill which observations actually meet the specification.

``allowed_values`` was doing two jobs: offering the operator a list of
options, *and* deciding whether a cell passed. Because that list holds the
failing option too ("Off Odour"), a failing observation was being reported
as in-spec. ``conforming_values`` now carries the passing subset.

Matched on the parameter name, case-insensitively, so it applies to the
seeded NMW form and to any copy of it created since. Only rows whose
``allowed_values`` still match the seeded pair are touched, so a form an
administrator has already customised is left alone.
"""

from django.db import migrations

# parameter name -> (offered options, the subset that passes)
CONFORMANCE = {
    "taste": (["Agreeable", "Not Agreeable"], ["Agreeable"]),
    "odour": (["No off Odour", "Off Odour"], ["No off Odour"]),
    "appearance": (["Clear", "Not Clear"], ["Clear"]),
}


def _norm(values):
    return [str(v).strip().casefold() for v in (values or [])]


def forwards(apps, schema_editor):
    db = schema_editor.connection.alias
    Parameter = apps.get_model("quality_control", "RecordTemplateParameter")

    for parameter in Parameter.objects.using(db).filter(value_type="CHOICE"):
        entry = CONFORMANCE.get(parameter.name.strip().casefold())
        if not entry:
            continue
        expected_options, conforming = entry
        # Leave a customised option list untouched -- we cannot know which of
        # its entries pass.
        if _norm(parameter.allowed_values) != _norm(expected_options):
            continue
        if parameter.conforming_values:
            continue
        parameter.conforming_values = conforming
        parameter.save(update_fields=["conforming_values"])


def backwards(apps, schema_editor):
    db = schema_editor.connection.alias
    Parameter = apps.get_model("quality_control", "RecordTemplateParameter")
    Parameter.objects.using(db).filter(value_type="CHOICE").update(
        conforming_values=[]
    )


class Migration(migrations.Migration):

    dependencies = [
        ("quality_control", "0045_recordtemplateparameter_conforming_values_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
