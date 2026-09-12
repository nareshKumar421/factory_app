"""Activation becomes the default: printed labels are inactive everywhere.

Changing the field default does not touch rows that already exist, and every row
that exists was created implicitly by ``get_or_create`` under the old
off-by-default — nobody has ever deliberately switched a company off, because
the feature has not been in service. So the rows are brought up to the new
default too; without that, the companies that happened to print a label during
development would be the only ones silently exempt.

Reversing restores the off-by-default and switches every row back off.
"""
from django.db import migrations, models


def enable_existing_rows(apps, schema_editor):
    Settings = apps.get_model('barcode', 'BarcodeActivationSettings')
    Settings.objects.update(is_enabled=True)


def disable_existing_rows(apps, schema_editor):
    Settings = apps.get_model('barcode', 'BarcodeActivationSettings')
    Settings.objects.update(is_enabled=False)


class Migration(migrations.Migration):

    dependencies = [
        ('barcode', '0022_backfill_legacy_activation'),
    ]

    operations = [
        migrations.AlterField(
            model_name='barcodeactivationsettings',
            name='enforced_warehouses',
            field=models.JSONField(blank=True, default=list, help_text='Leave EMPTY to cover every warehouse (the default). List codes, e.g. ["BH-PF"], to narrow the rule to those only.'),
        ),
        migrations.AlterField(
            model_name='barcodeactivationsettings',
            name='is_enabled',
            field=models.BooleanField(default=True, help_text='Master switch for this company. Untick to go back to labels being trusted the moment they are printed.'),
        ),
        migrations.RunPython(enable_existing_rows, disable_existing_rows),
    ]
