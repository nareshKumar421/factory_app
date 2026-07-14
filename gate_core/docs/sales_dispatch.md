# Sales Dispatch & Docking — Backend (customer dispatch gate-out)

> **Scope:** the server side of the customer finished-goods dispatch gate-out ("Docking")
> flow. Django apps: **`gate_core`** (dockings, arrivals, inside-vehicle console),
> **`docking_admin`** (scan-skip / partial-scan approvals), **`barcode`** (box scan resolution).
> Paired frontend doc: **`C:/Users/gurpa/dev/FactoryFlow/docs/modules/sales-dispatch-docking.md`**.
>
> This document is written from the **code as of this commit**, not from older design notes.
> Where a hard-won edge case contradicts older docs, the code wins.

---

## Overview — what it does & who uses it

"Docking" is the gate-out that a **loaded customer truck** goes through when it leaves the
factory with finished goods. It sits downstream of dispatch planning (`dispatch_plans`) and
the empty-vehicle gate-in (`EmptyVehicleGateIn`): a truck is gated in empty, loaded against
one or more SAP bills, then **docked** — its bills snapshotted from SAP, its boxes scanned,
a truck photo and freight/e-way/bilty documents attached, a **gatepass printed and committed**,
a **gross weighment** taken, and finally **marked dispatched** and physically **departed**.

Two operator personas touch it, on two URL surfaces that hit the *same* `SalesDispatchGateOut`:

- **Docking / warehouse staff** (`/dispatch/docking`): create the docking, scan boxes,
  attach photo + documents, **print** and **commit** the gatepass.
- **Gate security** (`/gate/sales-dispatch`): wait for the committed gatepass, record the
  **gross weight**, **mark dispatched**, and record the **physical exit** (depart).

A single physical truck may carry bills for **several sibling companies**. Those are threaded
together by a non-company-scoped **`VehicleArrival`** so the truck is gated in once, prints one
combined gatepass, dispatches every company atomically, and leaves once.

---

## Key concepts & entities

All models live under `gate_core/models/` (chiefly `sales_dispatch.py`, `vehicle_arrival.py`)
and `docking_admin/models.py`.

| Entity | File | What it is |
|---|---|---|
| **`SalesDispatchGateOut`** ("the docking") | `models/sales_dispatch.py` | Company-scoped gate-out record for one truck-load of one company. Holds the SAP header snapshot, transport/driver snapshot, gatepass number, truck photo, weights, status. `entry_no` = `DOCK-YYYYMMDD-NNNN`. |
| **`SalesDispatchGateOutDocument`** ("a bill") | same | One SAP bill (A/R Invoice or Stock Transfer) carried by the docking. **A docking can carry several bills.** Snapshot of that bill's SAP fields. |
| **`SalesDispatchGateOutItem`** | same | A line item, linked to its parent bill (`document`). `dispatched_quantity` < `quantity` marks a partial (held) line. |
| **`SalesDispatchBoxScan`** | same | One scanned box. Unique per `(sales_dispatch, box_barcode)`. `document` FK = which bill this box was attributed to (nullable = "outside list"). |
| **`SalesDispatchAttachment`** | same | Uploaded file with a type: `TRUCK_PHOTO`, `GATEPASS`, `INVOICE_COPY`, `DELIVERY_NOTE`, `BILTY`, `EWAY_BILL`, `CREDIT_NOTE`, `OTHER`. Truck photo also stored inline on the docking with geo. |
| **`SalesDispatchAdditionalWeight`** | same | Named operator-entered weight of **non-goods** (packaging, dunnage). Never touches the weighbridge weight; used to reconcile net loaded weight vs invoice. |
| **`PartialDispatchApproval`** | same | Authorisation to ship a bill **short** (some items held + credited). Blocks gatepass until APPROVED + credit note recorded. |
| **`SalesDispatchGatepassPrintLog`** | same | Append-only print/reprint audit. Exactly one `ORIGINAL` per docking (DB constraint); reprints are `REPRINT` with a reason. |
| **`SalesDispatchGatepassSequence`** | same | Per-company, per-financial-year running number → gatepass `DCK/{company.code}/{FY}/{NNNNNN}`. |
| **`SalesDispatchLock`** | same | Company-level hold on gatepass **printing** (unique per company). Returns HTTP **423** when locked. |
| **`VehicleArrival`** | `models/vehicle_arrival.py` | **NOT company-scoped.** One physical truck trip. Threads per-company gate-ins + dockings via their nullable `arrival` FK. One tare weight, one exit, one combined `ARV/{FY}/{NNNNNN}` gatepass. Status: `INSIDE` → `LOADING` → `DEPARTED` (or `CANCELLED`). |
| **`ArrivalGatepassSequence`** | same | Company-agnostic FY running number for the combined `ARV/...` gatepass. |
| **`DockingScanSkipRequest`** | `docking_admin/models.py` | Operator request to skip **all** box scanning for a docking. `PENDING`/`APPROVED`/`REJECTED`. One pending per docking. |
| **`DockingPartialScanRequest`** | same | Operator request to dispatch with **some** boxes scanned (the partial case). Same lifecycle; carries scanned/expected counts. |

### Docking status lifecycle

```
DOCKED → PHOTO_ATTACHED → READY_FOR_GATEPASS → GATEPASS_PRINTED → PRINT_COMMITTED → DISPATCHED
                                                                              ↘ (REJECTED / CANCELLED)
```

- `DOCKED` — created; bills + items snapshotted. **Only** state in which more bills can be
  merged in and box scans can be added/removed (`can_edit` = DOCKED / PHOTO_ATTACHED /
  READY_FOR_GATEPASS; box scans specifically gated by `can_edit`).
- `READY_FOR_GATEPASS` — set by the gatepass **preview** endpoint once readiness passes.
- `GATEPASS_PRINTED` — original gatepass number assigned, QR built, print logged.
- `PRINT_COMMITTED` — the "point of no return": can no longer be cancelled; box scanning closed.
- `DISPATCHED` — gate recorded gross weight and released the truck; plans/covers settled.

`ACTIVE_DOCUMENT_STATUSES` (DOCKED..DISPATCHED) is what the **unique-active-document**
constraint and duplicate check use — a REJECTED/CANCELLED docking frees the bill to be re-docked.

---

## End-to-end flows

### 1. Create a docking (happy path) — `SalesDispatchGateOutListCreateView.post` (`views_sales_dispatch.py`)

1. Body carries one or more `documents` (`{document_type, sap_doc_entry, dispatch_plan_id}`) plus
   `vehicle_id`, `driver_id`, and transport fields.
2. **Company is resolved from the dispatch plans**, not the active `Company-Code` header
   (`_resolve_docking_company`) — the cross-company board can start a sibling company's docking.
   All bills in one docking must belong to one company; the user must be in scope (`assert_company_in_scope`).
3. Each SAP document is fetched **live from SAP** via `SalesDispatchDocumentService.get_document`.
   SAP unreachable → **503**; SAP data error → **502**; not found → **404**.
4. Validation (`_validate_document_set`): no mixing invoice + stock-transfer, stock-transfer is
   single-document, all invoices share one SAP **branch**. `_duplicate_response` blocks a bill
   already docked in an active status (returns the existing entry's ids).
5. **De-fragment / merge:** if the truck's gate-in already has an **open (DOCKED)** docking,
   the new bills are appended to it (`_append_to_open_docking` → `add_plan_to_open_docking`)
   instead of creating a second docking — one truck+company keeps one docking. Returns **200**.
6. Otherwise a fresh `VehicleEntry` (`entry_type="SALES_DISPATCH"`) + `SalesDispatchGateOut` are
   created, the empty-in **tare weighment is copied** onto the new vehicle entry, bills + items
   are snapshotted, freight is synced to the plans, and the docking is threaded onto the
   `VehicleArrival` (arrival flips `INSIDE → LOADING`). Returns **201** with `warnings`
   (multiple customers / multiple e-way bills are non-blocking warnings).

### 2. Box scanning — `SalesDispatchBoxScanListCreateView` / `...BatchView`

- Each barcode is validated through `barcode.services.scan_service.ScanService.process_scan`
  (`scan_type="SHIP"`, context `SALES_DISPATCH`). Must resolve to a `Box` in ACTIVE/PARTIAL state.
- The scan is **attributed to exactly one bill** by `resolve_scan_document`
  (`services/sales_dispatch_box_match.py`): (1) the box's **origin bill** from the barcode
  dispatch session, else (2) **greedy fill** the first bill still short on that item, else
  (3) overflow onto the first invoicing bill, else (4) `None` = unattributed ("outside list").
- **Invoice cap:** `remaining_invoiced_qty` blocks scanning more than the bill's invoiced qty.
- **Duplicate:** a box already scanned returns **200** with `duplicate: true` (not an error).
- Batch endpoint accepts many barcodes, returns `{saved, failed}` with machine-readable reasons —
  the offline queue submits here. Soft-deleted scans are reactivated via `get_or_create`.

### 3. Gatepass readiness & print — `get_gatepass_readiness` + `SalesDispatchGatepassPrintView`

1. **Preview** (`SalesDispatchGatepassPreviewView`) recomputes readiness; if ready and status is
   `PHOTO_ATTACHED`, advances to `READY_FOR_GATEPASS`.
2. **Print** re-checks `ensure_gatepass_ready` + `ensure_partial_dispatch_cleared` under a
   `select_for_update` lock, refuses if the company is print-locked (**423**) or an ORIGINAL
   print already exists, assigns `DCK/...` via `assign_gatepass` (status → `GATEPASS_PRINTED`,
   QR payload built), and records the `ORIGINAL` print log.
3. **Commit** (`SalesDispatchCommitPrintView`) moves `GATEPASS_PRINTED → PRINT_COMMITTED`.

Readiness (`services/sales_dispatch_gatepass.py::get_gatepass_readiness`) requires **all** of:
truck photo **with geolocation**; box scans OK; at least one item; bilty no + date + bilty
attachment; if `sap_doc_total > ₹50,000` an e-way bill number **and** e-way attachment;
(weighment is reported but is enforced at dispatch, not print). "Box scans OK" =
fully scanned **or** an approved scan-skip (zero-scan) **or** an approved partial-scan **or**
the company has scanning turned off (`DOCKING_BOX_SCAN_OPTIONAL_COMPANY_CODES`).

### 4. Dispatch & depart — `SalesDispatchMarkDispatchedView` → `mark_docking_dispatched` / `dispatch_arrival`

- Single-company truck → `mark_docking_dispatched` (`services/sales_dispatch_dispatch.py`):
  requires `PRINT_COMMITTED`, a committed gatepass, and a **valid weight**
  (`get_dispatch_weight_error`: gross > 0, tare ≥ 0, tare ≤ gross). Sets `DISPATCHED`, marks the
  `VehicleEntry` `COMPLETED`, flips its dispatch plans to `DISPATCHED`, and
  `consume_covers_for_dispatched_plans` retires the gate-in so the truck stops counting as inside.
- Multi-company truck (`arrival.company_ids > 1`) → `dispatch_arrival` dispatches **every**
  company's docking in one atomic transaction; a not-ready sibling rolls the whole thing back
  with a `ValueError` naming the blocking company code. Idempotent (already-DISPATCHED skipped).
- **Depart** (`VehicleArrivalDepartView`) records the single physical exit once every gate-in on
  the arrival is retired; dispatching the last chain also auto-departs, so depart is idempotent.

### 5. Combined multi-company gatepass — `views_arrival.py` + `services/arrival_gatepass.py`

`arrivals/<id>/gatepass/{readiness,print,commit,reprint}` and `.../dispatch`, `.../depart`,
`.../empty-out` operate across **all** the user's companies. `assign_arrival_gatepass` assigns
one `ARV/...` number and runs each docking's own `assign_gatepass` (so per-company `DCK/...`
numbers, QR, and ORIGINAL audit are still produced for SAP/GST). Every involved company must be
in the user's scope (`_ArrivalGatepassBaseView.get_arrival`) or **403**.

> **Permission asymmetry — read this.** These arrival-level endpoints
> (`VehicleArrivalDepartView`, `VehicleArrivalEmptyOutView`, and the
> `_ArrivalGatepassBaseView` subclasses: readiness/print/commit/reprint/dispatch) declare
> `permission_classes = [IsAuthenticated]` — the gatepass ones add only the company-scope
> guard. They do **not** require `can_print_sales_dispatch_gatepass` /
> `can_commit_sales_dispatch_print` / `can_dispatch_sales_dispatch_out`, unlike the
> equivalent single-docking endpoints. So on a shared truck the combined gatepass can be
> printed/committed/dispatched (and the truck departed / emptied-out) by any authenticated
> user who belongs to those companies, even without the fine-grained print/dispatch perm.
> The single-docking Mark-Dispatched button (`SalesDispatchMarkDispatchedView`, which routes a
> multi-company truck through `dispatch_arrival`) **is** perm-gated; the standalone
> `arrivals/<id>/dispatch/` endpoint is not. The frontend combined-gatepass panel calls the
> arrival endpoints (`arrivals.api.ts`), so this is the live path for shared trucks.

### 6. Late bill / correction paths

- **Auto-merge:** a bill booked *after* the docking exists is folded into the truck's open
  docking by `merge_bill_into_open_docking` — only while `DOCKED` (pre-photo-lock), same branch,
  not already docked; SAP failure is swallowed and the bill stays a pending row.
- **Manual add:** `SalesDispatchAddDocumentView` (`views_partial_dispatch.py`) adds a same-truck
  bill to an open docking when auto-merge couldn't run.
- **Inside-Vehicle console** (`views.py`: `InsideDispatchVehiclesView` + add/remove/move/unlink):
  fix bill↔truck mistakes without DB edits. Each action has its **own** permission (see below).
- **Partial ship:** `SalesDispatchPartialApprovalRequestView` records held `dispatched_quantity`
  and opens an approval; `...DecideView` approves (with credit-note no) or rejects.

---

## Critical business rules & invariants

1. **One active docking per SAP bill per company.** Enforced by the partial unique constraint
   `unique_active_sales_dispatch_document` on `(company, document_type, sap_doc_entry)` for active
   statuses, plus the child-doc constraint and the create-time `_duplicate_response`.
2. **One open docking per vehicle+company.** New bills merge into the truck's `DOCKED` docking
   rather than fragmenting into a second one.
3. **Company follows the record, not the header.** Reads resolve across *all* the user's companies
   (`get_sales_dispatch_or_404` uses `user_company_ids`); writes resolve the owning company from the
   plan/record. This is the root-cause fix for "blank in sibling company" bugs — never trust the
   active `Company-Code` for a cross-company docking.
4. **Photo-lock cut-off.** Once a truck photo is attached (docking leaves `DOCKED`), no more bills
   can be merged/added and box scans are frozen.
5. **Gatepass gating is per-bill, not just load-total.** `has_unscanned_bill_lines` compares
   scanned vs invoiced quantity **per (bill, item)** so a surplus on one bill can't mask a
   shortfall on another; `load_scan_status` combines this with the load-wide box count. The
   gatepass gate and the partial-scan-approval endpoint share this rule so they can never deadlock.
6. **Partial dispatch needs approval + credit note.** `ensure_partial_dispatch_cleared` blocks
   print while any approval is `PENDING`, and while any is `APPROVED` without a `CREDIT_NOTE`
   attachment **and** a credit-note number.
7. **Dispatch weight guard.** No dispatch without gross > 0, tare ≥ 0, tare ≤ gross.
8. **PRINT_COMMITTED is a one-way door.** Cannot be cancelled after commit; DISPATCHED cannot be
   rejected. Challan weight and additional weights *can* still be edited after commit (the operator
   is at the weighbridge then) but not after DISPATCHED/REJECTED/CANCELLED.
9. **One ORIGINAL print, audited reprints.** DB constraint `unique_original_sales_dispatch_gatepass_print`;
   reprints require a reason and are logged with copy number, printer, IP, and user-agent.
10. **Print lock → 423.** `SalesDispatchLock` for the **docking's** company halts print/commit/reprint.
11. **Multi-company atomicity.** A shared-truck dispatch is all-or-nothing; the truck can never leave
    for one company while another is still inside.

---

## Integrations & cross-module boundaries

- **SAP (per-company schema).** Documents are read live via `SalesDispatchDocumentService`
  (`DispatchPlansService` for invoices, `SAPClient.list/get_stock_transfer` for transfers). Each
  company has its own SAP schema (multi-DB); the service is constructed with `company.code`. The
  docking **stores a snapshot and does not post to SAP** — the invoice/delivery already exists in
  SAP; `pgi_reference` and e-way are captured as gatepass fields. SAP outages surface as 502/503.
- **`dispatch_plans`.** The upstream planning module. A docking bill links to a `DispatchPlan`;
  dispatch flips plans to `DISPATCHED` and consumes their `EmptyVehicleGateInCover`s. Freight is
  allocated back to the plans (`sync_sales_dispatch_transport_to_plans`).
- **`barcode`.** Box identity + scan validation (`ScanService`, `Box`, `DispatchSession`). The
  docking can also **import** boxes already scanned in the legacy barcode dispatch flow
  (`SalesDispatchBarcodeScansView` / `...ImportView`) matched by shared SAP document.
- **`weighment`.** Tare copied from the empty-in; gross recorded at the gate. Dispatch reads
  `vehicle_entry.weighment`.
- **`docking_admin`.** Scan-skip / partial-scan approvals; queried by `get_gatepass_readiness`
  via reverse relations (`scan_skip_requests`, `partial_scan_requests`) to avoid a circular import.
- **`gate_core` empty-vehicle + arrival.** Gate-in / tare / arrival threading and retirement.
- **`notifications`.** Approval requests notify approvers by permission codename; decisions notify
  the requester (`docking_admin/services.py`).

---

## Real-world edge cases

Each: **trigger → current behaviour → operator-visible symptom → risk/gap.**

1. **Partial truck load (some boxes scanned).**
   Trigger: fewer boxes scanned than invoiced, or a bill/line still short. →
   `load_scan_status` marks it partial; gatepass print is blocked until a **partial-scan approval**
   is APPROVED (`DockingPartialScanRequest`). → Operator sees "Locked — scan all boxes, or request
   partial dispatch approval". → Risk: a weight/loose line with no pack size could historically read
   as "fully scanned"; `_expected_item_boxes` now counts one box per unit for such lines to catch it.

2. **Scanner offline / duplicate / re-scanned box.**
   Trigger: flaky network or the same box scanned twice. → Scans queue client-side and POST one at a
   time; a duplicate returns **200 `duplicate:true`**; a soft-deleted scan is reactivated by
   `get_or_create`; batch failures come back in `failed[]`. → Operator sees "already scanned" or a
   Failed-scans retry list. → Risk: none server-side (idempotent on `box_barcode`).

3. **SAP down / rejecting during creation or bill fetch.**
   Trigger: SAP unreachable/errors while creating a docking or fetching a document. → **503** (connection)
   or **502** (data); auto-merge of a late bill silently swallows it and leaves the bill pending. →
   Operator sees "SAP system is currently unavailable" and no docking is created. → Risk: a bill can
   linger as a pending row; manual add (`SalesDispatchAddDocumentView`) or a retry recovers it.

4. **A bill booked after gate-in.**
   Trigger: 4th bill decided after the first three are docked. → If the docking is still `DOCKED`,
   `merge_bill_into_open_docking` / `SalesDispatchAddDocumentView` / Inside-Vehicle "Add Bill" folds
   it into the same docking (box scans intact). After photo-lock it cannot be added. → Operator sees
   the bill appear on the existing docking, or "the load is already locked". → Risk: post-lock late
   bills must ride a new trip.

5. **Stale vehicle arrival.**
   Trigger: a truck stuck "inside" from a bad/abandoned trip. → `VehicleArrivalEmptyOutView` cancels
   the arrival and releases every gate-in's plans; a new arrival cannot be opened while one is open
   (`This vehicle already has an open arrival`). Dispatching the last chain auto-departs. → Operator
   sees the truck leave the inside list, or a "vehicle already has an open arrival" block. → Risk: if
   a gate-in isn't retired on dispatch the truck sticks inside (see `mark_docking_dispatched` →
   `consume_covers_for_dispatched_plans`); an unretired cover blocks depart.

6. **Cross-company blank data.**
   Trigger: acting on a sibling company's docking under a different active `Company-Code`. → Reads use
   `user_company_ids` (all companies); writes resolve the company from the plan/record; print-lock and
   readiness are computed for the **docking's** company. → Operator sees the sibling docking normally
   instead of a blank/404. → Risk: any new endpoint that filters by the active header instead of
   `user_company_ids` reintroduces the "blank in sibling company" bug.

7. **Missing weighbridge weight at dispatch.**
   Trigger: gross/tare missing or tare > gross when marking dispatched. → `get_dispatch_weight_error`
   raises a specific `ValueError` → **400**. → Operator sees "Gross weight is required…" / "Tare weight
   cannot be greater than gross weight." → Risk: none; dispatch is hard-blocked.

8. **Retry after a failed dispatch.**
   Trigger: dispatch 400s (sibling not ready) then retried. → `dispatch_arrival` is atomic + idempotent;
   already-dispatched dockings are skipped. → Operator sees the same "COMPANY: …" block until the
   sibling is ready, then success. → Risk: none.

9. **PRINT_COMMITTED stall.**
   Trigger: gatepass committed but the truck can't finish (bad weight, missing gross, sibling not ready,
   or an approval that never came). → The docking sits in `PRINT_COMMITTED`; it **cannot be cancelled**
   (`SalesDispatchCancelView` blocks it), only challan/additional weights remain editable. → Operator
   sees a load stuck at "Print committed" with no cancel option. → Risk/gap: recovering a wrongly
   committed load needs a reject-before-dispatch or a DB/admin fix; there is no self-service unwind.

10. **Inside-Vehicle action attempted without permission.**
    Trigger: a user without the specific per-action perm tries add/remove/move/unlink. → Backend
    `CanAddBillInsideVehicle` / `CanRemoveBillInsideVehicle` / `CanMoveBillInsideVehicle` /
    `CanUnlinkBillsInsideVehicle` reject; the frontend also hides the button. → Operator without the
    perm never sees the button; a forged request gets **403**. → Risk: none for those four —
    server-enforced, not just UI-gated. **Exception:** the Inside-Vehicle **Mark Out** button
    (`can_mark_out_inside_vehicle`) is a *frontend-only* gate — it just navigates to the
    empty-vehicle-out wizard, and neither that wizard (`EmptyVehicleGateOutListCreateView`) nor the
    arrival empty-out/depart endpoints enforce `can_mark_out_inside_vehicle` (they are
    `IsAuthenticated` + company scope). A user with the empty-out route but not the button-perm can
    still mark a truck out.

---

## Failure modes / what can break

| Failure | Where | Operator/manager-visible symptom |
|---|---|---|
| SAP unreachable | create, bill fetch, stock-transfer list | "SAP system is currently unavailable" (503) / "Failed to retrieve … document" (502); can't start or grow a docking |
| Company print-locked | print / commit / reprint | HTTP **423** "Docking gatepass printing is locked. Reason: …" |
| Readiness incomplete | print | **400** "Docking entry is not ready for gatepass: truck_photo_geolocation, bilty_no, …" |
| Partial not cleared | print | **400** "A partial-dispatch approval is still pending…" / "A credit note … is required…" |
| Bad/missing weight | mark dispatched | **400** naming the exact weight problem |
| Sibling not ready | multi-company dispatch | **400** "`<CODE>`: Print must be committed before marking Docking as dispatched." (whole truck rolls back) |
| Depart before all retired | arrival depart | **400** "All companies must be dispatched before the truck can depart." |
| Duplicate SAP bill | create | **400** "SAP document … is already docked as DOCK-…" with the linked entry ids |
| Scan on a locked docking | box scan POST | **400** "Box scans cannot be changed in this Docking status." |
| Over-scan past invoice | box scan POST | **400** "Bill … already has the full invoiced quantity of … scanned." |
| Stuck-inside truck | dispatch/retire gap | truck stays on the inside list; depart blocked until the gate-in is retired / empty-out is used |

---

## Improvement opportunities & known gaps

- **No self-service unwind from `PRINT_COMMITTED`.** A wrongly committed load can't be cancelled;
  only reject-before-commit or an admin fix recovers it. A guarded "revert commit" would help.
- **Silent late-merge failures.** `merge_bill_into_open_docking` swallows SAP/branch/duplicate
  errors and leaves the bill pending with no surfaced reason; a status/telemetry hook would make
  the "why didn't my bill attach?" question answerable.
- **Box-count estimation is heuristic.** `_expected_item_boxes` parses pack size from item names
  (`… 12 PCS`); a mis-named item skews the expected count and the partial/complete gate. Prefer a
  stored `total_boxes` from SAP where available.
- **Weighment is only enforced at dispatch, not print.** Readiness reports `has_weighment` but does
  not block printing; a gatepass can print before the gross weight exists.
- **Twin of a known perf issue.** Dispatch/bilty flows elsewhere have N+1 patterns (see the repo
  performance audit); the docking querysets are heavily prefetched (`_sales_dispatch_base_queryset`)
  — keep new serializer fields reading `.all()` off the prefetch cache, never `.filter()/.count()`.
- **Arrival endpoints bypass the fine-grained dispatch perms.** The combined-arrival gatepass /
  dispatch / depart / empty-out endpoints require only auth + company scope, so the shared-truck path
  is authorised more loosely than the single-docking path (which enforces `can_print/commit/dispatch`).
  If those permissions are meant to be real controls, the `_ArrivalGatepassBaseView` subclasses and
  `VehicleArrival{Depart,EmptyOut}View` should add the matching `required_permissions`.
- **`can_mark_out_inside_vehicle` is a declared-but-unwired permission.** It exists on
  `DispatchPlan.Meta.permissions` and gates the frontend button, but no DRF permission class references
  it and no view enforces it — so it protects nothing server-side. Either wire it onto the empty-out
  path or drop it to avoid a false sense of control.

---

## Permissions & roles

Permissions are Django perms checked by `HasRequiredDjangoPermission` (per-view `required_permissions`,
optionally a `{METHOD: perm}` map). Model perms are declared on `SalesDispatchGateOut.Meta`,
`PartialDispatchApproval.Meta`, `SalesDispatchLock.Meta`, the `docking_admin` models, and
`dispatch_plans` (inside-vehicle).

| Capability | Permission codename |
|---|---|
| View dockings / reports | `gate_core.can_view_sales_dispatch_out` / `can_view_sales_dispatch_reports` |
| Create / edit docking | `gate_core.can_create_sales_dispatch_out` / `can_edit_sales_dispatch_out` |
| Scan boxes | create **or** edit perm (`ensure_sales_dispatch_scan_permission`) |
| Upload truck photo | `gate_core.can_upload_sales_dispatch_photo` |
| Print / reprint / commit gatepass | `can_print_…` / `can_reprint_…` / `can_commit_sales_dispatch_print` |
| Reject / cancel / dispatch | `can_reject_…` / `can_cancel_…` / `can_dispatch_sales_dispatch_out` |
| Manage print lock | `gate_core.can_manage_sales_dispatch_lock` |
| Approve partial (held-items) | `gate_core.can_approve_partial_sales_dispatch` |
| Request / view / approve **scan-skip** | `docking_admin.can_request/view/approve_docking_scan_skip` |
| Request / view / approve **partial-scan** | `docking_admin.can_request/view/approve_docking_partial_scan` |
| Inside-Vehicle: view / add / remove / move / unlink (**server-enforced**) | `dispatch_plans.can_view_inside_vehicle_manager` / `can_add_bill_inside_vehicle` / `can_remove_bill_inside_vehicle` / `can_move_bill_inside_vehicle` / `can_unlink_bills_inside_vehicle` |
| Inside-Vehicle: mark-out (**frontend-only gate**) | `dispatch_plans.can_mark_out_inside_vehicle` — hides the button only; declared on `DispatchPlan.Meta` but no view enforces it (the empty-out wizard / arrival endpoints are auth + scope) |
| Combined-arrival gatepass / dispatch / depart / empty-out | **no fine-grained perm** — `IsAuthenticated` + company-scope guard only (see the Permission-asymmetry callout in §5) |

**Nav gating note (see memory):** the frontend sidebar gates by *permission*, not group — changing a
group's perms alone can hide/show whole modules. The Inside-Vehicle **add / remove / move / unlink**
buttons are gated on **both** ends (the backend permission class rejects a forged call, and the UI
hides the button). **Mark Out** and the combined-arrival actions are gated only in the UI — a forged
call reaches an endpoint guarded by auth + company scope, not the fine-grained perm (see §5 and the
table rows above).

---

## Developer file map

### Backend (`C:/Users/gurpa/dev/factory_app`)

| Path | Contents |
|---|---|
| `gate_core/models/sales_dispatch.py` | Docking, document, item, box scan, attachment, additional weight, partial approval, gatepass sequence + print log, lock |
| `gate_core/models/vehicle_arrival.py` | `VehicleArrival`, `ArrivalGatepassSequence` |
| `gate_core/views_sales_dispatch.py` | Docking CRUD, box scan (single/batch/detail), barcode import, gatepass preview/print/reprint/commit/pdf, challan + additional weights, mark-dispatched, reject, cancel, lock, reports, pending bookings; querysets + helpers |
| `gate_core/views_partial_dispatch.py` | Remove/add bill (C1), partial-approval request + decide (C2) |
| `gate_core/views_arrival.py` | Cross-company arrival: list/create, depart, empty-out, combined gatepass readiness/print/commit/reprint, dispatch |
| `gate_core/views.py` | `InsideDispatchVehiclesView` + add/remove/move/unlink/add-to-truck (correction console) |
| `gate_core/services/sales_dispatch_docking.py` | Build/grow a docking from SAP docs; late-bill merge; header aggregation |
| `gate_core/services/sales_dispatch_dispatch.py` | `mark_docking_dispatched`, `dispatch_arrival`, weight guard |
| `gate_core/services/sales_dispatch_box_match.py` | `resolve_scan_document`, invoice-cap helpers (bill attribution) |
| `gate_core/services/sales_dispatch_gatepass.py` | Readiness gate, `load_scan_status`, box-count estimation, e-way rule, `can_edit` |
| `gate_core/services/sales_dispatch_documents.py` | SAP document fetch + normalization (invoice / stock transfer) |
| `gate_core/services/arrival_gatepass.py` | Combined `ARV/...` gatepass print/commit/reprint/readiness |
| `gate_core/services/sales_dispatch_gatepass_pdf.py` | Gatepass PDF rendering (Crystal/fallback) |
| `gate_core/serializers_sales_dispatch.py` | Docking serializers + computed fields (`gatepass_readiness`, `arrival_*`, weights, lock) |
| `gate_core/serializers_arrival.py` | Arrival serializers |
| `gate_core/permissions.py` | `HasRequiredDjangoPermission` |
| `gate_core/urls.py`, `gate_core/services/user_scope.py` | Routes; `user_company_ids` / `wants_all_companies` / `assert_company_in_scope` |
| `docking_admin/{models,views,serializers,services,urls}.py` | Scan-skip + partial-scan approval requests, queues, notifications |
| `dispatch_plans/permissions.py` | Inside-Vehicle per-action permission classes |
| `barcode/services/{scan_service,dispatch_service,box_ownership}.py` | Box scan validation + ownership |

Tests worth reading: `gate_core/test_docking_merge.py`, `test_inside_vehicle_flow.py`,
`test_partial_dispatch.py`, `test_arrival.py`, `docking_admin/tests.py`.

---

## Related docs

- **Paired frontend doc:** `C:/Users/gurpa/dev/FactoryFlow/docs/modules/sales-dispatch-docking.md`
- `gate_core/docs/api.md` — gate_core API notes
- `gate_core/README.md` — gate_core app overview
- FactoryFlow `docs/modules/gate.md`, `docs/modules/barcode.md`,
  `docs/modules/barcode-dispatch-design.md` — adjacent gate/barcode flows
