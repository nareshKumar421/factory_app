# Generated for the invoice-sourced (cross-company) BST feature.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('company', '0001_initial'),
        ('warehouse', '0007_bsttransferdoc_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='bsttransfer',
            name='source_type',
            field=models.CharField(
                choices=[('STOCK_TRANSFER', 'Stock Transfer'), ('INVOICE', 'Invoice')],
                default='STOCK_TRANSFER',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='bsttransfer',
            name='destination_company',
            field=models.ForeignKey(
                blank=True,
                help_text='Receiving company for an INVOICE (cross-company) transfer.',
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='bst_transfers_incoming',
                to='company.company',
            ),
        ),
        migrations.AddField(
            model_name='bsttransfer',
            name='customer_code',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
        migrations.AddField(
            model_name='bsttransfer',
            name='customer_name',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
    ]
