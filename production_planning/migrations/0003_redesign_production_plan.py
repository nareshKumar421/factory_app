"""
Migration 0003 — Redesign ProductionPlan for local-first creation + SAP posting.

Changes vs 0001_initial:
- ProductionPlan:
  * sap_doc_entry / sap_doc_num / sap_status  → nullable (filled after SAP post)
  * Remove unique_together (company, sap_doc_entry)
  * Add DRAFT to status choices; change default to DRAFT
  * Add sap_posting_status (NOT_POSTED / POSTED / FAILED)
  * Add sap_error_message
  * Add uom, warehouse_code
  * Rename imported_by → created_by  (new FK, old FK removed)
  * Remove customer_code, customer_name
  * Remove imported_at (created_at already exists and serves the same purpose)
- PlanMaterialRequirement:
  * Remove issued_qty
  * Add warehouse_code
- Permissions on ProductionPlan:
  * Remove can_fetch_sap_orders, can_import_production_plan
  * Add can_create_production_plan, can_edit_production_plan,
    can_delete_production_plan, can_post_plan_to_sap
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('production_planning', '0002_create_production_planning_group'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # ----------------------------------------------------------------
        # ProductionPlan — make sap_doc_entry / sap_doc_num / sap_status nullable
        # ----------------------------------------------------------------
        migrations.AlterField(
            model_name='productionplan',
            name='sap_doc_entry',
            field=models.IntegerField(
                null=True, blank=True, help_text='SAP OWOR DocEntry (filled after posting)'
            ),
        ),
        migrations.AlterField(
            model_name='productionplan',
            name='sap_doc_num',
            field=models.IntegerField(
                null=True, blank=True, help_text='SAP OWOR DocNum (filled after posting)'
            ),
        ),
        migrations.AlterField(
            model_name='productionplan',
            name='sap_status',
            field=models.CharField(
                max_length=2, null=True, blank=True,
                help_text='SAP status: P=Planned, R=Released'
            ),
        ),

        # ----------------------------------------------------------------
        # ProductionPlan — remove unique_together constraint
        # ----------------------------------------------------------------
        migrations.AlterUniqueTogether(
            name='productionplan',
            unique_together=set(),
        ),

        # ----------------------------------------------------------------
        # ProductionPlan — update status choices + default (add DRAFT)
        # ----------------------------------------------------------------
        migrations.AlterField(
            model_name='productionplan',
            name='status',
            field=models.CharField(
                choices=[
                    ('DRAFT', 'Draft'),
                    ('OPEN', 'Open'),
                    ('IN_PROGRESS', 'In Progress'),
                    ('COMPLETED', 'Completed'),
                    ('CLOSED', 'Closed'),
                    ('CANCELLED', 'Cancelled'),
                ],
                default='DRAFT',
                max_length=20,
            ),
        ),

        # ----------------------------------------------------------------
        # ProductionPlan — new fields
        # ----------------------------------------------------------------
        migrations.AddField(
            model_name='productionplan',
            name='sap_posting_status',
            field=models.CharField(
                choices=[
                    ('NOT_POSTED', 'Not Posted to SAP'),
                    ('POSTED', 'Posted to SAP'),
                    ('FAILED', 'SAP Posting Failed'),
                ],
                default='NOT_POSTED',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='productionplan',
            name='sap_error_message',
            field=models.TextField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name='productionplan',
            name='uom',
            field=models.CharField(max_length=20, blank=True, default=''),
        ),
        migrations.AddField(
            model_name='productionplan',
            name='warehouse_code',
            field=models.CharField(max_length=20, blank=True, default=''),
        ),

        # ----------------------------------------------------------------
        # ProductionPlan — created_by (replaces imported_by)
        # ----------------------------------------------------------------
        migrations.AddField(
            model_name='productionplan',
            name='created_by',
            field=models.ForeignKey(
                null=True, blank=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='created_production_plans',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RemoveField(
            model_name='productionplan',
            name='imported_by',
        ),
        migrations.RemoveField(
            model_name='productionplan',
            name='imported_at',
        ),

        # ----------------------------------------------------------------
        # ProductionPlan — remove SAP-import-only fields
        # ----------------------------------------------------------------
        migrations.RemoveField(
            model_name='productionplan',
            name='customer_code',
        ),
        migrations.RemoveField(
            model_name='productionplan',
            name='customer_name',
        ),

        # ----------------------------------------------------------------
        # PlanMaterialRequirement — remove issued_qty, add warehouse_code
        # ----------------------------------------------------------------
        migrations.RemoveField(
            model_name='planmaterialrequirement',
            name='issued_qty',
        ),
        migrations.AddField(
            model_name='planmaterialrequirement',
            name='warehouse_code',
            field=models.CharField(max_length=20, blank=True, default=''),
        ),
        migrations.AlterField(
            model_name='planmaterialrequirement',
            name='uom',
            field=models.CharField(max_length=20, blank=True, default=''),
        ),

        # ----------------------------------------------------------------
        # ProductionPlan — update Meta.permissions
        # ----------------------------------------------------------------
        migrations.AlterModelOptions(
            name='productionplan',
            options={
                'verbose_name': 'Production Plan',
                'verbose_name_plural': 'Production Plans',
                'ordering': ['-due_date', 'item_code'],
                'permissions': [
                    ('can_create_production_plan', 'Can create production plan'),
                    ('can_edit_production_plan', 'Can edit production plan'),
                    ('can_delete_production_plan', 'Can delete production plan'),
                    ('can_post_plan_to_sap', 'Can post production plan to SAP'),
                    ('can_view_production_plan', 'Can view production plans'),
                    ('can_manage_weekly_plan', 'Can create and manage weekly plans'),
                    ('can_add_daily_production', 'Can add daily production entries'),
                    ('can_view_daily_production', 'Can view daily production entries'),
                    ('can_close_production_plan', 'Can close production plans'),
                ],
            },
        ),

        # ----------------------------------------------------------------
        # Refresh the production_planning group permissions to match new set
        # ----------------------------------------------------------------
        migrations.RunPython(
            code=lambda apps, schema_editor: _refresh_group_permissions(apps),
            reverse_code=migrations.RunPython.noop,
        ),
    ]


def _refresh_group_permissions(apps):
    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')

    group, _ = Group.objects.get_or_create(name='production_planning')
    permissions = Permission.objects.filter(
        content_type__app_label='production_planning'
    )
    group.permissions.set(permissions)
