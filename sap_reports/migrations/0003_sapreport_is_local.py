from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sap_reports", "0002_sapreportaccess"),
    ]

    operations = [
        migrations.AddField(
            model_name="sapreport",
            name="is_local",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Authored in this app (sap_reports/local_reports.py) rather than "
                    "mirrored from SAP's Query Manager; a sync never refreshes it and "
                    "never flags it as missing."
                ),
            ),
        ),
    ]
