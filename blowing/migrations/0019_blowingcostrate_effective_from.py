"""Effective-date the blowing Cost Master.

Before this, `BlowingCostRate` held ONE active row per (company, scope, category)
and `upsert_cost_rate` overwrote its `rate` in place. Consequences: the previous
rate was destroyed, and because `recalculate_run_cost` resolves rates live and
rebuilds the cost lines, any recompute of an old run silently repriced it at
today's rate. `BlowingRateConfig` and `BottleBuyPrice` were already
effective-dated; the Cost Master was the outlier.

The backfill is deliberately a NO-OP for existing data: every existing rate row
is dated at or before the earliest run of its company, so every existing run
still resolves to exactly the rate it resolves to today. Recomputing any run
immediately after this migration must reproduce its current cost to the paisa.
"""
import datetime

from django.db import migrations, models


def backfill_effective_from(apps, schema_editor):
    """Date each rate row early enough that no existing run changes cost.

    Earliest of: the company's first run date, and the row's own created_at date.
    A company with no runs just uses created_at. `FALLBACK` covers a row with
    neither (created_at is auto_now_add, so this is belt-and-braces).
    """
    BlowingCostRate = apps.get_model('blowing', 'BlowingCostRate')
    BlowingRun = apps.get_model('blowing', 'BlowingRun')
    FALLBACK = datetime.date(2000, 1, 1)

    first_run = {}
    for row in (BlowingRun.objects.values('company_id')
                .annotate(first=models.Min('date'))):
        first_run[row['company_id']] = row['first']

    for rate in BlowingCostRate.objects.all().iterator():
        candidates = [d for d in (first_run.get(rate.company_id),
                                  rate.created_at.date() if rate.created_at else None)
                      if d is not None]
        rate.effective_from = min(candidates) if candidates else FALLBACK
        rate.save(update_fields=['effective_from'])


def noop_reverse(apps, schema_editor):
    """Nothing to undo: the column itself is removed by the reverse AddField."""


class Migration(migrations.Migration):

    dependencies = [
        ('blowing', '0018_alter_preformspec_std_make_cost_per_bottle'),
    ]

    operations = [
        # Add nullable first so existing rows survive, then backfill, then pin.
        migrations.AddField(
            model_name='blowingcostrate',
            name='effective_from',
            field=models.DateField(
                null=True,
                help_text='Applies to runs dated on or after this date. To change '
                          'a rate, add a row with a later date rather than editing '
                          'this one.',
            ),
        ),
        migrations.RunPython(backfill_effective_from, noop_reverse),
        migrations.AlterField(
            model_name='blowingcostrate',
            name='effective_from',
            field=models.DateField(
                help_text='Applies to runs dated on or after this date. To change '
                          'a rate, add a row with a later date rather than editing '
                          'this one.',
            ),
        ),
        # One rate per category per scope per DATE replaces one-per-category.
        migrations.RemoveConstraint(
            model_name='blowingcostrate',
            name='uniq_active_global_blowing_cost_rate_per_category',
        ),
        migrations.RemoveConstraint(
            model_name='blowingcostrate',
            name='uniq_active_machine_blowing_cost_rate_per_category',
        ),
        migrations.AlterModelOptions(
            name='blowingcostrate',
            options={
                'ordering': ['company_id', 'machine_id', 'category', '-effective_from'],
                'verbose_name': 'Blowing Cost Rate',
                'verbose_name_plural': 'Blowing Cost Rates',
            },
        ),
        migrations.AddConstraint(
            model_name='blowingcostrate',
            constraint=models.UniqueConstraint(
                condition=models.Q(('is_active', True), ('machine__isnull', True)),
                fields=('company', 'category', 'effective_from'),
                name='uniq_active_global_blowing_cost_rate_per_date',
            ),
        ),
        migrations.AddConstraint(
            model_name='blowingcostrate',
            constraint=models.UniqueConstraint(
                condition=models.Q(('is_active', True), ('machine__isnull', False)),
                fields=('company', 'machine', 'category', 'effective_from'),
                name='uniq_active_machine_blowing_cost_rate_per_date',
            ),
        ),
    ]
