from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ar_invoice", "0006_ar_invoice_payment_group"),
    ]

    operations = [
        migrations.AddField(
            model_name="arinvoiceposting",
            name="dispatch_date",
            field=models.DateField(blank=True, null=True),
        ),
    ]
