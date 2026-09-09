from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('production_execution', '0038_productionrun_litres_per_piece'),
    ]

    operations = [
        migrations.AddField(
            model_name='productionrun',
            name='planned_start_at',
            field=models.DateTimeField(
                blank=True, null=True,
                help_text='When the run is planned to start. Set the evening before by the production supervisor; drives the schedule view and the line/machine clash check. Null on runs entered as they start.'
            ),
        ),
        migrations.AddField(
            model_name='productionrun',
            name='planned_end_at',
            field=models.DateTimeField(
                blank=True, null=True,
                help_text='When the run is expected to finish. Derived from required_qty x pieces_per_case / rated_speed unless the supervisor overrode it (see planned_end_is_manual).'
            ),
        ),
        migrations.AddField(
            model_name='productionrun',
            name='planned_end_is_manual',
            field=models.BooleanField(
                default=False,
                help_text='True when the supervisor typed the finish time instead of taking the speed-derived one, so a later speed change does not silently move a window they chose deliberately.'
            ),
        ),
        migrations.AddField(
            model_name='productionrun',
            name='planning_remark',
            field=models.TextField(
                blank=True, default='',
                help_text='Why this plan was saved despite a material shortfall or a clash with another plan. Required at creation when the readiness check reports either, so an overridden warning always carries a reason.'
            ),
        ),
    ]
