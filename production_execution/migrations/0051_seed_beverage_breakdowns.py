"""Seed Jivo Beverages' main breakdowns and their sub-breakdowns.

The plant's own list. A main with no subs is logged on its own. Beverages
only: Oil keeps its categories as they are. Idempotent, and matches existing
names case-insensitively, so a main someone already added by hand is reused
(and reactivated) rather than duplicated.
"""
from django.db import migrations

COMPANY_CODE = 'JIVO_BEVERAGES'

BREAKDOWNS = {
    'Power cut': [],
    'Blow moulding': [
        'Lock/unlock mould',
        'Blow pin safety',
        'Servo safety fault',
        'Oven heater',
        'Elevator jam',
        'Timing out',
        'Neck chilling supply problem',
    ],
    'Filler': [
        'Timing out',
        'Capper',
        'Filling wall',
        'Cap torque',
        'Belt',
        'Cap stuck',
    ],
    'Labeler': [
        'Vacuum problem',
        'Glue drum',
        'Label cutting problem',
        'Worm stuck',
        'Main label station',
        'Glue heating problem',
    ],
    'Shrink machine': [
        'Main tunnel heating problem',
        'Shrink welding problem',
        'Shrink cutting problem',
        'Bottle fallen',
    ],
    'Conveyor': [],
    'Batch coding': [],
    'Ogobature': [],
    'RO': [],
    'Chiller': [],
    'HP': [],
    'LP': [],
    'Manpower': [],
}


def _active(model, lookup, create):
    row = model.objects.filter(**lookup).first()
    if row is None:
        return model.objects.create(**create)
    if not row.is_active:
        row.is_active = True
        row.save(update_fields=['is_active', 'updated_at'])
    return row


def seed(apps, schema_editor):
    Company = apps.get_model('company', 'Company')
    BreakdownCategory = apps.get_model('production_execution', 'BreakdownCategory')
    BreakdownSubCategory = apps.get_model('production_execution', 'BreakdownSubCategory')

    company = Company.objects.filter(code=COMPANY_CODE).first()
    if company is None:
        return

    for main, subs in BREAKDOWNS.items():
        category = _active(
            BreakdownCategory,
            {'company': company, 'name__iexact': main},
            {'company': company, 'name': main},
        )
        for sub in subs:
            _active(
                BreakdownSubCategory,
                {'category': category, 'name__iexact': sub},
                {'category': category, 'name': sub},
            )


class Migration(migrations.Migration):

    dependencies = [
        ('production_execution', '0050_breakdown_subcategory'),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
