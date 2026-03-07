# Production Execution — Implementation Phases

> Each phase is self-contained and can be deployed independently.
> All phases follow the same file structure: models → migration → serializers → services → views → urls → admin.

---

## Phase 1: Master Data (Lines, Machines, Checklist Templates)

**Priority:** High (required before anything else)

### Tasks:
1. Create Django app `production_execution` (`python manage.py startapp production_execution`)
2. Add to `INSTALLED_APPS` in `config/settings.py`
3. Create models: `ProductionLine`, `Machine`, `MachineChecklistTemplate` + all choice enums
4. Create migration `0001_initial.py`
5. Create serializers for CRUD on all 3 models
6. Create service methods: `create_line`, `update_line`, `list_lines`, `create_machine`, etc.
7. Create views: `LineListCreateAPI`, `LineDetailAPI`, `MachineListCreateAPI`, `MachineDetailAPI`, `ChecklistTemplateListCreateAPI`, `ChecklistTemplateDetailAPI`
8. Create URL patterns
9. Register in `config/urls.py`
10. Create permissions: `can_manage_production_lines`, `can_manage_machines`, `can_manage_checklist_templates`
11. Create migration `0002_create_production_execution_group.py`
12. Register models in `admin.py`

### Endpoints delivered:
- `GET/POST /lines/`
- `PATCH/DELETE /lines/<id>/`
- `GET/POST /machines/`
- `PATCH/DELETE /machines/<id>/`
- `GET/POST /checklist-templates/`
- `PATCH/DELETE /checklist-templates/<id>/`

---

## Phase 2: Production Runs + Hourly Logs + Breakdowns

**Priority:** High (core production tracking)
**Depends on:** Phase 1

### Tasks:
1. Add models: `ProductionRun`, `ProductionLog`, `MachineBreakdown`
2. Create/update migration
3. Serializers:
   - `ProductionRunCreateSerializer` (input)
   - `ProductionRunListSerializer`, `ProductionRunDetailSerializer` (output)
   - `ProductionLogSerializer` (input/output)
   - `MachineBreakdownSerializer` (input/output)
4. Service methods:
   - `create_run(plan_id, line_id, date, ...)` — auto-increment run_number
   - `update_run(run_id, data)` — DRAFT/IN_PROGRESS only
   - `complete_run(run_id)` — recompute totals, lock
   - `save_hourly_logs(run_id, logs_data)` — bulk upsert
   - `update_log(run_id, log_id, data)`
   - `add_breakdown(run_id, data)` — validate machine.line == run.line
   - `update_breakdown(run_id, breakdown_id, data)`
   - `delete_breakdown(run_id, breakdown_id)`
   - `recompute_run_totals(run_id)` — sum logs + breakdowns
5. Views:
   - `RunListCreateAPI`, `RunDetailAPI`, `CompleteRunAPI`
   - `RunLogListCreateAPI`, `RunLogDetailAPI`
   - `BreakdownListCreateAPI`, `BreakdownDetailAPI`
6. Permissions: `can_view_production_run`, `can_create_production_run`, `can_edit_production_run`, `can_complete_production_run`, `can_view_production_log`, `can_edit_production_log`, `can_view_breakdown`, `can_create_breakdown`, `can_edit_breakdown`
7. URL patterns
8. Admin registration

### Endpoints delivered:
- `GET/POST /runs/`
- `GET/PATCH /runs/<id>/`
- `POST /runs/<id>/complete/`
- `GET/POST /runs/<id>/logs/`
- `PATCH /runs/<id>/logs/<id>/`
- `GET/POST /runs/<id>/breakdowns/`
- `PATCH/DELETE /runs/<id>/breakdowns/<id>/`

---

## Phase 3: Material Usage + Machine Runtime + Manpower

**Priority:** High (yield tracking)
**Depends on:** Phase 2

### Tasks:
1. Add models: `ProductionMaterialUsage`, `MachineRuntime`, `ProductionManpower`
2. Migration
3. Serializers for all 3 models
4. Service methods:
   - `save_material_usage(run_id, materials_data)` — bulk create/update, auto-compute wastage
   - `update_material(run_id, material_id, data)`
   - `save_machine_runtime(run_id, runtime_data)` — bulk create/update
   - `save_manpower(run_id, data)`
5. Views:
   - `MaterialUsageListCreateAPI`, `MaterialUsageDetailAPI`
   - `MachineRuntimeListCreateAPI`, `MachineRuntimeDetailAPI`
   - `ManpowerListCreateAPI`, `ManpowerDetailAPI`
6. Permissions: material usage, machine runtime, manpower (view + create + edit)
7. URL patterns

### Endpoints delivered:
- `GET/POST /runs/<id>/materials/`
- `PATCH /runs/<id>/materials/<id>/`
- `GET/POST /runs/<id>/machine-runtime/`
- `PATCH /runs/<id>/machine-runtime/<id>/`
- `GET/POST /runs/<id>/manpower/`
- `PATCH /runs/<id>/manpower/<id>/`

---

## Phase 4: Line Clearance

**Priority:** Medium (pre-production quality check)
**Depends on:** Phase 1

### Tasks:
1. Add models: `LineClearance`, `LineClearanceItem`
2. Migration
3. Define standard 9 checklist items as a constant list
4. Serializers:
   - `LineClearanceCreateSerializer` — auto-creates 9 items
   - `LineClearanceDetailSerializer` — includes items
   - `LineClearanceItemUpdateSerializer`
5. Service methods:
   - `create_clearance(data)` — create header + 9 items
   - `update_clearance(clearance_id, data)` — update items + signatures
   - `submit_clearance(clearance_id)` — DRAFT → SUBMITTED
   - `approve_clearance(clearance_id, user, approved)` — QA approve/reject
   - `check_clearance_exists(plan_id, line_id)` — helper for run creation
6. Views:
   - `LineClearanceListCreateAPI`, `LineClearanceDetailAPI`
   - `SubmitClearanceAPI`, `ApproveClearanceAPI`
7. Permissions: `can_view_line_clearance`, `can_create_line_clearance`, `can_approve_line_clearance_qa`

### Endpoints delivered:
- `GET/POST /line-clearance/`
- `GET/PATCH /line-clearance/<id>/`
- `POST /line-clearance/<id>/submit/`
- `POST /line-clearance/<id>/approve/`

---

## Phase 5: Machine Checklists

**Priority:** Medium (maintenance tracking)
**Depends on:** Phase 1

### Tasks:
1. Add model: `MachineChecklistEntry`
2. Migration
3. Serializers for single entry + bulk
4. Service methods:
   - `get_checklist_calendar(machine_id, month, year)` — returns grid data
   - `create_entry(data)`
   - `bulk_upsert_entries(entries_data)` — for calendar save
5. Views:
   - `MachineChecklistListCreateAPI`
   - `MachineChecklistBulkAPI`
   - `MachineChecklistDetailAPI`
6. Permissions: `can_view_machine_checklist`, `can_create_machine_checklist`

### Endpoints delivered:
- `GET/POST /machine-checklists/`
- `POST /machine-checklists/bulk/`
- `PATCH /machine-checklists/<id>/`

---

## Phase 6: Waste Management

**Priority:** Medium (waste tracking + approval workflow)
**Depends on:** Phase 2

### Tasks:
1. Add model: `WasteLog`
2. Migration
3. Serializers:
   - `WasteLogCreateSerializer`
   - `WasteLogDetailSerializer` (includes approval status per level)
   - `WasteApprovalSerializer` (sign + remarks)
4. Service methods:
   - `create_waste_log(run_id, data)`
   - `approve_waste(waste_id, level, user, sign, remarks)` — handles sequential validation
   - `get_pending_approvals(user)` — for dashboard
5. Views:
   - `WasteLogListCreateAPI`, `WasteLogDetailAPI`
   - `WasteApproveEngineerAPI`, `WasteApproveAMAPI`, `WasteApproveStoreAPI`, `WasteApproveHODAPI`
6. Permissions: `can_view_waste_log`, `can_create_waste_log`, 4 approval permissions

### Endpoints delivered:
- `GET/POST /waste/`
- `GET /waste/<id>/`
- `POST /waste/<id>/approve/engineer/`
- `POST /waste/<id>/approve/am/`
- `POST /waste/<id>/approve/store/`
- `POST /waste/<id>/approve/hod/`

---

## Phase 7: Reports & Analytics

**Priority:** Low (read-only, can be added anytime)
**Depends on:** Phases 2 + 3

### Tasks:
1. Service methods (no new models):
   - `get_daily_production_report(date, line_id)` — compile runs + logs + breakdowns
   - `get_yield_report(run_id)` — material usage + machine runtime
   - `get_line_clearance_report(date_from, date_to)`
   - `get_analytics(date_from, date_to, line_id)` — OEE, efficiency, material loss, downtime
2. Serializers for report responses
3. Views:
   - `DailyProductionReportAPI`
   - `YieldReportAPI`
   - `LineClearanceReportAPI`
   - `AnalyticsAPI`
4. Permission: `can_view_reports`

### Endpoints delivered:
- `GET /reports/daily-production/`
- `GET /reports/yield/<run_id>/`
- `GET /reports/line-clearance/`
- `GET /reports/analytics/`

---

## File Creation Order (for each phase)

```
1. models.py        ← Add new models
2. makemigrations   ← Generate migration
3. serializers.py   ← Input/output serializers
4. services.py      ← Business logic
5. permissions.py   ← Permission classes
6. views.py         ← API views
7. urls.py          ← URL patterns
8. admin.py         ← Admin registration
9. migrate          ← Apply migration
10. test            ← Manual/automated testing
```

---

## Dependency Graph

```
Phase 1 (Master Data)
    ├── Phase 2 (Runs + Logs + Breakdowns)
    │       ├── Phase 3 (Materials + Runtime + Manpower)
    │       ├── Phase 6 (Waste Management)
    │       └── Phase 7 (Reports)
    ├── Phase 4 (Line Clearance)
    └── Phase 5 (Machine Checklists)
```

Phases 4 and 5 can run in parallel with Phase 2.
Phase 3 and 6 require Phase 2.
Phase 7 requires Phases 2 + 3.
