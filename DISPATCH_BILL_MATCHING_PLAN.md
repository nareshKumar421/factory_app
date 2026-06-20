# Bill-Accurate Dispatch Gate-In + Cross-Company Unified Arrival

## Context

**The bug (root cause, verified in code).** A dispatch empty-vehicle gate-in is matched to bills by **vehicle number only**, never by bill, and is **never retired** after the loaded truck leaves. Three matchers all key off `vehicle_id`:
- Completion matcher — `gate_core/views.py:671-682` links *all* BOOKED plans for the vehicle.
- Late-booking matcher — `dispatch_plans/services.py:571-609` (`_link_completed_empty_in`) links a new plan to the most-recent COMPLETED dispatch gate-in for `plan.vehicle_id`.
- Docking eligibility — `gate_core/views_sales_dispatch.py:277-328` (`pending_dispatch_plan_queryset`) builds `completed_dispatch_gate_in_vehicle_ids` then filters `vehicle_id__in`.

And `SalesDispatchMarkDispatchedView` (`views_sales_dispatch.py:2029-2089`) never touches the source gate-in, so a reused truck's COMPLETED gate-in stays "eligible" forever. Net effect: a reused truck's new bills auto-link to an old/foreign bill's gate-in and appear dockable without their own gate-in.

**Reframed domain understanding (from the user).**
- The **planner** attaches bills to a vehicle at **Vehicle Linking** (`PATCH /dispatch-plans/bills/{docEntry}/plan/`, already sends `linked_invoice_doc_entries[]`, sets `booking_status=BOOKED`). The vehicle then appears in **Empty Vehicle In → Expected Dispatch** *and nowhere else*.
- The **gate person** only confirms the vehicle is inside (completes the gate-in) — they do **not** pick bills.
- "An empty vehicle in cannot appear without first getting linked with an invoice" → there is no "gate-in with no bills" case; covers are always derivable from booked plans.
- Correction path the user sanctioned for any wrong bill↔vehicle attachment: **Empty-Vehicle-Out → re-link in Vehicle Linking → re-gate-in** (the existing `release_dispatch_plans_for_empty_out`, `gate_core/views.py:2604-2667`).
- **One physical truck can carry bills for all 3 companies** (Beverages/Oil/Mart). The user chose to **build a cross-company unified arrival**.

**Architecture reality (verified).** `Vehicle`/`Driver`/`Transporter` are **global** (no company FK; `vehicle_number` globally unique). `VehicleEntry`, `EmptyVehicleGateIn`, `DispatchPlan`, `SalesDispatchGateOut` are **strictly company-scoped**, each company has its own SAP HANA DB, and `sap_invoice_doc_entry` only exists within one company. There is **no cross-company concept anywhere** today. Active company is resolved from the `Company-Code` HTTP header (`company/permissions.py:HasCompanyContext`); a user's companies come from `UserCompany`.

**Outcome.** (A) Make matching/eligibility/retirement **bill-accurate within each company** (fixes the reported bug). (B) Add a **cross-company unified arrival** that anchors the whole physical lifecycle — one truck is gated in once, loaded across all companies, and departs once — with every per-company gate-in *and* docking hanging off that single arrival.

---

## Architecture decisions

1. **Explicit covers, not vehicle-matching.** A dispatch gate-in records the exact bills it covers via a through-model keyed on the stable `sap_doc_entry`. All matching/eligibility/retirement read covers.
2. **Covers are auto-derived, not gate-person-selected.** At gate-in completion the system snapshots the vehicle's currently BOOKED + unlinked plans (per company) as covers — the planner already curated them. (Frontend may also pass them explicitly for precision.)
3. **Retirement lifecycle.** A gate-in is *retired* once all its covered bills are dispatched, or the truck leaves empty (empty-out). **"Retired ⇒ makes no plan eligible" means:** a retired gate-in stops contributing *any* of its bills to the docking-ready list (`pending_dispatch_plan_queryset`). So a truck that has already left can no longer keep old/foreign bills showing as "ready to dock" — which is exactly the current bug. Concretely: eligibility joins a plan → its covering gate-in, and skips the plan if that gate-in's `retired_at` is set (or the plan's cover is already consumed).
4. **`VehicleArrival` is the cross-company anchor** (NOT company-scoped) that threads the **entire physical lifecycle** of one truck trip — **gate-in (`EmptyVehicleGateIn`) → loading/docking (`SalesDispatchGateOut`) → departure** — across all companies. *Every* per-company record in that chain carries an `arrival` FK, not just the gate-in. The physical truck arrives once, is loaded across companies, and departs once; the arrival is the single object that ties those per-company chains together. A single-company truck is just an arrival whose chain happens to touch one company (backward compatible).
5. **Cross-company reads are one ORM query** (`DispatchPlan.company__in=user_companies`); cross-company writes create one company-scoped gate-in per company in a transaction. We do **not** merge SAP DBs.

---

## Part A — Per-company bill-accurate gate-in + retirement (foundation; fixes the bug)

### A1. Schema — `gate_core/models/empty_vehicle_gate_in.py`
- New `EmptyVehicleGateInCover(BaseModel)`:
  - `empty_vehicle_gate_in` FK→`EmptyVehicleGateIn` (CASCADE, related_name `covers`)
  - `dispatch_plan` FK→`dispatch_plans.DispatchPlan` (SET_NULL, null=True) — may be null if the bill was gated before the plan row existed
  - `sap_doc_entry` Int (stable bill identity; mirrors `DispatchPlan.sap_invoice_doc_entry`), `sap_doc_num` Char
  - `consumed_at` DateTime null — set when this bill's docking dispatches
  - `UniqueConstraint(empty_vehicle_gate_in, sap_doc_entry)`; index on `sap_doc_entry`, `dispatch_plan`
- Add to `EmptyVehicleGateIn`: `retired_at` DateTime null, `retired_reason` Char (e.g. `DISPATCHED`/`EMPTY_OUT`/`CANCELLED`); `is_retired` property. Keep `document_reference` for display.
- Migration: additive (no behavior change yet). Plus a **data migration/backfill**: for every active COMPLETED, non-retired DISPATCH gate-in, create cover rows from its currently-linked BOOKED plans (`DispatchPlan.linked_vehicle_entry == gate_in.vehicle_entry`) so existing in-flight bills don't lose docking eligibility when readers switch (A5).

### A2/A3. Covers population + completion matcher — `gate_core/views.py` `EmptyVehicleGateInCompleteView` (671-682)
Replace the vehicle-wide blanket `.update(linked_vehicle_entry=...)` with a snapshot that also records covers: select the company's BOOKED + `linked_vehicle_entry__isnull=True` plans for `gate_in.vehicle`, set their `linked_vehicle_entry`, and `bulk_create` an `EmptyVehicleGateInCover` per plan (`sap_doc_entry`, `sap_doc_num`, `dispatch_plan`). Accept an optional explicit bill list from the request to constrain the set (frontend already knows it). Zero matching plans ⇒ zero covers (allowed).

### A4. Late-booking matcher — `dispatch_plans/services.py:571-609` (`_link_completed_empty_in`)
Make **bill-strict**: only link a freshly-BOOKED plan to a COMPLETED, **non-retired** gate-in that **already has a cover for `plan.sap_invoice_doc_entry`** (the gate-in-before-plan case) and whose `vehicle_id == plan.vehicle_id`. On match, set `linked_vehicle_entry` and attach `dispatch_plan` onto that cover. Otherwise **do not auto-link** — the new bill flows through the correction path (empty-out → re-link → re-gate-in). This is the single change that kills the reported bug's auto-mislink. Caller at `services.py:332` unchanged.

### A5. Docking eligibility — `gate_core/views_sales_dispatch.py:277-328` (`pending_dispatch_plan_queryset`)
Remove the `completed_dispatch_gate_in_vehicle_ids` / `vehicle_id__in` block. A plan is dockable iff: `booking_status=BOOKED`, `linked_vehicle_entry__status="COMPLETED"`, its covering gate-in is **not retired**, and its cover row is **not consumed** (join `covers` where `empty_vehicle_gate_in=linked_vehicle_entry.empty_vehicle_gate_in`, `consumed_at__isnull=True`, `retired_at__isnull=True`); `.distinct()`. Keep the three existing `.exclude(...)` active-docking clauses (313-315). This is bill-accurate by construction.

### A6. Retire on dispatch — `gate_core/views_sales_dispatch.py:2029-2089` (`SalesDispatchMarkDispatchedView`)
Inside the existing `transaction.atomic()`, after plans flip to DISPATCHED: set `consumed_at=now` on the cover rows for the dispatched plans (match by `dispatch_plan` or `sap_doc_entry`); for each touched gate-in, if **all** active covers are consumed → set `retired_at`, `retired_reason="DISPATCHED"`. New reusable helper `retire_empty_in_if_fully_consumed(gate_in, user, reason)` next to `release_dispatch_plans_for_empty_out` in `gate_core/views.py`.

### A7. Reverse on reject / cancel / empty-out
- `SalesDispatchRejectView` / `SalesDispatchCancelView` (`views_sales_dispatch.py:2092+/2123+`): un-consume covers for the gate-out's plans and un-retire the gate-in if no longer fully consumed (new helper `unconsume_covers_for_plans`). Normally a no-op (reject/cancel precede dispatch) but correct for un-dock-after-dispatch.
- `release_dispatch_plans_for_empty_out` (`gate_core/views.py:2604-2667`) already clears `linked_vehicle_entry=None` on the BOOKED plans and cancels unscanned dockings. Extend it to **also retire the source gate-in** (`retired_reason="EMPTY_OUT"`, resolved via `vehicle_entry.empty_vehicle_gate_in`).
  - **Net round-trip after empty-out (the sanctioned correction path):** (a) the bills stay `BOOKED` but unlinked, so they **reappear in Empty Vehicle In → Expected Dispatch**, ready to be gated in again; (b) **vehicle linking/editing re-enables** — the lock (`is_vehicle_link_locked` → `linked_vehicle_entry.status=="COMPLETED"`, `serializers.py:149-160`; guard `_assert_link_not_locked`, `services.py:535-569`) releases the moment `linked_vehicle_entry` is null; (c) the old gate-in is retired so it no longer makes any bill eligible. Retirement does **not** block re-entry — re-gating creates a fresh gate-in with fresh covers.

### A8. `fix_orphaned_dispatch_links` — `dispatch_plans/management/commands/fix_orphaned_dispatch_links.py`
Add a second orphan class: active BOOKED/PENDING plans whose `linked_vehicle_entry` belongs to a **retired** gate-in → clear the link (keep dry-run default + the concurrent-safe `.filter(id=…, linked_vehicle_entry_id=…)` update at 75-78).

---

## Part B — Cross-company unified arrival (anchors the whole flow: gate-in → docking → departure)

Per comment: the cross-company anchor must thread the **entire** physical lifecycle, not just the gate-in. So both `EmptyVehicleGateIn` **and** `SalesDispatchGateOut` carry an `arrival` FK, and the truck's physical **departure** is a single cross-company event.

### B1. `VehicleArrival` — new `gate_core/models/vehicle_arrival.py` (explicitly NOT company-scoped)
Fields: `arrival_no` (unique; `ARV-YYYYMMDD-####`, same generator style as existing entry-no helpers), `vehicle` FK (global), `driver` FK (global), `gate_in_date`, `in_time`, `tare_weight`, `weighbridge_slip_no`, `status` (`INSIDE` → `LOADING` → `DEPARTED`, plus `CANCELLED`), `security_name`, `remarks`; physical-exit fields `gate_out_date`, `out_time`, `exit_security_name`, `departed_at`; cancel fields. Convenience: `companies` (distinct across child gate-ins), `is_fully_dispatched`. Represents **one physical truck trip** and is the parent of every per-company record below.

### B2. Anchor FKs on BOTH gate-in and docking
- `EmptyVehicleGateIn.arrival = FK(VehicleArrival, null=True, related_name="gate_ins")`.
- `SalesDispatchGateOut.arrival = FK(VehicleArrival, null=True, related_name="gate_outs")` — so each company's docking is reachable from the physical arrival (the truck is loaded across companies under one trip).
- Both nullable ⇒ backward compatible: legacy single-company records keep `arrival=null` and behave exactly as today.

### B3. Cross-company gate-in API — new views in `gate_core/views.py` (+ routes in `gate_core/urls.py`)
- `GET /gate-core/arrivals/expected/?vehicle_id=` → resolve the user's companies (`UserCompany.objects.filter(user=request.user, is_active=True)`), one ORM query `DispatchPlan.objects.filter(company__in=companies, vehicle_id=X, booking_status=BOOKED, linked_vehicle_entry__isnull=True, is_active=True)`, group by company → expected bills per company. (Local rows only; no SAP call.)
- `POST /gate-core/arrivals/` → body `{vehicle_id, driver_id, gate_in_date, in_time, tare_weight, weighbridge_slip_no?, per_company_bills?}`. In one transaction: create `VehicleArrival` (`INSIDE`); for each company with bills, create a company-scoped `VehicleEntry` (`entry_type=SALES_DISPATCH`) + `EmptyVehicleGateIn(reason=DISPATCH, arrival=…)` marked COMPLETED, write the **one** physical tare to each company's `vehicle_entry.weighment`, and record covers (reuse A2/A3 per company). Companies with no bills are skipped. A new permission iterates the user's companies instead of the single `Company-Code` header.
- Guard: refuse a new arrival if the vehicle already has an `INSIDE`/`LOADING` arrival (cross-company analogue of the per-company "active gate-in" guard at `views.py:361-376`).

### B4. Docking under the arrival — `gate_core/views_sales_dispatch.py` `SalesDispatchGateOutListCreateView.post`
When a per-company docking is created, set `SalesDispatchGateOut.arrival` from the vehicle's current `INSIDE`/`LOADING` arrival (resolve via the company gate-in's `arrival`, or by vehicle + open arrival). On first docking of a trip, flip `arrival.status` `INSIDE → LOADING`. Tare copy is unchanged (`_copy_empty_vehicle_tare_weighment`, `views_sales_dispatch.py:998`) — the value already equals the arrival tare. Docking/box-scan/gatepass remain per-company; the arrival just threads them together for a unified view and departure.

### B5. Cross-company departure — single physical exit
The truck leaves once. Add `POST /gate-core/arrivals/{id}/depart/` (gate exit): requires **every** company chain to be commercially complete (all child gate-ins retired / all dockings `DISPATCHED`), records the single physical exit on the arrival (`gate_out_date`, `out_time`, `exit_security_name`, `departed_at`, `status=DEPARTED`). Also auto-advance: when `SalesDispatchMarkDispatchedView` retires the last outstanding gate-in of an arrival (A6), mark the arrival **ready to depart** (and `DEPARTED` if a unified exit step isn't used). Per-company `dispatched` state stays as-is; the arrival is the one physical "truck out" record. Surface in the gate dashboard / gate-entry view as one exit event.

### B6. Cross-company correction (the user's escape hatch, unified)
`POST /gate-core/arrivals/{id}/empty-out/` → for each child gate-in call `release_dispatch_plans_for_empty_out` (clears links, cancels unscanned dockings, retires the gate-in `EMPTY_OUT`) and set `arrival.status=CANCELLED`. One action resets the whole physical trip across all companies; the planner then re-links and the truck is re-gated. Single-company empty-out (existing per-company endpoint) still works and, if the gate-in has an `arrival`, updates the arrival too.

---

## Part C — Partial dispatch (whole-bill reschedule + item-level credit-note approval)

**Detection (single rule).** At **gatepass readiness (before print)**, compare **scanned vs invoiced** per bill/document (scanned from `SalesDispatchBoxScan`; invoiced from `SalesDispatchGateOutItem`/document). Per bill: **full** → proceed; **zero scanned** → C1 (whole-bill drop); **short (>0 but < invoiced)** → C2 (partial, needs approval + credit note).

### C1. Whole-bill drop — operational reschedule (no approval, no credit note)
New action `POST /gate-core/sales-dispatch/{id}/documents/{doc_id}/remove/`: in one transaction — cancel that bill's `SalesDispatchGateOutDocument` + its `SalesDispatchGateOutItem`s, release its `SalesDispatchBoxScan`s (boxes back to available), return its `DispatchPlan` to `BOOKED` + `linked_vehicle_entry=None`, and **release that bill's cover** (void the `EmptyVehicleGateInCover`). The bill reappears in Expected Dispatch for a future trip; the gate-in/arrival retires on the **remaining** bills only. (Reuses the unlink/release pattern of `release_dispatch_plans_for_empty_out`.) No credit note — the invoice will be fulfilled on a later trip.

### C2. Partial items within a bill — approval + credit note, **blocks gatepass print**
- **Held items:** add `dispatched_quantity` (+ optional `is_held`) to `SalesDispatchGateOutItem`; shipped = scanned, shortfall = **credited** (held items are *not* rescheduled under this invoice).
- **`PartialDispatchApproval`** (new model, per docking+document): `sales_dispatch` FK, `document` FK, held-items snapshot + `reason`, `status` (`PENDING`/`APPROVED`/`REJECTED`), `requested_by/at`, `approved_by/at`. New permission `can_approve_partial_sales_dispatch`.
- **Credit note (attachment + reference only):** add `CREDIT_NOTE` to `SalesDispatchAttachmentType` (`gate_core/models/sales_dispatch.py:29-36`) + a `credit_note_no` field. SAP credit-note posting is **out of scope** (done separately).
- **Gate at gatepass print:** in the gatepass print/assign path (`assign_gatepass`, `models/sales_dispatch.py:414`; the gatepass-print view; `gatepass_readiness` in `serializers_sales_dispatch.py`) **block printing** while any bill on the load is partial unless its `PartialDispatchApproval` is `APPROVED` **and** a `CREDIT_NOTE` attachment + `credit_note_no` exist. Surface the block reason in `gatepass_readiness`.
- **Covers/retirement:** a partially-dispatched bill is still `DISPATCHED` at mark-out (its cover consumed); the shortfall is accounted by the credit note, not deferred.

---

## Frontend — `c:\Users\gurpa\dev\FactoryFlow`
- **Types/API** (`src/modules/gate/api/emptyVehicleIn/emptyVehicleIn.api.ts`): add covers to `EmptyVehicleGateInCreateRequest`/`Entry`; add new `arrivals` api module (`expected`, `create`) + react-query hooks mirroring `emptyVehicleIn.queries.ts`.
- **Expected dispatch** (`src/modules/gate/pages/emptyVehicleInPages/emptyVehicleInDispatch.ts` + `EmptyVehicleInPage.tsx`): switch the source from per-company `useDispatchBills` to the cross-company `arrivals/expected` endpoint; group rows by **company** under each vehicle.
- **Start Entry / arrival** (`EmptyVehicleInNewPage.tsx`): one form — pick vehicle+driver, show expected bills grouped by company (read-only confirmation), enter tare once, submit → `POST /arrivals/`. Keep the legacy single-company create for non-dispatch reasons (BST/repair/job-work) unchanged.
- **Docking + departure under the arrival** (`SalesDispatchDashboardPage.tsx` / gate dashboard): show the arrival's per-company chains together (one truck → Beverages/Oil/Mart loads) and a single **Depart** action (`arrivals/{id}/depart`) enabled only when all companies are dispatched; plus a unified **Empty-out** (`arrivals/{id}/empty-out`).
- **Partial dispatch** (docking detail, `SalesDispatchNewPage.tsx`/detail): show scanned-vs-invoiced per bill; a **Remove bill** action (C1); a **Request partial-dispatch approval** flow (held-item selection + reason) and an **Approve/Reject** screen (gated by `can_approve_partial_sales_dispatch`); a **credit-note upload + number** field; and a **Print Gatepass** button blocked with the `gatepass_readiness` reason until approved + credit note attached.
- **Vehicle Linking** (`DispatchVehicleLinkingPage.tsx`): unchanged (already books bills + sends `linked_invoice_doc_entries[]`).
- **Company header**: the arrivals endpoints are cross-company; the axios company interceptor (`src/core/api/client.ts`) should omit/zero `Company-Code` for these (new permission ignores it).

---

## Edge cases (and resolution)
1. **Cross-day accumulation** (user's example: 5 today + 1 stale from yesterday): covers snapshot the vehicle's BOOKED+unlinked plans, so a still-BOOKED stale bill is included only if it's genuinely still booked to the truck. If wrong → correction path. If yesterday's bill sits under yesterday's (un-retired) gate-in, consolidate via empty-out + re-link + re-gate-in.
2. **Same truck, multiple trips/day**: each arrival is separate; trip-1 retires on full dispatch; INSIDE-arrival guard (B3) blocks a concurrent second arrival until trip 1 dispatches.
3. **Partial dispatch of a multi-bill trip**: per-cover `consumed_at`; gate-in/arrival retire only when all covers consumed. Remaining bills stay dockable.
4. **Bill booked after gate-in**: links only if a cover already exists (A4); else correction path.
5. **Reject/cancel/un-dock**: A7 un-consumes covers + un-retires.
6. **Gate user lacks a company**: that company's bills are excluded from the arrival; document the access requirement (gate role needs `UserCompany` for all 3).
7. **Vehicle-number variants are different rows** (`DL01LAM0715` vs `DL1LAM0715` seen live): cross-company matching by `vehicle_id` misses bills booked under the other row. **Prerequisite data-hygiene task**: normalize/merge duplicate `Vehicle` rows (or match on a normalized number). Flagged as a risk, scoped separately.
8. **Historical ~38 COMPLETED gate-ins**: handled by the A1 backfill (cover rows from currently-linked BOOKED plans) so in-flight bills keep docking; truly stale ones get retired by `fix_orphaned_dispatch_links` + the retire-on-empty-out path.
9. **Single-company truck**: an arrival whose chain touches one company (one gate-in + that company's dockings) — same code path; legacy records with `arrival=null` behave exactly as today.
10. **Truck ready but one company not yet dispatched**: the unified depart (B5) refuses to release the physical truck until *every* company chain is DISPATCHED, so a truck can't leave the gate while one company's load is still incomplete; the others wait under `LOADING`.
11. **Cross-company correction mid-trip**: `arrivals/{id}/empty-out` (B6) resets all companies at once; a per-company empty-out resets only that company's chain but still updates the shared arrival.
12. **Whole-bill drop vs partial** (C): decided by scanned-vs-invoiced at gatepass readiness — 0 scanned ⇒ C1 reschedule (bill returns to Expected Dispatch, no credit note); short ⇒ C2 approval + credit note before print.
13. **Partial approval pending/rejected**: gatepass print stays blocked (`gatepass_readiness` reason); operator finishes scanning the held items, drops the bill (C1), or escalates for approval.

## Testing
- `gate_core/tests.py`: covers populated at completion; eligibility excludes retired/consumed; reused-truck new bill **not** dockable without a fresh gate-in (core regression); mark-dispatched retires when all covers consumed; partial multi-bill does not retire; reject un-consumes; empty-out retires; **cross-company arrival** creates one gate-in per company with shared tare and per-company covers; arrival departs only when all companies dispatched; INSIDE-arrival guard.
- `dispatch_plans/tests.py`: `_link_completed_empty_in` matches by bill not vehicle; backfills `dispatch_plan` on cover; extend `fix_orphaned_dispatch_links` for retired-gate-in links.
- Partial dispatch (C): `remove-bill` returns the plan to Expected Dispatch + releases boxes and the cover + retires the gate-in on remaining bills; a short bill **blocks** gatepass print until `PartialDispatchApproval=APPROVED` **and** a `CREDIT_NOTE` attachment + `credit_note_no` exist; held qty recorded; mark-out consumes the cover; `can_approve_partial_sales_dispatch` enforced.
- Reuse existing tare-copy and `is_vehicle_link_locked`/`compute_pipeline_stage` tests to confirm no regression (both still key off `linked_vehicle_entry.status`, unchanged semantics).

## Rollout (each step independently deployable)
1. **Schema + backfill** (A1) — additive; old code keeps running.
2. **Writers** (A2/A3/A4, B1/B2) — covers recorded, late-matcher bill-strict; readers still old → safe coexistence.
3. **Readers** (A5) — flip docking eligibility to covers; coordinate with frontend sending covers. This is the behavior flip.
4. **Retirement** (A6/A7/A8) then **cross-company arrival** (B1–B6 + frontend): `VehicleArrival` model + the two anchor FKs first, then the gate-in API (B3), docking anchor (B4), and the unified depart/empty-out (B5/B6).
5. **Partial dispatch** (C + frontend): schema (`dispatched_quantity`, `PartialDispatchApproval`, `CREDIT_NOTE` type) → `remove-bill` + approval/credit-note writers → gatepass-print guard → UI.

## Verification (end-to-end, against a test company/DB — not prod)
- Unit/integration: `python manage.py test gate_core dispatch_plans` (use the venv at `.venv/Scripts/python.exe`).
- Manual reproduction of the original bug on a scratch dataset: book bill X to a reused truck that has an old completed gate-in for bill Y → confirm X is **not** auto-linked and **not** dockable until its own gate-in; dispatch X → gate-in retires; reused truck needs a fresh gate-in.
- Empty-out round-trip: gate a truck in → empty-vehicle-out → confirm (a) its bills reappear in Expected Dispatch, (b) Vehicle Linking is editable again (lock released), (c) the old gate-in is `retired_at`-set and no longer makes bills dockable; then re-link + re-gate-in produces a fresh gate-in with fresh covers.
- Cross-company: book bills for the same vehicle in 2 companies → `arrivals/expected` shows both → one `POST /arrivals/` creates 2 gate-ins (each `arrival`-anchored) sharing one tare → each company's docking is created with the same `arrival` → dispatch each → `arrivals/{id}/depart` is blocked until *both* dispatched, then records one physical exit and flips the arrival DEPARTED. Confirm `arrivals/{id}/empty-out` resets all companies.

## Open risks / prerequisites
- **Vehicle-number duplication** (edge case 7) is a real data-quality prerequisite for reliable cross-company matching.
- **Cross-company gate permission/role**: needs a gate user with `UserCompany` access to all relevant companies; define this role.
- Behavior flip (A5) must be sequenced with the frontend covers change to avoid a window where bills stop appearing dockable.
