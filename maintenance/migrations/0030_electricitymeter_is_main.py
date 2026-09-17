from django.db import migrations, models


def _normalise(name):
    """Same reduction the expense board matches meter names with."""
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


# The site's incomers, already named in factory_expense.constants as the meters
# every other one is a part of. Flagging them here moves that fact onto the
# master, where the register and the board can both read it.
KNOWN_MAINS = {_normalise(n) for n in ("KWH", "KVAH", "LP-196", "LP")}


def flag_known_mains(apps, schema_editor):
    ElectricityMeter = apps.get_model("maintenance", "ElectricityMeter")
    for meter in ElectricityMeter.objects.all():
        if _normalise(meter.name) in KNOWN_MAINS:
            ElectricityMeter.objects.filter(pk=meter.pk).update(is_main=True)


def unflag(apps, schema_editor):
    apps.get_model("maintenance", "ElectricityMeter").objects.update(is_main=False)


class Migration(migrations.Migration):

    dependencies = [
        ("maintenance", "0029_alter_maintenancepermission_options"),
    ]

    operations = [
        migrations.AddField(
            model_name="electricitymeter",
            name="is_main",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Main (incoming supply) meter. Every other meter is a sub-meter of "
                    "it, so its units are reported on their own and left out of the "
                    "register total — adding them would count the same electricity twice."
                ),
            ),
        ),
        migrations.RunPython(flag_known_mains, unflag),
    ]
