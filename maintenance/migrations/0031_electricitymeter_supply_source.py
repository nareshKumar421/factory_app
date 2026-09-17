from django.db import migrations, models


def _normalise(name):
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


# KVAH is KWH counted as apparent energy — the same grid feed measured a second
# way, not a second supply. It is read and shown, but adding it into the day's
# total supply would double the grid.
DUPLICATE_OF_ANOTHER_SUPPLY = {_normalise("KVAH")}


def name_the_supply(apps, schema_editor):
    """Every main meter on site today measures the grid; the DG is new."""
    ElectricityMeter = apps.get_model("maintenance", "ElectricityMeter")
    for meter in ElectricityMeter.objects.filter(is_main=True):
        ElectricityMeter.objects.filter(pk=meter.pk).update(
            supply_source="GRID",
            counts_as_supply=_normalise(meter.name) not in DUPLICATE_OF_ANOTHER_SUPPLY,
        )


def unname(apps, schema_editor):
    apps.get_model("maintenance", "ElectricityMeter").objects.update(
        supply_source="", counts_as_supply=True
    )


class Migration(migrations.Migration):

    dependencies = [
        ("maintenance", "0030_electricitymeter_is_main"),
    ]

    operations = [
        migrations.AddField(
            model_name="electricitymeter",
            name="supply_source",
            field=models.CharField(
                blank=True,
                choices=[("GRID", "Grid"), ("DG", "DG Set"), ("SOLAR", "Solar")],
                default="",
                help_text=(
                    "Which supply a main meter measures. Blank on a sub-meter, which "
                    "measures whatever the plant is running on that day."
                ),
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="electricitymeter",
            name="counts_as_supply",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "Add this main meter into the day's total supply. Untick a meter "
                    "that measures a supply another meter already counts — KVAH is the "
                    "grid's KWH as apparent energy, so counting both doubles the grid."
                ),
            ),
        ),
        migrations.RunPython(name_the_supply, unname),
    ]
