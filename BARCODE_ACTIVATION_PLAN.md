# Barcode Activation Plan — printed ≠ present

**Problem.** Box/pallet labels are printed on the production floor and pasted as the
line runs. Labels that never get pasted (over-printed, dropped, thrown away) still
exist as live `barcode.Box` rows in `BH-PF`, so the app believes stock is in the PF
godown that was never there. That phantom stock inflates box counts, dispatch
expectations and the WMS map.

**Change.** A printed barcode is born **inactive**. It becomes active only when a
receiver at the PF-godown gate scans it into the warehouse it was printed for, or
when a supervisor approves an explicit activation request. Nothing else in the app
may treat an inactive barcode as stock.

---

## 1. Decisions taken

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | Inactive is a **status** (`PENDING`), not a boolean flag | Every downstream flow already gates on `status in (ACTIVE, PARTIAL)` — dispatch, docking, BST, intercompany, vehicle load, WMS mirror. A new status is **fail-closed by construction**; a boolean would leak through every query that forgot to add it. |
| D2 | Pallet-QR scan at the gate = **scan + physical count confirm** | The pallet's box rows were created at print time, phantom labels included. If the receiver's count equals the pallet's pending-box count → activate all. If it is short → that pallet drops into box-by-box mode, so only real boxes activate. |
| D3 | A dispatch/invoice scan of a pending box is **rejected**, not auto-activated | With the gate desk staffed, a pending box at the dock is genuinely suspect. Rejection message points at the approval route. *(Note: this reverses the "scanned against an invoice" activation route from the original brief — flagged deliberately; it is a one-line change in `activation_service` if you want it back.)* |
| D4 | Stale pending labels are voided **manually from a report**, no scheduled job | Nothing is destroyed automatically; the barcode team reviews the aging report and bulk-voids. |
| D5 | ~~Enforcement is per company + per warehouse, default off~~ → **on by default, everywhere** | Shipped off-by-default, then reversed on the user's instruction (2026-09-08). `BarcodeActivationSettings` still exists but inverted: an empty `enforced_warehouses` now means *every* warehouse, so the list narrows the rule rather than opting into it, and a company with no row still enforces. Stepping back is deliberate — list warehouses, or untick `is_enabled`. The cost accepted: a warehouse with nobody assigned to receive accumulates PENDING labels dispatch refuses. |
| D6 | Repacked boxes are born **ACTIVE** (`activation_source=REPACK`) | A repack box is built inside the godown from stock that was already activated; it never passes the gate, so requiring a gate scan would strand it. |

---

## 2. Data model

`barcode/models.py`

**New status values** (choices-only — no DB constraint change):
- `BoxStatus.PENDING = "PENDING", "Pending Activation"`
- `PalletStatus.PENDING = "PENDING", "Pending Activation"`
  (distinct from the existing, rarely-used `PalletStatus.INACTIVE`; do **not** overload it — it sits in `PALLET_MANUAL_TERMINAL_STATUSES` and would freeze recompute.)

**New fields** on `Box` **and** `Pallet`:

| Field | Type | Notes |
|-------|------|-------|
| `activated_at` | `DateTimeField(null=True)` | |
| `activated_by` | `FK(User, SET_NULL, null=True)` | |
| `activation_source` | `CharField(choices=ActivationSource)` | `GATE_SCAN` / `APPROVAL` / `REPACK` / `LEGACY` |
| `activation_warehouse` | `CharField(max_length=20, blank=True)` | warehouse it was received into, snapshotted |

**New enum members**
- `BoxMovementType.ACTIVATE`, `PalletMovementType.ACTIVATE`
- `BarcodeAuditTransactionType.ACTIVATED`
- `ScanType.ACTIVATE` (gate scans land in the existing `ScanLog`, which already
  carries `reject_code` / `reject_message` from migration `0020`)

**New models**

```
BarcodeActivationSettings      # one row per company (D5)
  company (OneToOne), is_enabled, enforced_warehouses (JSON list of codes),
  updated_by, updated_at

BarcodeActivationRequest       # the approval route — modelled on PalletVerifyRequest
  company, status (OPEN/APPROVED/REJECTED/CANCELLED), warehouse,
  pallet (FK, nullable), reason (required), requested_by/at,
  decided_by/at, decision_note

BarcodeActivationRequestLine
  request (FK), box (FK), activated (bool)
```

`BarcodeActivationRequest` deliberately mirrors `PalletVerifyRequest` (same
status/notification shape) so the FE ticket components are reusable.

**Permissions**

| Permission | App | Who |
|------------|-----|-----|
| `warehouse.can_receive_barcodes` | `warehouse` (class `CanReceiveBarcodes` in `warehouse/permissions.py`) | the receiver at the godown gate — **and** `warehouse_scope.assert_manages()` for the selected warehouse |
| `barcode.can_request_barcode_activation` | `barcode` | the printing person, from the label page |
| `barcode.can_approve_barcode_activation` | `barcode` | supervisor/admin; also gates bulk-void of stale pending |

The receive permission belongs to `warehouse`, not `barcode`, because the page is a
warehouse screen and the real gate is the **manager assignment**: a receiver may
only activate into a warehouse they manage. `warehouse_scope` already enforces
"no assignment = no access" and is exempt for superusers.

**Migration `0021_barcode_activation`** — fields + models + permissions, plus a data
migration that stamps every existing box/pallet with
`activation_source='LEGACY'`, `activated_at=created_at`,
`activation_warehouse=current_warehouse`. **No existing row changes status.**

---

## 3. Backend — creation paths flip to PENDING

Gate every one of these on `BarcodeActivationSettings` (enabled **and** the target
warehouse is in `enforced_warehouses`), else keep today's `ACTIVE`.

| Path | File | Change |
|------|------|--------|
| Bulk label generation | `barcode/services/barcode_service.py:149` `generate_boxes` | `status=PENDING` |
| Pallet-QR box top-up | `…:399` `ensure_pallet_boxes` | `status=PENDING`; relax the `pallet.status != ACTIVE` guard (line 401) to accept `PENDING` |
| Empty pallet create | `…:328/347` `create_pallet` / `_create_empty_pallet` | pallet born `PENDING` |
| Production run labels | `barcode/services/production_integration_service.py` | inherits `generate_boxes` — verify it does not set status itself |
| Repack | `…:1412` `repack` | stays `ACTIVE`, stamp `activation_source=REPACK` (D6) |

### 3.1 Traps found in the current code — must be fixed with the same commit

These will silently break the print workflow if missed:

1. **`add_boxes_to_pallet` (`barcode_service.py:884`)** filters candidate boxes to
   `status__in=[ACTIVE, PARTIAL]`. Step 4 of the print workflow (attach freshly
   generated boxes to the pallet) would find **zero** boxes. Must accept `PENDING`.
2. **`_prepare_pallet_for_receiving_boxes` (`:1604`)** — pallet-status gate must
   accept `PENDING`, and its `current_boxes_exist` / `pallet_is_empty` computation
   counts only ACTIVE/PARTIAL, so a pallet full of pending boxes reads as *empty*
   and would let another item's boxes join it. Include `PENDING` in that count.
3. **`_recalculate_pallet` (`:1692`)** — `elif not available_boxes and total_boxes:
   status = EMPTY` flips a freshly printed pallet to **EMPTY** the moment boxes are
   attached. Needs a `PENDING` branch *before* the EMPTY branch.
4. **`recalculate_pallet_state` (`services/pallet_state.py:47-95`)** — same EMPTY
   trap, plus `active_boxes` must not count pending. Rule: *all remaining boxes
   pending → pallet `PENDING`; some activated → the existing ACTIVE/PARTIAL logic.*
5. **`move_pallet` (`:603`)** and `transfer_boxes` (`:1192`) — a pending pallet must
   not be movable/transferable. Their `status not in (ACTIVE, PARTIAL)` guards
   already refuse it; only the error text needs to name the real reason.
6. **WMS mirror** — `_sync_wms_pallet_inventory` (`:690`) already requires
   `status == ACTIVE`, and `wms_sync.reconcile_pallet_to_wms` keys off box counts.
   Add an explicit early return for `PENDING` so a pending pallet can never appear
   on the warehouse map. This is the phantom-stock symptom the whole change targets.

### 3.2 Consumers that must reject PENDING with a *useful* message

All of these already refuse a non-ACTIVE box; the work is replacing
"Box X is PENDING and cannot be dispatched" with a message that tells the operator
what to do, and attaching a machine-readable reject code so the failure shows in
the existing rejected-scan reports.

| Flow | File:line | New reject code |
|------|-----------|-----------------|
| Docking box scan (live dispatch) | `gate_core/services/sales_dispatch_loading.py:118` | `BOX_NOT_ACTIVATED` |
| Docking scan endpoint | `gate_core/views_sales_dispatch.py:2227` | `BOX_NOT_ACTIVATED` |
| Barcode DispatchSession box / pallet | `barcode/services/dispatch_service.py:1076`, `:1219` | `BOX_NOT_ACTIVATED` / `PALLET_NOT_ACTIVATED` |
| BST transfer scan | `warehouse/services/bst_service.py:878` | `BOX_NOT_ACTIVATED` |
| Intercompany transfer scan | `barcode/services/intercompany_transfer_service.py:458`, `:466` | — (raises `ValueError`) |
| Vehicle load | `barcode/services/vehicle_load.py:23` `LOADABLE_STATUSES` | already excludes PENDING — no change |

Message template: *"Box BOX-…-0007 was never activated at the PF gate. Get it
gate-scanned into BH-PF, or raise an activation approval."*

---

## 4. Backend — the activation service

New `barcode/services/activation_service.py`. Single writer for activation; nothing
else may set `status = ACTIVE` on a pending row.

```
activate_boxes(boxes, *, warehouse, user, source, reference="") -> Result
    per box: must be PENDING, must belong to the company,
             box.current_warehouse must equal `warehouse` (trim + upper)
    writes:  status=ACTIVE, activated_at/by/source/warehouse,
             BoxMovement(ACTIVATE), BarcodeAuditLog(ACTIVATED)
    then:    recalculate_pallet_state() per touched pallet

scan_for_activation(barcode_raw, *, warehouse, user, confirmed_box_count=None)
    resolves via ScanService (box / pallet / BarcodeMaster),
    logs every scan to ScanLog(ScanType.ACTIVATE) — accepted and rejected,
    rejection codes: NOT_PENDING | WAREHOUSE_MISMATCH | OTHER_COMPANY |
                     COUNT_MISMATCH | BARCODE_NOT_FOUND

activate_pallet(pallet, *, warehouse, confirmed_box_count, user)   # D2
    pending = pallet.boxes.filter(status=PENDING)
    confirmed == len(pending) -> activate all + pallet
    confirmed <  len(pending) -> return requires_box_scan=True,
                                 activate nothing, and open a
                                 PalletVerifyRequest(source=GATE) naming the gap
    confirmed >  len(pending) -> COUNT_MISMATCH (more boxes than labels)
```

`PalletVerifyRequestSource` gains a `GATE` member so the short-count case lands in
the ticket queue the barcode team already watches — no new review surface.

### API

The activation **logic** lives in `barcode` (it owns `Box`/`Pallet`); the receive
**endpoints** live in `warehouse` so they can use `warehouse_scope` and sit with the
screen that calls them. This is the same split `gate_core` already uses when its
docking views call `barcode.services.vehicle_load` — new file
`warehouse/views_receive.py`, wired in `warehouse/urls.py`.

**Warehouse app — the receive screen**

| Method | Path | Perm |
|--------|------|------|
| `POST` | `warehouse/receive/scan/` `{warehouse, barcode, confirmed_box_count?}` | `can_receive_barcodes` + `assert_manages(warehouse)` |
| `GET`  | `warehouse/receive/session/?warehouse=` — today's activations at this warehouse, for the running tally | same |
| `GET`  | `warehouse/my-warehouses/` | **already exists** — populates the selector |

**Barcode app — approvals, report, settings**

| Method | Path | Perm |
|--------|------|------|
| `GET/POST` | `barcode/activation/requests/` | POST: `can_request_barcode_activation` |
| `GET`  | `barcode/activation/requests/<id>/` | either activation perm |
| `POST` | `barcode/activation/requests/<id>/approve/` \| `/reject/` | `can_approve_barcode_activation` |
| `GET`  | `barcode/activation/pending/` — aging report, grouped by date/line/pallet | any barcode perm |
| `POST` | `barcode/activation/void/` `{box_ids[], reason}` — bulk void stale pending | `can_approve_barcode_activation` |
| `GET/PUT` | `barcode/activation/settings/` | `can_manage_barcode_dispatch_settings` |

`activate_boxes(..., source=APPROVAL)` is called by the approve endpoint and skips
the warehouse-match check only in the sense that there is no *scanned* warehouse to
compare — it still activates each box into its own `current_warehouse` and refuses
a box that is no longer `PENDING`.

---

## 5. Frontend (`c:\Users\gurpa\dev\FactoryFlow`)

### 5.1 Page 1 — Receive (warehouse module)

`src/modules/warehouse/pages/receive/BarcodeReceivePage.tsx` → **`/warehouse/receive`**,
listed in the warehouse module nav, gated on `warehouse.can_receive_barcodes`.

- **Warehouse selector** — from `useWarehouseScope()` / `useMyWarehouses()`, offering
  only warehouses the user manages (superuser sees all). Sticky per device: the gate
  receiver picks `BH-PF` once at shift start, never again. Never free text.
- **Scanner-first body** — reuse `BarcodeScanner` + `useScanner` and the
  `shared/components/scanReview` kit (already renders both docking review and every
  BST screen — do not fork the markup) for the accepted/rejected rows.
- **Box scan** → activate, row turns green with item/batch/qty.
- **Pallet scan** → count-confirm dialog: *"How many boxes physically on this
  pallet?"* Match → whole pallet activates in one action. Short → the dialog switches
  that pallet into box-by-box mode and says why, and a verify ticket is opened.
- **Rejections are loud and specific** — wrong warehouse names both warehouses
  ("printed for `BH-FG`, you are receiving into `BH-PF`"); already-active says when
  and by whom; unknown barcode is distinguished from another company's barcode
  (`ScanService.explain_scan_miss` already does this).
- **Running tally** for the shift: activated boxes / pallets / rejected, so the
  receiver can reconcile against the trolley in front of them.

New `api/receive.api.ts` + `api/receive.queries.ts` in the warehouse module,
alongside the existing `bst.*` / `userWarehouse.*` pairs.

> `useWarehouseScope` **fails open** by design (a 404 from an undeployed backend
> once concluded that every user managed nothing and took out BST creation on
> 27 Aug 2026). Keep that behaviour: the server is the enforcement point.

### 5.2 Page 2 — Activation approvals (barcode module, admin)

`src/modules/barcode/pages/ActivationApprovalsPage.tsx` (+ detail) →
**`/barcode/activation-approvals`**, gated on `barcode.can_approve_barcode_activation`.
No scanning anywhere on this page.

- Queue of `BarcodeActivationRequest` rows: requester, printed-at, item, batch,
  warehouse, box count, pallet, and the **mandatory reason** they typed.
- Approve → boxes activate immediately with `activation_source=APPROVAL`.
  Reject → boxes stay `PENDING`, decision note goes back to the requester.
- Reuse the `barcode/components/verify` ticket kit and the `PalletVerifyRequest`
  notification pattern (both directions, best-effort).
- **The request is raised from the label printing page**, not from here:
  `LabelGeneratePage.tsx` gets a *Request activation approval* action next to the
  print button, scoped to the pallet/batch just printed, reason required.

Because this route activates stock with no physical scan behind it, it is the one
that can re-introduce phantom stock. Mitigation is visibility, not friction:
`activation_source=APPROVAL` is stamped on every box, so a single filter answers
"what is in BH-PF that nobody ever scanned in?"

### 5.3 Page 3 — Pending activation report (barcode module)

`src/modules/barcode/pages/PendingActivationPage.tsx` → **`/barcode/activation/pending`**.
Required by D4 (manual void, no scheduled job): aging buckets, grouped by print
batch / line / pallet, multi-select **bulk void** with a reason, gated on
`can_approve_barcode_activation`. Link it from the receive page so the gate
receiver can see what was printed today but never arrived.

### 5.4 Shared frontend changes

| File | Change |
|------|--------|
| `barcode/types/barcode.types.ts:6-15` | add `'PENDING'` to `PalletStatus` + `BoxStatus`; activation request/receive types |
| `barcode/api/barcode.api.ts`, `.queries.ts` | approval + pending-report endpoints |
| `config/permissions/warehouse.permissions.ts` | `RECEIVE_BARCODES` |
| `config/permissions/barcode.permissions.ts` | `REQUEST_BARCODE_ACTIVATION`, `APPROVE_BARCODE_ACTIVATION` |
| `config/constants/api.constants.ts` | new paths beside `MY_WAREHOUSES` |
| `warehouse/module.config.tsx`, `barcode/module.config.tsx` | routes + nav entries (children honour **only** `permissions`) |
| `barcode/pages/BoxListPage`, `PalletListPage`, detail pages | PENDING badge (amber), "Pending activation" filter tab, `activated_at`/`by`/`source` on detail |
| `barcode/pages/LabelGeneratePage.tsx` | "These labels are inactive until received at the godown" + *Request activation approval* |
| `barcode/pages/PalletizePage.tsx` | unpalletized-box picker must include PENDING boxes |

Per `factoryflow-build-does-not-typecheck`: `vite build` skips tsc — run
`tsc --noEmit` and filter to the touched paths. Watch the shared barrel files
(`api/index.ts`, `components/index.ts`) — stage only your own exports.

---

## 6. Tests

New `barcode/tests_activation.py` (+ additions to `gate_core` dispatch tests):

1. Generation into an enforced warehouse → `PENDING`; into a non-enforced one → `ACTIVE`.
2. Print workflow end-to-end still works: generate → attach to pallet → pallet is
   `PENDING`, **not** `EMPTY` (regression guard for trap 3/4).
3. Gate scan of a pending box into the matching warehouse → `ACTIVE` + movement +
   audit row; into a different warehouse → `WAREHOUSE_MISMATCH`, no state change.
4. Pallet scan, count matches → all boxes + pallet active. Count short → nothing
   activated, `requires_box_scan`, a `PalletVerifyRequest(source=GATE)` exists.
5. Pending box rejected by: docking scan, barcode DispatchSession, BST scan,
   intercompany scan — each with its reject code.
6. Approval route: request from the label page → approve → boxes active with
   `source=APPROVAL`, no scan involved; reject leaves them `PENDING`.
6b. Receive scan into a warehouse the user does **not** manage → 403, nothing
   activated (and an unassigned user is refused outright — `warehouse_scope`
   treats "no assignment" as no access).
7. Pending pallet never reaches the WMS map.
8. Aging report + bulk void (permission-gated).

Run per `test-run-deletes-untracked-files`: explicit app labels only, never a bare
`manage.py test`, never `--parallel`:
`python manage.py test barcode gate_core warehouse --settings=<scratchpad settings>`
(shared-dev-PG contention → use the scratchpad settings module with a `TEST NAME`
override).

---

## 7. Rollout

1. Deploy code with `BarcodeActivationSettings.is_enabled = False` everywhere —
   zero behaviour change, migrations applied, screens present but idle.
2. Create the three permissions and assign: godown receiver →
   `warehouse.can_receive_barcodes`; floor printer →
   `barcode.can_request_barcode_activation`; PF supervisor →
   `barcode.can_approve_barcode_activation`.
3. **Assign the receivers as warehouse managers of `BH-PF`** (`UserWarehouse`).
   Without a row they are blocked, by design — run
   `manage.py report_warehouse_scope_gaps` first to see who would be locked out.
   ⚠️ Confirm `warehouse` migration `0017` (UserWarehouse) is actually applied on
   prod before shipping; it was last noted as pending. The receive page cannot
   work without it.
4. Migrate **both** databases per `live-db-without-flipping-env`:
   LIVE `…117/factory_flow` and test `…118/factory`.
5. Brief and staff the gate desk; dry-run a shift with enforcement still off (scan
   screen works, labels are still born ACTIVE).
6. Flip `is_enabled=True` with `enforced_warehouses=["BH-PF"]` for the one company.
   Watch the pending-aging report and the rejected-scan report for a week.
7. Rollback = flip the toggle off; already-pending boxes are activated via the
   approval route or bulk-voided.

**Day-1 risk to watch:** with D3 (reject at dispatch), any label that misses the
gate stalls at the dock. The per-warehouse toggle plus the approval route are the
two release valves; do not enable enforcement on a shift with no approver on duty.

---

## 8. Sequence

| Phase | Content | Rough size |
|-------|---------|-----------|
| 1 | Models, settings, permissions, migration + LEGACY backfill | ~½ day |
| 2 | Generation → PENDING; fix the six traps in §3.1 | ~1 day |
| 3 | `activation_service` + `warehouse/views_receive.py` + reject codes in §3.2 | ~1 day |
| 4 | Approval request workflow + notifications | ~½ day |
| 5 | Pending-aging report + bulk void | ~½ day |
| 6 | Frontend: receive page (warehouse), approvals page (barcode), pending report, badges/filters/types | ~2 days |
| 7 | Tests + `barcode/docs/README.md` update + deploy | ~1 day |

Phases 1–3 are the load-bearing ones; 4–5 can ship a release later if the gate desk
needs to start sooner, as long as the approval route exists (it is the only escape
hatch once enforcement is on).
