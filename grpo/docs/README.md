# GRPO (SAP Goods-Receipt Posting) — Backend

> Django app: `grpo` · Base URL: `/api/v1/grpo/` (mounted in `config/urls.py`)
> · Service layer: `grpo/services.py` (`GRPOService`)
>
> Paired frontend doc: `C:/Users/gurpa/dev/FactoryFlow/docs/modules/grpo.md`

This doc describes the code as it is **today** (verified against `models.py`,
`services.py`, `views.py`, `serializers.py`, `permissions.py`, `urls.py`,
`signals.py`, `notifications.py`). Older files in this folder (`api.md`,
`models.md`, `workflow.md`, `frontend_guide.md`, the `fev*/fv3` notes) are
partially stale — trust the code and this README first.

---

## Overview — what it does & who uses it

The GRPO app posts two kinds of SAP Business One **Purchase Delivery Note**
(`PurchaseDeliveryNotes`) document, both created through the SAP Service Layer:

1. **Material GRPO** — the goods receipt for a completed raw-material gate entry.
   After a truck is received and QC accepts the material, a warehouse / stores
   operator posts an *item-type* GRPO that links back to the original SAP
   Purchase Order(s). Endpoints live under `/api/v1/grpo/`.

2. **Service GRPO** (a.k.a. **Bilty GRPO** / transport freight GRPO) — a
   *service-type* delivery note (`DocType: dDocument_Service`) booked against a
   transporter for a dispatched sale. It records the freight cost for a bilty
   (lorry receipt). The models and all logic live in **this** app, but the
   endpoints the frontend actually calls are exposed by the `dispatch_plans`
   app under `/api/v1/dispatch/bilty-grpo/` (see *Integrations*).

Primary users: raw-material **stores / GRPO operators** (material) and
**dispatch / logistics accounts** users (service). Both flows finish inside SAP,
so the app is really a guarded, audited bridge between factory-floor events and
SAP posting.

---

## Key concepts & entities

Domain models (`grpo/models.py`):

| Model | Purpose | Key fields |
|---|---|---|
| `GRPOPosting` | One material GRPO attempt for a gate entry. | `vehicle_entry` (PROTECT), `po_receipt` (legacy single FK, nullable), `po_receipts` (M2M — the real link, supports **merged** GRPOs), `sap_doc_entry/num/total`, `status`, `error_message`, `posted_at/by` |
| `GRPOLinePosting` | One posted line, tied to a `POItemReceipt`. | `quantity_posted`, `base_entry`/`base_line` (the PO DocEntry/line it consumed in SAP) |
| `GRPOAttachment` | A file linked to a material GRPO. | `file`, `original_filename`, `sap_attachment_status`, `sap_absolute_entry`, `sap_error_message` |
| `ServiceGRPOPosting` | One service/bilty GRPO for a `DispatchPlan`. | `dispatch_plan` (PROTECT), `vendor_code` (transporter BP), SAP dimension fields (`place_of_supply`, `effective_month`, `budget_delivery_point`, `sub_account`, `location_code`, `sac_*`, `product_variety`, `total_litres`), `sap_doc_*`, `status` |
| `ServiceGRPOLinePosting` | One freight line (one per invoice/dispatch plan in a bilty group). | `dispatch_plan`, `amount`, `unit_price`, `tax_code`, dimension columns, `total_litres` |
| `ServiceGRPOAttachment` | A file linked to a service GRPO (e.g. the bilty scan). | same shape as `GRPOAttachment` |

Enums (`grpo/models.py`):

- `GRPOStatus`: `PENDING`, `POSTED`, `FAILED`, `PARTIALLY_POSTED`.
- `SAPAttachmentStatus`: `PENDING`, `UPLOADED`, `LINKED`, `FAILED`.

Cross-app records the app reads (never owns):

- `VehicleEntry` (`driver_management`) — the gate entry; `entry_type="RAW_MATERIAL"`.
- `POReceipt` / `POItemReceipt` (`raw_material_gatein`) — the "bill" and its lines; each carries `supplier_code`, `branch_id`, `sap_doc_entry`, `sap_line_num`.
- `RawMaterialInspection` / arrival slip (`quality_control`) — the QC verdict per item.
- `DispatchPlan` (`dispatch_plans`) — the booked sale/bilty for service GRPO.
- `Weighment` (`weighment`) — updated when a tare weight is captured at GRPO.

**Bill = one `POReceipt`.** A single truck (`VehicleEntry`) can carry several
bills from several suppliers.

**Merged GRPO.** Several `POReceipt`s from the **same supplier and same branch**
on one truck can be posted as a **single** SAP document. Each SAP line still
references its own PO via `BaseType=22, BaseEntry, BaseLine`.

**Per-bill QC readiness.** `QC_GRPO_READY_STATUSES = {ACCEPTED}`. A bill is
GRPO-ready only when *every inspected item on it* has passed QC. Readiness is
evaluated **per bill, not per truck**, so a rejected/held/pending bill blocks
only itself — the other bills on the same truck can still be posted. Legacy
gate entries with no arrival slip at all are allowed through (`arrival_slip`
absent ⇒ not blocked — see `_get_qc_blocking_reason`).

---

## End-to-end flows

### Flow A — Material GRPO (happy path)

1. **Gate + QC complete.** `VehicleEntry.status` reaches `COMPLETED` or
   `QC_COMPLETED`, and at least one bill's items are all `ACCEPTED`.
2. **Pending list.** `GET /pending/` → `GRPOService.get_pending_grpo_entries()`
   returns trucks with ≥1 ready-and-unposted bill, grouped by supplier for merge
   selection. `GET /all-entries/` shows *every* non-cancelled RAW_MATERIAL entry
   (gate / QC / done) with a `phase` + `status_label` and a read-only QC
   drill-down (`get_entry_qc_breakdown`). `GET /summary/` powers the dashboard
   tiles (`get_grpo_dashboard_summary`).
3. **Preview.** `GET /preview/<vehicle_entry_id>/` (optional
   `?po_receipt_ids=1,2`) → `get_grpo_preview_data()` returns each bill, its
   items with QC status, and SAP-prefilled fields (`unit_price`, `tax_code`,
   `warehouse_code`, `gl_account`, `variety`, `sap_line_num`, `branch_id`,
   `po_date` lazily fetched from SAP `OPOR.DocDate` via `resolve_po_date`).
4. **Post.** `POST /post/` (multipart) → `GRPOService.post_grpo()` inside one
   `@transaction.atomic`:
   - Validates the selected bills share one supplier + one branch, the entry is
     COMPLETED/QC_COMPLETED, and every selected item passed QC.
   - Persists edited `accepted_qty`/`rejected_qty` onto `POItemReceipt`
     (`rejected = max(received - accepted, 0)`).
   - Optionally updates the `Weighment` tare weight (with `select_for_update`).
   - Uploads each attachment to SAP `Attachments2` **first**, capturing an
     `AbsoluteEntry`, and puts `AttachmentEntry` on the GRPO payload.
   - Builds `DocumentLines` (one per accepted item, each with its
     `BaseEntry/BaseLine/BaseType=22`), `Comments` (structured, truncated to 254),
     optional `DocumentAdditionalExpenses` (extra charges), optional `RoundDif`.
   - Calls `SAPClient.create_grpo()`. On success writes `sap_doc_*`, sets status
     `POSTED`, and creates `GRPOLinePosting` + `GRPOAttachment` (`LINKED`) rows.
5. **Notify.** A `post_save` signal on the `→POSTED` transition queues a
   "GRPO Posted to SAP" notification to the `grpo` group (after commit).
6. **History.** `GET /history/` and `GET /<posting_id>/`.

### Flow B — Material GRPO failure & retry

- Any SAP error inside `post_grpo` rolls back the atomic block, so the FAILED
  row written there **does not survive**. The **view** (`PostGRPOAPI`) instead
  calls `record_material_grpo_failure()` from its exception handlers, which
  commits a separate `FAILED` `GRPOPosting` so the attempt shows in
  **History → Failed**, and fires `notify_material_grpo_failed`.
- The operator fixes the cause and re-posts. A later `POSTED` posting for the
  same PO(s) marks the old failure **superseded** (`is_failure_superseded`),
  which drops it from the Failed tab (still visible under All for audit).

### Flow C — Service / Bilty GRPO

1. A `DispatchPlan` transport booking is marked `BOOKED`.
2. `GET /dispatch/bilty-grpo/pending/` → `get_pending_service_grpo_entries()`
   lists booked plans not yet service-posted, **grouped by bilty** (`_service_group_key`:
   bilty no + date + vehicle + transporter). Bill headers are batch-fetched from
   HANA in one query (`get_dispatch_bill_snapshots`) — the fix for the old N+1 hang.
3. `GET /dispatch/bilty-grpo/preview/<dispatch_plan_id>/` → `get_service_grpo_preview_data()`
   builds defaults: freight amount, inferred product variety / service description,
   resolved SAP dimension codes, tax supply state, per-invoice line breakdown.
4. `POST /dispatch/bilty-grpo/post/` → `GRPOService.post_service_grpo()`
   (`@transaction.atomic`): resolves the bilty group, allocates freight across
   invoices by weight (`_allocate_amount_by_weight`), resolves the tax code
   (inter- vs intra-state, RCM — `_resolve_service_line_tax_code`), resolves
   active SAP dimensions (variety / effective-month / budget / state), drops
   UDFs the target company doesn't have (`_filter_purchase_delivery_note_udfs`),
   attaches the bilty scan by default, and creates a service-type delivery note.
   On success writes `sap_doc_*` and line/attachment rows.
5. Options for the form come from `GET /dispatch/bilty-grpo/options/`
   (`SAPClient.get_service_grpo_options()`).
6. **Bilty attachment corrections** — the vehicle-linking bilty on the plan is
   sometimes the wrong document. `GET/POST/DELETE
   /dispatch/bilty-grpo/attachment/<dispatch_plan_id>/`
   (`GRPOService.get_dispatch_bilty_attachment_state` /
   `replace_dispatch_bilty_attachment` / `delete_dispatch_bilty_attachment`)
   read, replace or detach `DispatchPlan.bilty_attachment` *before posting*;
   refused once the group's GRPO is POSTED. Every change — including
   vehicle-linking syncs — writes a `dispatch_plans.DispatchPlanAttachmentAudit`
   row, and replaced blobs stay in storage so the trail remains openable.

---

## Critical business rules & invariants

- **QC gate (per bill).** `post_grpo` calls `_validate_qc_ready_for_grpo` on the
  *selected* POs only. Any item that is `ARRIVAL_SLIP_PENDING`,
  `INSPECTION_PENDING`, `REJECTED`, `HOLD`, or `PENDING` raises `ValueError` and
  blocks that bill — never the siblings.
- **Entry status gate.** Posting requires `COMPLETED` or `QC_COMPLETED`.
- **One supplier + one branch per (merged) GRPO.** Mixed `supplier_code` or
  mixed non-null `branch_id` across selected POs → `ValueError`.
- **No double posting.** If any selected PO already has a `POSTED` `GRPOPosting`
  (via M2M or legacy FK), posting raises `ValueError` naming the SAP Doc Num.
- **Attachment required (material).** `PostGRPOAPI` returns 400 "At least one
  attachment is required for material GRPO" when no file is sent — even though
  `post_grpo` itself would accept zero attachments. The view enforces it.
- **Attachment-before-create.** SAP requires `AttachmentEntry` at document
  creation, so files go to `Attachments2` *before* `create_grpo`. Only a real
  multipart (binary) upload is acceptable — a JSON metadata-only entry produces
  an SAP row with no openable file (`allow_metadata_fallback=True` still tries a
  metadata fallback, documented as a known failure mode in `attachment_fix.md`).
- **Tare weight sanity.** If supplied it must be `> 0` and `≤ gross_weight`
  (when a gross exists); it updates the gate `Weighment` row and stamps
  `second_weighment_time`.
- **Company scoping.** `GRPOService(company_code=...)` is built from
  `request.company.company.code`. Every read filters `company__code`; writes
  resolve the company from the record. A wrong/missing company context yields
  **empty** lists, not another company's data.
- **Atomicity.** Both `post_grpo` and `post_service_grpo` are
  `@transaction.atomic` and re-raise, so a SAP failure leaves **no** partial
  local rows (the material Failed row is written by the view, outside the
  service's rolled-back block; the service route writes nothing).
- **Service GRPO uniqueness is per bilty group.** `post_service_grpo` refuses if
  any plan in the bilty group already has a `POSTED` service GRPO
  (`_posted_service_grpo_for_group`).
- **Service dimensions are mandatory.** Effective month must resolve to an active
  SAP dimension (dim 2); product variety must resolve to an active dimension
  distribution rule (dim 1) — otherwise `ValueError` (surfaces as 400). Amount
  and vendor code are also required and validated.

---

## Integrations & cross-module boundaries

- **SAP Service Layer** via `sap_client` (`SAPClient`). `grpo/services.py` uses
  `create_grpo`, `upload_attachment`, `add_line_to_existing_attachment`,
  `get_grpo_attachment_entry`, `link_attachment_to_grpo`,
  `get_service_grpo_options`, `get_po_date_by_doc_entry`.
- **SAP HANA (direct reads)** for master data used to shape the service payload:
  tax codes (`OSTC`), branch/BP states (`OBPL`, `OCRD`, `CRD1`), dimension
  distribution rules (`OOCR`), and `OPDN`/`PDN1` column metadata to filter UDFs.
  These reads are `lru_cache`d per company (module-level cache — restart to bust).
- **`dispatch_plans` app — the service GRPO front door.** The
  `DispatchBilty*GRPO*API` views in `dispatch_plans/views.py` are thin wrappers
  that instantiate **this app's** `GRPOService` and reuse **this app's**
  serializers. They are mounted at `/api/v1/dispatch/bilty-grpo/` and gated by
  the `dispatch_plans` bilty permission classes, which accept **any of**
  `dispatch_plans.can_post_bilty_service_grpo`, the matching grpo view perm, or
  `grpo.add_grpoposting` (`has_any_permission`, see `dispatch_plans/permissions.py`).
  The near-identical `service-grpo-*` views inside `grpo/urls.py` are the
  app-local equivalents (gated by grpo perms) and are **not** what the current
  frontend calls.
- **Upstream:** `raw_material_gatein` (bills/items), `driver_management`
  (vehicle entry), `quality_control` (QC verdict + inspection report),
  `weighment` (weights), `dispatch_plans` (bookings).
- **Downstream:** `notifications`
  (`NotificationService.send_notification_by_auth_group`, group `grpo`).

---

## API surface (`grpo/urls.py`, base `/api/v1/grpo/`)

| Method + path | View | Permission |
|---|---|---|
| `GET summary/` | `GRPODashboardSummaryAPI` | `can_view_pending_grpo` |
| `GET all-entries/` | `AllGRPOEntriesListAPI` | `can_view_pending_grpo` |
| `GET pending/` | `PendingGRPOListAPI` | `can_view_pending_grpo` |
| `GET preview/<vehicle_entry_id>/` | `GRPOPreviewAPI` | `can_preview_grpo` |
| `GET inspection-report/<arrival_slip_id>/` | `GRPOInspectionReportAPI` | `can_preview_grpo` |
| `POST post/` | `PostGRPOAPI` | `add_grpoposting` |
| `GET history/` | `GRPOPostingHistoryAPI` | `can_view_grpo_history` |
| `GET <posting_id>/` | `GRPOPostingDetailAPI` | `view_grpoposting` |
| `GET/POST <posting_id>/attachments/` | `GRPOAttachmentListCreateAPI` | `add_grpoattachment` |
| `DELETE <posting_id>/attachments/<id>/` | `GRPOAttachmentDeleteAPI` | `add_grpoattachment` |
| `POST <posting_id>/attachments/<id>/retry/` | `GRPOAttachmentRetryAPI` | `add_grpoattachment` |
| `service/*` (pending/options/preview/post/history/`<id>`) | app-local service views | grpo perms — **unused by UI** |

Every endpoint also requires `IsAuthenticated` + `HasCompanyContext`. The
UI-facing service endpoints are the `/api/v1/dispatch/bilty-grpo/*` set in
`dispatch_plans/dispatch_urls.py`.

---

## Real-world edge cases

Each: **trigger → current behaviour → operator-visible symptom → risk/gap.**

- **Partial truck / mixed QC verdicts.** One bill on a truck is rejected/held,
  others accepted → the accepted bills are postable; the blocked bill is listed
  separately and never blocks the rest; the truck still appears in Pending →
  operator sees "Awaiting QC — not ready to post" on the blocked bill → *risk:*
  a bill stuck in QC quietly never gets a GRPO if nobody resolves QC.
- **SAP rejects via `SBO_SP_TransactionNotification` (2000xx).** e.g. `(200032)`
  gross-weight mandatory for item group 105, `(200019)` "Please Attach its
  Receiving", inactive branch/item → `create_grpo` raises `SAPValidationError`
  → material returns **400** with the SAP message, writes a Failed row, and
  notifies → operator sees the SAP reason and a **Retry** in History → Failed →
  *these are SAP-side rules, not app bugs; fix the data and re-post.*
- **SAP Service Layer down mid-post.** `SAPConnectionError` → material returns
  **503** "SAP system is currently unavailable", records a Failed row, notifies
  → operator retries later. **Service** GRPO on the dispatch route returns 503 but
  records **nothing** and sends **no** notification (see Failure modes) → *gap:*
  a failed service post leaves no trace beyond the toast.
- **Attachment folder / Linux mount broken on SAP.** For material GRPO the
  attachment upload happens *before* `create_grpo`; if `Attachments2` errors the
  exception propagates, the whole transaction rolls back, and the GRPO **cannot
  be posted at all** → operator sees an SAP error, not a partial post → *risk:*
  posting is fully blocked until SAP Basis fixes the attachment-folder mount.
- **Re-scanned / duplicate post attempt.** Operator posts a PO that already has a
  `POSTED` GRPO → `ValueError` "GRPO already posted for PO … SAP Doc Num: …"
  (400) → safe, idempotent guard; no second SAP document is created.
- **Bill booked after gate-in.** A `POReceipt` added to the live entry after
  gate-in simply appears as another bill in preview once its items pass QC; the
  posted-PO set is recomputed from M2M + legacy FK every request → no stale
  "already posted" state.
- **Entry not yet completed / stale arrival.** Posting a truck still at gate/QC
  → `ValueError` "Gate entry is not completed. Current status: …" (400).
- **Missing weighbridge weight.** Tare not captured → tare is simply omitted
  (optional). If a tare is sent but gross is missing, tare is still saved (the
  `≤ gross` check only runs when a gross exists).
- **Cross-company blank data.** Operating under the wrong company context →
  Pending/Preview return empty because every query filters `company__code`
  (matches the known "blank in sibling company" class of bugs) → operator sees
  an empty list, not an error.
- **Service GRPO — bilty with multiple invoices.** Several dispatch plans share
  one bilty → they post as one service document with freight allocated across
  lines by weight; if **any** plan in the group is already posted, the whole
  group is blocked with the existing SAP Doc Num.
- **Service GRPO — unresolvable variety / effective month.** The item summary
  can't be mapped to an active SAP dimension → `ValueError` (400) "SAP Variety is
  required … could not resolve" or "Effective Month is not configured as an
  active SAP dimension" → operator must pick a valid variety/month.
- **UDF missing in target company.** A `U_*` field absent from that company's
  `OPDN`/`PDN1` is silently dropped before posting
  (`_filter_purchase_delivery_note_udfs`), preventing an SAP rejection at the
  cost of that field not being written.

---

## Failure modes / what can break

| Failure | Cause | Operator/manager sees |
|---|---|---|
| **SAP unavailable** | Service Layer / HANA down or unreachable | Material: 503 + Failed-tab row + notification. Service (dispatch route): 503 toast only. |
| **SAP validation reject** | Transaction-notification rule, inactive branch/item, over-receipt | 400 with the SAP message; material row lands in Failed with Retry. |
| **Attachment upload fails (material, at post time)** | SAP attachment folder/mount broken | Whole GRPO fails (upload precedes create); no document created. |
| **Attachment fails after posting** | Same, but during detail-page upload | Attachment saved locally as `FAILED` with `sap_error_message`; **Retry** available (`retry_attachment_upload`). The GRPO itself is fine. |
| **Service failure leaves no trail** | `DispatchBiltyServiceGRPOPostAPI` doesn't record a FAILED row and doesn't notify; the atomic block rolls back the row `post_service_grpo` wrote | Only an error toast; plan stays in the service pending queue. |
| **Slow service pending list** | Live HANA bill reads | Historically an N+1 hang; now batched via `get_dispatch_bill_snapshots`, but a slow HANA still slows the page. |
| **Lost notification** | `NotificationService` throws | Swallowed and logged (`signals.py` / view handlers); posting still succeeds. |

---

## Improvement opportunities & known gaps

- **Attachment permission mismatch (frontend vs backend).** The backend
  attachment endpoints gate on `grpo.add_grpoattachment` (`CanManageGRPOAttachments`),
  but the **frontend** gates its attachment upload/retry/delete controls on
  `grpo.can_manage_grpo_attachments` — a codename that **does not exist** in this
  app (no model `Meta.permissions` entry, no migration creates it). Net effect: a
  user who legitimately holds `add_grpoattachment` can upload via the API but sees
  **no** attachment controls in the UI (only a superuser passes the frontend
  check). Either add a real `can_manage_grpo_attachments` permission here or point
  the frontend constant at `grpo.add_grpoattachment`.
- **`PARTIALLY_POSTED` is defined but never set** by `post_grpo` (all-or-nothing
  per attempt). The status, supersede logic, and Failed-tab filter all handle it,
  but nothing produces it today.
- **Service GRPO has no persisted failure history** and no failure notification
  on the `/dispatch/bilty-grpo/` route — unlike material GRPO (and unlike the
  app-local `PostServiceGRPOAPI`, which *does* notify). A failed service post via
  the UI route is invisible after the toast disappears.
- **Two parallel service-GRPO API surfaces** (`grpo/urls.py service-*` vs
  `dispatch_plans` `bilty-grpo/*`) with **different permissions** for the same
  `GRPOService`. Only the dispatch one is wired to the UI; the grpo-app copy is
  drift risk.
- **Legacy single `po_receipt` FK** coexists with the `po_receipts` M2M; every
  read must check both. `migration 0011` backfilled the M2M; new code should
  prefer it.
- **Older docs in this folder are stale** (`workflow.md`, `models.md`,
  `frontend_guide.md`, `error_codes.md` list `po_receipt_id`-only payloads and a
  no-attachment path that the current view rejects).

---

## Permissions & roles

Custom + default Django permissions (`grpo/permissions.py`, `grpo/models.py Meta`):

| Permission | Guards |
|---|---|
| `grpo.can_view_pending_grpo` | dashboard summary, pending list, all-entries |
| `grpo.can_preview_grpo` | material preview, inspection report |
| `grpo.add_grpoposting` | **post** material GRPO (and is accepted by the dispatch service views) |
| `grpo.can_view_grpo_history` | material history list |
| `grpo.view_grpoposting` | single posting detail |
| `grpo.add_grpoattachment` | list / upload / delete / retry attachments |
| `dispatch_plans.can_post_bilty_service_grpo` | the **dispatch** service-GRPO views (post); view/preview/history variants also accept the matching grpo perm or `add_grpoposting` |

Custom permissions actually declared on `GRPOPosting.Meta` are
`can_view_pending_grpo`, `can_preview_grpo`, `can_view_grpo_history`; the rest are
Django defaults (`add_/view_grpoposting`, `add_grpoattachment`). The `grpo`
Django **group** is granted every `grpo` app permission by data migrations
(`0003`, `0005`, `0009`). Notifications target this group. Note the memory rule:
changing a group's perms can hide/show whole frontend modules — the sidebar gates
by permission, not group.

---

## Developer file map

**Backend (`C:/Users/gurpa/dev/factory_app/grpo/`)**

- `models.py` — the 6 models + `GRPOStatus`/`SAPAttachmentStatus` enums.
- `services.py` — `GRPOService`: all posting/preview/history logic (~2960 lines);
  material (`post_grpo`, `get_pending_grpo_entries`, `get_grpo_preview_data`,
  `get_grpo_dashboard_summary`, `record_material_grpo_failure`,
  `is_failure_superseded`, `upload_grpo_attachment`, `retry_attachment_upload`)
  and service (`post_service_grpo`, `get_pending_service_grpo_entries`,
  `get_service_grpo_preview_data`, `get_dispatch_bill_snapshots`, and the
  tax/dimension/UDF helpers).
- `views.py` — material APIs + the app-local service APIs.
- `serializers.py` — request/response + option serializers (shared by dispatch).
- `permissions.py` — the 6 permission classes.
- `urls.py` — `/api/v1/grpo/…` routes.
- `notifications.py` / `signals.py` — posted/failed notifications to the `grpo` group.
- `migrations/0003,0005,0009` — permission group setup; `0011` M2M backfill;
  `0012` service models; `0014` service GRPO sub-account.

**Cross-app (service GRPO front door & SAP)**

- `C:/Users/gurpa/dev/factory_app/dispatch_plans/views.py` — `DispatchBilty*GRPO*API` (reuse `GRPOService`).
- `C:/Users/gurpa/dev/factory_app/dispatch_plans/permissions.py` — `Can*BiltyServiceGRPO*` (`has_any_permission`).
- `C:/Users/gurpa/dev/factory_app/dispatch_plans/dispatch_urls.py` — `/api/v1/dispatch/bilty-grpo/…`.
- `C:/Users/gurpa/dev/factory_app/sap_client/client.py` + `service_layer/grpo_writer.py`, `service_layer/attachment_writer.py`.

**Key frontend files** (see the paired doc for the full map)

- `C:/Users/gurpa/dev/FactoryFlow/src/modules/warehouse/grpo/` — material pages, api, types, components.
- `C:/Users/gurpa/dev/FactoryFlow/src/modules/warehouse/grpo/pages/ServiceGRPO*.tsx` — service pages, routed by the **dispatch** module at `/dispatch/bilty-grpo/*`.

---

## Related docs

- **Frontend (paired):** `C:/Users/gurpa/dev/FactoryFlow/docs/modules/grpo.md`
- Still-useful in this folder: `attachment_fix.md` (the 200019 attachment-first
  rationale — but note the view now *requires* an attachment). `api_reference.md`,
  `error_codes.md`, `models.md`, `workflow.md`, `frontend_guide.md` are partly
  outdated (single-PO payloads, no-attachment path) — cross-check against
  `services.py`/`views.py` before relying on them.
