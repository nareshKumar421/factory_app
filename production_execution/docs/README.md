# Production Execution — App Documentation

> Django app: `production_execution`
> Base URL: `/api/v1/production-execution/`
> All endpoints require: `Authorization: Bearer <token>` + `Company-Code: <code>`

---

## Overview

This module handles everything **after** a Production Plan/Order is created — from line clearance before production begins, through hourly production logging, material consumption, machine runtime, breakdowns, maintenance checklists, waste management, and manpower tracking.

**Depends on:** `production_planning`, `company`

---

## Module Structure

```
production_execution/
    __init__.py
    apps.py
    models.py              ← 14 models + 9 choice enums
    serializers.py          ← Input/output serializers for all endpoints
    services.py             ← Business logic layer (ProductionExecutionService)
    permissions.py          ← 30 DRF permission classes
    views.py                ← 30+ API views
    urls.py                 ← URL patterns
    admin.py                ← Admin panel registrations
    tests.py                ← 48 API tests (all passing)
    migrations/
        0001_initial.py
        0002_create_production_execution_group.py
    docs/
        README.md           ← This file
        api.md              ← Full API reference
```

---

## Models (3 Layers)

### Level 1 — Master Data
| Model | Purpose |
|-------|---------|
| `ProductionLine` | Production lines (Line-1, Line-2, etc.) |
| `Machine` | Machines assigned to lines (Filler, Capper, etc.) |
| `MachineChecklistTemplate` | Reusable checklist tasks per machine type |

### Level 2 — Transaction Data
| Model | Purpose |
|-------|---------|
| `ProductionRun` | Central entity — one production session on a line |
| `ProductionLog` | Hourly production entries (12 slots: 07:00-19:00) |
| `MachineBreakdown` | Machine breakdown records |
| `ProductionMaterialUsage` | Material consumption / yield tracking |
| `MachineRuntime` | Runtime per machine type |
| `ProductionManpower` | Worker count per shift |

### Level 3 — Quality & Maintenance
| Model | Purpose |
|-------|---------|
| `LineClearance` | Pre-production checklist (9 standard items) |
| `LineClearanceItem` | Individual checklist items |
| `MachineChecklistEntry` | Daily/weekly/monthly machine maintenance checks |
| `WasteLog` | Waste records with 4-level sequential approval |

---

## Key Business Rules

1. **Production Run auto-increments** `run_number` per plan+date
2. **Plan must be OPEN or IN_PROGRESS** to start a run
3. **COMPLETED runs are locked** — no edits allowed
4. **Material wastage auto-calculated**: `opening + issued - closing`
5. **Line Clearance flow**: DRAFT → SUBMITTED → CLEARED/NOT_CLEARED
6. **Waste approval is sequential**: Engineer → AM → Store → HOD
7. **Breakdown machines must belong to the same line** as the run
8. **Run totals auto-computed** from hourly logs and breakdowns

---

## Running Tests

```bash
python manage.py test production_execution -v2 --keepdb
```

48 tests covering all endpoints:
- Production Lines (5 tests)
- Machines (5 tests)
- Checklist Templates (4 tests)
- Production Runs (8 tests)
- Hourly Logs (3 tests)
- Breakdowns (4 tests)
- Material Usage (2 tests)
- Machine Runtime (2 tests)
- Manpower (2 tests)
- Line Clearance (3 tests)
- Machine Checklists (2 tests)
- Waste Management (3 tests)
- Reports (5 tests)

---

## Permissions

All 31 permissions are defined in `ProductionRun.Meta.permissions` and grouped in a Django auth group `production_execution` via migration `0002`.

See [api.md](api.md) for endpoint-specific permission requirements.
