"""
Seed the blowing Cost Master (BlowingCostRate) from each company's current
effective BlowingRateConfig, so existing runs recompute to ~today's numbers
under the new two-electricity / preform-vs-blowing model.

Per company (global rows): operator, labour, electricity-utility carried over
from the rate config; electricity-machine 0 (new); packing 0 (manager: keep 0);
scrap recovery from the rate card (credit); benchmark 0.50.
"""
from decimal import Decimal

from django.db import migrations


def seed(apps, schema_editor):
    Company = apps.get_model('company', 'Company')
    BlowingRateConfig = apps.get_model('blowing', 'BlowingRateConfig')
    BlowingMachine = apps.get_model('blowing', 'BlowingMachine')
    BlowingCostRate = apps.get_model('blowing', 'BlowingCostRate')

    company_ids = set(
        BlowingRateConfig.objects.values_list('company_id', flat=True)
    ) | set(
        BlowingMachine.objects.values_list('company_id', flat=True)
    )

    for company in Company.objects.filter(id__in=company_ids):
        rc = (
            BlowingRateConfig.objects
            .filter(company=company, is_active=True)
            .order_by('-effective_from')
            .first()
        )
        rows = [
            ('OPERATOR', 'PER_PERSON_DAY', rc.operator_rate_per_day if rc else Decimal('0'), False),
            ('LABOUR', 'PER_PERSON_DAY', rc.labour_rate_per_day if rc else Decimal('0'), False),
            ('ELECTRICITY_MACHINE', 'PER_DAY', Decimal('0'), False),
            ('ELECTRICITY_UTILITY', 'PER_UNIT', rc.electricity_rate_per_unit if rc else Decimal('0'), False),
            ('PACKING', 'PER_BOTTLE', Decimal('0'), False),
            ('SCRAP_RECOVERY', 'PER_BOTTLE', rc.scrap_rate_per_bottle if rc else Decimal('0'), True),
            ('BENCHMARK_BLOWING_PER_BOTTLE', 'PER_BOTTLE', Decimal('0.50'), False),
        ]
        for category, basis, rate, is_credit in rows:
            BlowingCostRate.objects.update_or_create(
                company=company, machine=None, category=category, is_active=True,
                defaults={'basis': basis, 'rate': rate, 'is_credit': is_credit, 'label': ''},
            )


def unseed(apps, schema_editor):
    BlowingCostRate = apps.get_model('blowing', 'BlowingCostRate')
    BlowingCostRate.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('blowing', '0013_blowingruncost_benchmark_blowing_per_bottle_and_more'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
