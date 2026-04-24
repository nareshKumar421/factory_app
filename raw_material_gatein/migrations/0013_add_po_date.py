from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('raw_material_gatein', '0012_merge_20260415_1350'),
    ]

    operations = [
        migrations.AddField(
            model_name='poreceipt',
            name='po_date',
            field=models.DateField(blank=True, help_text='PO posting date from SAP (OPOR.DocDate)', null=True),
        ),
    ]
