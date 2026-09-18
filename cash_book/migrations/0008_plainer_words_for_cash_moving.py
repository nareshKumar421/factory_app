"""
Plainer words for the two things that happen to cash.

"Advance given" and "Cash returned" read as jargon, and the screen built on
them asked people to choose between a noun and a verb tense. One is handing
somebody cash; the other is taking cash back off them. Nothing is going on
beyond that, so the labels say it.

Labels only: the stored values are untouched, so nothing has to be migrated
and no row changes meaning. This exists because the choices live in the field
definition and Django notices.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cash_book', '0007_permissions_follow_the_entry'),
    ]

    operations = [
        migrations.AlterField(
            model_name='advanceentry',
            name='direction',
            field=models.CharField(choices=[('GIVEN', 'Cash given'), ('RETURNED', 'Cash taken back')], max_length=10),
        ),
    ]
