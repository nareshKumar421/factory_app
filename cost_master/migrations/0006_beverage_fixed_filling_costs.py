from datetime import date
from decimal import Decimal

from django.db import migrations

# Frozen here rather than imported from codes.py, so a later edit there cannot
# change what this migration did.
# code -> (name, description, Beverages' monthly rate)
TYPES = {
    'beverage-ground-water': ('Beverage — Ground Water Extraction',
                              'The ground water extraction bill.', '16000'),
    'beverage-amc': ('Beverage — AMC', 'Annual maintenance contracts, by the month.', '21510'),
    'beverage-servicing': ('Beverage — Servicing Cost', '', '74353'),
}
EFFECTIVE_FROM = date(2026, 9, 1)
NOTE = "Beverages' filling cost sheet"

# Miscellaneous went in at 10,000 a month (0004); the factory's figure is
# 1,00,000, as its own sheets have it.
MISC = 'beverage-miscellaneous'
MISC_WAS, MISC_IS = Decimal('10000'), Decimal('100000')


def add_fixed_filling_costs(apps, schema_editor):
    """The sheet's fixed monthly bills, and Miscellaneous at its real figure.

    Rates go in only where Beverages exists and has none for the date yet, and
    Miscellaneous is corrected only while it still reads what 0004 put in, so a
    rate entered by hand is left as it is.
    """
    CostType = apps.get_model('cost_master', 'CostType')
    CostRate = apps.get_model('cost_master', 'CostRate')
    Company = apps.get_model('company', 'Company')

    beverages = Company.objects.filter(code='JIVO_BEVERAGES').first()
    for code, (name, description, rate) in TYPES.items():
        cost_type, _ = CostType.objects.get_or_create(
            code=code, defaults={'name': name, 'default_basis': 'PER_MONTH',
                                 'description': description})
        if beverages is None:
            continue
        CostRate.objects.get_or_create(
            cost_type=cost_type, scope='COMPANY', company=beverages,
            effective_from=EFFECTIVE_FROM, is_active=True,
            defaults={'basis': 'PER_MONTH', 'rate': Decimal(rate), 'notes': NOTE})

    if beverages is not None:
        CostRate.objects.filter(
            cost_type__code=MISC, scope='COMPANY', company=beverages,
            effective_from=EFFECTIVE_FROM, is_active=True, rate=MISC_WAS,
        ).update(rate=MISC_IS)


class Migration(migrations.Migration):

    dependencies = [
        ('company', '0003_alter_usercompany_role'),
        ('cost_master', '0005_beverage_scrap_recovery'),
    ]

    operations = [
        migrations.RunPython(add_fixed_filling_costs, migrations.RunPython.noop),
    ]
