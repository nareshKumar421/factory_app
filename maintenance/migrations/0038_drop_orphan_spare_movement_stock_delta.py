"""Drop the ``stock_delta`` column live grew outside any migration.

No migration here ever made it, the model has no such field, and it is
NOT NULL without a default, so every stock movement the code writes (opening
stock on "Add item", give-out, adjustments) failed with an IntegrityError. The
table held no rows when this was written. ``IF EXISTS`` keeps it a no-op on a
database built from these migrations alone.
"""

from django.db import migrations


def drop_stock_delta(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        "ALTER TABLE maintenance_sparemovement DROP COLUMN IF EXISTS stock_delta"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("maintenance", "0037_spare_photos"),
    ]

    operations = [
        migrations.RunPython(drop_stock_delta, migrations.RunPython.noop),
    ]
