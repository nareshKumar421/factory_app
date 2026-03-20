# Factory App — Permissions & Groups Reference

> Complete inventory of all custom permissions, Django groups, and how to assign them to users.

---

## Table of Contents

1. [Overview](#1-overview)
2. [All Permissions by App](#2-all-permissions-by-app)
3. [Recommended Groups](#3-recommended-groups)
4. [Group-to-Permission Matrix](#4-group-to-permission-matrix)
5. [How to Create Groups & Assign Users](#5-how-to-create-groups--assign-users)

---

## 1. Overview

Every API endpoint requires:
- **`IsAuthenticated`** — valid JWT token in `Authorization: Bearer <token>`
- **`HasCompanyContext`** — valid `Company-Code` header (for company-scoped endpoints)
- **App-specific permission** — checked via DRF `BasePermission` classes

Permissions are defined in model `Meta.permissions` and enforced by permission classes in each app's `permissions.py`.

---

## 2. All Permissions by App

### 2.1 Production Execution (31 permissions)

Model: `ProductionRun` — App label: `production_execution`

| # | Codename | Description | Category |
|---|----------|-------------|----------|
| 1 | `can_manage_production_lines` | Can manage production lines | Master Data |
| 2 | `can_manage_machines` | Can manage machines | Master Data |
| 3 | `can_manage_checklist_templates` | Can manage checklist templates | Master Data |
| 4 | `can_view_production_run` | Can view production runs | Production Runs |
| 5 | `can_create_production_run` | Can create production runs | Production Runs |
| 6 | `can_edit_production_run` | Can edit production runs | Production Runs |
| 7 | `can_complete_production_run` | Can complete production runs | Production Runs |
| 8 | `can_view_production_log` | Can view production logs | Hourly Logs |
| 9 | `can_edit_production_log` | Can create/edit production logs | Hourly Logs |
| 10 | `can_view_breakdown` | Can view breakdowns | Breakdowns |
| 11 | `can_create_breakdown` | Can create breakdowns | Breakdowns |
| 12 | `can_edit_breakdown` | Can edit breakdowns | Breakdowns |
| 13 | `can_view_material_usage` | Can view material usage | Material Usage |
| 14 | `can_create_material_usage` | Can create material usage | Material Usage |
| 15 | `can_edit_material_usage` | Can edit material usage | Material Usage |
| 16 | `can_view_machine_runtime` | Can view machine runtime | Machine Runtime |
| 17 | `can_create_machine_runtime` | Can create machine runtime | Machine Runtime |
| 18 | `can_view_manpower` | Can view manpower | Manpower |
| 19 | `can_create_manpower` | Can create manpower | Manpower |
| 20 | `can_view_line_clearance` | Can view line clearance | Line Clearance |
| 21 | `can_create_line_clearance` | Can create line clearance | Line Clearance |
| 22 | `can_approve_line_clearance_qa` | Can QA-approve line clearance | Line Clearance |
| 23 | `can_view_machine_checklist` | Can view machine checklists | Machine Checklists |
| 24 | `can_create_machine_checklist` | Can create machine checklist entries | Machine Checklists |
| 25 | `can_view_waste_log` | Can view waste logs | Waste Management |
| 26 | `can_create_waste_log` | Can create waste logs | Waste Management |
| 27 | `can_approve_waste_engineer` | Can engineer-approve waste | Waste Approval |
| 28 | `can_approve_waste_am` | Can AM-approve waste | Waste Approval |
| 29 | `can_approve_waste_store` | Can store-approve waste | Waste Approval |
| 30 | `can_approve_waste_hod` | Can HOD-approve waste | Waste Approval |
| 31 | `can_view_reports` | Can view production reports | Reports |

### 2.2 Quality Control (9 custom + Django defaults)

App label: `quality_control`

**Custom permissions:**

| # | Codename | Description | Model |
|---|----------|-------------|-------|
| 1 | `can_manage_material_types` | Can manage material types | `MaterialType` |
| 2 | `can_manage_qc_parameters` | Can manage QC parameters | `MaterialType` |
| 3 | `can_submit_arrival_slip` | Can submit arrival slip to QA | `MaterialArrivalSlip` |
| 4 | `can_send_back_arrival_slip` | Can send arrival slip back for correction | `MaterialArrivalSlip` |
| 5 | `can_submit_inspection` | Can submit inspection for approval | `RawMaterialInspection` |
| 6 | `can_approve_as_chemist` | Can approve inspection as QA Chemist | `RawMaterialInspection` |
| 7 | `can_approve_as_qam` | Can approve inspection as QA Manager | `RawMaterialInspection` |
| 8 | `can_reject_inspection` | Can reject inspection | `RawMaterialInspection` |
| 9 | `can_view_production_qc` | Can view production QC | `ProductionQCSession` |
| 10 | `can_create_production_qc` | Can create production QC session | `ProductionQCSession` |
| 11 | `can_submit_production_qc` | Can submit production QC session | `ProductionQCSession` |

**Django default permissions also used in views:**

| Codename | Description |
|----------|-------------|
| `add_materialarrivalslip` | Can add material arrival slip |
| `view_materialarrivalslip` | Can view material arrival slip |
| `change_materialarrivalslip` | Can change material arrival slip |
| `add_rawmaterialinspection` | Can add raw material inspection |
| `view_rawmaterialinspection` | Can view raw material inspection |
| `change_rawmaterialinspection` | Can change raw material inspection |

### 2.3 Person Gate-In (6 permissions)

App label: `person_gatein`

| # | Codename | Description | Model |
|---|----------|-------------|-------|
| 1 | `can_manage_person_type` | Can manage person type | `PersonType` |
| 2 | `can_manage_gate` | Can manage gate | `Gate` |
| 3 | `can_cancel_entry` | Can cancel person gate entry | `EntryLog` |
| 4 | `can_exit_entry` | Can mark person gate exit | `EntryLog` |
| 5 | `can_search_entry` | Can search person gate entries | `EntryLog` |
| 6 | `can_view_dashboard` | Can view person gate dashboard | `EntryLog` |

Also uses Django defaults: `add_entrylog`, `view_entrylog`, `change_entrylog`, `delete_entrylog` and defaults for `Contractor`, `Visitor`, `Labour`.

### 2.4 Gate Core (4 permissions)

App label: `gate_core` — created via migration (no concrete model)

| # | Codename | Description |
|---|----------|-------------|
| 1 | `can_view_raw_material_full_entry` | Can view full raw material gate entry |
| 2 | `can_view_daily_need_full_entry` | Can view full daily need gate entry |
| 3 | `can_view_maintenance_full_entry` | Can view full maintenance gate entry |
| 4 | `can_view_construction_full_entry` | Can view full construction gate entry |

### 2.5 Raw Material Gate-In (2 permissions)

App label: `raw_material_gatein`

| # | Codename | Description | Model |
|---|----------|-------------|-------|
| 1 | `can_complete_raw_material_entry` | Can complete raw material gate entry | `POReceipt` |
| 2 | `can_receive_po` | Can receive PO items | `POReceipt` |

### 2.6 Maintenance Gate-In (2 permissions)

App label: `maintenance_gatein`

| # | Codename | Description | Model |
|---|----------|-------------|-------|
| 1 | `can_manage_maintenance_type` | Can manage maintenance type | `MaintenanceType` |
| 2 | `can_complete_maintenance_entry` | Can complete maintenance gate entry | `MaintenanceGateEntry` |

### 2.7 Daily Needs Gate-In (2 permissions)

App label: `daily_needs_gatein`

| # | Codename | Description | Model |
|---|----------|-------------|-------|
| 1 | `can_manage_category` | Can manage category | `CategoryList` |
| 2 | `can_complete_daily_need_entry` | Can complete daily need gate entry | `DailyNeedGateEntry` |

### 2.8 Construction Gate-In (2 permissions)

App label: `construction_gatein`

| # | Codename | Description | Model |
|---|----------|-------------|-------|
| 1 | `can_manage_material_category` | Can manage material category | `ConstructionMaterialCategory` |
| 2 | `can_complete_construction_entry` | Can complete construction gate entry | `ConstructionGateEntry` |

### 2.9 GRPO (3 permissions)

App label: `grpo`

| # | Codename | Description | Model |
|---|----------|-------------|-------|
| 1 | `can_view_pending_grpo` | Can view pending GRPO entries | `GRPOPosting` |
| 2 | `can_preview_grpo` | Can preview GRPO data | `GRPOPosting` |
| 3 | `can_view_grpo_history` | Can view GRPO posting history | `GRPOPosting` |

Also uses Django defaults: `view_grpoposting`, `add_grpoposting`, `add_grpoattachment`.

### 2.10 SAP Plan Dashboard (2 permissions)

App label: `sap_plan_dashboard`

| # | Codename | Description | Model |
|---|----------|-------------|-------|
| 1 | `can_view_plan_dashboard` | Can view SAP Plan Dashboard | `PlanDashboardPermission` |
| 2 | `can_export_plan_dashboard` | Can export SAP Plan Dashboard data | `PlanDashboardPermission` |

### 2.11 Notifications (2 permissions)

App label: `notifications`

| # | Codename | Description | Model |
|---|----------|-------------|-------|
| 1 | `can_send_notification` | Can send manual notifications | `Notification` |
| 2 | `can_send_bulk_notification` | Can send bulk/broadcast notifications | `Notification` |

---

## 3. Recommended Groups

Groups bundle permissions by **job role**. A user can belong to multiple groups.

### 3.1 Production Groups

| Group Name | Description | Who gets it |
|------------|-------------|-------------|
| `Production Operator` | Floor-level data entry for runs, logs, breakdowns, material, checklists, waste | Machine operators, line workers |
| `Shift Incharge` | Everything an Operator can do + complete runs, manpower, reports | Shift supervisors |
| `Production Engineer` | Shift Incharge + manage machines, templates, approve waste (engineer) | Production engineers |
| `Production HOD` | Full production access including line management and HOD waste approval | Head of production department |
| `QA Officer` | View runs, line clearance QA approval, production QC | Quality assurance staff on the production floor |
| `Store Incharge` | View runs, view material usage, view waste, approve waste (store) | Store/inventory personnel |
| `Area Manager (AM)` | View runs, view waste, AM-level waste approval | Area/plant managers |

### 3.2 Quality Control Groups

| Group Name | Description | Who gets it |
|------------|-------------|-------------|
| `QC Store` | Create & submit arrival slips, view inspections | Store/security at receiving dock |
| `QC Chemist` | Perform inspections, submit, chemist-level approval, production QC | Lab chemists |
| `QC Manager` | Full QC access — all permissions including QAM approval, master data | QA/QC department head |

### 3.3 Gate & Security Groups

| Group Name | Description | Who gets it |
|------------|-------------|-------------|
| `Gate Security` | Create/manage all gate entries (person, raw material, daily needs, maintenance, construction), view dashboards | Security guards at factory gates |
| `Gate Supervisor` | Everything Gate Security can do + manage master data (person types, gates, categories, maintenance types, material categories) | Head of security / gate admin |
| `Gate Core Viewer` | View full entry details across all gate types | Management, auditors who need read access |

### 3.4 SAP & GRPO Groups

| Group Name | Description | Who gets it |
|------------|-------------|-------------|
| `GRPO Operator` | View pending GRPO, preview, create postings | Store personnel posting GRPOs |
| `SAP Plan Viewer` | View and export SAP Plan Dashboard | Production planning, management |

### 3.5 System Groups

| Group Name | Description | Who gets it |
|------------|-------------|-------------|
| `Notification Sender` | Send manual and bulk notifications | Admins, HR, department heads |

---

## 4. Group-to-Permission Matrix

### 4.1 Production Execution Permissions

| Permission | Operator | Shift Incharge | Engineer | HOD | QA Officer | Store Incharge | AM |
|------------|:--------:|:--------------:|:--------:|:---:|:----------:|:--------------:|:--:|
| `can_view_production_run` | x | x | x | x | x | x | x |
| `can_create_production_run` | x | x | x | x | | | |
| `can_edit_production_run` | x | x | x | x | | | |
| `can_complete_production_run` | | x | x | x | | | |
| `can_manage_production_lines` | | | | x | | | |
| `can_manage_machines` | | | x | x | | | |
| `can_manage_checklist_templates` | | | x | x | | | |
| `can_view_production_log` | x | x | x | x | | | |
| `can_edit_production_log` | x | x | x | x | | | |
| `can_view_breakdown` | x | x | x | x | | | |
| `can_create_breakdown` | x | x | x | x | | | |
| `can_edit_breakdown` | x | x | x | x | | | |
| `can_view_material_usage` | x | x | x | x | | x | |
| `can_create_material_usage` | x | x | x | x | | | |
| `can_edit_material_usage` | x | x | x | x | | | |
| `can_view_machine_runtime` | x | x | x | x | | | |
| `can_create_machine_runtime` | x | x | x | x | | | |
| `can_view_manpower` | | x | x | x | | | |
| `can_create_manpower` | | x | x | x | | | |
| `can_view_line_clearance` | x | x | x | x | x | | |
| `can_create_line_clearance` | x | x | x | x | | | |
| `can_approve_line_clearance_qa` | | | | | x | | |
| `can_view_machine_checklist` | x | x | x | x | | | |
| `can_create_machine_checklist` | x | x | x | x | | | |
| `can_view_waste_log` | x | x | x | x | | x | x |
| `can_create_waste_log` | x | x | x | x | | | |
| `can_approve_waste_engineer` | | | x | x | | | |
| `can_approve_waste_am` | | | | | | | x |
| `can_approve_waste_store` | | | | | | x | |
| `can_approve_waste_hod` | | | | x | | | |
| `can_view_reports` | | x | x | x | | | |

### 4.2 Quality Control Permissions

| Permission | QC Store | QC Chemist | QC Manager |
|------------|:--------:|:----------:|:----------:|
| `add_materialarrivalslip` | x | | x |
| `view_materialarrivalslip` | x | x | x |
| `change_materialarrivalslip` | x | | x |
| `can_submit_arrival_slip` | x | | x |
| `can_send_back_arrival_slip` | | | x |
| `add_rawmaterialinspection` | | x | x |
| `view_rawmaterialinspection` | x | x | x |
| `change_rawmaterialinspection` | | x | x |
| `can_submit_inspection` | | x | x |
| `can_approve_as_chemist` | | x | x |
| `can_approve_as_qam` | | | x |
| `can_reject_inspection` | | | x |
| `can_manage_material_types` | | | x |
| `can_manage_qc_parameters` | | | x |
| `can_view_production_qc` | | x | x |
| `can_create_production_qc` | | x | x |
| `can_submit_production_qc` | | x | x |

### 4.3 Gate & Security Permissions

| Permission | Gate Security | Gate Supervisor | Gate Core Viewer |
|------------|:------------:|:---------------:|:----------------:|
| `person_gatein.add_entrylog` | x | x | |
| `person_gatein.view_entrylog` | x | x | |
| `person_gatein.change_entrylog` | x | x | |
| `person_gatein.can_cancel_entry` | x | x | |
| `person_gatein.can_exit_entry` | x | x | |
| `person_gatein.can_search_entry` | x | x | |
| `person_gatein.can_view_dashboard` | x | x | |
| `person_gatein.can_manage_person_type` | | x | |
| `person_gatein.can_manage_gate` | | x | |
| `raw_material_gatein.can_complete_raw_material_entry` | x | x | |
| `raw_material_gatein.can_receive_po` | x | x | |
| `daily_needs_gatein.can_complete_daily_need_entry` | x | x | |
| `daily_needs_gatein.can_manage_category` | | x | |
| `maintenance_gatein.can_complete_maintenance_entry` | x | x | |
| `maintenance_gatein.can_manage_maintenance_type` | | x | |
| `construction_gatein.can_complete_construction_entry` | x | x | |
| `construction_gatein.can_manage_material_category` | | x | |
| `gate_core.can_view_raw_material_full_entry` | x | x | x |
| `gate_core.can_view_daily_need_full_entry` | x | x | x |
| `gate_core.can_view_maintenance_full_entry` | x | x | x |
| `gate_core.can_view_construction_full_entry` | x | x | x |

### 4.4 SAP, GRPO & Notifications

| Permission | GRPO Operator | SAP Plan Viewer | Notification Sender |
|------------|:------------:|:---------------:|:-------------------:|
| `grpo.can_view_pending_grpo` | x | | |
| `grpo.can_preview_grpo` | x | | |
| `grpo.can_view_grpo_history` | x | | |
| `grpo.view_grpoposting` | x | | |
| `grpo.add_grpoposting` | x | | |
| `grpo.add_grpoattachment` | x | | |
| `sap_plan_dashboard.can_view_plan_dashboard` | | x | |
| `sap_plan_dashboard.can_export_plan_dashboard` | | x | |
| `notifications.can_send_notification` | | | x |
| `notifications.can_send_bulk_notification` | | | x |

---

## 5. How to Create Groups & Assign Users

### 5.1 Via Management Command (recommended for initial setup)

Create a management command at `core/management/commands/setup_groups.py`:

```python
from django.core.management.base import BaseCommand
from django.contrib.auth.models import Group, Permission

GROUPS = {
    "Production Operator": [
        "production_execution.can_view_production_run",
        "production_execution.can_create_production_run",
        "production_execution.can_edit_production_run",
        "production_execution.can_view_production_log",
        "production_execution.can_edit_production_log",
        "production_execution.can_view_breakdown",
        "production_execution.can_create_breakdown",
        "production_execution.can_edit_breakdown",
        "production_execution.can_view_material_usage",
        "production_execution.can_create_material_usage",
        "production_execution.can_edit_material_usage",
        "production_execution.can_view_machine_runtime",
        "production_execution.can_create_machine_runtime",
        "production_execution.can_view_line_clearance",
        "production_execution.can_create_line_clearance",
        "production_execution.can_view_machine_checklist",
        "production_execution.can_create_machine_checklist",
        "production_execution.can_view_waste_log",
        "production_execution.can_create_waste_log",
    ],
    # ... define other groups similarly
}

class Command(BaseCommand):
    help = "Create default permission groups"

    def handle(self, *args, **options):
        for group_name, perms in GROUPS.items():
            group, created = Group.objects.get_or_create(name=group_name)
            for perm_str in perms:
                app_label, codename = perm_str.split(".")
                try:
                    perm = Permission.objects.get(
                        content_type__app_label=app_label,
                        codename=codename,
                    )
                    group.permissions.add(perm)
                except Permission.DoesNotExist:
                    self.stderr.write(f"  Permission not found: {perm_str}")
            action = "Created" if created else "Updated"
            self.stdout.write(f"  {action} group: {group_name}")
```

Run: `python manage.py setup_groups`

### 5.2 Via Django Admin

1. Go to **Admin > Auth > Groups**
2. Click **Add Group**
3. Enter group name (e.g., `Production Operator`)
4. Select permissions from the list (filter by app label)
5. Save

To assign a user:
1. Go to **Admin > Auth > Users > [select user]**
2. Scroll to **Groups** section
3. Add the user to the appropriate group(s)
4. Save

### 5.3 Via Django Shell

```python
from django.contrib.auth.models import Group
from accounts.models import User

# Assign user to group
user = User.objects.get(email="operator@example.com")
group = Group.objects.get(name="Production Operator")
user.groups.add(group)

# Assign multiple groups
user.groups.add(
    Group.objects.get(name="Production Operator"),
    Group.objects.get(name="Gate Security"),
)

# Check user permissions
user.has_perm("production_execution.can_view_production_run")  # True
```

### 5.4 Via API (if you build a user management endpoint)

```json
PATCH /api/accounts/users/{id}/
{
    "groups": ["Production Operator", "Gate Security"]
}
```

---

## 6. Multi-Role Users

A single user can belong to multiple groups. Common combinations:

| Person | Groups |
|--------|--------|
| Factory floor operator | `Production Operator` |
| Shift supervisor | `Shift Incharge` + `Notification Sender` |
| Production engineer | `Production Engineer` + `SAP Plan Viewer` |
| Production HOD | `Production HOD` + `SAP Plan Viewer` + `Notification Sender` |
| QA officer on floor | `QA Officer` + `QC Chemist` |
| QC department head | `QC Manager` + `Notification Sender` |
| Gate guard | `Gate Security` |
| Store person | `Store Incharge` + `QC Store` + `GRPO Operator` |
| Plant manager | `Area Manager (AM)` + `SAP Plan Viewer` + `Notification Sender` |
| Admin | All groups (or `is_staff=True` for Django admin) |

---

## 7. Groups Already Created by Migrations

These groups are auto-created by existing data migrations:

| Group | Created By | Permissions |
|-------|-----------|-------------|
| `production_execution` | `production_execution/0002_*` | All production_execution permissions |
| `gate_core` | `gate_core/0001_*` | 4 gate_core view permissions |
| `Notification Sender` | `notifications/0002_*` | `can_send_notification`, `can_send_bulk_notification` |
| `qc_store` | `quality_control` migrations | QC store permissions |
| `qc_chemist` | `quality_control` migrations | QC chemist permissions |
| `qc_manager` | `quality_control` migrations | All QC permissions |

The recommended groups in Section 3 are more granular role-based groups. You can use either approach — the migration-created groups give broad app-level access, while the role-based groups give fine-grained control per job function.
