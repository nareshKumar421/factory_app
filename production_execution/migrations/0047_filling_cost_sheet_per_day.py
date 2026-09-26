# The filling cost sheet is kept a day at a time instead of a month at a time:
# ``period`` (the first of the month) becomes ``date`` (the day itself), and
# the one-sheet-per-month constraints become one sheet per day. Renamed rather
# than dropped and re-added, so a sheet already entered keeps its date.

from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('production_execution', '0046_production_settings'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='fillingcostsheet',
            name='uniq_filling_cost_sheet_per_month',
        ),
        migrations.RemoveConstraint(
            model_name='fillingcostsheet',
            name='uniq_filling_cost_sheet_per_line_month',
        ),
        migrations.RenameField(
            model_name='fillingcostsheet',
            old_name='period',
            new_name='date',
        ),
        migrations.AlterField(
            model_name='fillingcostsheet',
            name='date',
            field=models.DateField(help_text='The day the sheet covers.'),
        ),
        migrations.AlterField(
            model_name='fillingcostsheet',
            name='cases',
            field=models.DecimalField(decimal_places=2, help_text="Cases the day's cost is spread over — the sheet's 'Per N Cases' heading, and the divisor behind every per-case figure on it.", max_digits=15),
        ),
        migrations.AlterField(
            model_name='fillingcostsheetentry',
            name='amount',
            field=models.DecimalField(decimal_places=2, default=Decimal('0'), help_text="The day's amount for this head.", max_digits=15),
        ),
        migrations.AlterModelOptions(
            name='fillingcostsheet',
            options={'ordering': ['-date', 'line'], 'verbose_name': 'Filling Cost Sheet', 'verbose_name_plural': 'Filling Cost Sheets'},
        ),
        migrations.AddConstraint(
            model_name='fillingcostsheet',
            constraint=models.UniqueConstraint(condition=models.Q(('line__isnull', True)), fields=('company', 'date'), name='uniq_filling_cost_sheet_per_day'),
        ),
        migrations.AddConstraint(
            model_name='fillingcostsheet',
            constraint=models.UniqueConstraint(condition=models.Q(('line__isnull', False)), fields=('company', 'line', 'date'), name='uniq_filling_cost_sheet_per_line_day'),
        ),
    ]
