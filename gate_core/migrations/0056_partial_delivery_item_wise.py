"""Move partial delivery from a per-bill box split to a per-item quantity split.

0055 shipped a per-bill ``boxes_delivered`` / ``boxes_returned`` split and was
applied before we checked the data: ``total_boxes`` is 0 on every dispatched bill
and item in production, so that split had no usable reference. Quantity (in the
item's own uom) is the field that carries data, and the shortfall is per item.

This is a forward migration rather than an edit to 0055 because 0055 is already
applied. The rename is safe: the line table is empty (the feature never went
live), so no values move.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('gate_core', '0055_truckdispatchupdate_delivered_date_and_more'),
    ]

    operations = [
        migrations.RenameField(
            model_name='truckdispatchpartialdeliveryline',
            old_name='boxes_delivered',
            new_name='qty_delivered',
        ),
        migrations.RenameField(
            model_name='truckdispatchpartialdeliveryline',
            old_name='boxes_returned',
            new_name='qty_returned',
        ),
        migrations.CreateModel(
            name='TruckDispatchPartialDeliveryItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('is_active', models.BooleanField(default=True)),
                ('qty_delivered', models.DecimalField(decimal_places=3, default=0, max_digits=18)),
                ('qty_returned', models.DecimalField(decimal_places=3, default=0, max_digits=18)),
                ('remarks', models.TextField(blank=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='%(class)s_created', to=settings.AUTH_USER_MODEL)),
                ('item', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='partial_delivery_items', to='gate_core.salesdispatchgateoutitem')),
                ('line', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='items', to='gate_core.truckdispatchpartialdeliveryline')),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='%(class)s_updated', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['id'],
            },
        ),
        migrations.AddIndex(
            model_name='truckdispatchpartialdeliveryitem',
            index=models.Index(fields=['line'], name='gate_core_t_line_id_778609_idx'),
        ),
        migrations.AddIndex(
            model_name='truckdispatchpartialdeliveryitem',
            index=models.Index(fields=['item'], name='gate_core_t_item_id_be3f01_idx'),
        ),
        migrations.AddConstraint(
            model_name='truckdispatchpartialdeliveryitem',
            constraint=models.UniqueConstraint(fields=('line', 'item'), name='unique_partial_delivery_item_per_line'),
        ),
    ]
