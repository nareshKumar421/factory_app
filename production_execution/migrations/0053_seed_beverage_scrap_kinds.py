from datetime import date
from decimal import Decimal

from django.db import migrations

# Beverages' scrap table as the factory forwarded it on 12 Oct 2026, with the
# SAP items each row is. Frozen here, so a later change on the page is never
# undone by a re-run.
# (name, items, grams a piece, pieces a box, price a kg)
KINDS = [
    ('Preform 10.5 GM', [('PM0000659', 'PREFORM 10/10.5 GMS'), ('PM0000739', 'PREFORM 10.5 GMS')],
     '10.5', 2064, '49'),
    ('Preform 13.5 GM', [('PM0000705', 'PREFORM 13.5 GMS')], '13.5', 1512, '49'),
    ('Preform 19.5 GM', [('PM0000676', 'PREFORM 19.5 GMS')], '19.5', 1100, '49'),
    # The weight of a cap was not given: until it is entered, caps are named
    # on the sheet rather than guessed.
    ('Cap', [('PM0000641', 'CAP ALASKA SKY BLUE A27W'), ('PM0000787', 'CAP ALASKA DARK BLUE SANO'),
             ('PM0000788', 'CAP ALASKA NEPTUNE GREEN DIVINE')], None, 6000, '49'),
    # Counted in grams in SAP, so no weight a piece is needed.
    ('Shrink/BOPP', [
        ('PM0000664', 'SHRINK ROLL 250 MLS 24 PCS (460 MM)'),
        ('PM0000669', 'SHRINK ROLL 500 MLS 24 PCS (530MM)'),
        ('PM0000707', 'SHRINK ROLL 1000 MLS (490 MM 85 MC)'),
        ('PM0000665', 'SHRINK ROLL 750 MLS SODA 15 GMSS (510 MM)'),
        ('PM0000719', 'SHRINK SLEEVE 500 MLS MANGO'),
        ('PM0000666', 'BOPP 250 MLS WATER'),
        ('PM0000667', 'BOPP 500 MLS WATER'),
        ('PM0000668', 'BOPP 1000 MLS WATER'),
        ('PM0000783', 'BOPP 250 MLS WATER SANO'),
        ('PM0000784', 'BOPP 500 MLS WATER SANO'),
        ('PM0000785', 'BOPP 1000 MLS WATER SANO'),
        ('PM0000792', 'BOPP 250 MLS WATER ARSHDEEP'),
        ('PM0000793', 'BOPP 500 MLS WATER ARSHDEEP'),
        ('PM0000794', 'BOPP 1000 MLS WATER ARSHDEEP'),
        ('PM0000818', 'BOPP 1 LTR WATER VITAMIN D'),
        ('PM0000730', 'BOPP 160 MLS JEERA BEVERAGES'),
    ], None, None, '49'),
    # On the table without a price yet.
    ('Boxes', [], None, None, None),
]
EFFECTIVE_FROM = date(2026, 9, 1)


def seed_scrap_kinds(apps, schema_editor):
    Company = apps.get_model('company', 'Company')
    ScrapKind = apps.get_model('production_execution', 'ScrapKind')
    ScrapPrice = apps.get_model('production_execution', 'ScrapPrice')

    beverages = Company.objects.filter(code='JIVO_BEVERAGES').first()
    if beverages is None or ScrapKind.objects.filter(company=beverages).exists():
        return
    for order, (name, items, grams, per_box, price) in enumerate(KINDS):
        kind = ScrapKind.objects.create(
            company=beverages, name=name, sort_order=order,
            items=[{'code': code, 'name': item} for code, item in items],
            grams_per_piece=Decimal(grams) if grams else None,
            pieces_per_box=per_box,
        )
        if price:
            ScrapPrice.objects.create(kind=kind, price_per_kg=Decimal(price),
                                      effective_from=EFFECTIVE_FROM)


class Migration(migrations.Migration):

    dependencies = [
        ('company', '0003_alter_usercompany_role'),
        ('production_execution', '0052_scrap_kinds'),
    ]

    operations = [
        migrations.RunPython(seed_scrap_kinds, migrations.RunPython.noop),
    ]
