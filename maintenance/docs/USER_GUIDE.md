# Maintenance Module — User Guide

This guide explains **every screen, every field, and the effect of the values
you enter** in the Maintenance module. It is written for end users (store
keepers, technicians, maintenance heads) and for implementers who need to know
exactly what each value does behind the scenes.

> Source of truth: this guide is generated from the backend models, constants,
> serializers and views in `factory_app/maintenance/`. Where a value triggers an
> automatic action (e.g. an asset changing status), it is called out explicitly.

---

## 1. What the module does

The Maintenance module manages the full plant-maintenance lifecycle:

- **Assets** — the machines/equipment you maintain, in a parent→child hierarchy.
- **Work Orders** — every breakdown, complaint, or job done on an asset.
- **Preventive Maintenance (PM)** — recurring scheduled maintenance with
  checklists.
- **Spares & Store** — spare-part stock, issue/consume/return, and stock
  adjustments, fully backed by a movement ledger.
- **Vendor Visits** — third-party/AMC service visits tied to a work order.
- **Gate-in integration** — spares received at the gate flow into stock after QC.
- **Dashboards & Reports** — KPIs (MTTR, MTBF, work pressure) and exports.

Everything is **scoped to the company** you are logged into — you only ever see
and edit your own company's records.

---

## 2. Roles & permissions

Membership in a Django **auth group** grants a bundle of permissions. The four
shipped roles:

| Role (group) | Can do |
|---|---|
| `maintenance` / `maintenance_admin` | Everything in the module. |
| `maintenance_head` | View + manage all areas (assets, work orders, PM, spares, vendors, reports) but not module-settings reserved bits. |
| `maintenance_technician` | View assets/PM/vendors; create/start/complete work orders; upload photos/docs. **Cannot** approve/close work orders or manage spares. |
| `maintenance_viewer` | Read-only across the module. |

Effect of **not** having a permission: the related buttons are disabled in the
UI and the API returns **403 Forbidden**. Managing spares (create/edit/adjust
stock) specifically requires `can_manage_spare`.

---

## 3. Master data

These are the dropdown sources. Each is unique **per company**.

### 3.1 Asset Category / Spare Category
| Field | Meaning / effect |
|---|---|
| `name` | Display name. **Must be unique per company** — a duplicate name is rejected. |
| `description` | Free text; informational only. |

### 3.2 Asset Location
| Field | Meaning / effect |
|---|---|
| `name` | Location name. |
| `area` | Sub-area within the location. |
| `line` | Production line. |
| | The combination `(company, name, area, line)` must be unique. |

### 3.3 Asset Department
| Field | Meaning / effect |
|---|---|
| `name` | Department name (unique per company). |
| `department_code` | Optional short code; informational. |

---

## 4. Assets

An **asset** is a piece of equipment. Create one under **Assets → New Asset**.

| Field | Values | Effect of the value |
|---|---|---|
| `asset_code` | Text, **unique per company** | The asset's ID. A duplicate code is rejected. Used in QR codes and search. |
| `name` | Text | Display name. |
| `category` | An Asset Category | Must belong to your company. Used for grouping/reports. |
| `location` | An Asset Location | Must belong to your company. |
| `department` | An Asset Department | Drives department filters and is copied onto work orders created from PM. |
| `parent_asset` | Another asset (optional) | Builds the hierarchy (plant → line → machine → component). If the parent is deleted, this is set to empty (not cascaded). |
| `production_machine` | A production-line Machine (optional) | Links maintenance to the production module so breakdowns can auto-create work orders. |
| `hierarchy_level` | `PLANT`, `AREA`, `LINE`, `MACHINE` (default), `COMPONENT`, `UTILITY` | Classifies where the asset sits in the hierarchy. Informational/reporting. |
| `area`, `line` | Text | Free-text area/line labels copied onto work orders for filtering. |
| `status` | `RUNNING` (default), `IDLE`, `BREAKDOWN`, `UNDER_PM`, `UNDER_REPAIR`, `RETIRED` | **Important:** this is largely **driven automatically** by work orders (see §6.4). Setting it manually is possible but the next work-order action may overwrite it. `RETIRED` is set when you deactivate. |
| `make`, `model`, `serial_number` | Text | Nameplate details; informational. |
| `purchase_date` | Date | Informational / age reports. |
| `warranty_start_date` / `warranty_end_date` | Dates | If end < start the form is rejected. Used to flag in-warranty assets. |
| `amc_vendor`, `amc_start_date`, `amc_end_date` | Text/Dates | AMC contract details. End < start is rejected. |
| `responsible_person` | A user | The owner/accountable person for the asset. |
| `qr_code` | Text | The scannable code. Scanning it opens the asset / raises a work order from mobile. |
| `description` | Text | Free notes. |
| `is_active` | true/false | Whether the asset is in use. See deactivate below. |

### 4.1 Deactivating an asset
Using **Deactivate** (needs `can_deactivate_asset`):
- sets `is_active = false`,
- sets `status = RETIRED`,
- stamps `deactivated_at` with the current time.
A retired asset stays in history but is filtered out of active lists.

---

## 5. Spares & Store

A **spare** is a stockable part. **Stock is ledger-controlled** — read this
section carefully, because how you change stock matters.

| Field | Values | Effect of the value |
|---|---|---|
| `category` | A Spare Category | Must belong to your company. |
| `name` | Text | Display name. |
| `part_number` | Text, **unique per company** | The part ID. Stored upper-cased. A duplicate is rejected. |
| `sap_item_code` | Text | Link to SAP item; informational. |
| `uom` | Text (default `NOS`) | Unit of measure shown next to quantities. |
| `compatible_assets` | List of assets | Restricts which assets a spare can be requested for. Spare requests for an incompatible asset are rejected. All listed assets must be in your company. |
| `is_critical` | true/false | `true` = part appears in "critical spare" alerts and is prioritised in low-stock lists. |
| `minimum_stock` | Number ≥ 0 | The hard floor. When `current_stock ≤ minimum_stock` the spare shows a **"Minimum"** flag (`is_below_minimum`). |
| `reorder_level` | Number ≥ `minimum_stock` | When `current_stock ≤ reorder_level` the spare is **"Low"** (`is_low_stock`) and appears on the low-stock dashboard. **Must be ≥ minimum_stock**, else the form is rejected. |
| `current_stock` | Number ≥ 0 | **On-hand quantity.** See the box below — this behaves differently on create vs. edit. |
| `unit_cost` | Number ≥ 0 | Default cost used to value issues/consumption when no override is given. |
| `storage_location` | Text | Bin/rack location; informational. |

### 5.1 How `current_stock` behaves (important)
> **On create:** you may type an opening stock value — it is accepted as the
> starting balance.
>
> **On edit:** `current_stock` is **read-only**. Editing it is ignored. This is
> deliberate: on-hand stock must only change through the **movement ledger**
> (issue / consume / return / adjustment) so the number always reconciles with
> history.
>
> **To correct stock**, use **Adjust Stock** (§5.2). Do not try to "edit" it.

Derived indicators (read-only, computed live):
- `is_low_stock` → `current_stock ≤ reorder_level`
- `is_below_minimum` → `current_stock ≤ minimum_stock`
- `reorder_shortage_qty` → how much to buy to reach the reorder level (0 if not low).

### 5.2 Adjust Stock
Use the **Adjust** button on a spare row (needs `can_manage_spare`).

| Field | Effect |
|---|---|
| `new_stock` | The **counted/corrected** on-hand value. Must be ≥ 0 and **different** from the current value, else rejected. |
| `reason` | **Mandatory.** Recorded on the audit movement so the change is traceable. |

Effect: `current_stock` is set to `new_stock`, and an `ADJUSTMENT` movement is
created (quantity = the difference, direction noted in its remarks). This keeps
the ledger and the on-hand number in sync.

---

## 6. Work Orders

A **work order (WO)** records a job on an asset. WO numbers are auto-generated
as `MWO-YYYYMMDD-NNNN`.

### 6.1 Fields
| Field | Values | Effect of the value |
|---|---|---|
| `work_type` | `COMPLAINT` (default), `BREAKDOWN`, `GENERAL`, `PREVENTIVE`, `INSPECTION`, `CALIBRATION`, `AMC_VENDOR`, `PROJECT` | Drives **asset-status** behaviour (see §6.4). `BREAKDOWN` open WOs put the asset into `BREAKDOWN`; `PREVENTIVE`/`INSPECTION`/`CALIBRATION` put it `UNDER_PM`; others `UNDER_REPAIR`. |
| `priority` | `NORMAL` (default), `HIGH`, `CRITICAL` | Sorting and dashboard "work pressure". |
| `asset` | An asset | The equipment being worked on. |
| `department` | A department | Usually the asset's department; used for filtering. |
| `area`, `line` | Text | Filtering labels. |
| `title` | Text | Short summary. |
| `problem_statement` | Text (required) | What's wrong. |
| `impact` | `NO_IMPACT` (default), `DEGRADED`, `STOPPAGE`, `SAFETY_RISK` | Describes production impact; used in reports. |
| `impact_notes`, `downtime_reason` | Text | Detail. `downtime_reason` is copied to the linked production breakdown on completion. |
| `production_run` / `production_breakdown` | Links | Set when a WO is auto-created from a production breakdown. Completing the WO **closes** the breakdown timer (see §6.5). |
| `assigned_to` | A user | The technician responsible. Auto-set to you if you start an unassigned WO. |
| `target_date` | Date | Due date for the job. |
| `start_time`, `end_time` | Timestamps | Set by the start/complete actions; used to compute response/repair/downtime minutes. |
| `technician_remarks`, `completion_remarks`, `root_cause`, `corrective_action`, `preventive_action`, `closure_remarks` | Text | Captured during complete/approve. |

### 6.2 Status values and what they mean
| Status | Meaning | How you reach it |
|---|---|---|
| `DRAFT` | Not yet active | Manual via set-status |
| `OPEN` | Logged, unassigned (default) | On create |
| `ASSIGNED` | Has a technician | **Assign** action |
| `IN_PROGRESS` | Work started | **Start** action |
| `WAITING_SPARE` | Paused for parts | Auto when a spare is requested, or set-status |
| `WAITING_VENDOR` | Paused for vendor | Auto when a vendor visit is created, or set-status |
| `ON_HOLD` | Paused | set-status |
| `COMPLETED` | Work finished | **Complete** action |
| `APPROVED` | Verified by supervisor | **Approve** action (only from COMPLETED) |
| `CLOSED` | Final | **Close** action (only from APPROVED) |

### 6.3 Lifecycle actions (and their exact effects)
- **Assign** → sets `assigned_to` (+ optional `target_date`), status → `ASSIGNED`.
- **Start** → sets `start_time` (if empty), assigns you if unassigned, status → `IN_PROGRESS`.
- **Complete** → sets `end_time` (defaults to now), `completed_at`, status → `COMPLETED`, saves the remarks/root-cause fields, and **closes any linked production breakdown**.
- **Approve** → **only allowed from `COMPLETED`**; sets `approved_at`/`approved_by`, status → `APPROVED`. Approving a non-completed WO returns 400.
- **Close** → **only allowed from `APPROVED`**; sets `closed_at`/`closed_by`, status → `CLOSED`. Closing a non-approved WO returns 400.
- **Set-status** → for the intermediate statuses only. It **rejects** `COMPLETED`/`APPROVED`/`CLOSED` (use the dedicated actions for those).
- A **CLOSED** work order is locked — assign/start/complete/set-status all refuse to touch it.

### 6.4 Automatic asset-status effect (cross-effect)
Saving or progressing a WO automatically updates the **asset's** status:
- Open/Assigned **BREAKDOWN** WO → asset `BREAKDOWN`.
- In-progress/waiting/completed/approved WO → asset `UNDER_PM` (for preventive/inspection/calibration) or `UNDER_REPAIR` (otherwise).
- **Closing** a WO → if the asset has **no other open work**, it returns to `RUNNING`.
This is why you usually don't set asset status by hand.

### 6.5 Production breakdown closure (cross-effect)
If the WO is linked to a production `MachineBreakdown`, **Complete** ends the
breakdown: it stamps `end_time`, computes `breakdown_minutes`, marks it inactive,
copies the `downtime_reason`, and appends a completion note.

### 6.6 Computed time metrics (read-only)
- `response_time_minutes` = start_time − created_at
- `repair_time_minutes` = end_time − start_time
- `downtime_minutes` = end_time − created_at
These feed MTTR/MTBF on the dashboard. If the relevant timestamps are missing,
the metric is shown as blank.

---

## 7. Preventive Maintenance (PM)

### 7.1 PM Plan fields
A **PM Plan** is a recurring schedule. Plan codes auto-generate as `PM-YYYYMMDD-NNNN`.

| Field | Values | Effect of the value |
|---|---|---|
| `title` | Text | Plan name. |
| `asset` | An asset | What gets maintained. |
| `frequency` | `DAILY`, `WEEKLY`, `MONTHLY`, `QUARTERLY`, `HALF_YEARLY`, `YEARLY` | **Determines the gap between due dates.** After each due date is generated, the next is computed by adding 1 day / 7 days / 1,3,6,12 months accordingly. |
| `work_type` | (same list as WO; default `PREVENTIVE`) | The type stamped on auto-created work orders. |
| `priority` | `NORMAL`/`HIGH`/`CRITICAL` | Priority stamped on generated work orders. |
| `assigned_to` | A user | Default technician. If set, generated work orders start as `ASSIGNED`; if empty they start `OPEN`. |
| `start_date` | Date (default today) | When the schedule begins. |
| `next_due_date` | Date (required) | **The next date a PM is due.** This is what generation reads and advances. |
| `advance_days` | Whole number ≥ 0 | Lead time. A plan is considered **due** when `next_due_date ≤ today + advance_days` — i.e. it surfaces this many days early. |
| `auto_create_work_order` | true/false | `true` → generating a PM execution also creates a linked work order automatically. `false` → only the execution (checklist) is created, no WO. |
| `checklist_required` | true/false | `true` → completing the execution **requires** all required checklist items to be filled, else completion is rejected. `false` → no checklist enforcement. |
| `last_generated_date` | Date | Bookkeeping; the last due date that was generated. |

### 7.2 Checklist Template Items
Each line a technician must check during the PM.

| Field | Values | Effect |
|---|---|---|
| `task` | Text | The instruction. |
| `input_type` | `CHECKBOX` (default), `PASS_FAIL`, `NUMBER`, `TEXT` | How the technician records the result on execution. |
| `is_required` | true/false | If `true` and the plan has `checklist_required`, the execution can't be completed without this item. |
| `expected_text` | Text | Expected answer (for text/pass-fail) — guidance. |
| `min_value` / `max_value` | Numbers | Acceptable range for `NUMBER` items — guidance for the technician. |
| `uom` | Text | Unit for numeric readings. |
| `safety_critical` | true/false | Flags safety-critical checks for emphasis/reporting. |
| `sort_order` | Number | Display order. |

### 7.3 Generating PM executions
**Generate** (per plan) or **Generate Due** (bulk) walks `next_due_date` forward
up to the chosen "due until" date, creating one **execution** per due date
(and a work order if `auto_create_work_order`). It then **advances
`next_due_date`** to the next un-generated date.

> Note: generation is idempotent — if executions for those dates already exist it
> won't duplicate them, **but it still advances `next_due_date`** so the plan
> never gets stuck repeatedly showing as "due."

### 7.4 PM Execution fields & statuses
| Field | Effect |
|---|---|
| `due_date` | When this occurrence is due. Unique per `(company, plan, due_date)`. |
| `status` | `PENDING` (default) → `IN_PROGRESS` → `COMPLETED`, or `SKIPPED`. |
| `started_at` / `completed_at` / `skipped_at` | Timestamps set by start/complete/skip. |
| `skip_reason` | Required context when skipping. |
| `is_overdue` / `effective_status` | If still `PENDING` past its due date, it reports as `OVERDUE`. |

Execution actions:
- **Start** → only from `PENDING`/`OVERDUE`; sets the linked WO `IN_PROGRESS` and the asset `UNDER_PM`.
- **Complete** → records checklist results (enforced if required), stamps completion, and **completes the linked work order**. A completed/skipped execution cannot be completed again.
- **Skip** → records `skip_reason`; cannot skip a completed one.

---

## 8. Spare Requests, Issue, Consume, Return

A **spare request** asks the store for parts against a work order.

### 8.1 Request fields
| Field | Effect |
|---|---|
| `work_order` | The job the parts are for. |
| `spare` | The part. Must be **compatible** with the WO's asset (else rejected). |
| `requested_qty` | Must be > 0. The amount asked for. |
| `required_by` | Needed-by date. |
| `purpose` | Why. |
| `status` | Auto-managed (see below). |

Creating a request can move the work order to **WAITING_SPARE**.

### 8.2 Store actions and stock effects
| Action | What you enter | Effect on stock & request |
|---|---|---|
| **Issue** | `quantity`, optional `unit_cost`, `remarks` | Decreases the spare's `current_stock`; records an `ISSUE` movement; increases `issued_qty`. Rejected if quantity > available stock. (Issued cost defaults to the spare's unit cost if no override.) |
| **Consume** | `quantity`, `remarks` | Marks issued parts as used; records a `CONSUME` movement. Cannot exceed the unused-issued quantity. |
| **Return Unused** | `quantity`, `remarks` | Returns unused parts to stock — **increases** `current_stock`; records a `RETURN` movement. Cannot exceed unused-issued quantity. |
| **Cancel** | — | Cancels the request. **Not allowed once anything has been issued.** |

All four run atomically with row-level locking, so concurrent store actions
can't oversell or corrupt the balance.

### 8.3 Request status (auto-computed)
| Status | When |
|---|---|
| `REQUESTED` | Nothing issued yet. |
| `PARTIALLY_ISSUED` | Some but not all of the requested qty issued. |
| `ISSUED` | Full requested qty issued, none consumed/returned yet. |
| `PARTIALLY_CONSUMED` | Some consumed or returned. |
| `CLOSED` | Nothing left to issue and nothing left unused. |
| `CANCELLED` | Cancelled (cannot be cancelled after issue). |

Helper figures: `pending_issue_qty` = requested − issued; `available_to_consume_qty`
= issued − consumed − returned; `total_cost` = consumed × unit cost.

### 8.4 Spare Movements (the ledger)
Every stock change is recorded as a **movement** (`RECEIPT`, `ISSUE`, `CONSUME`,
`RETURN`, `ADJUSTMENT`) with quantity, unit cost, who did it, and remarks. This
is read-only history and is the audit trail behind `current_stock`.

---

## 9. Gate-in integration (receiving spares)

When a maintenance spare arrives at the gate it creates a **Gate Link**, and
after acceptance a **Spare Receipt** that adds stock.

| Field (Gate Link) | Effect |
|---|---|
| `qc_required` | Whether the incoming material needs QC before stock is added. |
| `qc_status` | `NOT_REQUIRED`/`PENDING`/`ACCEPTED`/`REJECTED`/`WAIVED`. Stock is only received after it's effectively cleared. |
| `receipt_status` | `NOT_RECEIVED` → `RECEIVED` (or `BLOCKED`). |
| `received_quantity` / `received_at` / `received_by` | Set when stock is taken in. |
| `grpo_reference`, `grpo_doc_entry`, `grpo_doc_num` | SAP goods-receipt references. |

The **Spare Receipt** records the accepted `quantity`, `unit_cost`, invoice, and
posts a `RECEIPT` movement that increases the spare's `current_stock`.

These screens are **read-only** in the maintenance UI (the data originates in the
gate-in flow).

---

## 10. Vendor Visits

Tracks a third-party/AMC engineer visit against a work order.

| Field | Effect |
|---|---|
| `work_order` | The job. Creating a visit can move the WO to **WAITING_VENDOR**. |
| `asset` | Equipment serviced. |
| `vendor_code`, `vendor_name`, `contact_person`, `contact_phone` | Vendor details (`vendor_name` required). |
| `status` | `PLANNED` (default) → `IN_PROGRESS` → `COMPLETED`, or `CANCELLED`. |
| `planned_start` / `planned_end` | Schedule. |
| `actual_start` / `actual_end` | Set by Start/Complete. |
| `person_gate_entry` / `material_gate_entry` | Links to the person/material gate entries for the visit. |
| `service_report_attachment`, `invoice_number`, `invoice_attachment` | Uploads/references. |

Visit actions and **state guards**:
- **Start** → sets `actual_start`, status `IN_PROGRESS`. **Rejected** if the visit is already completed or cancelled.
- **Complete** → sets `actual_end`, status `COMPLETED`. **Rejected** if already completed or cancelled.
- **Cancel** → status `CANCELLED`. **Rejected** if the visit is already completed.

---

## 11. Photos & Documents

- **Asset Photos** — `photo`, `caption`, `taken_on`, `is_monthly_photo` (flags the monthly condition photo).
- **Asset Documents** — `document_type` (`MANUAL`/`WARRANTY`/`AMC`/`SERVICE_REPORT`/`CALIBRATION`/`OTHER`), `title`, `document`, `document_date`, `notes`.
- **Work Order Photos** — `photo_type` (`BEFORE`/`AFTER`/`GENERAL`), `caption`, `taken_on`. Before/After pairs document the repair.

---

## 12. Notifications

The module sends in-app/push notifications for key store events (e.g. low/
critical-spare alerts) to users who hold the relevant permission. Membership of
the maintenance groups determines who receives them.

---

## 13. Quick reference — values that trigger automatic effects

| You set / do | Automatic effect |
|---|---|
| Open a `BREAKDOWN` work order | Asset → `BREAKDOWN` |
| Start any work order | Asset → `UNDER_REPAIR` (or `UNDER_PM` for preventive/inspection/calibration) |
| Close a work order (no other open work) | Asset → `RUNNING` |
| Complete a WO linked to a production breakdown | Breakdown timer stops and is closed |
| Request a spare | Work order → `WAITING_SPARE` |
| Create a vendor visit | Work order → `WAITING_VENDOR` |
| Issue a spare | `current_stock` decreases + `ISSUE` movement |
| Return unused spare | `current_stock` increases + `RETURN` movement |
| Adjust stock | `current_stock` set to new value + `ADJUSTMENT` movement (reason required) |
| PM plan `auto_create_work_order = true` | Generating an execution also creates a work order |
| PM plan `checklist_required = true` | Execution can't complete until required checklist items are filled |
| PM plan `advance_days = N` | Plan shows as "due" N days before `next_due_date` |
| Deactivate an asset | `is_active = false`, status → `RETIRED` |

---

*Field names in this guide match the API payloads. If a value is rejected, the
API returns a 400 with a message naming the field and the rule it violated.*
