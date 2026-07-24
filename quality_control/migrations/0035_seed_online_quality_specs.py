from django.db import migrations


DEFAULT_SPECS = [
    # key, name, unit, min, max, spec_text, validation_type, sequence
    ("ph", "pH", "", "6.5", "8.5", "6.5-8.5", "RANGE", 1),
    ("tds", "TDS", "mg/L", "150", "250", "150-250", "RANGE", 2),
    ("turbidity", "Turbidity", "NTU", None, "1", "<1", "MAX", 3),
    ("alkalinity", "Alkalinity", "mg/L", "75", "100", "75-100", "RANGE", 4),
    ("total_hardness", "Total Hardness", "mg/L", None, None, "", "NONE", 5),
    ("calcium", "Calcium", "mg/L", None, None, "", "NONE", 6),
    ("magnesium", "Magnesium", "mg/L", None, None, "", "NONE", 7),
    ("chloride", "Chloride", "mg/L", None, None, "", "NONE", 8),
    ("torque", "Torque", "LBS", "8", "12", "10 ± 2", "RANGE", 9),
]


def seed(apps, schema_editor):
    Spec = apps.get_model("quality_control", "OnlineQualitySpec")
    for key, name, unit, mn, mx, text, vtype, seq in DEFAULT_SPECS:
        Spec.objects.get_or_create(
            company=None,
            parameter_key=key,
            defaults=dict(
                parameter_name=name, unit=unit,
                min_value=mn, max_value=mx,
                specification_text=text, validation_type=vtype, sequence=seq,
            ),
        )


def unseed(apps, schema_editor):
    Spec = apps.get_model("quality_control", "OnlineQualitySpec")
    Spec.objects.filter(company__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("quality_control", "0034_onlinequalityrecord_onlinequalityreading_and_more")]
    operations = [migrations.RunPython(seed, unseed)]
