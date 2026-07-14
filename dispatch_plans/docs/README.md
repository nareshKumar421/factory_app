# Dispatch (Outbound Finished Goods) — Backend

Django app(s): **`dispatch_plans`** (heart of the module) plus the **`barcode`**
standalone dispatch-scan subsystem, and heavy read/write boundaries into
**`gate_core`** (docking / empty-vehicle gate-in / vehicle arrival) and **`grpo`**
(freight service GRPO).

> Frontend companion doc: `C:/Users/gurpa/dev/FactoryFlow/docs/modules/dispatch.md`

---

## Overview — what it does & who uses it

The dispatch module tracks a finished-goods **A/R invoice** from the moment the
dispatch-planning team assigns a truck to it, through the physical gate → dock →
box-scan → gatepass → weigh-out → dispatch-out journey, and finally through the
**transporter freight billing** (bilty service GRPO → transporter A/P invoice).

The unit of work is a **`DispatchPlan`** — a local planning/booking overlay on one
SAP A/R invoice (identified by `sap_invoice_doc_entry`). The SAP invoice itself is
**never created here**; it already exists in SAP Business One. `dispatch_plans`
mirrors a snapshot of it and layers planning data (vehicle, driver, transporter,
bilty/LR, freight, scheduled dispatch date) on top.

Who uses it:
- **Dispatch planners** — review SAP bills, link vehicles, set booking status/date (`can_view_dispatch_plans`, `can_edit_dispatch_plans`, `can_link_dispatch_vehicle`).
- **Warehouse / gate operators** — the read-only dispatch **schedule** and the **docking** flow (docking lives in `gate_core`).
- **Dispatch supervisors** — the **Inside Vehicle Manager** correction console (`can_*_inside_vehicle`, endpoints in `gate_core`).
- **Finance / transporter-billing** — open bilties, bilty **service GRPO**, and **transporter A/P invoice** posting (`can_view_open_bilties`, `can_post_bilty_service_grpo`, `can_post_transporter_ap_invoice`).
- **Managers** — the read-only fulfilment **dashboard** and **pipeline** board.

The whole gate→dock→dispatch flow is **cross-company** (one physical factory serves
Jivo Oil / Beverages / Mart), but every SAP entity is per-company. See "Integrations".

---

## Key concepts & entities

### `DispatchPlan` (`models.py`)
One row per `(company, sap_invoice_doc_entry)` — enforced by a unique constraint.
- **`booking_status`** (`DispatchPlanStatus`): `PENDING` → `BOOKED` → `DISPATCHED`, or `CANCELLED`. `PENDING` = the SAP bill exists but no plan has been actively booked; `BOOKED` = a truck is assigned and it is committed to go; `DISPATCHED` = it physically left the gate.
- **Transport identity**: `vehicle`, `transporter`, `driver` FKs plus denormalised text copies (`vehicle_no`, `driver_name`, `transporter_gstin`, …) frozen for the gatepass/print.
- **`linked_vehicle_entry`** (FK → `driver_management.VehicleEntry`): the gate empty-vehicle entry this bill rides. Set when the empty-vehicle gate-in completes.
- **Bilty / freight**: `bilty_no`, `bilty_date`, `bilty_attachment`, `freight`, `total_freight`, `kanta_weight`.
- **Planning**: `dispatch_date` (SCHEDULED date — lags the real gate-out by 1–4 days), `priority`, free-text `location`.
- **SAP mirror**: `invoice_number`, `eway_bill`, `invoice_weight`, `invoice_amount`, `customer_code/name`, `place_of_supply`, `total_litres`, `product_variety`.

### Pipeline stages (computed, NOT stored) — `services.py`
A bill's live position is **derived on the fly** by `compute_pipeline_stage(plan)`;
there is no stage column. Order (`PIPELINE_STAGE_ORDER`):

`BOOKED → EMPTY_IN → READY_TO_DOCK → DOCKED → PHOTO_ATTACHED → READY_FOR_GATEPASS → GATEPASS_PRINTED → PRINT_COMMITTED → DISPATCHED` (+ terminal `REJECTED`).

Derivation rules (first match wins):
1. Representative `SalesDispatchGateOut` (docking) exists → its status maps 1:1 to a stage (`DOCKED`…`DISPATCHED`); a `REJECTED`/`CANCELLED` docking with no superseding active one → `REJECTED`.
2. Else if `linked_vehicle_entry` is `IN_PROGRESS` → `EMPTY_IN`; `COMPLETED` → `READY_TO_DOCK`.
3. Else → `BOOKED`.

`_pick_representative_gate_out` considers both the plan's **direct** dockings and
the dockings it rides as a **secondary bill** of a multi-bill load (via the
`documents` reverse FK). It returns the newest docking whose `is_active` **and**
whose status is one of the live `DOCKED…DISPATCHED` (`ACTIVE_DOCUMENT_STATUSES`);
**only when none qualifies** does it fall back to the most-recent docking regardless
of status. **Known gap:** reject/cancel only flips a docking's `status` to
`REJECTED`/`CANCELLED` and leaves its `dispatch_plan` link intact (`is_active` even
stays `True`), so a freed bill whose sole docking is terminal hits that fallback and
reads `REJECTED` (see Edge Cases). `aggregate_pipeline_status` rolls up a vehicle's
bills to the *least advanced active* stage.

### `TransporterAPInvoicePosting` (+ `Line`, `Attachment`) — `models.py`
The transporter's **freight A/P invoice**. Consolidates one or more **posted bilty
service GRPOs** (each GRPO is the freight for one bilty). Statuses:
`PENDING` → `POSTED` (to SAP) / `FAILED` / `CANCELLED`. Lines carry the GRPO
base-document references (`base_entry`/`base_line`) used to build the SAP A/P
service-invoice payload.

### Cross-app entities this module reads/writes
- **`EmptyVehicleGateIn` / `EmptyVehicleGateInCover`** (`gate_core`) — the empty-truck arrival; a *cover* is a snapshot of one BOOKED bill taken at gate-in time.
- **`VehicleArrival`** (`gate_core`) — one **physical truck trip** grouping the per-company gate-ins + dockings (a cross-company truck is ONE arrival, many per-company entities).
- **`SalesDispatchGateOut` / `SalesDispatchBoxScan` / `…Document` / `…Item`** (`gate_core.models.sales_dispatch`) — the **docking** record and its box scans. This is the real box-scan of the dispatch flow.
- **`ServiceGRPOPosting`** (`grpo`) — freight booked as a *service* GRPO.

### `barcode.DispatchSession` — separate standalone scan subsystem
`barcode/services/dispatch_service.py` + `barcode/models.py` define a **parallel**
box/pallet barcode-dispatch system keyed by `bill_number` (`DispatchSession`,
`DispatchSessionLine`, `DispatchScannedUnit`, `DispatchScanLog`, `DispatchSapSyncLog`).
It looks up the SAP bill through `DispatchPlansService`, scans boxes/pallets against
bill lines, and marks the session dispatched — but **does not write back to SAP**
(`update_dispatch_status` returns `NOT_CONFIGURED`; "SAP sync is disabled for barcode
dispatch"). It is **not wired into the dispatch FE module navigation** — the docking
box-scan used by the live flow is the `gate_core` `SalesDispatchBoxScan` one. Treat
`DispatchSession` as an independent/experimental subsystem with its own URLs under
`/api/v1/barcode/dispatch/...`.

---

## End-to-end flows

### Flow 1 — Plan & book a bill (happy path)
1. Planner opens **Plans** / **Vehicle Linking**. `DispatchBillListAPI` (`GET /api/v1/dispatch-plans/bills/`) calls `DispatchPlansService.get_bills()`, which reads SAP A/R invoices for the window via `HanaDispatchBillReader` and left-joins the local `DispatchPlan` for each (`_empty_plan` for un-booked ones). Each row carries a computed `pipeline_status`.
2. Planner PATCHes the plan: `DispatchPlanUpdateAPI` (`PATCH /api/v1/dispatch-plans/bills/<doc_entry>/plan/`) → `DispatchPlansService.update_plan()`. This `get_or_create`s the `DispatchPlan`, applies master data (`_apply_master_data` auto-fills transporter/driver text from the FK records), and typically sets `booking_status=BOOKED` + `vehicle`/`dispatch_date`.
3. `post_save` signal (`signals.py`) fires `notify_dispatch_plan_status` → notifies the `dispatch` auth group on `BOOKED`/`DISPATCHED` (on transaction commit).
4. **Batch linking variant**: if the payload includes `linked_invoice_doc_entries`, `update_linked_plans` applies shared transport data to several invoices at once and **allocates the freight across them** by weight/litres (`_allocate_batch_freight`). All must be the same SAP branch.

### Flow 2 — Empty vehicle in → dock → dispatch out (physical journey)
1. Truck arrives empty → **Empty Vehicle Gate-In** (`gate_core`). Completing it runs `record_dispatch_covers` (snapshots the vehicle's BOOKED + unlinked bills as covers, sets `DispatchPlan.linked_vehicle_entry`) and `replicate_dispatch_gate_in_across_companies` (creates sibling per-company gate-ins under one `VehicleArrival`).
2. Once the `VehicleEntry` is `COMPLETED`, the plan's **vehicle link is LOCKED** — `is_vehicle_link_locked=True`; `_assert_link_not_locked` rejects any change to vehicle/transporter/driver/linked_entry/booking_status.
3. **Docking** (`gate_core` `SalesDispatchGateOut`, one per company): create docking → **box scan** (`SalesDispatchBoxScan`) → attach truck photo → print gatepass → commit print → weigh-out → **mark dispatched**. Status walks `DOCKED → PHOTO_ATTACHED → READY_FOR_GATEPASS → GATEPASS_PRINTED → PRINT_COMMITTED → DISPATCHED`.
4. Marking a docking dispatched **consumes its covers** (`consume_covers_for_dispatched_plans` → `_retire_if_fully_consumed`) and sets `DispatchPlan.booking_status=DISPATCHED`. A multi-company truck dispatches all its companies' dockings collectively via the `VehicleArrival`, then **departs** (arrival auto-closes when every gate-in is retired).
5. The **Pipeline board** (`DispatchPipelineView`) reflects each bill's stage live — one Postgres query, no SAP.

### Flow 3 — Read-only schedule (warehouse)
`DispatchScheduleListAPI` (`GET /api/v1/dispatch-plans/schedule/`) lists plans with a
`dispatch_date`, enriched with one SAP query (`get_schedule_enrichment`: item
summary, source warehouse(s), box/litre/weight totals) so the warehouse knows what
to issue and from where. If SAP is down it still loads with `sap_available:false`
and blank item fields. Line items load on demand (`DispatchScheduleItemsAPI`).

### Flow 4 — Transporter freight billing (post-dispatch)
1. **Bilty service GRPO**: the freight for a bilty is posted as a *service* GRPO. `DispatchPendingBiltyGRPOListAPI` lists BOOKED plans still needing a GRPO; preview/post go through `GRPOService` (`bilty-grpo/preview`, `bilty-grpo/post`). This is a thin dispatch-facing facade over the `grpo` app.
2. **Open bilties**: `OpenBiltyListAPI` → `DispatchInvoiceService.get_open_bilties()` lists posted service GRPOs not yet on a transporter A/P invoice (joins SAP `OPDN`/`PDN1` to confirm lines are un-invoiced).
3. **Transporter A/P invoice**: `TransporterAPInvoiceSubmitAPI` / `…PostAPI` → `DispatchInvoiceService`. Two-step: `submit_ap_invoice` (validate + persist a `PENDING` posting with lines + attachment) then `post_submitted_ap_invoice` (upload attachments to SAP, build the A/P service-invoice payload with `BaseType 20` GRPO lines, call `SAPClient.create_ap_invoice`). `post_ap_invoice` does both in one call.

---

## Critical business rules & invariants

- **One plan per invoice per company** — `UniqueConstraint(company, sap_invoice_doc_entry)`.
- **Company resolution (rule D2)** — reads opt into cross-company via `?all_companies=1` (`wants_all_companies` → `user_company_ids`); **writes resolve the company from the target record**, not the `Company-Code` header. The header + `HasCompanyContext` only gate access. A write under the wrong company's HANA schema 404s the SAP invoice.
- **Link lock after gate-in** — once the linked `VehicleEntry.status == COMPLETED`, `LINK_LOCK_GUARDED_FIELDS` (`vehicle_id`, `transporter_id`, `driver_id`, `linked_vehicle_entry_id`, `booking_status`) are frozen (`_assert_link_not_locked`). Bilty/freight/remarks stay editable.
- **No fresh bill onto an inside truck from the linking board** — `_assert_bill_not_added_to_inside_vehicle` blocks booking a *new* bill onto a vehicle that already has a live (COMPLETED, non-retired) DISPATCH gate-in. Such bills must be added through the **Inside Vehicle Manager** ("Add Bills to Inside Vehicle"), never silently from linking.
- **Late-bill auto-link** — `_link_completed_empty_in` links a just-booked bill to a live gate-in **only if that gate-in already holds an unconsumed cover for exactly this bill on this vehicle**, then folds it into the truck's open docking (`_merge_into_open_docking`, best-effort). A bill the gate-in never covered does **not** auto-attach.
- **Vehicle arrival must auto-close on full dispatch and never be reused when stale** (see Edge Cases + the `vehicle-arrival-lifecycle` operational memory). A cross-company arrival departs only when ALL its gate-ins are retired.
- **A/P invoice guards** (`DispatchInvoiceService`): all selected GRPOs same **vendor** + same **SAP branch**; invoice amount must match the selected GRPO total within **INR 1.00** (`AMOUNT_TOLERANCE`); at least one **attachment** required; **duplicate guard** on `(vendor, invoice_number)` both locally and in SAP `OPCH`; only `PENDING`/`FAILED` postings can be (re)posted to SAP.
- **Gatepass numbering** — `SalesDispatchGatepassSequence.next_gatepass_no` issues `DCK/<COMPANY>/<FY>/<000001>` under a `select_for_update` per company + financial year.

---

## Integrations & cross-module boundaries

- **SAP Business One (per-company HANA schema)**:
  - *Reads* — `HanaDispatchBillReader` (`hana_reader.py`) for A/R invoice bills, lines, and schedule enrichment; `DispatchInvoiceService._fetch_sap_grpo_lines` joins `OPDN`/`PDN1`/`PCH1`/`OPCH` for open-bilty and A/P preview. Each company is its own schema (`CompanyContext` / `sap_client`), so cross-company reads **fan out per schema and merge** (`DispatchBillListAPI._get_bills_all_companies`).
  - *Writes* — Service GRPO (`grpo`) and the transporter A/P **service** invoice via the SAP **Service Layer** (`SAPClient.create_ap_invoice`), plus attachment upload. Dev/test bypass flags: `DISPATCH_SIMULATE_AP_INVOICE_POSTING`, `DISPATCH_USE_LOCAL_GRPO_LINES_FOR_TESTING`.
  - SAP rejections raise `SAPValidationError` (surfaced as `(2000xx)` messages from SAP's own `SBO_SP_TransactionNotification` — not our validation).
- **`gate_core`** — downstream owner of the physical truck lifecycle: empty-vehicle gate-in + covers, `VehicleArrival`, docking (`SalesDispatchGateOut`) and box scans, the Inside Vehicle Manager endpoints, and the `empty_vehicle_dispatch.py` services (`record_dispatch_covers`, `replicate_dispatch_gate_in_across_companies`, `_retire_if_fully_consumed`, `_depart_arrival_if_complete`). `dispatch_plans` **reads** these for pipeline stages and is **written by** them (booking_status → DISPATCHED, `linked_vehicle_entry`).
- **`grpo`** — the actual GRPO posting engine; `dispatch_plans` hosts the bilty-facing facade + permissions and the transporter A/P invoice that consumes posted GRPOs.
- **`notifications`** — `notify_dispatch_plan_status` pushes to the `dispatch` group on BOOKED/DISPATCHED.

---

## Real-world edge cases

Each: **trigger → current behaviour → operator-visible symptom → risk/gap.**

1. **Partial / staggered truck load** — a gate-in snapshots 2 bills; one dispatches and the truck weighs out, the other stays a never-loaded `BOOKED` phantom. → The gate-in can't reach "all covers consumed" so `_retire_if_fully_consumed` never retires it and the arrival stays `LOADING`. → Truck shows **"inside since …"**; Expected Dispatch says *"Already inside — can't start"*. → **Gap:** no auto-release of the un-loaded cover on physical departure (can't blindly auto-release — staggered dispatch is legitimate). Manual unstick needed. (`stuck-inside-truck-root-causes` #2.)

2. **Stale vehicle arrival reused** — a daily truck returns; its new gate-in matches an old open `INSIDE/LOADING` arrival (arrivals are matched by vehicle). → New gate-in glues onto yesterday's un-departed arrival. → Truck reads **"at dock / inside since yesterday"**; only a manual empty-vehicle-out frees it. → Largely fixed (auto-depart on last-gate-in retire, commit `edbf023`), but reuse-by-vehicle-only remains a latent risk. (`vehicle-arrival-lifecycle`.)

3. **Bill booked AFTER gate-in** — planner books a bill to a truck that is already inside. → No cover, no `linked_vehicle_entry`; the linking board rejects it via `_assert_bill_not_added_to_inside_vehicle` ("… is already inside … Add bills from 'Add Bills to Inside Vehicle'"). → Operator sees the bill **stuck in Expected Dispatch / "can't start"**. → **Recovery:** Inside Vehicle Manager add-bill (`record_dispatch_covers` + `replicate_dispatch_gate_in_across_companies`) attaches it to the live arrival; or `_link_completed_empty_in` auto-links if a matching cover already exists. (`late-booked-bills-attach-to-inside-truck`.)

4. **PRINT_COMMITTED stall / missing weigh-out** — a docking prints & commits the gatepass but is never weighed out and marked dispatched (`gross=None`). → Its cover stays unconsumed → gate-in stuck. → Bill sits at **"Print Committed"** on the pipeline; truck stays inside. → **Gap:** no "finish or abandon" nudge for a committed-but-not-dispatched docking. (`stuck-inside-truck-root-causes` #3.)

5. **Freed bill reads REJECTED instead of BOOKED** — a docking is rejected/cancelled (`SalesDispatchRejectView`/`SalesDispatchCancelView` set `status=REJECTED`/`CANCELLED`) but its `dispatch_plan` FK is **never nulled** (and `is_active` stays `True`). → With no *active* docking left, `_pick_representative_gate_out` falls back to that terminal docking → `compute_pipeline_stage` returns `REJECTED`. → Bill shows **"Rejected / Cancelled"** on the pipeline though it is really re-bookable. → **Gap:** the picker already filters `is_active`+status; the missing fix is to null the plan/document `dispatch_plan` link on reject/cancel (or let the fallback skip terminal dockings).

6. **SAP down during bill list / lookup** — HANA unreachable. → `DispatchBillListAPI` / `DispatchBillByNumberAPI` return **503** ("SAP system is currently unavailable"); `SAPDataError` → **502**. The read-only **schedule** degrades gracefully (`sap_available:false`, blank item columns). The **pipeline** is unaffected (Postgres only). → Symptom: Plans page shows an SAP-unavailable banner; schedule loads without item detail.

7. **SAP rejects the transporter A/P invoice** — `SAPClient.create_ap_invoice` raises `SAPValidationError`/`SAPDataError`. → `post_submitted_ap_invoice` catches it, marks the posting `FAILED` with `error_message`, and re-raises. → Operator sees the invoice under the **Failed** count with the SAP reason; can **retry** from the pending queue (only `PENDING`/`FAILED` are re-postable). → Attachments already uploaded are re-linked on the retry.

8. **A/P amount mismatch / duplicate** — the entered invoice amount differs from the selected GRPO total by > INR 1.00, or the `(vendor, invoice_number)` already exists locally or in SAP `OPCH`. → `submit_ap_invoice`/`post_submitted_ap_invoice` raise `ValueError` → **400** before anything posts. → Operator sees "amount does not match … within INR 1.00" or "already been submitted/posted".

9. **Cross-company blank data** — a read endpoint is hit without `?all_companies=1` while the active `Company-Code` is a sibling company. → The queryset filters to the header company only. → A truck/bill visible in one company shows **blank/missing** in another. → Mitigated where flows send `all_companies` (Plans, pipeline expected section) and keyed in the React-Query key; still the classic root cause of "blank in sibling company". (`cross-company-flow-boundary`.)

10. **Bilty / Service-GRPO N+1 (performance)** — `DispatchPendingBiltyGRPOListAPI.get` (`views.py:~542`) loops over pending plans calling `GRPOService._get_dispatch_bill_snapshot(plan)`, and **each call does a live SAP `get_bill_by_number` HANA read**. → The Pending Bilty GRPO page issues one slow SAP round-trip **per row**. → Symptom: the queue is **slow to load and hammers SAP** as the backlog grows. → **Fix available, unused:** `GRPOService.get_dispatch_bill_snapshots()` (batch, one HANA query) already exists — swap the loop for the batch call. (Audit item **B-C1**; twin of the fixed Service-GRPO hang.)

11. **Re-scanned / already-dispatched box (barcode subsystem)** — an operator re-scans a box in a `DispatchSession`. → `_scan_box` rejects with `BOX_ALREADY_SCANNED`; a box already dispatched → `BOX_ALREADY_DISPATCHED`; duplicate submits are de-duped by `request_id`. → Operator sees a rejected-scan message; the rejection is logged (`DispatchScanLog`, `rejected_scan_report`). (Note: this is the standalone `barcode` scanner, not the live docking scan.)

---

## Failure modes / what can break

| Failure | Server behaviour | Operator/manager sees |
|---|---|---|
| SAP HANA unreachable | `503` on bill list/lookup/GRPO options; schedule → `sap_available:false` | "SAP system is currently unavailable" banner; schedule item columns blank |
| SAP data/query error | `502` (`SAPDataError`) | "SAP data error: …" |
| SAP rejects A/P invoice | posting → `FAILED` + `error_message`, re-raised | Red **Failed** count; SAP reason on detail; retry from queue |
| Edit a locked plan | `400` from `_assert_link_not_locked` | "Vehicle linking is locked: the empty vehicle gate-in is already completed…" |
| Book a bill onto an inside truck | `400` from `_assert_bill_not_added_to_inside_vehicle` | "<vehicle> is already inside (gate-in …). Add bills from 'Add Bills to Inside Vehicle'…" |
| Truck stuck inside (cases 1/4) | gate-in never retires; arrival stays `LOADING` | Truck listed in Inside Vehicle Manager; Expected Dispatch "can't start" |
| Duplicate transporter invoice | `400` (local or SAP `OPCH` guard) | "already been submitted/posted" |
| Pending Bilty GRPO backlog grows | N+1 live SAP reads (case 10) | Slow page; SAP load spikes |
| Phantom BOOKED bills | re-snapshot as covers on every gate-in | Bills silently re-ride each trip; extra orphan covers |

---

## Improvement opportunities & known gaps

- **Apply the B-C1 batch fix** — swap `_get_dispatch_bill_snapshot` in the loop for `get_dispatch_bill_snapshots()` in `DispatchPendingBiltyGRPOListAPI`.
- **Detach the plan link on reject/cancel** — `_pick_representative_gate_out` (`services.py` `~L46`) already filters `is_active`+status; the freed-bill-reads-`REJECTED` gap comes from `SalesDispatchRejectView`/`CancelView` not nulling the docking's `dispatch_plan` FK, so the picker's fallback returns the terminal docking. Null the link on reject/cancel, or skip terminal dockings in the fallback.
- **Partial-trip auto-close & abandoned-`PRINT_COMMITTED` sweep** — close the two remaining "stuck inside" gaps without breaking legitimate staggered dispatch.
- **Phantom-booking hygiene** — operationally, planners must cancel dead bookings or actually dispatch them; code can't decide which. Consider surfacing "BOOKED, never docked, N trips" as an alert.
- **Arrival reuse by vehicle only** — tightening reuse to require ≥1 live gate-in (partial unique constraints noted as deploy-ready in `vehicle-arrival-lifecycle`).
- **No global DRF pagination** on the dispatch list endpoints — large windows serialize everything.

---

## Permissions & roles

Defined on `DispatchPlan.Meta.permissions` and `TransporterAPInvoicePosting.Meta.permissions`
(app label `dispatch_plans`); enforced by `permissions.py`.

| Permission | Grants | Enforced by |
|---|---|---|
| `can_view_dispatch_plans` | View Plans dashboard / bills | `CanViewDispatchPlansOrLinkDispatchVehicle`, dashboard views |
| `can_edit_dispatch_plans` | Edit bookings | `CanEditDispatchPlans(OrLink)` |
| `can_link_dispatch_vehicle` | Link vehicles (also implies edit for linking) | `CanEditDispatchPlansOrLinkDispatchVehicle` |
| `can_view_dispatch_schedule` | Read-only warehouse schedule | `CanViewDispatchSchedule` |
| `can_view_dispatch_pipeline` | Pipeline board | `CanViewDispatchPipeline` |
| `can_view_inside_vehicle_manager` + `can_add/remove/move/unlink_inside_vehicle` | Inside Vehicle Manager (per-action; endpoints in `gate_core`) | `CanViewInsideVehicleManager`, `CanAddBillInsideVehicle`, etc. |
| `can_mark_out_inside_vehicle` | Hides the Inside-Vehicle **mark-out** button (frontend-only gate) | **No view enforces it** — declared on `DispatchPlan.Meta`, but mark-out runs through the empty-vehicle-out / arrival endpoints (auth + scope only) |
| `can_view_open_bilties` | Open bilties list | `CanViewOpenBiltiesOrPostTransporterAPInvoice` |
| `can_post_bilty_service_grpo` | Bilty service GRPO queue/preview/post/history/detail (OR'd with `grpo.*`) | `CanViewBiltyServiceGRPO*` |
| `can_view_transporter_ap_invoice` / `can_post_transporter_ap_invoice` | View / post transporter A/P invoices | `CanViewTransporterAPInvoice`, `CanPostTransporterAPInvoice` |

Docking actions use `gate_core.can_*_sales_dispatch_out` (view/create/edit/print/commit/reject/cancel/dispatch/reprint/reports/manage-lock). All views also require `IsAuthenticated` + `HasCompanyContext`. The whole module is gated cross-company by `Company-Code` header membership.

---

## Developer file map

### Backend (`C:/Users/gurpa/dev/factory_app/`)
- `dispatch_plans/models.py` — `DispatchPlan`, `DispatchPlanStatus`, `TransporterAPInvoicePosting/Line/Attachment`, permissions.
- `dispatch_plans/services.py` — `DispatchPlansService` (bill fetch/merge, plan update, master-data fill, link lock, late-link, batch freight); pipeline stage engine (`compute_pipeline_stage`, `pipeline_gate_out_prefetch`, `aggregate_pipeline_status`).
- `dispatch_plans/views.py` — bill list (+cross-company merge), bill-by-number, plan update, pipeline, schedule + items, bilty service-GRPO facade, transporter A/P invoice endpoints.
- `dispatch_plans/invoice_services.py` — `DispatchInvoiceService` (open bilties, A/P preview/submit/post, SAP `OPDN/PDN1/PCH1/OPCH` reads, Service-Layer post).
- `dispatch_plans/hana_reader.py` — `HanaDispatchBillReader` (SAP A/R invoice reads).
- `dispatch_plans/dashboard_service.py` / `dashboard_views.py` — read-only fulfilment dashboard (Dispatched vs Billed vs Backlog; bill-wise drill-down).
- `dispatch_plans/serializers.py`, `permissions.py`, `signals.py`, `notifications.py`.
- `dispatch_plans/urls.py` (`/api/v1/dispatch-plans/`) + `dispatch_plans/dispatch_urls.py` (`/api/v1/dispatch/`); mounted in `config/urls.py`.
- `barcode/services/dispatch_service.py`, `barcode/models.py` (`DispatchSession…`), `barcode/views.py`, `barcode/urls.py` (`/api/v1/barcode/dispatch/…`) — standalone barcode-dispatch subsystem.
- `gate_core/models/sales_dispatch.py`, `gate_core/services/empty_vehicle_dispatch.py`, `gate_core/services/sales_dispatch_docking.py`, `gate_core/views_sales_dispatch.py` — docking / gate-in / arrival (downstream boundary).
- `grpo/services.py` — `GRPOService` (Service GRPO engine; `_get_dispatch_bill_snapshot` / `get_dispatch_bill_snapshots`).

### Frontend (key files — see the companion doc)
- `src/modules/dispatch/module.config.tsx`, `api/dispatch.api.ts`, `api/dispatch.queries.ts`, `pages/*`.
- Cross-module screens: `src/modules/dashboards/dispatch-plans/…`, `src/modules/vehicle-management/pages/{DispatchVehicleLinkingPage,InsideVehicleManagerPage}.tsx`, `src/modules/gate/pages/customerSalesFlow/…`, `src/modules/warehouse/grpo/…`.

---

## Related docs
- **Frontend companion**: `C:/Users/gurpa/dev/FactoryFlow/docs/modules/dispatch.md`
- Docking / gate flow: `C:/Users/gurpa/dev/FactoryFlow/docs/modules/gate.md`
- Service GRPO: `C:/Users/gurpa/dev/FactoryFlow/docs/modules/grpo.md`
- Barcode dispatch subsystem (the standalone `DispatchSession`): `C:/Users/gurpa/dev/FactoryFlow/docs/modules/barcode-dispatch-design.md`, `barcode.md`
- Operational memories (point-in-time, verify against code): `vehicle-arrival-lifecycle`, `late-booked-bills-attach-to-inside-truck`, `stuck-inside-truck-root-causes`, `cross-company-flow-boundary`, `performance-audit-backlog`.
