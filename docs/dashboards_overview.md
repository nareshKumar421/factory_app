# SAP Dashboards (BI / Reporting) — Backend Overview

> **Scope:** the read-only, SAP-HANA-backed reporting apps that power the
> **Dashboards** module of the factory platform.
> **Django apps:** `sap_plan_dashboard`, `stock_dashboard`, `inventory_age`,
> `non_moving_rm`, `sales_planning_requirement`.
> **Paired frontend doc:** [`FactoryFlow/docs/modules/dashboards.md`](../../FactoryFlow/docs/modules/dashboards.md)
> (absolute: `C:/Users/gurpa/dev/FactoryFlow/docs/modules/dashboards.md`).

Documentation is **grounded in the code** as of this writing. Older docs
(`docs/sap_plan_flow.md`, the stale `src/modules/dashboards/sap-plan/docs/README.md`)
predate the current shape — trust this file and the code.

---

## Overview — what it does & who uses it

These five apps are **business-intelligence read models over SAP Business One**.
They expose DRF endpoints that a planner / procurement officer / warehouse or
finance manager uses to answer:

| App | Question it answers | URL prefix |
|-----|--------------------|-----------|
| `sap_plan_dashboard` | "For every open production order, which BOM components will fall short of stock, and what must I buy?" | `/api/v1/sap/plan-dashboard/` |
| `stock_dashboard` | "Which items are below their warehouse minimum-stock benchmark right now?" (+ push alerts) | `/api/v1/dashboards/stock/` |
| `inventory_age` | "How old is each stock layer, and what is it worth?" | `/api/v1/dashboards/inventory-age/` |
| `non_moving_rm` | "Which raw materials have not moved in N days (dead stock)?" | `/api/v1/non-moving-rm/` |
| `sales_planning_requirement` | "Given the weekly sales forecast, what quantity must I procure/produce after stock and open POs?" | `/api/v1/dashboards/sales-planning-requirement/` |

**Two architectural styles live here:**

1. **Live pass-through (4 apps):** `sap_plan_dashboard`, `stock_dashboard`,
   `inventory_age`, `non_moving_rm` open a HANA connection **per request**, run
   SQL (or `CALL` a stored procedure), shape the result, and return it. **No
   report data is persisted.** Main risks: **slow queries** and the fact that
   every reader holds a request worker while HANA works.
2. **Cached snapshot (1 app):** `sales_planning_requirement` runs a heavy SAP
   HANA **stored procedure** on a schedule (or on demand), and **materialises
   the result into PostgreSQL** (`SalesPlanningRequirementRow`). Reads hit
   Postgres, not SAP. Main risk: **stale snapshot** between refreshes.

`stock_dashboard` is a hybrid: reads are live, but it also runs a **background
alert job** (APScheduler) that persists `StockAlertLog` rows to de-duplicate
push notifications.

---

## Key concepts & entities

**Company → HANA schema resolution.** Every request carries a `Company-Code`
header. `company.permissions.HasCompanyContext` resolves it to
`request.company.company.code`. Services build a
`sap_client.context.CompanyContext(company_code)`, which looks the company up in
`sap_client/registry.py::COMPANY_SAP_REGISTRY`. There is **one HANA host**; each
company maps to a **different schema** (`settings.COMPANY_DB[...]`). Supported
company codes: `JIVO_OIL`, `JIVO_MART`, `JIVO_BEVERAGES`. An unknown code raises
`SAPValidationError`. This is how a single query template reads a different
company's data — by swapping the schema name in the SQL.

**HANA connection.** `sap_client/hana/connection.py::HanaConnection` wraps
`hdbcli.dbapi`. It sets **`connectTimeout=15 s`** and
**`communicationTimeout=60 s`** so a dead/slow SAP fails fast instead of hanging
a worker forever. Every reader's `_execute*` helper opens a connection, runs one
statement, `fetchall()`s, and closes the connection in a `finally` block.

**SAP B1 tables & procedures used:**
- `OWOR` / `WOR1` — production order header / BOM component lines.
- `OITM` — item master (`OnHand`, `IsCommited`, `OnOrder`, `MinStock`, avg prices, `U_*` UDFs).
- `OITW` — item **per warehouse** (`OnHand`, `MinStock`, `AvgPrice`).
- `OITB` — item groups (dropdown source, `ItmsGrpCod`/`ItmsGrpNam`).
- `OWHS` — warehouse master (name, `Inactive`).
- `OINM` — inventory transaction journal (movements; `InQty`/`OutQty`/`TransType`/`DocDate`/`TransValue`).
- `OFCT` / `FCT1` — sales forecast header / lines (sales planning).
- `REPORT_BP_NON_MOVING_RM(age, item_group)` — stored proc for non-moving RM.
- `"SALES PLANNING VS REQUIREMENT_WEEKLY"(param)` — stored proc for sales planning.

**Core computed terms:**
- **Net available** = `OnHand − IsCommited` (what is really free to consume).
- **Shortfall** = `max(0, component_remaining − net_available)`; `component_remaining = PlannedQty − IssuedQty`.
- **Benchmark / MinStock** = the warehouse minimum-stock level; the stock health baseline.
- **Slow-moving** = last consumption > **30 days** ago (`SLOW_MOVING_DAYS`); consumption = `OINM` OutQty with `TransType ∈ (15, 60, 202)` (Delivery, Goods Issue, Production Order).
- **Age** = `DAYS_BETWEEN(effective_date, CURRENT_DATE)`, where effective date is a FIFO-style reconstruction (see Inventory Age flow).
- **Net shortage** (sales planning) = `max(required_qty − open_po_qty, 0)`.

**Errors → HTTP status (uniform across all apps):**
`SAPConnectionError → 503`, `SAPDataError → 502`, `SAPValidationError → 400`,
bad query params → `400`, not-found order → `404`.

---

## End-to-end flows

### 1. SAP Plan Dashboard — production shortfall & procurement
`sap_plan_dashboard/views.py` → `services.PlanDashboardService` → `hana_reader.HanaPlanDashboardReader`.

1. Client GETs one of `summary/`, `details/`, `procurement/`, or `sku/<doc_entry>/`.
2. `PlanDashboardFilterSerializer` validates filters (`status` planned/released/all, `due_date_from/to`, `warehouse`, `sku`, `show_shortfall_only`).
3. Reader joins `OWOR T0 → WOR1 T1 → OITM T2`. `_common_where` **always** forces
   `Status IN ('P','R')` (only open orders), `WOR1.ItemType = 4` (inventory
   items, not resources/text), and `OITM.InvntItem = 'Y'`.
4. **Summary** = one row per order with `COUNT` of components and a
   `SUM(CASE …)` count of components in shortfall. **Details** = one row per BOM
   line with per-line stock + shortfall, grouped into orders in Python
   (`_group_details_by_order`). **Procurement** = details aggregated **by
   component code** across all orders (`_aggregate_procurement`): sums required
   qty, computes consolidated shortfall + `suggested_purchase_qty`, lists
   `related_prod_orders`, sorts worst-shortfall-first.
5. `sku/<doc_entry>/` reuses the details query filtered to one `DocEntry`; empty
   result → `ValueError` → **404** ("not found or not in Planned/Released status").
6. Every line gets a `stock_status`: `sufficient` / `partial` / `stockout`.

### 2. Stock Benchmark — live health vs MinStock
`stock_dashboard/views.py` → `StockDashboardService` → `HanaStockDashboardReader`.

1. GET `/dashboards/stock/` with `warehouse` (CSV), `item_group`, `status` (CSV of healthy/low/critical/unset), `movement_status` (CSV recent/slow), `search`, `sort_by/sort_dir`, `page/page_size`.
2. **Grouping switch:** if **≥ 2 warehouses** are selected, the service uses the
   *grouped* query (`get_grouped_stock_levels`) — items aggregated across
   warehouses (`SUM(OnHand)`, **`SUM(MinStock)`**, `COUNT` warehouses). Otherwise
   the flat per-warehouse query runs.
3. Reader joins `OITW w → OITM m → OITB grp` plus a `LEFT JOIN` subquery on
   `OINM` (`_movement_joins`) that finds each item's **last consumption date**.
4. Health thresholds (`_stock_status`): `MinStock ≤ 0` → **unset**; `OnHand ≥
   MinStock` → **healthy**; `OnHand ≥ 0.6·MinStock` → **low**; else **critical**.
   **A slow-moving item returns `stock_status = "none"`** and is excluded from
   healthy/low/critical counts (so dead stock never shows as "critical").
5. Stats (`get_stock_stats` / `get_grouped_stock_stats`) are computed over the
   **whole filtered set** (not just the page) so the meta cards match the table.
6. Row-expand: GET `/dashboards/stock/<item_code>/warehouses/?warehouse=…`
   returns the per-warehouse breakdown for one item.
7. **As-of (experimental):** GET `/dashboards/stock/as-of/?as_of_date=YYYY-MM-DD`
   reconstructs a historical on-hand = `current OITW.OnHand − net OINM movements
   posted after as_of_date`, keeping **benchmark & master data current** (see
   `reconstruction_note` in the response meta).

### 3. Stock alert job — push notifications (background)
`stock_dashboard/jobs.py`, launched by `manage.py runapscheduler`.

1. APScheduler `IntervalTrigger` every `STOCK_ALERT_INTERVAL_MINUTES` (default **10 min**).
2. For each **active** company, `get_stock_levels({})` (no filters) is run; rows
   with `stock_status ∈ (low, critical)` need alerts. **Caveat:** `{}` means
   `page=1, page_size=50`, so the job only ever inspects the **first 50 rows**
   (ordered `health_ratio ASC` = the 50 worst-health items). If a company has
   **more than 50** low/critical items, the overflow is silently un-alerted.
3. Cooldown: `StockAlertLog` (unique per `company_code+item_code+warehouse`)
   suppresses re-alerting until `cooldown_until` (default **60 min**,
   `STOCK_ALERT_COOLDOWN_MINUTES`). Exception: a **low → critical** worsening
   re-alerts immediately.
4. `NotificationService.send_notification_by_permission("can_view_stock_dashboard", …)`
   sends to everyone who can view the dashboard, deep-linking to
   `/dashboards/stock-levels?search=<item_code>`; the log is upserted.
5. Per-company SAP errors are caught & logged; other companies continue.

### 4. Inventory Age & Value — FIFO age + valuation
`inventory_age/views.py` → `InventoryAgeService` → `HanaInventoryAgeReader`.

1. GET `filter-options/` first (cheap `OITB` + distinct `OITW/OITM` query) to
   populate item-group / sub-group / warehouse / variety dropdowns.
2. GET `report/` runs a **multi-CTE HANA query**: `StockItems` (on-hand > 0,
   active warehouses) → `InboundByDate`/`InboundCumulative` (running total of
   inbound `OINM`, newest-first, excluding `TransType 67`) → `EffectiveLayer`
   (the inbound date whose cumulative qty first covers current `OnHand` — the
   FIFO "effective date") → `CalcPrice` (value-weighted price) → `ReportRows`
   (age = `DAYS_BETWEEN(effective_date, today)`, value = qty × price).
3. Price fallback chain: computed `CalcPrice` → warehouse `AvgPrice` → item
   `AvgPrice` → `LastPurPrc`. If there is no inbound movement, effective date
   falls back to the item's `CreateDate`.
4. `min_age` is applied **in SQL**; the rest of the filters (`search`,
   `warehouse`, `sub_group`, `variety`, `item_group`) are applied **in Python**
   (`_apply_filters`), then a warehouse summary + totals meta are computed.

### 5. Non-Moving Raw Material — central procedure + fallback
`non_moving_rm/views.py` → `NonMovingRMService` → `HanaNonMovingRMReader`.

1. GET `report/?age=<days>&item_group=<code>` (`age=0` = all stock).
2. **Cross-company quirk:** the primary read `CALL`s
   `REPORT_BP_NON_MOVING_RM(age, item_group)` **in a single central schema**
   (`JIVO_BEVERAGES_HANADB`, constant `PROCEDURE_SOURCE_SCHEMA`), regardless of
   the caller's company. The procedure returns rows for **all branches**; the
   service keeps only rows whose `branch` matches the caller via
   `COMPANY_BRANCH_LABELS` (`JIVO_OIL→OIL`, `JIVO_MART→MART`, `JIVO_BEVERAGES→BEV`).
3. **Fallback:** if the central procedure returns **no rows** for a
   company+item_group (and the company has a branch label),
   `_fallback_to_company_stock_age_report` runs a stock-age SQL query
   **against the caller's own schema** (`get_stock_age_report`) so the report
   isn't silently empty.
4. Warehouse distribution: current `OITW` stock per item (batched 200 codes at a
   time) is fetched from the **caller's** schema and used to **pro-rate** each
   item's qty/value across warehouses (`_build_warehouse_summary`). Items with no
   current warehouse stock land in an **"Unassigned"** bucket.
5. GET `item-groups/` returns `OITB` groups (central schema) for the dropdown.

### 6. Sales Planning vs Requirement — scheduled snapshot refresh
`sales_planning_requirement/{views,services,hana_reader,jobs,models}.py`.

**Read path (fast, Postgres-only):**
1. GET `report/` → `SalesPlanningRequirementService.get_report` queries
   `SalesPlanningRequirementRow` filtered by `company_code`, applies `search` /
   `status` (shortage / po_covered), paginates (default 50, max 200, ordered by
   `-net_shortage_qty`), and returns rows + an aggregate `summary` (totals,
   `open_po_coverage_percent`) + the current refresh status.
2. GET `status/` (latest + last-success run), `analysis/` (procedure + column
   metadata, scheduler config), `forecasts/` (live `OFCT/FCT1` list for the
   refresh picker).

**Refresh path (heavy, SAP → Postgres):** POST `refresh/` (or scheduler/CLI).
1. `_ensure_supported_company` — **only `JIVO_BEVERAGES` and `JIVO_OIL`** are in
   `PROCEDURE_COMPANY_CONFIG`; others raise `SalesPlanningUnsupportedCompany` → **400**.
2. `_start_run` first **reaps stale RUNNING rows** older than
   `SALES_PLANNING_REQUIREMENT_RUN_TIMEOUT_HOURS` (default **4 h**), then inserts
   a new `RUNNING` row. A partial unique index
   (`uniq_running_sales_planning_refresh`) means a second concurrent refresh
   hits `IntegrityError` → `SalesPlanningRefreshInProgress` → **409**.
3. `HanaSalesPlanningRequirementReader.execute_procedure` resolves the forecast
   (explicit `forecast_id`/`forecast_name`, else latest current forecast from
   `OFCT`), picks the per-company parameter (**BEV uses `forecast_id`, OIL uses
   `forecast_name`**), and `CALL`s the procedure, capturing column metadata.
4. Rows are normalised (`_build_rows`): the required-qty column name **differs
   by company** (`Final Required Qty` for OIL, `Required Qty` for BEV, `RequiredQty`
   base), `net_shortage = max(required − open_po, 0)`, and the **raw procedure
   row is retained in `raw_payload` JSONB** for audit.
5. **Inside one transaction:** update the run, **delete all existing rows for the
   company**, `bulk_create` the new rows, then `mark_success`. On any exception,
   `mark_failed` records the message and the **old rows are left intact** (the
   delete/insert only commits on success).

---

## Critical business rules & invariants

- **Read-only.** No dashboard app writes to SAP. The only Postgres writes are
  `SalesPlanningRequirementRow`/`…RefreshRun` (snapshot) and `StockAlertLog`
  (notification de-dupe). None of the report SQL mutates SAP tables.
- **Company isolation is by schema.** A report only ever sees the schema for the
  `Company-Code` header — except `non_moving_rm`, which reads a **shared central
  schema** and then filters by branch label. Get the branch mapping wrong and a
  company sees another branch's data or nothing.
- **Open orders only** (`sap_plan_dashboard`): `Status IN ('P','R')` and
  `ItemType = 4` are non-negotiable in `_common_where`.
- **Slow-moving overrides health** (`stock_dashboard`): an item unconsumed for
  > 30 days is reported as `none` and **excluded** from low/critical counts even
  if `OnHand < MinStock`.
- **Grouped MinStock is summed** (`stock_dashboard`, ≥ 2 warehouses): the
  benchmark for a grouped item is the **sum** of per-warehouse `MinStock`.
- **Snapshot atomicity** (`sales_planning_requirement`): rows for a company are
  replaced **only after** the procedure succeeds, inside `transaction.atomic()`.
  A failed refresh never blanks the table.
- **One running refresh per company** enforced by a DB partial-unique constraint,
  not just app logic.
- **Supported-company allow-list** (`sales_planning_requirement`): refresh &
  forecasts are hard-limited to BEV + OIL.
- **Permissions are per-app Django perms** on unmanaged sentinel models (see
  Permissions section); all endpoints also require `IsAuthenticated` + `HasCompanyContext`.

---

## Integrations & cross-module boundaries

- **SAP B1 HANA** is the single upstream for all six data paths, via `hdbcli`
  and `sap_client` (`CompanyContext`, `HanaConnection`, `registry`, `exceptions`).
- **`company` app** — `HasCompanyContext` permission and the `Company` model
  (the alert job and sales-planning scheduler iterate `Company.objects.filter(is_active=True)`).
- **`notifications` app** — `stock_dashboard/jobs.py` calls
  `NotificationService.send_notification_by_permission` with
  `NotificationType.STOCK_ALERT`.
- **APScheduler / django_apscheduler** — two long-running management commands:
  `runapscheduler` (stock alerts, interval) and
  `run_sales_planning_requirement_scheduler` (monthly cron, default day 1 @ 02:30).
- **Cross-company boundary:** these are single-company **reads** scoped by the
  header. The one true cross-company case is `non_moving_rm` reading the central
  `JIVO_BEVERAGES_HANADB` schema for all companies. (See the platform memo on the
  cross-company flow boundary — reads resolve the schema from the header, not the record.)
- **Frontend** consumes every endpoint via TanStack Query hooks; see the paired
  frontend doc.

---

## Real-world edge cases

Each: **trigger → current behaviour → operator-visible symptom → risk/gap.**

1. **SAP HANA is down / unreachable.**
   → `HanaConnection.connect()` raises `dbapi.Error` → `SAPConnectionError` → **503**
   (after up to the 15 s connect timeout).
   → Operator sees the amber **"SAP System Unavailable"** banner with a Retry button.
   → Risk: during the 15 s timeout a request worker is tied up; a burst of tabs can exhaust workers.

2. **HANA reachable but the query/procedure errors** (missing proc, permission,
   schema typo, bad UDF).
   → `dbapi.ProgrammingError` → `SAPDataError` → **502**.
   → Operator sees **"SAP Data Error"** (no Retry button on 502).
   → Risk: `inventory_age` surfaces the raw HANA message in the `detail`
   (`f"Inventory age query error: {e}"`) — potential internal-detail leakage.

3. **Non-moving report empty for a company/item-group.**
   → Central `REPORT_BP_NON_MOVING_RM` returns nothing → fallback stock-age query
   runs against the company's own schema.
   → Operator still sees rows (from the fallback), possibly with slightly
   different values than the central procedure.
   → Risk: two code paths can disagree; a company with **no branch-label mapping**
   skips both the branch filter *and* the fallback and shows **all branches' rows**.

4. **Stale sales-planning snapshot.**
   → No refresh since last month (or manual refresh overdue).
   → `report/` returns last-successful rows; the refresh panel shows the old
   "Last refresh" timestamp and forecast name.
   → Risk: planners act on month-old forecast/PO data believing it is current.

5. **Sales-planning refresh crashes mid-run** (worker killed, HANA drop after `CALL`).
   → The `RUNNING` row is never marked failed by that process.
   → New refreshes get **409 "already running"** until the **4 h** stale-reaper
   auto-fails it; the panel shows status "running" the whole time.
   → Risk: up to 4 h with refresh blocked; old snapshot served meanwhile.

6. **Two users hit Refresh at once.**
   → Partial-unique constraint → second request **409**.
   → Second user sees a 409 (frontend does not auto-retry 409); button stays disabled while status is `running`.
   → Working as designed; no data corruption.

7. **JIVO_MART opens Sales Planning.**
   → Not in `PROCEDURE_COMPANY_CONFIG`. `report/`/`status/` return **empty** (no
   cached rows); `refresh/` and `forecasts/` return **400 unsupported company**.
   → Operator sees an empty table and a refresh that errors.
   → Risk: looks like "blank data / broken" rather than "not enabled for this company".

8. **Item below benchmark but not consumed in 30+ days.**
   → `stock_dashboard` returns `stock_status = "none"`; it is **not** counted as low/critical and no alert fires.
   → Manager scanning the "Critical" tile won't see it.
   → Risk/gap: genuinely short but dormant items are invisible in the health view by design.

9. **Multi-warehouse benchmark inflation.**
   → User selects ≥ 2 warehouses; grouped query **sums** `MinStock`.
   → An item healthy in each warehouse individually can still appear healthy, but
   the benchmark shown is the combined figure.
   → Risk: benchmark semantics differ between single- and multi-warehouse views;
   easy to misread.

10. **Large unpaginated result.**
    → `sap_plan_dashboard`, `inventory_age`, `non_moving_rm` return the **entire**
    filtered set (no `LIMIT`). A broad filter (all item groups, `age=0`) can be tens of thousands of rows.
    → Operator sees a long spinner, then a heavy payload; the browser table lags.
    → Risk: slow API + memory pressure; ties into the platform performance backlog.

11. **Duplicate item rows in non-moving before grouping.**
    → The procedure can return the same item across warehouses; the **frontend**
    groups by `branch::item_code` (`groupNonMovingItemsBySku`). The backend
    warehouse summary independently pro-rates by current stock.
    → Symptom: table counts (grouped) can differ from warehouse-summary counts.
    → Risk: reconciling "item count" between the two panels confuses users.

12. **More than 50 low/critical items in one company (stock alerts).**
    → The alert job calls `get_stock_levels({})`, which defaults to
    `page_size=50`; only the first 50 rows (worst `health_ratio`) are scanned for
    `low`/`critical`.
    → Symptom: managers get alerts for the worst items but **never** for the
    51st-worst-onward, even if genuinely critical. No error is logged.
    → Risk/gap: silent under-alerting on companies with a long low-stock tail; the
    job should page through the whole set (or query unpaginated) for alerting.

---

## Failure modes / what can break

- **Slow HANA query blocks a worker.** The multi-CTE `inventory_age` query and
  the grouped `stock_dashboard` query are the heaviest. Up to the 60 s
  `communicationTimeout`, the gunicorn worker is occupied. **Symptom:** the whole
  app feels slow under load; other requests queue. (See the platform note on the
  shared prod box — a stall here is felt app-wide.)
- **Connection churn.** Every request (and every alert-job company loop) opens
  and closes its own HANA connection. **Symptom:** connection setup latency
  dominates small queries; a tight alert interval multiplies it across companies.
- **Stock alert storm / silence.** If `runapscheduler` isn't running, **no stock
  alerts** are ever sent (silent gap). If cooldown is misconfigured to 0, users
  get repeated pings every interval. **Symptom:** managers either never hear
  about low stock or get spammed. Separately, the job's **50-row page cap** (see
  edge case 12) means a long low-stock tail is never alerted — a *partial* silence
  that is easy to miss because the worst items still alert.
- **Stale/limbo refresh run.** As in edge case 5 — a `RUNNING` row blocks refresh
  for up to 4 h. **Symptom:** "Refresh" disabled / 409, panel stuck on "running".
- **Central procedure schema outage** (`non_moving_rm`). If
  `JIVO_BEVERAGES_HANADB` is unavailable, **all three companies'** non-moving
  reports fail even though each company's own schema is up. **Symptom:** every
  branch's Non-Moving dashboard 502/503s together.
- **Forecast master gap** (`sales_planning_requirement`). If `OFCT/FCT1` has no
  current forecast, `fetch_latest_forecast` raises `SAPDataError` → **502** and
  the run is marked failed. **Symptom:** refresh fails with "No SAP forecast found".
- **Company misconfiguration.** A `Company-Code` not in the registry → 400 on
  every dashboard for that company. **Symptom:** whole Dashboards module errors
  for that tenant.

---

## Improvement opportunities & known gaps

- **Add pagination** to `sap_plan_dashboard`, `inventory_age`, `non_moving_rm`
  (only `stock_dashboard` and `sales_planning_requirement` paginate).
- **Push filtering into SQL** for `inventory_age` (search/warehouse/sub_group/
  variety are Python-side) and `non_moving_rm` age filter (re-applied in Python
  and again in the frontend) to cut payload size and CPU.
- **Sanitise error detail** — `inventory_age` returns raw HANA exception text in
  the API `detail`; align with the generic messages used by the other apps.
- **Cache/snapshot the live apps** — the same scheduled-snapshot pattern proven
  in `sales_planning_requirement` would fix both slow queries and worker
  occupancy for the 4 live apps.
- **Connection pooling** for HANA instead of per-request connect/close.
- **Surface "not enabled for this company"** explicitly for Sales Planning on
  JIVO_MART instead of a silent empty table.
- **Reconcile the two non-moving code paths** (central procedure vs company
  fallback) so counts and valuations are consistent.
- **Health view blind spot** — consider a separate "short but dormant" signal so
  slow-moving-yet-below-benchmark items aren't fully hidden.
- **Un-cap the stock alert scan** — `stock_dashboard/jobs.py` should iterate all
  pages (or run an unpaginated alert-specific query) instead of implicitly using
  `page_size=50`, so companies with > 50 low/critical items are fully covered.

---

## Permissions & roles

Custom permissions live on **unmanaged sentinel models** (no DB table); they
exist only to register Django perms:

| Permission (codename) | App label | Gate |
|-----------------------|-----------|------|
| `can_view_plan_dashboard` | `sap_plan_dashboard` | View SAP Plan endpoints |
| `can_export_plan_dashboard` | `sap_plan_dashboard` | (defined; reserved for export — not enforced by current views) |
| `can_view_stock_dashboard` | `stock_dashboard` | View Stock endpoints **and** receive stock alerts |
| `can_view_inventory_age` | `inventory_age` | View Inventory Age endpoints |
| `can_view_non_moving_rm` | `non_moving_rm` | View Non-Moving endpoints |
| `can_view_sales_planning_requirement` | `sales_planning_requirement` | View report/status/analysis/forecasts |
| `can_refresh_sales_planning_requirement` | `sales_planning_requirement` | POST refresh |

Every endpoint's `permission_classes = [IsAuthenticated, HasCompanyContext, <CanView…>]`.
The refresh endpoint additionally requires `CanRefreshSalesPlanningRequirement`.
`can_view_stock_dashboard` doubles as the **notification audience** for stock
alerts. Changing a Django group's perms alone can hide/show whole dashboards —
the frontend sidebar gates on these exact codenames (see the platform memo on
group-perms vs nav gating).

---

## Developer file map

**Backend (per app: `views.py` = API, `services.py` = business logic,
`hana_reader.py` = SQL, `permissions.py`, `serializers.py`, `models.py` = perms/cache):**

- **sap_plan_dashboard/** — `views.py` (4 `APIView`s), `services.py`
  (`PlanDashboardService`), `hana_reader.py` (`HanaPlanDashboardReader`, OWOR/WOR1/OITM),
  `serializers.py`, `permissions.py`, `urls.py`, `models.py` (sentinel perms).
- **stock_dashboard/** — `views.py` (`StockDashboardAPI`, `…AsOfAPI`,
  `StockItemDetailAPI`), `services.py` (`StockDashboardService`,
  thresholds/slow-moving), `hana_reader.py` (grouped/ungrouped/as-of query
  builders), `jobs.py` (`send_stock_alerts`), `models.py`
  (`StockAlertLog` + perms), `management/commands/runapscheduler.py`.
- **inventory_age/** — `views.py` (`…FilterOptionsAPI`, `…DashboardAPI`),
  `services.py`, `hana_reader.py` (multi-CTE FIFO age query), `serializers.py`.
- **non_moving_rm/** — `views.py` (`NonMovingRMReportAPI`, `ItemGroupDropdownAPI`),
  `services.py` (`NonMovingRMService`, branch filter + fallback + warehouse
  proration; `PROCEDURE_SOURCE_SCHEMA`), `hana_reader.py`
  (`REPORT_BP_NON_MOVING_RM`, `COMPANY_BRANCH_LABELS`, stock-age fallback).
- **sales_planning_requirement/** — `views.py` (5 endpoints), `services.py`
  (`SalesPlanningRequirementService`, refresh/snapshot logic), `hana_reader.py`
  (`PROCEDURE_COMPANY_CONFIG`, `OFCT/FCT1`, `execute_procedure`), `models.py`
  (`SalesPlanningRequirementRow`, `…RefreshRun`, `PROCEDURE_NAME`), `analysis.py`
  (column metadata), `jobs.py`, `management/commands/`
  (`refresh_sales_planning_requirement.py`, `run_sales_planning_requirement_scheduler.py`),
  `migrations/0002_use_weekly_procedure.py`.
- **Shared SAP client** — `sap_client/context.py`, `sap_client/registry.py`,
  `sap_client/hana/connection.py`, `sap_client/exceptions.py`.
- **Routing** — `config/urls.py` (lines ~58–67).

**Key frontend files** (see the paired doc for the full map):
- `src/modules/dashboards/module.config.tsx` — routes + permission gates.
- `src/modules/dashboards/{sap-plan,stock-level,inventory-age,non-moving,sales-planning-requirement}/` — one folder per dashboard.
- `src/config/constants/api.constants.ts` — endpoint paths.
- `src/config/permissions/dashboards.permissions.ts` — `DASHBOARDS_PERMISSIONS`.

---

## Related docs

- **Paired frontend doc:** `C:/Users/gurpa/dev/FactoryFlow/docs/modules/dashboards.md`
  (`../../FactoryFlow/docs/modules/dashboards.md`).
- Still-useful older backend notes: `docs/sap_plan_flow.md` (SAP Plan flow — verify against current code).
- Related frontend design notes (may be partially stale): `FactoryFlow/docs/modules/sap-plan-dashboard.md`,
  `sap-plan-dashboard-frontend.md`, `stock-benchmark.md`, `stock-benchmark-snapshot-plan.md`, `dashboard.md`.
- Platform memos: cross-company flow boundary; group-perms vs frontend nav gating; prod server & observability (shared box, slow-API = DRF N+1 / heavy HANA, not infra).
