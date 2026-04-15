from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('raw_material_gatein', '0010_add_po_detail_fields'),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name='poitemreceipt',
            unique_together=set(),
        ),
    ]
