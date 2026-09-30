from django.db import migrations


class Migration(migrations.Migration):
    """A parameter type's form number now lives in Print Documents (0067)."""

    dependencies = [
        ("quality_control", "0067_move_production_form_numbers"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="productionparametertype",
            name="document_code",
        ),
    ]
