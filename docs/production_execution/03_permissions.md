# Production Execution — Permissions & Roles

> Follows the same pattern as `production_planning/permissions.py`
> Permissions defined in model Meta, grouped in a Django auth group via migration

---

## 1. Django Permissions (defined in ProductionRun.Meta)

```python
permissions = [
    # Master Data
    ('can_manage_production_lines', 'Can manage production lines'),
    ('can_manage_machines', 'Can manage machines'),
    ('can_manage_checklist_templates', 'Can manage checklist templates'),

    # Production Runs
    ('can_view_production_run', 'Can view production runs'),
    ('can_create_production_run', 'Can create production runs'),
    ('can_edit_production_run', 'Can edit production runs'),
    ('can_complete_production_run', 'Can complete production runs'),

    # Hourly Logs
    ('can_view_production_log', 'Can view production logs'),
    ('can_edit_production_log', 'Can create/edit production logs'),

    # Breakdowns
    ('can_view_breakdown', 'Can view breakdowns'),
    ('can_create_breakdown', 'Can create breakdowns'),
    ('can_edit_breakdown', 'Can edit breakdowns'),

    # Material Usage
    ('can_view_material_usage', 'Can view material usage'),
    ('can_create_material_usage', 'Can create material usage'),
    ('can_edit_material_usage', 'Can edit material usage'),

    # Machine Runtime
    ('can_view_machine_runtime', 'Can view machine runtime'),
    ('can_create_machine_runtime', 'Can create machine runtime'),

    # Manpower
    ('can_view_manpower', 'Can view manpower'),
    ('can_create_manpower', 'Can create manpower'),

    # Line Clearance
    ('can_view_line_clearance', 'Can view line clearance'),
    ('can_create_line_clearance', 'Can create line clearance'),
    ('can_approve_line_clearance_qa', 'Can QA-approve line clearance'),

    # Machine Checklists
    ('can_view_machine_checklist', 'Can view machine checklists'),
    ('can_create_machine_checklist', 'Can create machine checklist entries'),

    # Waste Management
    ('can_view_waste_log', 'Can view waste logs'),
    ('can_create_waste_log', 'Can create waste logs'),
    ('can_approve_waste_engineer', 'Can engineer-approve waste'),
    ('can_approve_waste_am', 'Can AM-approve waste'),
    ('can_approve_waste_store', 'Can store-approve waste'),
    ('can_approve_waste_hod', 'Can HOD-approve waste'),

    # Reports
    ('can_view_reports', 'Can view production reports'),
]
```

---

## 2. DRF Permission Classes (in `permissions.py`)

Each permission maps to a `BasePermission` class, same pattern as `production_planning/permissions.py`:

```python
from rest_framework.permissions import BasePermission

class CanViewProductionRun(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_view_production_run')

class CanCreateProductionRun(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_create_production_run')

# ... one class per permission
```

---

## 3. Role-to-Permission Mapping

| Permission | Operator | Shift Incharge | Prod. Engineer | Prod. HOD | QA Officer | Store Incharge | AM |
|-----------|----------|---------------|----------------|-----------|------------|---------------|-----|
| **Master Data** |
| can_manage_production_lines | - | - | - | YES | - | - | - |
| can_manage_machines | - | - | YES | YES | - | - | - |
| can_manage_checklist_templates | - | - | YES | YES | - | - | - |
| **Production Runs** |
| can_view_production_run | YES | YES | YES | YES | YES | YES | YES |
| can_create_production_run | YES | YES | YES | YES | - | - | - |
| can_edit_production_run | YES | YES | YES | YES | - | - | - |
| can_complete_production_run | - | YES | YES | YES | - | - | - |
| **Hourly Logs** |
| can_view_production_log | YES | YES | YES | YES | - | - | - |
| can_edit_production_log | YES | YES | YES | YES | - | - | - |
| **Breakdowns** |
| can_view_breakdown | YES | YES | YES | YES | - | - | - |
| can_create_breakdown | YES | YES | YES | YES | - | - | - |
| can_edit_breakdown | YES | YES | YES | YES | - | - | - |
| **Material Usage** |
| can_view_material_usage | YES | YES | YES | YES | - | YES | - |
| can_create_material_usage | YES | YES | YES | YES | - | - | - |
| can_edit_material_usage | YES | YES | YES | YES | - | - | - |
| **Machine Runtime** |
| can_view_machine_runtime | YES | YES | YES | YES | - | - | - |
| can_create_machine_runtime | YES | YES | YES | YES | - | - | - |
| **Manpower** |
| can_view_manpower | - | YES | YES | YES | - | - | - |
| can_create_manpower | - | YES | YES | YES | - | - | - |
| **Line Clearance** |
| can_view_line_clearance | YES | YES | YES | YES | YES | - | - |
| can_create_line_clearance | YES | YES | YES | YES | - | - | - |
| can_approve_line_clearance_qa | - | - | - | - | YES | - | - |
| **Machine Checklists** |
| can_view_machine_checklist | YES | YES | YES | YES | - | - | - |
| can_create_machine_checklist | YES | YES | YES | YES | - | - | - |
| **Waste Management** |
| can_view_waste_log | YES | YES | YES | YES | - | YES | YES |
| can_create_waste_log | YES | YES | YES | YES | - | - | - |
| can_approve_waste_engineer | - | - | YES | YES | - | - | - |
| can_approve_waste_am | - | - | - | - | - | - | YES |
| can_approve_waste_store | - | - | - | - | - | YES | - |
| can_approve_waste_hod | - | - | - | YES | - | - | - |
| **Reports** |
| can_view_reports | - | YES | YES | YES | - | - | - |

---

## 4. Django Auth Group

Create via migration `0002_create_production_execution_group.py`:

```python
from django.db import migrations

def create_group(apps, schema_editor):
    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')

    group, _ = Group.objects.get_or_create(name='production_execution')

    perms = Permission.objects.filter(
        content_type__app_label='production_execution'
    )
    group.permissions.set(perms)

class Migration(migrations.Migration):
    dependencies = [
        ('production_execution', '0001_initial'),
    ]
    operations = [
        migrations.RunPython(create_group, migrations.RunPython.noop),
    ]
```

---

## 5. View Permission Pattern

Same as `production_planning/views.py`:

```python
class ProductionRunListCreateAPI(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewProductionRun()]
        return [IsAuthenticated(), HasCompanyContext(), CanCreateProductionRun()]
```

All views require `IsAuthenticated` + `HasCompanyContext` (from `company.permissions`) as base.
