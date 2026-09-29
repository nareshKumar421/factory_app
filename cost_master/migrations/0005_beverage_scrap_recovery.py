from django.db import migrations


def add_scrap_recovery(apps, schema_editor):
    """The filling cost sheet's Scrap Recovering head: a credit per kg of waste.

    Created without a rate — the rupees a kg of scrap fetches is entered in the
    Cost Master, and until it is the sheet leaves the head to be typed in.
    """
    CostType = apps.get_model('cost_master', 'CostType')
    CostType.objects.get_or_create(
        code='beverage-scrap-recovery',
        defaults={
            'name': 'Beverage — Scrap Recovery',
            'default_basis': 'PER_KG',
            'is_credit': True,
            'description': 'Credit: rupees per kg of logged waste sold as scrap.',
        },
    )


class Migration(migrations.Migration):

    dependencies = [
        ('cost_master', '0004_beverage_filling_cost_types'),
    ]

    operations = [
        migrations.RunPython(add_scrap_recovery, migrations.RunPython.noop),
    ]
