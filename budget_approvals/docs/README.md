# Budget Approvals Dashboard

Read-only dashboard over the data behind SAP's `DRAFT_APPROVAL_Budget` HANA
stored procedure, pinned to the **Factory** budget head
(`DRF1.OcrCode3 = 'Factory'`).

## Data source

- The dashboard mirrors the procedure's SELECT but does **not** CALL it: the
  procedure takes no parameters, copies both companies' full `JDT1` journal
  into temp tables, and runs a correlated per-line subquery — measured in
  minutes, far past any interactive request. The reader runs the same SELECT
  per branch with the budget filter pushed down (~0.3 s each) and fetches the
  per-month posted totals as a separate ~24-row aggregate, joined in Python.
  (Joining that aggregate in SQL makes HANA pick a catastrophic plan: 53 s
  vs 0.3 s, measured live.)
- Branches read: **Oil and Beverages** (`ODRF`/`DRF1` joined to the approval
  workflow tables `OWDD`/`WDD1`), matching the procedure. Schemas resolve
  from `settings.COMPANY_DB` via `CompanyContext`, credentials from the Oil
  context (one HANA login sees every company schema); the `Company-Code`
  header does not change what data is read.
- Each line carries the current month's already-posted expense for the same
  budget head (`current_month_posted_amount`, aggregated from `JDT1` per
  MM-YYYY, exactly like the procedure's `Current_month_Posted_Amount`).

## Behaviour

- `services.BudgetApprovalService` keeps only rows whose `BUDGET` equals
  `Factory` (case-insensitive) and caches the result for **3 minutes**
  (`django.core.cache`). `?refresh=true` bypasses the cache.
- Filtering (`status`, `branch`, `effect_month`, `search`) and pagination are
  applied server-side on the cached rows.
- Approval status comes from `WDD1.Status`: `W` pending, `Y` approved,
  `N` rejected.

## API

```
GET /api/v1/dashboards/budget-approvals/report/
    ?status=pending|approved|rejected
    &branch=OIL|BEVERAGE
    &effect_month=09-2026
    &search=<free text>
    &column_filters={"owner":["NARESH"],"sub_budget":["DIESEL"]}   (JSON)
    &sort_by=amount&sort_dir=desc
    &page=1&page_size=50
    &refresh=false

GET /api/v1/dashboards/budget-approvals/column-values/
    ?field=owner            (one of the filterable columns)
    &<same filters as the report>
```

`column_filters` are Excel-style per-column value filters. The
`column-values/` endpoint returns the distinct values (+ row counts) for one
column, computed over the dataset filtered by everything except that column —
it feeds the header filter dropdowns. Each line also carries `approver`
(from `WDD1.UserID` → `OUSR`); several approvers with the same decision on
one stage are comma-joined so line counts and totals stay unchanged.

Requires JWT auth, a `Company-Code` header, and the
`budget_approvals.can_view_budget_approvals` permission. SAP connectivity
failures map to 503, SAP data errors to 502 (same contract as the other
HANA-backed dashboards).

Response: `{ data: [lines], summary: {totals + by_status}, options:
{branches, effect_months}, meta: {pagination, fetched_at, from_cache} }`.

## Frontend

`FactoryFlow/src/modules/dashboards/budget-approvals/` —
route `/dashboards/budget-approvals`, sidebar entry under Dashboards, gated on
the same permission codename.

## Deploy notes

- New Django app: run `migrate` once so the `can_view_budget_approvals`
  permission row is created (the app has no tables of its own), then assign
  the permission to the relevant groups/users.
