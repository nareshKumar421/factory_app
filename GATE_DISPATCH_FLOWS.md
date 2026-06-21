# Gate → Dispatch Flows (sales dispatch lifecycle)

The whole sales-dispatch lifecycle is one pipeline hanging off a **bill**
(`DispatchPlan`), tracked by a single current **stage**:

```
BOOKED → EMPTY_IN → READY_TO_DOCK → DOCKED/scanning → PHOTO_ATTACHED
       → READY_FOR_GATEPASS → GATEPASS_PRINTED → PRINT_COMMITTED → DISPATCHED
                                                          (+ REJECTED / CANCELLED)
```

The stage is computed by `compute_pipeline_stage(plan)` (`dispatch_plans/services.py`)
from the bill's booking status, its linked empty-vehicle gate-in, and its
representative docking gate-out. The "`<status> at <module>`" label (e.g.
"docked at dock", "pending at sales dispatch out") comes from
`pipeline_module_status` / `compute_pipeline_status` next to it.

---

## 1. Vehicle Linking (planner) — `BOOKED`

The planner attaches bills to a vehicle (`PATCH /dispatch-plans/bills/{docEntry}/plan/`).
Sets `booking_status=BOOKED`, `vehicle`, and `linked_invoice_doc_entries`. The
truck then appears in **Empty Vehicle In → Expected Dispatch**.

- **Link lock** — once the gate-in completes the vehicle has physically arrived,
  so vehicle/transporter/driver can no longer be edited or unlinked
  (`is_vehicle_link_locked` → `linked_vehicle_entry.status == "COMPLETED"`,
  guard `_assert_link_not_locked`).
- **Late booking** — a bill booked *after* the gate-in: links to an existing
  cover for that exact bill if present; else, if the truck is inside, it
  auto-attaches to that gate-in; else it flows through the correction path.

## 2. Empty Vehicle In (gate) — `EMPTY_IN → READY_TO_DOCK`

The gate confirms the truck is in and completes the gate-in
(`EmptyVehicleGateIn`). On completion it **snapshots covers**
(`EmptyVehicleGateInCover`) — one per booked, unlinked bill — keyed on the stable
`sap_doc_entry`. Covers are the bill-accurate identity every downstream step
reads (`gate_core/services/empty_vehicle_dispatch.py`).

- **Inside-guard** — a vehicle can be inside only once. A second "Start Entry"
  is blocked with a message naming the open entry. "Inside" = a live gate-in:
  in-progress, or completed and not yet departed (not retired, no completed
  empty-vehicle or BST gate-out).
- **Already-inside flag** — Expected Dispatch shows an "Already inside" badge and
  blocks Start Entry for trucks already in (the `inside_only` list feeds this).
- **Auto-attach** — a late bill booked onto an already-inside truck joins that
  gate-in's covers (no second entry), bounded by the load photo-lock
  (`attach_bill_to_inside_vehicle`).
- **Cross-branch** — bills from different SAP branches are different physical
  flows (a gatepass carries one branch/GSTIN), so they get separate
  gate-ins/dockings; they are never merged.

## 3. Docking — `DOCKED → … → PRINT_COMMITTED`

A bill is **dockable** iff: `BOOKED`, with a live, unconsumed cover on a
non-retired, COMPLETED gate-in (`pending_dispatch_plan_queryset`,
`gate_core/views_sales_dispatch.py`). The gate creates a docking
(`SalesDispatchGateOut`) from the selected bills (documents) → scans boxes →
attaches the truck photo (locks the load) → prints the gatepass → commits.

- **Auto-merge of a late bill** into the truck's open docking, before the photo
  is attached, with a manual "Add to docking" fallback if SAP was unavailable
  (`merge_bill_into_open_docking`, `add_plan_to_open_docking`). Same-branch only.
- **Staggered docking** — dock some bills now, the rest later (they stay
  "pending at dock").
- **Duplicate guard** — a bill already on an active docking can't be docked
  again.

## 4. Sales Dispatch Out (gate-out) — `DISPATCHED`

After print-commit: gross weighment → mark dispatched → **consume the covers**
→ **retire the gate-in once all covers are consumed** (the truck is gone).

- **Reverse** — reject / cancel / un-dock un-consumes the covers and un-retires
  the gate-in (`unconsume_covers_for_plans`).

## 5. Retirement lifecycle (the core bug fix)

Original bug: a reused truck's new bills auto-linked to an *old* gate-in
(matched by vehicle number only) and showed dockable forever. Now:

- A gate-in is **retired** on full dispatch, empty-vehicle-out, or cancel
  (`retired_at` / `retired_reason`).
- A **retired gate-in makes none of its bills eligible** — a returning truck
  needs a fresh gate-in with fresh covers.
- Migration `0043` retroactively retires already-departed gate-ins so existing
  data behaves the same (see Deploy notes).

## 6. Cross-company arrival (Part B) — built, anchor-style

One physical truck carrying bills for Jivo Oil / Beverages / Mart can be tied
together by one `VehicleArrival` (not company-scoped) anchoring a per-company
gate-in + docking chain, with a single unified depart / empty-out
(`create_vehicle_arrival`, `views_arrival.py`). Legacy single-company records
have `arrival = null` and behave exactly as before.

## 7. Partial dispatch (Part C)

At gatepass readiness, comparing scanned vs invoiced per bill:

- **0 scanned → whole-bill drop** — the bill reschedules onto a future trip (no
  credit note); boxes released, cover voided, plan back to `BOOKED`.
- **Short (some held) → approval + credit note** — needs
  `PartialDispatchApproval` = APPROVED **and** a `CREDIT_NOTE` attachment before
  the gatepass can print.

## 8. Pipeline status (UI)

`compute_pipeline_status` → `{stage, module, "X at Y" label}`. Per vehicle it is
the shared stage of its bills (they move in parallel; a least-advanced fallback
covers only the cross-branch-on-one-gate-in anomaly). Surfaced on Empty Vehicle
Entries, Vehicle Linking, the docking / sales-dispatch-out tables, and the
per-module "Expected vehicles" lists.

---

## Cross-cutting edge cases

- **Cross-branch** = separate flows (e.g. the 9967 truck: a FACTORY oil bill vs
  a DELHI INFO IT-services bill — different branches, different gatepasses).
- **Vehicle-number variants** (`DL01LAM0715` vs `DL1LAM0715`) are different
  `Vehicle` rows; everything groups by `vehicle_id`, never the raw string.
- **Multiple live gate-ins** on one truck = a data anomaly; the inside-guard
  flags it.
- **Reused truck, same day** = each trip is its own gate-in (+ arrival); trip 1
  retires on dispatch before trip 2 can proceed.

## Deploy notes (migrations)

Order: `dispatch_plans 0013` → `gate_core 0039 → 0040 → 0041 → 0042 → 0043`.

- `0039` adds `retired_at` + the covers table; `0040` backfills covers for
  in-flight gate-ins so their bills stay dockable.
- `0041` adds `VehicleArrival` + nullable `arrival` FKs; `0042` adds nullable
  `dispatched_quantity`, the `CREDIT_NOTE` attachment choice, and
  `PartialDispatchApproval`.
- `0043` retires already-departed dispatch gate-ins (no live cover) so the
  inside-guard does not falsely block returning trucks after first deploy.

All additive / nullable / idempotent. Run once on a staging copy before main.
