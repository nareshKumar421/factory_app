# Production Execution Module — Master Plan

> Django app name: `production_execution`
> Depends on: `production_planning`, `company`, `sap_client`
> Location: `factory_app_v2/production_execution/`

---

## 1. What This Module Does

Everything that happens **after** a Production Plan/Order is created — from line clearance before production begins, through hourly production logging, material consumption, machine runtime, breakdowns, maintenance checklists, waste management, and manpower tracking.

**This is a backend-only module** — Django REST Framework APIs consumed by the frontend.

---

## 2. Module Structure (follows project conventions)

```
factory_app_v2/
    production_execution/
        __init__.py
        apps.py
        models.py              ← All models (see 01_models.md)
        serializers.py
        views.py
        urls.py
        services.py            ← Business logic layer
        permissions.py         ← DRF permission classes
        admin.py
        migrations/
            0001_initial.py
            0002_create_production_execution_group.py
        docs/
            README.md
            api.md
```

URL registration in `config/urls.py`:
```python
path("api/v1/production-execution/", include("production_execution.urls")),
```

---

## 3. Scope — 3 Layers

| Layer | What | Models |
|-------|------|--------|
| **Level 1** — Master Data | Production Lines, Machines, Checklist Templates | `ProductionLine`, `Machine`, `MachineChecklistTemplate` |
| **Level 2** — Transactions | Production Runs, Hourly Logs, Breakdowns, Material Usage, Machine Runtime, Manpower | `ProductionRun`, `ProductionLog`, `MachineBreakdown`, `ProductionMaterialUsage`, `MachineRuntime`, `ProductionManpower` |
| **Level 3** — Quality & Maintenance | Line Clearance, Machine Checklists, Waste Management | `LineClearance`, `LineClearanceItem`, `MachineChecklistEntry`, `WasteLog` |

---

## 4. Relationship to Existing Modules

```
ProductionPlan (production_planning app)
    │
    ├── ProductionRun (production_execution app)  ← NEW: Central entity
    │       ├── ProductionLog (hourly entries)
    │       ├── MachineBreakdown
    │       ├── ProductionMaterialUsage
    │       ├── MachineRuntime
    │       ├── ProductionManpower
    │       └── WasteLog
    │
    └── LineClearance (production_execution app)  ← NEW: Pre-production check
            └── LineClearanceItem

Company (company app)  ← All models scoped to company
```

The `ProductionRun` links to `ProductionPlan` via FK. This means:
- A Production Plan (from `production_planning`) can have multiple Production Runs
- Each Run captures one production session on a specific line on a specific date

---

## 5. Implementation Phases

See [05_implementation_phases.md](05_implementation_phases.md) for detailed breakdown.

| Phase | Scope | Priority |
|-------|-------|----------|
| **Phase 1** | Master Data (Lines, Machines, Checklist Templates) + Admin | High |
| **Phase 2** | Production Runs + Hourly Logs + Breakdowns | High |
| **Phase 3** | Material Usage (Yield) + Machine Runtime + Manpower | High |
| **Phase 4** | Line Clearance (checklist + QA approval) | Medium |
| **Phase 5** | Machine Checklists (daily/weekly/monthly) | Medium |
| **Phase 6** | Waste Management (multi-level approval) | Medium |
| **Phase 7** | Reports & Analytics APIs | Low |

---

## 6. Key Design Decisions

1. **New Django app** — `production_execution` is a separate app from `production_planning` to maintain clean separation. It depends on `production_planning.ProductionPlan` via FK.

2. **Service layer pattern** — All business logic goes in `services.py` (same as `production_planning`). Views call the service, never touch models directly for writes.

3. **Company-scoped** — All queries filter by `company__code` from the `Company-Code` header (via `HasCompanyContext` middleware).

4. **ProductionRun is the central entity** — Hourly logs, breakdowns, material usage, machine runtime, manpower, and waste all attach to a Run.

5. **Time slots are pre-defined** — 7:00-19:00, 12 hourly slots. Operators fill data per slot.

6. **Sequential waste approval** — Engineer -> AM -> Store -> HOD. Each must approve before the next can.

7. **Auto-calculations in service layer** — Totals, efficiency, OEE computed server-side, not stored (except summary fields on ProductionRun for performance).

8. **No SAP integration in Phase 1-6** — Production execution data lives entirely in Sampooran. SAP posting of receipts/issues can be added later.

---

## 7. Related Docs

| Document | Contents |
|----------|----------|
| [01_models.md](01_models.md) | Django model definitions (fields, types, choices, Meta) |
| [02_api_endpoints.md](02_api_endpoints.md) | Full API endpoint reference |
| [03_permissions.md](03_permissions.md) | Roles, permissions, Django groups |
| [04_business_rules.md](04_business_rules.md) | Workflows, validation rules, auto-calculations |
| [05_implementation_phases.md](05_implementation_phases.md) | Phased build plan with task breakdowns |
