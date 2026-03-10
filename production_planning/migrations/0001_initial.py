import django.core.validators
import django.db.models.deletion
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('company', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ProductionPlan',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('sap_doc_entry', models.IntegerField(help_text='SAP OWOR DocEntry')),
                ('sap_doc_num', models.IntegerField(help_text='SAP OWOR DocNum (human-readable)')),
                ('item_code', models.CharField(max_length=50, help_text='Finished product ItemCode')),
                ('item_name', models.CharField(max_length=255, help_text='Finished product name')),
                ('planned_qty', models.DecimalField(
                    decimal_places=3, max_digits=12,
                    validators=[django.core.validators.MinValueValidator(Decimal('0.001'))],
                    help_text='Total planned quantity from SAP OWOR'
                )),
                ('completed_qty', models.DecimalField(
                    decimal_places=3, default=Decimal('0'), max_digits=12,
                    help_text='Total produced so far (sum of daily entries)'
                )),
                ('target_start_date', models.DateField(help_text='SAP OWOR PlannedDate')),
                ('due_date', models.DateField(help_text='SAP OWOR DueDate')),
                ('sap_status', models.CharField(max_length=2, help_text='SAP status: P=Planned, R=Released')),
                ('status', models.CharField(
                    choices=[
                        ('OPEN', 'Open'),
                        ('IN_PROGRESS', 'In Progress'),
                        ('COMPLETED', 'Completed'),
                        ('CLOSED', 'Closed'),
                        ('CANCELLED', 'Cancelled'),
                    ],
                    default='OPEN', max_length=20
                )),
                ('customer_code', models.CharField(blank=True, default='', max_length=50)),
                ('customer_name', models.CharField(blank=True, default='', max_length=255)),
                ('branch_id', models.IntegerField(blank=True, null=True, help_text='SAP BPLId')),
                ('remarks', models.TextField(blank=True, default='', help_text='SAP OWOR Comments')),
                ('imported_at', models.DateTimeField(auto_now_add=True)),
                ('closed_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('company', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='production_plans',
                    to='company.company'
                )),
                ('imported_by', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='imported_production_plans',
                    to=settings.AUTH_USER_MODEL
                )),
                ('closed_by', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='closed_production_plans',
                    to=settings.AUTH_USER_MODEL
                )),
            ],
            options={
                'verbose_name': 'Production Plan',
                'verbose_name_plural': 'Production Plans',
                'ordering': ['-due_date', 'item_code'],
                'unique_together': {('company', 'sap_doc_entry')},
                'permissions': [
                    ('can_fetch_sap_orders', 'Can fetch production orders from SAP'),
                    ('can_import_production_plan', 'Can import production plan from SAP'),
                    ('can_view_production_plan', 'Can view production plans'),
                    ('can_manage_weekly_plan', 'Can create and manage weekly plans'),
                    ('can_add_daily_production', 'Can add daily production entries'),
                    ('can_view_daily_production', 'Can view daily production entries'),
                    ('can_close_production_plan', 'Can close production plans'),
                ],
            },
        ),
        migrations.CreateModel(
            name='PlanMaterialRequirement',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('component_code', models.CharField(max_length=50, help_text='Raw material ItemCode')),
                ('component_name', models.CharField(max_length=255, help_text='Raw material name')),
                ('required_qty', models.DecimalField(
                    decimal_places=3, max_digits=12,
                    help_text='Planned qty from SAP WOR1'
                )),
                ('issued_qty', models.DecimalField(
                    decimal_places=3, default=Decimal('0'), max_digits=12,
                    help_text='Already issued in SAP at import time'
                )),
                ('uom', models.CharField(max_length=20, help_text='Unit of measure')),
                ('production_plan', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='materials',
                    to='production_planning.productionplan'
                )),
            ],
            options={
                'verbose_name': 'Plan Material Requirement',
                'verbose_name_plural': 'Plan Material Requirements',
                'ordering': ['component_code'],
            },
        ),
        migrations.CreateModel(
            name='WeeklyPlan',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('week_number', models.PositiveSmallIntegerField(help_text='Week number (1, 2, 3, 4 ...)')),
                ('week_label', models.CharField(blank=True, default='', max_length=100)),
                ('start_date', models.DateField()),
                ('end_date', models.DateField()),
                ('target_qty', models.DecimalField(
                    decimal_places=3, max_digits=12,
                    validators=[django.core.validators.MinValueValidator(Decimal('0.001'))]
                )),
                ('produced_qty', models.DecimalField(
                    decimal_places=3, default=Decimal('0'), max_digits=12,
                    help_text='Actual produced (sum of daily entries)'
                )),
                ('status', models.CharField(
                    choices=[
                        ('PENDING', 'Pending'),
                        ('IN_PROGRESS', 'In Progress'),
                        ('COMPLETED', 'Completed'),
                    ],
                    default='PENDING', max_length=20
                )),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('production_plan', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='weekly_plans',
                    to='production_planning.productionplan'
                )),
                ('created_by', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='created_weekly_plans',
                    to=settings.AUTH_USER_MODEL
                )),
            ],
            options={
                'verbose_name': 'Weekly Plan',
                'verbose_name_plural': 'Weekly Plans',
                'ordering': ['week_number'],
                'unique_together': {('production_plan', 'week_number')},
            },
        ),
        migrations.CreateModel(
            name='DailyProductionEntry',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('production_date', models.DateField()),
                ('produced_qty', models.DecimalField(
                    decimal_places=3, max_digits=12,
                    validators=[django.core.validators.MinValueValidator(Decimal('0.001'))]
                )),
                ('shift', models.CharField(
                    blank=True, null=True, max_length=20,
                    choices=[
                        ('MORNING', 'Morning'),
                        ('AFTERNOON', 'Afternoon'),
                        ('NIGHT', 'Night'),
                    ],
                    help_text='Optional shift tracking'
                )),
                ('remarks', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('weekly_plan', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='daily_entries',
                    to='production_planning.weeklyplan'
                )),
                ('recorded_by', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='daily_production_entries',
                    to=settings.AUTH_USER_MODEL
                )),
            ],
            options={
                'verbose_name': 'Daily Production Entry',
                'verbose_name_plural': 'Daily Production Entries',
                'ordering': ['-production_date'],
                'unique_together': {('weekly_plan', 'production_date', 'shift')},
            },
        ),
    ]
