from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('weighment', '0002_alter_weighment_vehicle_entry'),
    ]

    operations = [
        migrations.AddField(
            model_name='weighment',
            name='challan_weight',
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=12, null=True),
        ),
    ]
