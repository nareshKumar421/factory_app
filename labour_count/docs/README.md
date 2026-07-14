# Labour — Backend (`labour_count` + `labour_gate`)

> Casual-labour headcount, gate in/out, and department allocation for the
> factory-operations platform. Django REST, no SAP.
>
> **Paired frontend doc:** [`FactoryFlow/docs/modules/labour.md`](../../../FactoryFlow/docs/modules/labour.md)
> (absolute: `C:/Users/gurpa/dev/FactoryFlow/docs/modules/labour.md`)

---

## Overview — what it does & who uses it

"Labour" is **two independent Django apps** that both track *casual / contract
labour by the head* (counts of anonymous labourers a **contractor** supplies),
not named individuals:

| App | Purpose | Primary user |
|-----|---------|--------------|
| **`labour_count`** | The **man-day register / attendance sheet**. A department supervisor enters the per-contractor headcount for a shift and submits it; the gate later verifies it by marking people out in batches and finalizing with a variance. Time-boxed by per-company shift lock/verify windows (with an auto-submit scheduler). | Department supervisor (submit) + gate operator (verify) |
| **`labour_gate`** | The gate's own **physical in/out headcount tally** per contractor per shift, plus an **HOD department-allocation split** of that intake. Soft-delete + append-only audit trail. | Gate person (in/out) + HOD (allocate) |

The two apps are **not linked by any foreign key**. They share only three
masters — `company.Company`, `accounts.Department`, `person_gatein.Contractor`
— and the DAY/NIGHT shift concept. They are two different views of the same
real-world thing (labour on site for a shift): `labour_count` is the register
that gets submitted/verified; `labour_gate` is the live gate tally + HOD split.

**Not this module:** `person_gatein.Labour` is a *named individual* labourer
(photo, ID, skill) used by the Visitor/Labour **gate-pass** flow. This module
never references `Labour` — it counts heads per `Contractor`.

**No SAP.** Neither app imports the SAP service layer, posts a document, or
touches a semaphore. This is a purely internal register/tally. (If you came
here expecting GRPO-style SAP posting, there is none.)

---

## Key concepts & entities

### `labour_count`  (`labour_count/models.py`)

- **`LabourShift`** — `DAY` (07:00–19:00), `NIGHT` (19:00–07:00). Text choices.
- **`SheetStatus`** — `DRAFT` → `SUBMITTED` → `VERIFIED`.
- **`VerifyBasis`** — `TOTAL` / `DEPARTMENT` / `CONTRACTOR` (how the gate tallied;
  the finalize flow always records `DEPARTMENT`).
- **`LabourShiftWindow`** — per-`(company, shift)` **editable** lock & verify
  times (`submit_lock_time`, `verify_deadline_time`, IST). If no row exists,
  code defaults apply (see `services/lock.py`).
- **`LabourCountSheet`** — one day's count for a unique
  `(company, department, work_date, shift)`. Holds `status`, `is_holiday`,
  `lock_at` (after which it auto-submits & locks), submit/verify audit fields,
  `gate_counted` (running out total), `verify_basis`, `verify_remark`.
  `total_count` property = sum of item counts. For NIGHT, `work_date` is the
  **shift-start** date.
- **`LabourCountItem`** — per-contractor count on a sheet, unique
  `(sheet, contractor)`. Only **non-zero** counts are stored; the grid
  re-derives zeros from the contractor roster.
- **`LabourVerification`** — an audit record of the gate's physical tally vs the
  submitted total (`basis`, optional `department`/`contractor`,
  `submitted_total`, `gate_counted`, `variance = gate_counted − submitted_total`).
- **`LabourOutBatch`** — one batch of people leaving, attached to a sheet.
  `sheet.gate_counted` is kept equal to the sum of its batches.

### `labour_gate`  (`labour_gate/models.py`)

- **`LabourGateEntry`** — one `(company, department, contractor, work_date,
  shift)` headcount. **Two kinds** distinguished by `department`:
  - `department = NULL` → the **gate intake** ("how many this contractor brought
    in this shift").
  - `department` set → an **HOD allocation split** (part of that intake assigned
    to a department, informational).
  - `count_in`, plus soft-delete fields (`is_active`, `deleted_at`,
    `deleted_by`). Properties: `total_out` (sum of out batches), `remaining`
    (`count_in − total_out`). Day and night are fully separate rows.
- **`LabourGateOutBatch`** — one batch marked out against an entry.
- **`LabourGateAudit`** — **append-only, never edited** trail. One row per
  action: `CREATE_IN`, `UPDATE_IN`, `MARK_OUT`, `UNDO_OUT`, `DELETE`, `RESTORE`
  (with `old_value`/`new_value`, `detail`, `performed_by`).

> Note: `LabourShift` is declared in **both** apps. In `labour_gate` the labels
> are just "Day"/"Night" (no time range) and the model default is `DAY`. In
> `labour_count` shift is required.

---

## End-to-end flows (as the server sees them)

### Flow A — Man-day register (`labour_count`, `views.py`)

1. **Open / ensure sheet.** `POST /api/v1/labour-count/sheet/ensure/`
   `{work_date, shift, department}` → `LabourSheetEnsureAPI` get-or-creates the
   DRAFT sheet, computing `lock_at` from the company's shift window
   (`lock.compute_lock_at`). Returns the sheet with items.
2. **Enter counts.** `PUT /sheet/{id}/items/` `{items:[{contractor,count}]}` →
   `LabourSheetItemsAPI`. Rejected with **409** if `lock.is_editable` is false
   (not DRAFT, or `now ≥ lock_at`). Validates that every contractor with
   `count>0` is active; **replaces** all items (deletes then re-inserts non-zero
   rows); clears any `is_holiday`.
3. **Submit.** `POST /sheet/{id}/submit/` → `LabourSheetSubmitAPI`. Only from
   DRAFT; requires at least one item **or** `is_holiday`. Sets SUBMITTED,
   `auto_submitted=False`.
   - **Holiday variant:** `POST /sheet/{id}/holiday/` clears items, sets
     `is_holiday=True`, and submits in one step.
   - **Auto-submit variant:** the scheduler (`jobs.auto_submit_labour_sheets`)
     flips any DRAFT past `lock_at` that has items or is a holiday to SUBMITTED
     with `auto_submitted=True`. **Empty drafts are skipped.**
4. **Pull back (correction).** `POST /sheet/{id}/pull-back/` → SUBMITTED→DRAFT,
   allowed **only before `lock_at`** (`lock.can_pull_back`), never once VERIFIED.
5. **Gate board.** `GET /sheet/gate/board/?work_date=&shift=[&department=&contractor=]`
   → `LabourGateBoardAPI` returns SUBMITTED + VERIFIED sheets for the shift; the
   frontend builds the by-department / by-contractor / grand-total matrix.
6. **Mark out in batches.** `POST /gate/out/` `{sheet, count}` →
   `LabourGateMarkOutAPI` appends a `LabourOutBatch` and recomputes
   `gate_counted`. People leave incrementally, so this is called per group.
   Rejected if the sheet is still DRAFT (400) or already VERIFIED (409).
   Undo the last batch: `POST /gate/out/undo/` `{sheet}`.
7. **Finalize.** `POST /gate/finalize/` `{sheet, remark}` →
   `LabourGateFinalizeAPI`. Only from SUBMITTED. Sets VERIFIED,
   `verify_basis="DEPARTMENT"`, and writes a `LabourVerification` with
   `variance = gate_counted − total_count`.
8. **Reopen.** `POST /gate/sheet/{id}/reopen/` → VERIFIED→SUBMITTED, **keeping**
   the out batches and `gate_counted`, so the gate can mark more out / correct.

### Flow B — Gate in/out tally + HOD allocation (`labour_gate`, `views.py`)

1. **Record gate intake.** `POST /api/v1/labour-gate/in/`
   `{contractor, work_date, shift, count_in}` **with no `department`** →
   `LabourInAPI`. Requires `can_record_labour_in`. Get-or-creates the
   department-less `LabourGateEntry`. Re-posting a **soft-deleted** row
   **reactivates it in place** (the unique slot is still occupied) and logs a
   `RESTORE`; otherwise updates `count_in` (logging `UPDATE_IN`) — but never
   below `total_out` (400).
2. **HOD allocation split.** Same endpoint **with `department`** set → requires
   `can_allocate_labour_department` (routed by `CanRecordOrAllocateLabourIn`).
   **Invariant:** the split cannot exceed what the contractor brought in *for the
   same shift*: `available = gate_count_in(shift) − Σ(other departments, shift)`.
   Over-allocation returns **400** with `{detail, entered, used, left}`.
3. **Edit / delete / restore an entry.** `PATCH /labour-gate/{id}/` edits
   `count_in` (never below `total_out`). `DELETE /labour-gate/{id}/` **soft**-
   deletes — blocked with **409** if any out batch exists. `POST
   /labour-gate/{id}/restore/` undoes a soft-delete **within 10 minutes**
   (`UNDO_WINDOW_MINUTES`). Object-level permission (`CanManageLabourEntry`)
   routes by the entry's kind: intake rows need `can_record_labour_in`,
   allocation rows need `can_allocate_labour_department`.
4. **Mark out / undo.** `POST /labour-gate/{id}/out/` `{count}` appends a
   `LabourGateOutBatch` (`count ≤ remaining`, else 400). `POST
   /labour-gate/{id}/out/undo/` removes the last batch, **within 10 minutes**.
5. **Read.** `GET /labour-gate/?date=YYYY-MM-DD` → every entry for the day (both
   kinds, active + soft-deleted), feeding the In, Out, and allocation screens.
   `GET /labour-gate/{id}/audit/` → the full audit trail for one entry.

---

## Critical business rules & invariants

**Company scoping (all endpoints).** Every query is filtered by
`request.company.company` via `HasCompanyContext`. A sheet/entry created under
company A is invisible under any other active-company context — see
*Real-world edge cases* and the repo-wide "cross-company flow boundary" note.

**`labour_count`:**
- Sheet unique per `(company, department, work_date, shift)`.
- Editable **only** while DRAFT **and** `now < lock_at`.
- Submit needs items **or** holiday. Pull-back only before `lock_at`. Finalize
  only from SUBMITTED. Reopen only from VERIFIED.
- Items persist only `count > 0`; zeros are implied by the roster.
- `gate_counted` is always the sum of `out_batches` (`_recompute_out`).
- **Shift-window timing** (`services/lock.py`, IST):
  - DAY → lock **18:30**, verify by **19:00**, same day.
  - NIGHT → lock **06:30**, verify by **09:00**, the morning **after**
    `work_date` (NIGHT's `work_date` is the shift-*start* date; `_target_date`
    adds a day).
  - Per-company overrides via `LabourShiftWindow`; otherwise `DEFAULT_WINDOWS`.

**`labour_gate`:**
- Entry unique per `(company, department, contractor, work_date, shift)`;
  `department=NULL` is intake, a set `department` is an allocation split.
- Allocation split ≤ gate intake for the **same shift** (summed across the
  other departments).
- `count_in ≥ total_out` at all times (edits and re-adds enforce it).
- Delete is a **soft** delete and is blocked once any out batch exists; the row
  survives (`is_active=False`) so the audit trail and released-labour history
  are preserved.
- Restore (undo delete) and undo-out are allowed only within
  `UNDO_WINDOW_MINUTES = 10` (`labour_gate/serializers.py`).
- `LabourGateAudit` is append-only and never edited (admin disables add/change).

**Permission routing (single write endpoint, two roles).** `/labour-gate/in/`
is deliberately shared: `CanRecordOrAllocateLabourIn` checks for a `department`
in the request body — present ⇒ HOD `can_allocate_labour_department`, absent ⇒
gate `can_record_labour_in`. This keeps the gate person and the HOD as clean,
separate roles (see migration `0007_hod_allocate_permission`).

---

## Integrations & cross-module boundaries

- **SAP:** none. No posting, no service layer, no semaphore. Purely internal.
- **Masters:**
  - `person_gatein.Contractor` — the aggregate is per contractor. `Contractor`
    has **no company FK** (it is a shared master with an `is_active` flag);
    company scoping happens on the sheet/entry, not the contractor.
  - `accounts.Department`, `company.Company`.
- **`gate_core.models.base.BaseModel`** — supplies `is_active`, `created_by`,
  `updated_by`, `created_at`, `updated_at` used across both apps.
- **Cross-company boundary:** consistent with the platform rule — *reads are
  scoped to the active company; there is no `all_companies` bypass here*, so
  labour data never spans companies. Writes resolve company from
  `request.company`.
- **No inter-app FK** between `labour_count` and `labour_gate`; a mark-out in one
  does not affect the other (a real double-entry risk — see edge cases).
- **URL mounting** (`config/urls.py`): `api/v1/labour-count/` →
  `labour_count.urls`; `api/v1/labour-gate/` → `labour_gate.urls`.

---

## API surface

**`labour_count`** (prefix `/api/v1/labour-count/`), permission per view:

| Method | Path | View | Permission |
|--------|------|------|------------|
| POST | `sheet/ensure/` | `LabourSheetEnsureAPI` | `can_submit_labour_count` |
| GET | `sheet/history/` | `LabourSheetHistoryAPI` | `view_labourcountsheet` |
| GET | `sheet/{id}/` | `LabourSheetDetailAPI` | `view_labourcountsheet` |
| PUT | `sheet/{id}/items/` | `LabourSheetItemsAPI` | `can_submit_labour_count` |
| POST | `sheet/{id}/submit/` | `LabourSheetSubmitAPI` | `can_submit_labour_count` |
| POST | `sheet/{id}/pull-back/` | `LabourSheetPullBackAPI` | `can_submit_labour_count` |
| POST | `sheet/{id}/holiday/` | `LabourSheetHolidayAPI` | `can_submit_labour_count` |
| GET | `gate/board/` | `LabourGateBoardAPI` | `can_verify_labour_count` |
| POST | `gate/out/` | `LabourGateMarkOutAPI` | `can_verify_labour_count` |
| POST | `gate/out/undo/` | `LabourGateUndoOutAPI` | `can_verify_labour_count` |
| POST | `gate/finalize/` | `LabourGateFinalizeAPI` | `can_verify_labour_count` |
| POST | `gate/sheet/{id}/reopen/` | `LabourGateReopenAPI` | `can_verify_labour_count` |

**`labour_gate`** (prefix `/api/v1/labour-gate/`):

| Method | Path | View | Permission |
|--------|------|------|------------|
| GET | `` (day) | `LabourGateDayAPI` | `view_labourgateentry` |
| POST | `in/` | `LabourInAPI` | record-in **or** allocate (routed by body) |
| PATCH/DELETE | `{id}/` | `LabourEntryDetailAPI` | manage (routed by entry kind) |
| GET | `{id}/audit/` | `LabourEntryAuditAPI` | `view_labourgateentry` |
| POST | `{id}/restore/` | `LabourRestoreAPI` | manage (routed by entry kind) |
| POST | `{id}/out/` | `LabourOutAPI` | `can_record_labour_out` |
| POST | `{id}/out/undo/` | `LabourOutUndoAPI` | `can_record_labour_out` |

All list/detail responses serialize computed helpers (`total_count`,
`variance`, `remaining`, `is_editable`, `can_pull_back` for sheets;
`total_out`, `remaining`, `is_deleted`, `can_undo_last`, `can_restore` for gate
entries).

---

## Real-world edge cases

Each: **trigger → current behaviour → operator-visible symptom → risk/gap.**

1. **HOD over-allocates a contractor across departments.**
   Trigger: allocation split would exceed the gate intake for that shift.
   Behaviour: `LabourInAPI` returns **400** `{detail, entered, used, left}`.
   Symptom: "N entered, M used, K left to allocate" popup; nothing saved.
   Risk: low — invariant holds. But if the gate intake is later *reduced* below
   the already-allocated sum, existing splits are **not** re-validated and can
   silently exceed intake.

2. **Delete a gate entry after some labour already left.**
   Trigger: `DELETE /labour-gate/{id}/` with out batches present.
   Behaviour: **409** "Cannot delete: labour has already been marked out".
   Symptom: delete button errors; row stays. Risk: none (intentional).

3. **Restore / undo attempted after 10 minutes.**
   Trigger: restore a soft-deleted entry or undo an out batch past
   `UNDO_WINDOW_MINUTES`. Behaviour: **409** with a "can no longer be…" message
   (`can_restore`/`can_undo_last` already read false in the serializer).
   Symptom: the Undo/Restore button has usually disappeared; a late API call is
   rejected. Risk: legitimate corrections need admin/DB fix after 10 min.

4. **Night-shift date confusion.**
   Trigger: a NIGHT sheet/entry for `work_date = D`. Behaviour: the shift runs
   D 19:00 → D+1 07:00; the sheet's `lock_at`/verify fall on **D+1** morning.
   Symptom: a supervisor filtering the gate board by "today" (D+1) sees nothing
   until they pick D. Risk: gate looks for the wrong date; counts appear
   "missing". Frontend defaults the count screens to `work_date = today`, which
   is the *start* date, mitigating this.

5. **Empty draft never submitted.**
   Trigger: a sheet is ensured (created) but no counts entered and not marked
   holiday. Behaviour: the auto-submit job **skips** empty drafts; after
   `lock_at`, `is_editable` is false so no more edits are possible.
   Symptom: the department shows nothing on the gate board; the sheet is stuck
   DRAFT and read-only. Risk: silent missing attendance — no alert is raised.

6. **Edit a count below what already left.**
   Trigger: `PATCH count_in` (gate) or a smaller re-post below `total_out`.
   Behaviour: **400** "Labour in cannot be less than the N already marked out".
   Symptom: save fails with that message. Risk: none (intentional).

7. **Pull-back after the lock time.**
   Trigger: supervisor tries to pull a submission back once `now ≥ lock_at`.
   Behaviour: **409** "Cannot pull back… past the lock time". Symptom: the Pull
   Back button is hidden (`can_pull_back=false`); a late call 409s. Risk:
   post-lock corrections require the gate to reopen or an admin edit.

8. **Non-zero variance at finalize.**
   Trigger: gate's out total ≠ submitted total when finalizing. Behaviour:
   allowed — VERIFIED with a `LabourVerification` recording the signed variance;
   no block. Symptom: an amber variance chip (`+3`, `−2`). Risk: variances are
   recorded but **nothing enforces reconciliation**; a persistent gap is only
   visible in `LabourVerification`/admin.

9. **Reopen a finalized sheet.**
   Trigger: `reopen`. Behaviour: VERIFIED→SUBMITTED keeping batches &
   `gate_counted`; clears `verified_*`. The original `LabourVerification` from
   the first finalize **remains**, and finalizing again creates a **second**
   verification row. Symptom: multiple verification records for one sheet/shift.
   Risk: double-counted verification history if reports naively sum them.

10. **Two parallel "out" flows.**
    Trigger: the same physical exit is recorded both on the `labour_count` gate
    board (`gate/out/`) and the `labour_gate` Labour-Out screen
    (`{id}/out/`). Behaviour: both succeed independently. Symptom: none at the
    time. Risk: **double bookkeeping** — the register and the gate tally can
    diverge; there is no cross-check.

11. **Cross-company context switch.**
    Trigger: an HOD/gate user switches active company. Behaviour: all reads now
    filter by the new company. Symptom: the labour screen goes **blank** for the
    day even though data exists under the other company. Risk: confusion; data
    is intact, just out of scope.

12. **Contractor deactivated mid-shift.**
    Trigger: a contractor is set `is_active=False` after intake. Behaviour:
    `labour_count` item saves reject inactive contractors (400) and the
    frontend rosters filter to active; existing `labour_gate` rows keep working
    (out/undo still function). Symptom: the contractor drops out of add/select
    lists but current rows remain actionable.

---

## Failure modes / what can break

- **Auto-submit scheduler not running.** `run_labour_count_scheduler` is a
  separate `BlockingScheduler` process (APScheduler, cron `hour=6,18 minute=30`
  by default; `LABOUR_AUTO_SUBMIT_HOURS`/`_MINUTE`). If it isn't running,
  sheets with data never flip to SUBMITTED. *Symptom a manager notices:* the
  gate board is empty at shift end even though supervisors "did the count" —
  because those sheets are still DRAFT and past `lock_at` (read-only). Manual
  fix: pull nothing back is possible; someone must submit before lock, or an
  admin edits status.
- **Wrong / missing `LabourShiftWindow`.** A bad per-company window shifts
  `lock_at`, so sheets lock too early or too late. Symptom: supervisors report
  the sheet "locked before I finished" or "still editable at 8pm".
- **Concurrent edits.** `items` PUT does delete-then-insert inside a
  transaction, and mark-out/undo recompute from batches, so a race between two
  gate operators marking out can momentarily mis-total until the next fetch;
  no row-level locking. Symptom: a brief flicker in `gate_counted`.
- **Register vs gate tally divergence** (edge case 10) — the biggest data-
  quality risk; no reconciliation job exists.
- **Time-zone drift.** All lock/verify math is IST (`Asia/Kolkata`) in
  `services/lock.py`; the auto-submit scheduler uses `settings.TIME_ZONE`. A
  mismatch would move the auto-submit instant.

---

## Improvement opportunities & known gaps

- Re-validate existing department splits when a contractor's gate intake is
  reduced (edge case 1).
- Alert on empty DRAFT sheets past `lock_at` instead of silently skipping
  (edge case 5).
- Reconcile the two "out" flows, or make the register's `gate_counted` derive
  from `labour_gate` so they cannot diverge (edge cases 10 & the failure mode).
- On reopen→re-finalize, supersede the prior `LabourVerification` instead of
  appending a second (edge case 9).
- No test file exists for `labour_count` (there is `labour_gate/tests.py`).

---

## Permissions & roles

Defined on the models' `Meta.permissions` and auto-created Django perms:

**`labour_count`:** `view_labourcountsheet` (view),
`can_submit_labour_count` (**department supervisor** — enter/submit/pull-back),
`can_verify_labour_count` (**gate operator** — board/mark-out/finalize/reopen).

**`labour_gate`:** `view_labourgateentry` (view),
`can_record_labour_in` (**gate person** — record raw gate intake),
`can_record_labour_out` (**gate person** — mark out),
`can_allocate_labour_department` (**HOD** — the /labour department split).

**The `labour` Django group** (migrations `0005_create_labour_group`,
`0007_hod_allocate_permission`) is the **HOD** role: `view_labourgateentry` +
`can_allocate_labour_department` + `person_gatein.view_contractor` +
`person_gatein.add_contractor`. Migration 0007 split the HOD from the gate
person by swapping `can_record_labour_in` for `can_allocate_labour_department`,
so an HOD can allocate but **cannot** record raw gate-in, and vice-versa.
Frontend nav gating relies on this split — see the paired frontend doc.

---

## Developer file map

**Backend — `labour_count/`:**
- `models.py` — `LabourShiftWindow`, `LabourCountSheet`, `LabourCountItem`,
  `LabourVerification`, `LabourOutBatch`, enums.
- `views.py` — sheet lifecycle + gate board/out/finalize/reopen APIs.
- `serializers.py` — sheet/item/out serializers + request serializers.
- `services/lock.py` — shift-window timing, `is_editable`, `can_pull_back`,
  `compute_lock_at`/`compute_verify_deadline` (IST, per-company override).
- `jobs.py` — `auto_submit_labour_sheets` (locks & auto-submits DRAFTs).
- `management/commands/run_labour_count_scheduler.py` — APScheduler cron runner.
- `permissions.py`, `urls.py`, `admin.py`.

**Backend — `labour_gate/`:**
- `models.py` — `LabourGateEntry`, `LabourGateOutBatch`, `LabourGateAudit`,
  `LabourAuditAction`.
- `views.py` — in/allocate, edit/delete/restore, out/undo, day list, audit;
  `_record_audit` helper.
- `serializers.py` — entry/out/audit serializers, `UNDO_WINDOW_MINUTES = 10`.
- `permissions.py` — role routing (`CanRecordOrAllocateLabourIn`,
  `CanManageLabourEntry`).
- `migrations/0005_create_labour_group.py`,
  `migrations/0007_hod_allocate_permission.py` — the `labour` (HOD) group.
- `tests.py`, `urls.py`, `admin.py`.

**Key frontend files** (see paired doc for detail):
- `FactoryFlow/src/modules/labour/module.config.tsx` — the standalone `/labour`
  route + sidebar entry.
- `FactoryFlow/src/modules/gate/pages/labourGatePages/LabourModulePage.tsx` —
  HOD allocation screen (rendered at `/labour`).
- `.../labourGatePages/GateLabourInPage.tsx`, `LabourOutPage.tsx` — gate in/out.
- `.../labourPages/LabourCountPage.tsx`, `LabourGatePage.tsx` — register + gate
  verification board.
- `FactoryFlow/src/modules/gate/api/labourGate/*`, `.../labourCount/*` — API +
  React Query hooks.

---

## Related docs

- **Paired frontend doc:** `C:/Users/gurpa/dev/FactoryFlow/docs/modules/labour.md`
- Gate module: `C:/Users/gurpa/dev/FactoryFlow/docs/modules/gate.md`
- Cross-company boundary (memory note) — why reads go blank across companies.
