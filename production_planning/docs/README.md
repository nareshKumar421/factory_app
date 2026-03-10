# Production Planning Module

## Overview

The Production Planning module lets planners create production plans directly in Sampooran, post them to SAP B1 as Production Orders, then manage execution through weekly targets and daily production entries — all tracked against the original plan.

## Module Location

```
factory_app_v2/
    production_planning/
        __init__.py
        apps.py
        models.py
        serializers.py
        views.py
        urls.py
        services.py
        permissions.py
        admin.py
        migrations/
            0001_initial.py
            0002_create_production_planning_group.py
            0003_redesign_production_plan.py
        sap/
            __init__.py
            item_reader.py          # SAP HANA: items + UoM dropdowns
        docs/
            README.md               ← this file
            api.md                  ← full API reference
```

## Business Flow

```
[Planner fills plan in Sampooran]
        |
[1] GET /dropdown/items/ · /uom/ · /warehouses/
    → Fetch item, UoM, warehouse lists from SAP HANA for form dropdowns
        |
        v
[2] POST /
    → Save plan as DRAFT in Sampooran (not yet in SAP)
        |
        v
[3] POST /<plan_id>/post-to-sap/
    → Create Production Order in SAP B1 (Service Layer)
    → Plan status: DRAFT → OPEN; sap_doc_num assigned
        |
        v
[4] POST /<plan_id>/weekly-plans/   (repeat for each week)
    → Create Week 1, 2, 3, 4 with target quantities and date ranges
    → Plan status: OPEN → IN_PROGRESS
        |
        v
[5] POST /weekly-plans/<week_id>/daily-entries/
    → Production team records daily output (date + qty + shift)
    → WeeklyPlan.produced_qty and ProductionPlan.completed_qty auto-updated
        |
        v
[6] POST /<plan_id>/close/
    → Manager closes the plan → status: COMPLETED (locked)
```

## Models

| Model | Description |
|-------|-------------|
| `ProductionPlan` | Plan created in Sampooran, posted to SAP as OWOR |
| `PlanMaterialRequirement` | BOM components (raw materials needed) |
| `WeeklyPlan` | Planner's weekly breakdown (Week 1–4 targets) |
| `DailyProductionEntry` | Production team's daily output log |

## Plan Status Flow

```
DRAFT → OPEN (after SAP posting) → IN_PROGRESS (after first weekly plan) → COMPLETED (closed)
```

| Status | Meaning |
|--------|---------|
| `DRAFT` | Created locally, not yet posted to SAP |
| `OPEN` | Posted to SAP, no weekly plans yet |
| `IN_PROGRESS` | Weekly plans created, production underway |
| `COMPLETED` | Closed by manager |
| `CLOSED` | Fully confirmed and locked |
| `CANCELLED` | Cancelled |

## SAP Posting Status

| Status | Meaning |
|--------|---------|
| `NOT_POSTED` | Plan not yet sent to SAP |
| `POSTED` | Successfully created in SAP (has sap_doc_num) |
| `FAILED` | SAP returned an error (see sap_error_message; can retry) |

## WeeklyPlan Status

| Status | Meaning |
|--------|---------|
| `PENDING` | No daily entries yet |
| `IN_PROGRESS` | Has entries but produced_qty < target_qty |
| `COMPLETED` | produced_qty ≥ target_qty |

## SAP Integration

### SAP HANA (Read-Only)

| Table | Purpose |
|-------|---------|
| `OITM` | Item master — finished goods and raw materials dropdowns |
| `OUOM` | Unit of measure dropdown |
| `OWHS` | Active warehouses dropdown |

### SAP Service Layer (Write)

| Operation | SAP Endpoint |
|-----------|-------------|
| Create production order | `POST /b1s/v2/ProductionOrders` |

## Permissions

| Permission | Who Needs It |
|-----------|--------------|
| `can_create_production_plan` | Planner |
| `can_edit_production_plan` | Planner |
| `can_delete_production_plan` | Planner |
| `can_post_plan_to_sap` | Planner, Manager |
| `can_view_production_plan` | All roles |
| `can_manage_weekly_plan` | Planner, Manager |
| `can_add_daily_production` | Production Team, Supervisor |
| `can_view_daily_production` | All roles |
| `can_close_production_plan` | Manager |

All permissions are bundled in the `production_planning` Django group (created by migration 0002).

## Setup

1. Add `'production_planning'` to `INSTALLED_APPS` in `config/settings.py`
2. Add URL include in `config/urls.py`:
   ```python
   path("api/v1/production-planning/", include("production_planning.urls")),
   ```
3. Run migrations:
   ```bash
   python manage.py migrate production_planning
   ```
4. Assign the `production_planning` Django group to relevant users.

## Required Headers (all endpoints)

```
Authorization: Token <token>
Company-Code: <company_code>
```

## Key Business Rules

1. **DRAFT plans are fully editable** — item, quantity, dates, materials can all be changed before SAP posting.
2. **Only DRAFT plans can be deleted.**
3. **SAP posting is a prerequisite for weekly planning** — weekly plans require the plan to be in `OPEN` or `IN_PROGRESS` status (already posted).
4. **SAP posting is retryable** — if posting fails (`sap_posting_status=FAILED`), fix the issue and retry `POST /<plan_id>/post-to-sap/`.
5. **Weekly target sum cannot exceed plan quantity.**
6. **Daily entry date must fall within the week's date range.**
7. **No future-dated entries** — `production_date` cannot be tomorrow or later.
8. **Closed/cancelled plans are locked** — no new weekly plans or daily entries allowed.
9. **Totals auto-computed** — `WeeklyPlan.produced_qty` and `ProductionPlan.completed_qty` are recomputed after every daily entry save.

## Running Tests

```bash
python manage.py test production_planning
```

## Related Docs

- [API Reference](api.md) — full endpoint documentation with request/response samples
