"""The right to link users to their SAP customers, on Admin › Customer Ledger
Links. Given to no group: superusers hold it, and it is granted by hand to
whoever looks after the links — like warehouse managers' own right.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('ar_invoice', '0010_user_customer_links'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='usercustomer',
            options={'ordering': ['company', 'customer_name', 'customer_code'], 'permissions': [('manage_customer_ledger_links', 'Can link users to their SAP customer accounts')], 'verbose_name': 'customer ledger link', 'verbose_name_plural': 'customer ledger links'},
        ),
    ]
