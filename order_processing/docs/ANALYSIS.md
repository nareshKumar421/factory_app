# Order Processing — analysis and phased plan

Deliverable for STEP 27. **Analysis only — no implementation.** Awaiting approval.

Grounded in a live read-only inspection of the OMS database on 11 Aug 2026, not in
the specification's description. Where the two disagree, this document follows the database
and says so.

---

# 0. Findings that change the design

Five things the live inspection settled. Each contradicts or completes the spec.

### 0.1 `qty` is total pieces. `pcs` is the pack size.

The spec warns *"`pcs` is in individual bottles (not cartons)"*. **The data says
otherwise.** `boxes × pcs = qty` holds for **5,250 of 5,476 lines (95.9%)**:

| Item | qty | pcs | boxes | ltrs |
| --- | --- | --- | --- | --- |
| MUSTARD KACHI GHANI 1 LTR **20 PCS** | 8000 | **20** | 400 | 8000 |
| REFINED OIL 15 LTR | 180 | **1** | 180 | 2700 |
| COLD PRESS GROUNDNUT 5 LTR **4 PCS** | 84 | **4** | 21 | 420 |

`pcs` mirrors the pack size printed in the item name. So:

> **`qty` is the field to compare against SAP stock.** Not `pcs`.

**226 lines (4.1%) break the relationship** and must be investigated before this is
trusted — likely scheme lines or manual edits.

### 0.2 There is no warehouse anywhere on an order

Searching every column in the database for `%warehouse%` returns exactly one hit:
`invoice_log.warehouse`. Not on `orders`, not on `order_items`.

A `warehouses` table exists (4 rows: WH-DEL, WH-GGN, WH-NOI, FAC-BGH) but **nothing
references it**.

So the spec's "WarehouseCode is resolved from `order_items.category`" happens in
**application code we cannot see**. Two ways to get it:

1. Ask Harshit for the mapping, or
2. **Read it from the data** — `sales_quotation_logs.request_data` is JSONB holding
   the exact payload SAP received, including `WarehouseCode` per line. 1,860
   successful pushes is more than enough to derive the true mapping and verify it.

Option 2 is better: it is the mapping actually used, not the one someone remembers.

### 0.3 OMS creates BOTH Quotations and Sales Orders

This was the question that decides whether we keep a stock ledger.

| Table | Rows |
| --- | --- |
| `sales_quotation_logs` | 2,146 (1,860 SUCCESS / 286 FAILED) |
| `sales_orders_logs` | **487** |

A SAP **Sales Order commits stock** (`OITW.IsCommited`); a **Quotation does not**.
Since both exist, neither "trust SAP's committed figure" nor "keep our own ledger"
is right on its own.

**The rule must be per order:** if it produced a Sales Order, SAP already holds the
commitment and we must not double-count it. If it produced only a Quotation — or
nothing — the demand is invisible to SAP and only we can track it.

### 0.4 A real reconciliation gap already exists

| | Count |
| --- | --- |
| Orders with `sap_created = false` | **273** |
| Failed quotation pushes | **286** |

Roughly **12% of orders never reached SAP.** Any stock calculation that assumes
SAP knows about every order is wrong today, before we write a line of code.

### 0.5 OMS already caches SAP stock

`sap_products` — **4,162 rows** — carries `on_hand`, `sal_factor2` (pieces per case),
`tax_rate`, `brand`, `sub_group`, `category`, `synced_at`.

Useful as a cross-check, **not as a source of truth**: it is a periodic snapshot,
and §21 of the spec is explicit that stale inventory must not drive allocation.
`product_details` and `sku` are **empty** despite being named in the spec.

---

# 1. Existing architecture summary

## 1.1 Backend

| Aspect | What is there |
| --- | --- |
| Framework | Django + Django REST Framework |
| Structure | ~50 domain apps at repo root (`gate_core`, `warehouse`, `marketplace`, `production_execution`, `supply_chain`, `oms`, …) |
| Database | PostgreSQL. Optional second alias pattern already exists (`ai_readonly`, added only when `AI_DB_NAME` is set) |
| Auth | JWT (`token_blacklist` present) + a **Company-Code header**; `HasCompanyContext` permission gates every company-scoped view |
| Permissions | Per-app DRF `BasePermission` classes over Django model permissions (`marketplace/permissions.py`, `supply_chain/permissions.py`, `oms/permissions.py`) |
| Serializers | DRF `ModelSerializer` + explicit input serializers |
| Services | Established convention: `<app>/services/*.py` holds business logic, views stay thin (`marketplace/services/`, `supply_chain/services/`) |
| Background jobs | **No Celery.** `APScheduler` (`maintenance/jobs.py`, `run_work_permit_scheduler`) plus cron-invoked management commands |
| SAP integration | `sap_client/` — `registry.py` maps company code → HANA schema + Service-Layer config for `JIVO_OIL` / `JIVO_MART` / `JIVO_BEVERAGES`; `sap_client/hana/connection.py` wraps `hdbcli` with 15s connect / 60s comm timeouts |
| HANA readers | Per-app: `warehouse/services/wms_hana_reader.py` (OITM/OITW), `dispatch_plans/hana_reader.py`, `sales_planning_requirement/hana_reader.py`, `non_moving_rm/hana_reader.py` |
| OMS integration | `oms/` — invoice-approval **proxy only**, HTTP client to `103.89.45.75:8081`. **No orders.** |
| Config | `python-decouple` over `.env`; `OMS_SIMULATE` / `MARKETPLACE_SIMULATE_SAP` fixture modes |
| Logging | stdlib `logging` per module |
| Testing | Django `TestCase`; `config/sqlite_test_settings.py` gives in-memory SQLite with migrations disabled |

## 1.2 Frontend

| Aspect | What is there |
| --- | --- |
| Framework | React + TypeScript + Vite, PWA (workbox) |
| Structure | `src/modules/<module>/` with `api/ components/ pages/ types/ module.config.tsx` |
| Routing | Central registry `src/app/registry/index.ts` composes each module's `ModuleConfig` |
| Auth | JWT + company context; `usePermission()` → `hasPermission` / `hasModulePermission` |
| Navigation | Sidebar filters on `modulePrefix` (any `app.*` permission) and optional `companies` |
| UI kit | shadcn-style primitives in `src/shared/components/ui/` + Tailwind, `DashboardHeader` shared |
| State/data | TanStack Query; per-module `*.queries.ts` with typed query keys |
| API client | `src/core/api` `apiClient`; endpoints centralised in `src/config/constants/api.constants.ts` |
| Notifications | `sonner` toasts + FCM push (`notifications` module) |
| Tests | Vitest |

## 1.3 Integrations already present

| | Status |
| --- | --- |
| PostgreSQL | Yes — primary |
| SAP HANA | Yes — `hdbcli`, per-company registry |
| SAP Service Layer | Yes — `marketplace/services/sap_gateway.py` posts DNs/GIs |
| OMS | Partial — invoice proxy only, no orders |
| Message queue | **None** |
| Celery | **None** — APScheduler + cron |

---

# 2. Files and modules to be MODIFIED

Deliberately few. Nothing existing is rewritten.

| File | Change |
| --- | --- |
| `config/settings.py` | Add optional `oms_orders` DB alias (mirrors `ai_readonly`); add `OMS_DB_*` config |
| `config/urls.py` | Mount `api/v1/order-processing/` |
| `.env` | `OMS_DB_*` keys — **done, gitignored, untracked** |
| `src/config/constants/api.constants.ts` | New endpoint group |
| `src/config/permissions/index.ts` | Export new permission file |
| `src/app/registry/index.ts` | Register the new module |

**Not modified:** `oms/`, `sap_client/`, `warehouse/`, `production_execution/`,
`marketplace/`, `supply_chain/`.

# 3. New modules to be CREATED

```
factory_app/
  order_processing/
    models/            order, allocation, requirement, audit
    services/          order_sync, availability, allocation,
                       production_planning, material_planning,
                       procurement_planning, reconciliation
    integrations/
      oms/             reader, mapper, validators      (DB, read-only)
      sap/             inventory, bom, procurement     (wraps sap_client)
    api/               views, serializers, urls, permissions
    jobs.py            APScheduler entry points
    tests/

FactoryFlow/
  src/modules/order-processing/
    api/ components/ pages/ types/ module.config.tsx
```

A **DB router** confines the `oms_orders` alias to reads and blocks migrations
against it — belt and braces on top of the read-only role.

---

# 4. Domain model proposal

Compared against what exists, per STEP 3.

| Proposed entity | Verdict |
| --- | --- |
| `Order`, `OrderLine` | **Create (mirror).** Local copy needed for workflow state and joins; OMS stays source of truth |
| `OrderProcessingStatus` | **Create** — as fields + an event log, not a table of statuses |
| `Warehouse` | **Do not create.** SAP owns it; OMS's own table is unreferenced |
| `InventorySnapshot` | **Create, but as an audit record** of what a check saw — never as a stock cache |
| `InventoryReservation` / `StockAllocation` | **Create as one model**, `StockAllocation`, conditional on §0.3 |
| `ProductionRequirement` | **Create** — links to existing `production_execution.ProductionRun` |
| `ProductionPlan` | **Do not create.** `ProductionRun` already is this |
| `BOMSnapshot` | **Create** — freeze the BOM used, or "why was this produced" is unanswerable later |
| `MaterialRequirement` | **Create** |
| `ProcurementRequirement` | **Create** |
| `Fulfillment` | **Defer** — dispatch already exists in `gate_core` / `dispatch_plans`; join later |
| `IntegrationLog`, `ProcessingEvent` | **Create as one** `ProcessingEvent` with a correlation id |

### Storage decisions

| Data | Decision | Why |
| --- | --- | --- |
| Orders / lines | **Mirror locally** | Workflow state, joins, and OMS may be unreachable |
| Item master | **Query SAP live, cache short-lived** | Changes rarely; needed for names/UOM only |
| FG + RM stock | **Query SAP live, never store** | §21: stale stock causes wrong allocation |
| BOM | **Query live, snapshot per requirement** | Live for correctness, snapshot for traceability |
| Open POs | **Query live** | Changes constantly |
| Allocations / requirements | **Ours** | Workflow, not ERP data |
| Every state change | **Ours, append-only** | §24 traceability |

---

# 5. API proposal

Following this repo's conventions (`/api/v1/<module>/`, DRF `APIView`, company-scoped):

```
GET    /api/v1/order-processing/orders/
GET    /api/v1/order-processing/orders/{id}/
POST   /api/v1/order-processing/orders/{id}/check-stock/
POST   /api/v1/order-processing/orders/{id}/allocate/
POST   /api/v1/order-processing/orders/{id}/release/
GET    /api/v1/order-processing/orders/{id}/timeline/

GET    /api/v1/order-processing/availability/?item=&warehouse=
GET    /api/v1/order-processing/production-requirements/
POST   /api/v1/order-processing/production-requirements/{id}/accept/
GET    /api/v1/order-processing/material-requirements/
GET    /api/v1/order-processing/procurement-requirements/
GET    /api/v1/order-processing/reconciliation/
POST   /api/v1/order-processing/sync/
```

Permissions: `order_processing.can_view_orders`, `.can_allocate_stock`,
`.can_plan_production`, `.can_plan_procurement`.

**No SAP write endpoints.** Rule 11.

---

# 6. OMS integration approach

**Read the database. Never write to it.**

- Django alias `oms_orders`, added only when `OMS_DB_NAME` is set
- A router blocking writes and migrations on that alias
- Incremental pull on `orders.updated_at`; keys `orders.id`, `order_items.id`
- Mapper producing a normalised internal order; idempotent upsert on `oms_order_id`

### Field mapping — from the live schema, not the spec

| Our field | OMS table | Column | Type | Note |
| --- | --- | --- | --- | --- |
| `oms_order_id` | `orders` | `id` | integer | Dedup key |
| `order_number` | `orders` | `order_number` | varchar | `ORD-YYYYMMDD-NNNN` |
| `customer_code` | `orders` | `card_code` | varchar | SAP `OCRD.CardCode` |
| `company_id` | `orders` | `company` | **varchar** | Holds `'1'` / `'2'`, not a name |
| `branch_bpl_id` | `orders` | `dispatch_from_id` | integer | → `branches.bpl_id` |
| `status` | `order_statuses` | `code` | varchar | 11 values, see §7 |
| `delivery_date` | `orders` | `delivery_date` | **text** | Not a date column — must be parsed |
| `sap_created` | `orders` | `sap_created` | boolean | 273 false |
| `updated_at` | `orders` | `updated_at` | timestamp | Watermark |
| `item_code` | `order_items` | `item_code` | varchar | **= SAP `OITM.ItemCode`** |
| `quantity` | `order_items` | `qty` | numeric | **Total pieces — see §0.1** |
| `pack_size` | `order_items` | `pcs` | numeric | Pieces per case |
| `cases` | `order_items` | `boxes` | numeric | |
| `litres` | `order_items` | `ltrs` | numeric | |
| `category` | `order_items` | `category` | varchar | OIL / BEVERAGES only |
| `scheme_qty` | `order_items` | `qty_scheme` | numeric | |
| Warehouse | — | **absent** | — | Derive from `sales_quotation_logs.request_data` |

**Columns the spec names that do not exist:** `is_auto_free`, `combo_source_code`,
`created_by`/`approved_by` (they are `*_by_id`), `order_item_schemes`,
`rate_approver_rules` as described. Do not build against them.

---

# 7. SAP / HANA integration approach

**Reuse `sap_client`. Do not open new connections.**

- `sap_client/registry.py` already routes company → schema + Service Layer
- Company `'1'` / `'2'` in OMS must be mapped to `JIVO_OIL` / `JIVO_BEVERAGES` —
  **confirm this mapping before coding**
- New `order_processing/integrations/sap/` exposing application-level methods
  (`get_available_stock`, `get_bom`, `get_open_po`), each wrapping existing readers
- Tables/views are **not named here on purpose.** `warehouse/services/wms_hana_reader.py`
  already uses OITM/OITW; the BOM (OITT/ITT1) and PO (OPOR/POR1) structures must be
  inspected in the live schema before use — Rule 3

### Order status → does it consume stock?

| Code | Orders | Consumes stock? |
| --- | --- | --- |
| `COMPLETED` | 2002 | Historical — no |
| `REJECTED` / `BILLING_REJECTED` | 258 | **No** |
| `APPROVED`, `AUDITOR_APPROVAL`, `BILLING`, `BILLING_PENDING`, `NEED_APPROVAL` | ~6 | **Probably yes** |
| `CREATED`, `RATE_APPROVAL`, `DRAFT` | 12 | Not yet committed |

**Confirm with Harshit** which codes mean "will ship". Only ~10 orders are in
flight today, so the live workload is small — good for a careful rollout.

---

# 8. Frontend structure

Top-level module (not a dashboard — it has state and writes):

```
/order-processing            Dashboard
/order-processing/orders     List → detail with timeline
/order-processing/inventory  Availability, flagged live vs cached
/order-processing/production Requirements
/order-processing/materials  BOM explosion + shortages
/order-processing/procurement
```

Reuses `DashboardHeader`, `ui/` primitives, `apiClient`, TanStack Query, `sonner`,
JWT + company context. New `order-processing.permissions.ts` with a `modulePrefix`
so the sidebar entry hides for users without access.

**No business logic in components** (Rule 5) — every number comes from the API.

---

# 9. Background jobs

**No Celery here.** Follow the existing pattern: management commands + APScheduler.

| Job | Cadence | Command |
| --- | --- | --- |
| OMS order sync | 15 min | `sync_oms_orders` |
| Stock check for pending orders | 30 min | `check_order_stock` |
| Reconciliation | daily | `reconcile_orders` |

Stock checks are **synchronous on demand** too — an operator pressing "check" must
get a fresh answer, not a queued one.

---

# 10. Security

| Concern | Position |
| --- | --- |
| **Credentials supplied** | `postgres` **superuser**, shared in plaintext chat |
| **Risk** | Full read/write/DROP on all 17 databases on that host — including `factory` itself |
| **Required** | Harshit offered *"a dedicated role with SELECT restricted"* on a replica. **Ask for it.** This violates §27 "minimum permissions necessary" and Rule 12 |
| **Also** | Rotate this password — it has been shared in cleartext |
| Interim mitigation | `.env` (gitignored, untracked, verified); `psycopg2 set_session(readonly=True)`; a router blocking writes/migrations on the alias |
| SAP | Read-only. No PO/production writes without explicit approval (Rule 11) |
| App access | Per-view DRF permissions + company scoping, as everywhere else here |

**This is the one item I would not proceed past without a decision.** Everything
else can start.

---

# 11. Testing strategy

Django `TestCase` with `config/sqlite_test_settings.py`, following `supply_chain`
(83 tests, no SAP or HANA needed).

| Area | Cases |
| --- | --- |
| OMS mapping | Real column names; `delivery_date` as text; `boxes×pcs≠qty` on the 226 outliers |
| Sync | Idempotent re-run; watermark; updated order; cancelled order |
| Availability | Full / partial / none; Sales-Order vs Quotation commitment (§0.3) |
| Allocation | **Concurrent orders** — `select_for_update`, no double-promise |
| Production | Shortage maths; merge across orders; no duplicates on re-run |
| BOM | Explosion; multi-level; **missing BOM** |
| Material | Net requirement with reserved + incoming |
| Procurement | Existing PO honoured; no duplicate requirement |
| Integration | OMS down; SAP down; timeout; retry; malformed row |

OMS and SAP are **mocked** in tests. No test may touch either live system.

---

# 12. Implementation phases

| Phase | Backend | Frontend | Output |
| --- | --- | --- | --- |
| **1 Foundation** | App, models, router, permissions, migrations | — | Tables exist |
| **2 OMS** | Reader, mapper, sync command, `ProcessingEvent` | Orders list | **Orders visible here** |
| **3 SAP** | `integrations/sap/`, inspect BOM/PO schema | — | Stock queryable |
| **4 Inventory** | Availability service, warehouse resolution from payload logs | Inventory screen | **"Can we fulfil this?"** ← first real value |
| **5 Engine** | Validate → check → allocate, idempotent, transactional | Order detail + timeline | Allocation works |
| **6 Production** | `ProductionRequirement` → `ProductionRun` | Production screen | Shortfalls become work |
| **7 BOM/Material** | Explosion + `BOMSnapshot` + net requirement | Materials screen | Material shortages |
| **8 Procurement** | Net procurement requirement | Procurement screen | Buy list |
| **9 Fulfilment** | Join to existing dispatch | Status only | End to end |
| **10 Dashboard** | Aggregates | Dashboard | Overview |
| **11 Jobs** | APScheduler + commands | — | Runs unattended |
| **12 Testing** | Full suite | Vitest | Confidence |
| **13 Monitoring** | Reconciliation + audit views | Reconciliation screen | Drift visible |

**Phase 4 is the first genuinely useful stop.** Phases 1–4 are worth building
before committing to the rest.

---

# 13. Open questions — blocking

1. **Read-only role on the replica** instead of the `postgres` superuser (§10)
2. **Warehouse mapping** — confirm, or approve deriving it from `sales_quotation_logs`
3. **Which order statuses mean "will ship"** (§7)
4. **Company `'1'` / `'2'` → `JIVO_OIL` / `JIVO_BEVERAGES`?**
5. **When does OMS create a Sales Order vs a Quotation?** (§0.3) — decides the ledger
6. The **226 lines where `boxes×pcs≠qty`** — expected, or data errors?

---

*Awaiting approval before Phase 1.*
