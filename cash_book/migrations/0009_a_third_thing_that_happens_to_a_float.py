"""
A third thing that happens to somebody's float, because two was a lie.

The workbook's advance list carries people who paid for something out of their
own pocket. There is no cash movement behind those rows at all -- nothing left
the box and nothing came back -- but the only two directions available were
"cash given" and "cash taken back", so the import wrote them as the latter.
The ledger then told a real person he had handed 15,232.00 back to the company,
which he never did.

``SPENT_OWN`` says what actually happened. It only ever arrives with the sheet:
when this happens from now on it is a payment on the cash book naming them,
which the ledger already shows as "Spent".

Labels and choices only -- no stored value changes. The rows written under the
old direction are corrected by re-importing, not by this migration, because the
sheet is the thing that knows which of them were really cash coming back.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('cash_book', '0008_plainer_words_for_cash_moving'),
    ]

    operations = [
        migrations.AlterField(
            model_name='advanceentry',
            name='direction',
            field=models.CharField(choices=[('GIVEN', 'Cash given'), ('RETURNED', 'Cash taken back'), ('SPENT_OWN', 'Paid it themselves')], max_length=10),
        ),
    ]
