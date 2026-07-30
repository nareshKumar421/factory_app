from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("gate_core", "0052_salesdispatchattachment_customer_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="salesdispatchattachment",
            name="bilty_no",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="salesdispatchattachment",
            name="bilty_date",
            field=models.DateField(blank=True, null=True),
        ),
    ]
