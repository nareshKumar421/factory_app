# Performance & Request-Pattern Quality Audit

Static code audit of the **factory_app** Django backend and the **FactoryFlow** React frontend, triggered after fixing the Service GRPO pending-page hang. Goal: find siblings of that bug (N+1 live SAP reads, whole-table scans, missing timeouts) plus other slow request patterns across every page.

> **Method note:** The original Service GRPO bug was *measured* against live SAP (35× query speedup, 90s→1 query). The findings below are **static code-pattern findings** from 7 parallel auditors — they identify the same anti-patterns by code shape, not by live measurement. Severity = estimated blast radius under real data.

**Totals:** ~11 Critical · ~26 High · ~37 Medium · ~41 Low.

---

## The headline: the bug we fixed still lives on another page

`DispatchPendingBiltyGRPOListAPI` is an **exact, unfixed clone** of the hang we just fixed in `PendingServiceGRPOListAPI`. It loops over every pending dispatch plan and calls `_get_dispatch_bill_snapshot(plan)` — one live, whole-table SAP read per row.

- File: `dispatch_plans/views.py:528-560`
- Fix is a **one-liner**: the batch method already exists. Replace the per-row call with `service.get_dispatch_bill_snapshots(dispatch_plans)` before the loop and index `bill_snapshots.get(plan.id, {})` — identical to the fix already shipped in `grpo/views.py:436`.

Two more instances of the same `_get_dispatch_bill_snapshot`-per-plan N+1 survive in the **service-GRPO preview and post** flows (smaller N — invoices sharing one bilty — but the post one runs inside `@transaction.atomic`, holding a DB transaction open across N slow SAP reads):
- `grpo/services.py:1842` + `:1863-1864` (preview)
- `grpo/services.py:2230` + `:2253` (post, inside transaction)

---

## Cross-cutting themes (fix the pattern, not just the instance)

1. **N+1 live SAP reads in per-row loops** — the exact bug class. Survives in `DispatchPendingBiltyGRPOListAPI` and the two service-GRPO group loops above.
2. **DRF `SerializerMethodField` doing `.filter()/.count()/.exists()/.aggregate()` per row** — defeats `prefetch_related` and is the most pervasive hidden N+1 (GRPO history/all-entries, sales-dispatch gate-outs, maintenance work-orders, QC inspections, production-QC).
3. **SAP paths that bypass the shared `HanaConnection`** (which now has the timeouts) — `production_execution/services/sap_reader.py` and two `barcode/services/*` open raw `dbapi.connect()` with **no timeout** → the original "hang forever" failure mode, still live.
4. **Connection-per-query + per-request `SYS.TABLE_COLUMNS` introspection** — every multi-query SAP endpoint pays N TCP/auth handshakes and re-introspects column metadata each request (the metadata is static).
5. **No global DRF pagination** (`config/settings.py` has no `DEFAULT_PAGINATION_CLASS`) — list endpoints return whole tables, multiplying every per-row N+1.
6. **Whole-table SAP fetch then filter/paginate in Python** — WMS stock-overview & dashboard.
7. **Frontend: un-debounced search wired into SAP-backed query keys** — one heavy fetch per keystroke; plus eager fetching of hidden tabs / dialog-only data / full lists used only for a `.length` count.
8. **Frontend: overly-broad `invalidateQueries`** — one mutation refetches a whole module subtree.

**Already healthy (good baselines):** the `HanaConnection` timeout fix is correct; `grpo/services.py` SAP master-data readers are `@lru_cache`'d; the frontend global QueryClient config is sane (staleTime 5min, `refetchOnWindowFocus:false`, retry 1) and the axios client has a 30s timeout; polling is disciplined (all `refetchInterval` ≥30s, no SAP endpoint faster than 60s, pauses on hidden tabs).

---

## BACKEND — Critical

| # | Endpoint / path | File | Issue |
|---|---|---|---|
| B-C1 | `GET .../bilty-grpo/pending/` `DispatchPendingBiltyGRPOListAPI` | `dispatch_plans/views.py:528-560` | **Exact twin of the fixed hang** — `_get_dispatch_bill_snapshot` per plan. One-line fix (batch method exists). |
| B-C2 | Service GRPO preview | `grpo/services.py:1842,1863-1864` | `_get_dispatch_bill_snapshot` per group plan (primary bill fetched twice). |
| B-C3 | `POST /grpo/service/post/` | `grpo/services.py:2230,2253` | Same N+1 **inside `@transaction.atomic`** — holds DB txn across N SAP reads. |
| B-C4 | `GET .../wms/stock-overview` | `warehouse/services/wms_hana_reader.py:35-127` | No `LIMIT`; fetches entire OITM×OITW, maps + paginates in Python. "page_size=50" still transfers the whole catalog. |
| B-C5 | `GET .../wms/dashboard` | `warehouse/services/wms_hana_reader.py:393-491` | 6 sequential whole-table aggregations, each on a **fresh** HANA connection. |
| B-C6 | Production-order SAP reads | `production_execution/services/sap_reader.py:234-252` | Private `_execute` opens raw `dbapi.connect()` **with no timeout** (bypasses the timeout-hardened wrapper) + string-interpolated SQL (injection). |
| B-C7 | All notification fan-out | `notifications/services.py:241-257` | ~3 queries per recipient (pref SELECT + INSERT + device SELECT), unbatched; company broadcast = 3×headcount. System-wide blast radius. |
| B-C8 | Person gate-in lists (`entries/`, `entry/inside/`) | `person_gatein/views.py:108-202`; `serializers.py:45-52` | 3 nested FK serializers, no `select_related`, no pagination; `inside/` is polled. ~3N queries. |
| B-C9 | Vehicle entry lists (`vehicle-entries/`, `.../list-by-status/`) | `vehicle_management/views.py:150,307`; `serializers.py:146-151` | vehicle/driver/company lazy-loaded per row (no `select_related`) → ~5 queries/row on the busiest gate screens. |

## BACKEND — High (selected)

| Endpoint / path | File | Issue |
|---|---|---|
| `AllGRPOEntriesListAPI`, `PendingGRPOListAPI`, dashboard summary | `grpo/views.py:84-87,151-155`; `services.py:1019-1022` | `entry.grpo_postings.filter(status="POSTED")` **ignores the prefetch cache** → fresh query per entry, + `.values_list` per posting. Unpaginated. |
| `GET /gate/sales-dispatch/gate-outs/` | `gate_core/views_sales_dispatch.py:914`; `services/sales_dispatch_gatepass.py:45-120` | ~12 uncached queries/row in `get_gatepass_readiness`; `vehicle_entry__weighment` not `select_related`; unpaginated. |
| Dispatch bill lookup (barcode) | `dispatch_plans/hana_reader.py:288-303` | `_table_columns` cache is **instance-scoped** (new reader per request) → up to 4 `SYS.TABLE_COLUMNS` queries on the first call of every request. Promote to process-level `lru_cache`. |
| `GET /barcode/oitm-items`, `/barcode/production-release-oil` | `barcode/services/oitm_item_service.py:140`; `production_release_service.py:146` | Raw `dbapi.connect()` **without timeouts** — siblings of the timeout bug. Route through `HanaConnection`. |
| Connection-per-query (no pooling) | `wms_hana_reader.py`, `sap_reader.py`, several `sap_client/hana/*` | Endpoints issuing N queries do N connects. `service_grpo_options_reader.py` (one conn, 9 queries) is the model to copy. |
| Maintenance work-order list | `maintenance/serializers.py:653-660` | `.aggregate()` + `.filter(movement_type=…)` per work order, defeating the prefetch. Annotate instead. |
| QC inspection queues | `quality_control/serializers.py:516-518` | `get_parameter_results` → `.filter(is_active=True)` per inspection (prefetch bypassed). Use filtered `Prefetch`. |
| Production-QC lists | `quality_control/serializers.py:746-753` | 3× `.count()` per session row. Use conditional `Count(filter=Q())` annotations. |
| Raw-material PO view | `raw_material_gatein/views.py:48-79,604-609` | per-PO/per-item `.exists()` on un-prefetched GRPO reverse relations. |
| Service Layer re-login per call | `sap_client/service_layer/auth.py:9-21` + all writers | every SAP write does `POST /Login` then the op (≥2 round-trips); attachment flows re-auth multiple times. Cache the SL session. |

**Backend Medium/Low (14 / ~17):** GRPO history serializer N+1 (`grpo/serializers.py:664-687`); `get_grpo_preview_data` per-PO query (`services.py:1114`); stock-dashboard warehouse list re-queried per request; WMS billing/transfer/backlog Python grouping; `_get_branch_for_order` over-fetches a full production order for one field; cross-company SAP fan-out (bounded by company count); notification/QC bulk write loops; missing `select_related` on several detail endpoints; `verify=False` on all SL calls (hygiene). Full detail in auditor notes.

---

## FRONTEND — Critical

| # | Page | File | Issue |
|---|---|---|---|
| F-C1 | ProductionMovementDashboardPage | `dashboards/production-movement/api/production-movement.queries.ts:67-90` | Selecting one item fires `useQueries` with **8 concurrent SAP report calls at `limit:1000`**, then discards the rows for a 4-number summary. |
| F-C2 | ArrivalSlipPage (edit mode) | `gate/pages/rawMaterialPages/ArrivalSlipPage.tsx:163-214` | Serial `await arrivalSlipApi.get(id)` **per PO item** in a `useEffect` loop — true N+1, uncached, bypasses react-query. |

## FRONTEND — High (selected)

| Page | File | Issue |
|---|---|---|
| DispatchVehicleLinkingPage | `vehicle-management/pages/DispatchVehicleLinkingPage.tsx:48-67`; `api/dispatch-linking.api.ts:69-87` | SAP `getBills` pulls **365+90 days at `limit:2000`**, discards client-side; 30s staleTime + **un-debounced search** → full SAP read per keystroke. |
| BarcodeDispatchReportsPage | `barcode/pages/BarcodeDispatchReportsPage.tsx:537-549` | 4 heavy aggregation reports on mount (3 inactive tabs) + 8 un-debounced filter inputs feeding all of them. |
| BarcodeDispatchPage | `barcode/pages/BarcodeDispatchPage.tsx:1138-1140` | 3 session-list queries on mount (one tab visible) + un-debounced filters refetch all three. |
| RunDetailPage | `production/execution/pages/RunDetailPage.tsx:152-166`; `api/execution.queries.ts:300,603` | ~10 queries on mount (2 dialog-only), `useRunDetail staleTime:0`, every mutation invalidates `EXECUTION_QUERY_KEYS.all`. |
| ResourceTrackingPage | `production/execution/pages/ResourceTrackingPage.tsx:98-107` | 9 queries on mount, 6 for hidden tabs. |
| Maintenance (≈35 mutations) | `maintenance/api/maintenance.queries.ts:266-268` | `invalidateMaintenance` nukes the whole `['maintenance']` tree → one click = ~7 refetches. |
| MaintenanceWorkOrderDetailPage | `maintenance/pages/MaintenanceWorkOrderDetailPage.tsx:688-704` | 7 queries on mount; 2 full lists only needed in dialogs (gate with `enabled`). |
| SalesDispatchBarcodeScanPage | `gate/pages/customerSalesFlow/SalesDispatchBarcodeScanPage.tsx:104-117` | Dependent waterfall + re-fetches `box_scans` already embedded in the dispatch detail. |
| GateDashboardPage | `gate/pages/GateDashboardPage.tsx:253-316` | ~15 concurrent queries on mount, most pulling full lists just to compute counts. |
| Admin approval badges | `admin/api/dockingApproval.queries.ts:24-34`; `partialScanApproval.queries.ts:24-34` | Two always-mounted sidebar badges fetch the full pending list for a count, staleTime 15s, + broad invalidation. |
| WarehouseDashboardPage | `warehouse/pages/WarehouseDashboardPage.tsx:11-14` | 4 full-list fetches for 4 `.length` KPIs (one redundant superset call). |
| Warehouse SAP search pages | `StockTrackerPage.tsx`, `BatchExpiryPage.tsx`, `SalesOrderBacklogPage.tsx`, `TransferActivityPage.tsx` | Un-debounced search feeding SAP endpoints (`limit:300/500`). `BillingTrackerPage` (fetch-once + client filter) is the model to copy. |

**Frontend Medium/Low (≈23 / ≈24):** short staleTime overrides (15-30s) on SAP dashboards re-hammering on revisit (dispatch-pipeline, dispatch-plans, grpo polls, sales-dispatch board); full lists fetched for counts (barcode/grpo/transporter dashboards); broad barcode mutation invalidation; un-debounced search across many barcode list pages; `/accounts/me/` permission-refresh `setInterval` (every 5min, every page) not pausing on hidden tabs (`AuthInitializer.tsx:176-184`); no SW runtime caching for API GETs; per-request IndexedDB reads in the axios interceptor. Full detail in auditor notes.

---

## Recommended fix order

1. **B-C1** `DispatchPendingBiltyGRPOListAPI` — one-line swap to the existing batch method. Same hang, already-built fix. *(do first)*
2. **B-C2 / B-C3** service-GRPO preview & post loops — batch the group-plan snapshots (extend the batch to carry line-level fields preview needs).
3. **B-C6 + High** — route `production_execution/services/sap_reader.py` and the two `barcode` services through `HanaConnection` (timeouts) and parameterize their SQL. Closes the remaining no-timeout paths.
4. **Theme #2 (ORM N+1)** — fix the `SerializerMethodField` `.filter()/.count()/.aggregate()`-per-row cases (GRPO all-entries/history, sales-dispatch gate-outs, maintenance work-orders, QC) via prefetch-cache iteration or annotations.
5. **B-C4 / B-C5** — push pagination/aggregation into SQL for WMS stock-overview & dashboard; reuse one connection.
6. **B-C7 / B-C8 / B-C9** — batch notification fan-out (`bulk_create`); add `select_related` + pagination to person/vehicle gate lists.
7. **Process-level metadata cache** + **per-request connection reuse** for SAP readers (theme #4) — broad multiplier.
8. **Frontend** — F-C1/F-C2 first; then debounce SAP-backed search inputs and gate hidden-tab/dialog queries with `enabled`; scope the broad mutation invalidations.
9. Add a global DRF pagination default (theme #5).
