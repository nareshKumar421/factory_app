# Written by hand (FA_GRPO_SEPARATE_MODE.md section 1: no makemigrations run
# against any database). Mirrors fixed_asset_grpo/models.py field for field, so
# `makemigrations --check` should find nothing to add.
#
# New tables only: FixedAssetGRPOClosure and FixedAssetGRPORecord. The two
# proxies over raw_material_gatein's POReceipt/POItemReceipt create no table.

import django.core.serializers.json
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('driver_management', '0015_alter_vehicleentry_entry_type'),
        ('grpo', '0020_po_print_settings'),
        ('raw_material_gatein', '0015_alter_poreceipt_options'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='FAReceipt',
            fields=[
            ],
            options={
                'verbose_name': 'Fixed asset PO receipt',
                'verbose_name_plural': 'Fixed asset PO receipts',
                'proxy': True,
                'indexes': [],
                'constraints': [],
            },
            bases=('raw_material_gatein.poreceipt',),
        ),
        migrations.CreateModel(
            name='FAItemReceipt',
            fields=[
            ],
            options={
                'verbose_name': 'Fixed asset PO receipt line',
                'verbose_name_plural': 'Fixed asset PO receipt lines',
                'proxy': True,
                'indexes': [],
                'constraints': [],
            },
            bases=('raw_material_gatein.poitemreceipt',),
        ),
        migrations.CreateModel(
            name='FixedAssetGRPOClosure',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('sap_doc_num', models.CharField(blank=True, default='', help_text='The SAP GRPO (or other document) number it was keyed as, if known.', max_length=30)),
                ('note', models.TextField(blank=True, default='')),
                ('closed_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('closed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='fixed_asset_grpo_closures', to=settings.AUTH_USER_MODEL)),
                ('vehicle_entry', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='fa_grpo_closure', to='driver_management.vehicleentry')),
            ],
            options={
                'verbose_name': 'Fixed asset GRPO closed by hand',
                'verbose_name_plural': 'Fixed asset GRPOs closed by hand',
                'default_permissions': (),
            },
        ),
        migrations.CreateModel(
            name='FixedAssetGRPORecord',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('simulated', models.BooleanField(default=False, help_text='Marked posted without calling SAP (FIXED_ASSET_GRPO_SIMULATE). No SAP document exists.')),
                ('sap_payload', models.JSONField(blank=True, encoder=django.core.serializers.json.DjangoJSONEncoder, help_text='For a simulated post: the GRPO document that would have been sent.', null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('grpo_posting', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='fa_record', to='grpo.grpoposting')),
                ('posted_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='fixed_asset_grpo_records', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Fixed asset GRPO record',
                'verbose_name_plural': 'Fixed asset GRPO records',
                'default_permissions': (),
                'permissions': [('can_view_fixed_asset_grpo', 'Can view fixed asset GRPOs'), ('can_post_fixed_asset_grpo', 'Can post fixed asset GRPOs to SAP'), ('can_link_fixed_asset_po', 'Can link an SAP PO to a fixed asset gate entry')],
            },
        ),
    ]
