"""Stamp every pre-existing box/pallet as LEGACY-activated.

Without this, every row printed before activation existed would read as "never
activated" — which is exactly the signal the new reports use to find phantom
stock, so the reports would open showing tens of thousands of false positives.

No row changes ``status``: existing stock stays exactly as trusted as it was.
"""
from django.db import migrations
from django.db.models import F


def stamp_legacy(apps, schema_editor):
    Box = apps.get_model('barcode', 'Box')
    Pallet = apps.get_model('barcode', 'Pallet')
    for model in (Box, Pallet):
        model.objects.filter(activation_source='').update(
            activation_source='LEGACY',
            activated_at=F('created_at'),
            activation_warehouse=F('current_warehouse'),
        )


def unstamp_legacy(apps, schema_editor):
    Box = apps.get_model('barcode', 'Box')
    Pallet = apps.get_model('barcode', 'Pallet')
    for model in (Box, Pallet):
        model.objects.filter(activation_source='LEGACY').update(
            activation_source='',
            activated_at=None,
            activation_warehouse='',
        )


class Migration(migrations.Migration):

    dependencies = [
        ('barcode', '0021_barcode_activation'),
    ]

    operations = [
        migrations.RunPython(stamp_legacy, unstamp_legacy),
    ]
