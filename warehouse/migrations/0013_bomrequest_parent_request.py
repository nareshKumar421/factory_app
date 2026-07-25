# Adds the self-referential parent link for BOM shortfall re-requests.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('warehouse', '0012_bomrequest_blowing_run_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='bomrequest',
            name='parent_request',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='follow_up_requests',
                to='warehouse.bomrequest',
                help_text='Original request this one re-requests the shortfall of.',
            ),
        ),
    ]
