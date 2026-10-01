from django.db import migrations, models


#: The floor each board read before the warehouse band became a ticked list.
#: Ticked here so neither wall changes the day this ships: Oil's BH-BT and
#: Beverages' BH-FG are what the two scopes were pinned to, and GP-FGM is Mart's
#: counterpart to BH-BT -- its main finished-goods godown, and the one Mart
#: warehouse somebody had already typed a capacity for.
BOARD_WAREHOUSES = (
    ("JIVO_OIL", "BH-BT"),
    ("JIVO_MART", "GP-FGM"),
    ("JIVO_BEVERAGES", "BH-FG"),
)


def tick_existing_floors(apps, schema_editor):
    WarehouseBoardSettings = apps.get_model("stock_dashboard", "WarehouseBoardSettings")
    for company_code, warehouse in BOARD_WAREHOUSES:
        WarehouseBoardSettings.objects.update_or_create(
            company_code=company_code,
            warehouse=warehouse,
            defaults={"on_board": True},
        )


class Migration(migrations.Migration):

    dependencies = [
        ("stock_dashboard", "0009_plantboardsettings"),
    ]

    operations = [
        migrations.AddField(
            model_name="warehouseboardsettings",
            name="on_board",
            # A database default, so a server still running code without this
            # field can go on inserting rows. See the model.
            field=models.BooleanField(
                default=False,
                db_default=False,
                help_text="Counted on the operations board's warehouse band.",
            ),
        ),
        # Unticking on the way back is unnecessary: the column goes with it.
        migrations.RunPython(tick_existing_floors, migrations.RunPython.noop),
    ]
