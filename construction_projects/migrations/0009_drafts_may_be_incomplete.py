"""A draft is a form somebody started, not a project that exists yet.

Five columns that were NOT NULL become nullable so a half-filled draft can be
parked and picked up tomorrow. Nothing is lost by widening them -- every row
that exists already satisfies the old constraint -- and the requirement moves
to ``services.submit_project``, which refuses an incomplete project by name.
Reversing this migration would fail on any draft that has since been saved
half-filled, which is correct: the rows would no longer fit the old columns.
"""

import django.core.validators
import django.db.models.deletion
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('construction_projects', '0008_reword_batch_status_labels'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name='project',
            name='estimated_cost',
            field=models.DecimalField(blank=True, decimal_places=2, help_text='The budget being asked for', max_digits=14, null=True, validators=[django.core.validators.MinValueValidator(Decimal('0.01'))]),
        ),
        migrations.AlterField(
            model_name='project',
            name='expected_end_date',
            field=models.DateField(blank=True, help_text='When this is expected to finish', null=True),
        ),
        migrations.AlterField(
            model_name='project',
            name='manager',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='construction_projects_managed', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AlterField(
            model_name='project',
            name='name',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
        migrations.AlterField(
            model_name='project',
            name='start_date',
            field=models.DateField(blank=True, null=True),
        ),
    ]
