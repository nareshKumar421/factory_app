from datetime import date
from decimal import Decimal

from django.db import migrations

# Frozen here rather than imported from codes.py, so a later edit there cannot
# change what this migration did.
# code -> (name, basis, description, Beverages' rate, rate notes)
TYPES = {
    'beverage-salary': ('Beverage — Fixed Manpower', 'PER_MONTH',
                        "Beverages' monthly salaries.", '1200000', ''),
    'beverage-maintenance': ('Beverage — Maintenance', 'PER_MONTH', '', '250000', ''),
    'beverage-batch-coding': ('Beverage — Batch Coding', 'PER_BOTTLE', '', '0.03', ''),
    # 0.2 over 15 litres, to the rate field's four places.
    'beverage-lubrication': ('Beverage — Lubrication', 'PER_LITRE',
                             'Rs. 0.2 per 15 litres.', '0.0133', 'Rs. 0.2 per 15 litres'),
    'beverage-lab': ('Beverage — Lab', 'PER_MONTH', '', '5000', ''),
    'beverage-miscellaneous': ('Beverage — Miscellaneous', 'PER_MONTH', '', '10000', ''),
}
EFFECTIVE_FROM = date(2026, 9, 1)
NOTE = "Beverages' filling cost sheet"


def add_beverage_filling_costs(apps, schema_editor):
    """The Cost Master types and Beverages rates the filling cost sheet opens from.

    The salary was first entered by hand as code '1', named 'beverage salary'.
    That type is renamed rather than duplicated, so what was entered against
    it carries on. Rates go in only where Beverages exists and has none for
    the date yet, so a rate already entered by hand is left as it is.
    """
    CostType = apps.get_model('cost_master', 'CostType')
    CostRate = apps.get_model('cost_master', 'CostRate')
    Company = apps.get_model('company', 'Company')

    hand_entered = CostType.objects.filter(
        code='1', name__icontains='salary', default_basis='PER_MONTH').first()
    if hand_entered and not CostType.objects.filter(code='beverage-salary').exists():
        hand_entered.code = 'beverage-salary'
        hand_entered.name = TYPES['beverage-salary'][0]
        hand_entered.save(update_fields=['code', 'name'])

    beverages = Company.objects.filter(code='JIVO_BEVERAGES').first()
    for code, (name, basis, description, rate, notes) in TYPES.items():
        cost_type, _ = CostType.objects.get_or_create(
            code=code, defaults={'name': name, 'default_basis': basis,
                                 'description': description})
        if beverages is None:
            continue
        CostRate.objects.get_or_create(
            cost_type=cost_type, scope='COMPANY', company=beverages,
            effective_from=EFFECTIVE_FROM, is_active=True,
            defaults={'basis': basis, 'rate': Decimal(rate),
                      'notes': notes or NOTE})


class Migration(migrations.Migration):

    dependencies = [
        ('company', '0003_alter_usercompany_role'),
        ('cost_master', '0002_costrate_idx_cost_rate_resolve_and_more'),
    ]

    operations = [
        migrations.RunPython(add_beverage_filling_costs, migrations.RunPython.noop),
    ]
