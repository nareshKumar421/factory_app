from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("gate_core", "0051_gateattachment_document_code"),
    ]

    operations = [
        migrations.AddField(
            model_name="salesdispatchattachment",
            name="customer_code",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="salesdispatchattachment",
            name="customer_name",
            field=models.TextField(blank=True),
        ),
    ]
