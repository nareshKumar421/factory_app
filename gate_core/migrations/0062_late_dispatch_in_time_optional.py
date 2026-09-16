"""``in_time`` is no longer something the requester can state.

The request moved from the gate to dispatch, who raise it before the truck
arrives -- so there is no arrival hour to record when the row is written. The
column is now filled by the gate-in that spends the approval, holding the time
the truck actually came in.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('gate_core', '0061_late_dispatch_approver_group'),
    ]

    operations = [
        migrations.AlterField(
            model_name='latedispatchgateinapproval',
            name='in_time',
            field=models.TimeField(blank=True, null=True),
        ),
    ]
