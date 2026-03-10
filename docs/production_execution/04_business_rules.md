# Production Execution — Business Rules & Workflows

---

## 1. Daily Production Workflow

```
Step 1: Line Clearance
   Operator fills checklist → Supervisor signs → Submit for QA → QA approves → CLEARED

Step 2: Start Production Run
   Select Production Plan → Select Line → System auto-increments run_number → DRAFT

Step 3: Hourly Production Entry (core loop, repeated every hour)
   Enter: cases produced, machine status, recorded minutes, breakdown detail
   System auto-computes: total production, total breakdown time, speed, efficiency

Step 4: Log Breakdowns (as they occur)
   Record: machine, start/end time, reason, type (LINE/EXTERNAL)
   System updates: total_breakdown_time, line_breakdown_time, external_breakdown_time

Step 5: End of Run
   Enter material consumption → wastage auto-calculated
   Enter machine runtime per equipment
   Record manpower (worker count, supervisor, engineer)
   POST /runs/<id>/complete/ → status: COMPLETED, summary fields recomputed

Step 6: Waste Approval (if wastage exists)
   Engineer signs → AM signs → Store signs → HOD signs
   Sequential — each must approve before the next can

Step 7: Machine Checklists (parallel daily activity)
   Operators fill daily items → Shift incharge signs off
```

---

## 2. Production Run Rules

| Rule | Description |
|------|-------------|
| **Auto-increment run_number** | For a given `production_plan` + `date`, run_number increments: 1, 2, 3... |
| **Unique constraint** | `(company, production_plan, date, run_number)` must be unique |
| **Plan must be OPEN or IN_PROGRESS** | Cannot start a run for a DRAFT, COMPLETED, CLOSED, or CANCELLED plan |
| **Line clearance check** | On run creation, check if a CLEARED LineClearance exists for the plan+line. If not, return a warning (not a blocker) |
| **Only DRAFT/IN_PROGRESS can be edited** | COMPLETED runs are locked |
| **Complete recomputes all totals** | On complete: sum hourly logs, sum breakdowns, compute efficiency |

---

## 3. Hourly Log Rules

| Rule | Description |
|------|-------------|
| **Pre-defined time slots** | 12 slots: 07:00-08:00, 08:00-09:00, ..., 18:00-19:00 |
| **Unique per run+time_start** | Only one entry per time slot per run |
| **Bulk save supported** | POST endpoint accepts an array to save all 12 slots at once |
| **Auto-sums** | `ProductionRun.total_production = SUM(produced_cases)` |
| **Recd minutes validation** | `recd_minutes` cannot exceed 60 per slot |

---

## 4. Breakdown Rules

| Rule | Description |
|------|-------------|
| **Machine must belong to same line** | `breakdown.machine.line == run.line` |
| **end_time >= start_time** | Validated on create/update |
| **breakdown_minutes auto-calculated** | If not provided: `(end_time - start_time).minutes` |
| **Type determines summary field** | `LINE` → adds to `line_breakdown_time`, `EXTERNAL` → adds to `external_breakdown_time` |
| **Run totals recomputed** | After create/update/delete, recompute run's breakdown summary fields |

---

## 5. Material Usage (Yield) Rules

| Rule | Description |
|------|-------------|
| **Wastage auto-calculated** | `wastage_qty = opening_qty + issued_qty - closing_qty` |
| **Batch support** | Up to 3 batches per run (batch_number: 1, 2, 3) |
| **Standard materials** | Bottle, Cap, Front Label, Back Label, Tikki, Shrink, Carton |
| **Non-negative quantities** | opening, issued, closing must be >= 0 |
| **wastage_qty can be negative** | Indicates surplus (valid scenario) |

---

## 6. Line Clearance Rules

| Rule | Description |
|------|-------------|
| **Auto-create 9 items** | When a clearance is created, auto-create the 9 standard checklist items |
| **Status flow** | DRAFT → SUBMITTED → CLEARED or NOT_CLEARED |
| **Only DRAFT can be edited** | Once submitted, checklist results are locked |
| **QA approval required** | Only users with `can_approve_line_clearance_qa` can approve |
| **One clearance per plan+line+date** | Business rule (not DB constraint — warn user) |

**Clearance status flow:**
```
DRAFT
  ├── (operator fills checklist, supervisor signs)
  └── Submit → SUBMITTED
                  ├── QA Approves → CLEARED
                  └── QA Rejects → NOT_CLEARED
                                      └── Can resubmit → SUBMITTED
```

---

## 7. Machine Checklist Rules

| Rule | Description |
|------|-------------|
| **Template-driven** | Entries are created from `MachineChecklistTemplate` records |
| **Calendar grid** | Monthly view: days as columns, tasks as rows |
| **Unique per machine+template+date** | One entry per task per machine per day |
| **Frequency filtering** | Show DAILY items every day, WEEKLY on Mondays, MONTHLY on 1st |
| **Bulk save** | Calendar save sends all changed cells at once |
| **Denormalized task_description** | Copied from template for display without JOINs |

---

## 8. Waste Approval Rules

| Rule | Description |
|------|-------------|
| **Sequential approval** | Engineer → AM → Store → HOD |
| **Cannot skip levels** | AM cannot sign until engineer has signed |
| **Status transitions** | PENDING → PARTIALLY_APPROVED (after first sign) → FULLY_APPROVED (after HOD signs) |
| **Each signer recorded** | `*_sign` (name), `*_signed_by` (FK to User), `*_signed_at` (timestamp) |
| **Permission-gated** | Each approval requires its own permission |

**Approval flow:**
```
PENDING
  └── Engineer signs → PARTIALLY_APPROVED
       └── AM signs → PARTIALLY_APPROVED
            └── Store signs → PARTIALLY_APPROVED
                 └── HOD signs → FULLY_APPROVED
```

---

## 9. Auto-Calculations (computed in service layer)

| Metric | Formula | When Computed |
|--------|---------|---------------|
| Total Production | `SUM(log.produced_cases)` | On log save, on run complete |
| Total PE Minutes | `SUM(log.recd_minutes)` | On log save, on run complete |
| Total Breakdown Time | `SUM(breakdown.breakdown_minutes)` | On breakdown save/delete |
| Line Breakdown Time | `SUM(breakdown.breakdown_minutes WHERE type=LINE)` | On breakdown save/delete |
| External Breakdown Time | `SUM(breakdown.breakdown_minutes WHERE type=EXTERNAL)` | On breakdown save/delete |
| Material Wastage | `opening + issued - closing` | On material save |
| Unrecorded Time | `Total shift minutes - PE minutes - Breakdown minutes` | On run complete |

**Report-level calculations (not stored, computed on report request):**

| Metric | Formula |
|--------|---------|
| Available Time | Total shift minutes (720 for 12-hour) - Planned downtime |
| Operating Time | Available Time - Total Breakdown Time |
| Actual Speed | Total Production / Operating Time (in minutes) |
| Performance % | (Actual Speed / Rated Speed) x 100 |
| Availability % | (Operating Time / Available Time) x 100 |
| OEE | Availability x Performance x Quality / 10000 |
| Line Efficiency % | (Actual output / Theoretical max output) x 100 |
| Material Loss % | (Wastage Qty / (Opening + Issued)) x 100 |

---

## 10. Validation Summary

### On ProductionRun create:
- `production_plan` must exist and belong to the same company
- `production_plan.status` must be `OPEN` or `IN_PROGRESS`
- `line` must exist and be active
- `date` cannot be in the future
- `run_number` auto-assigned

### On ProductionLog save:
- `time_start` must be one of the 12 pre-defined slots
- `recd_minutes` <= 60
- `produced_cases` >= 0

### On MachineBreakdown save:
- `machine.line` must match `run.line`
- `end_time` >= `start_time` (if end_time provided)
- `breakdown_minutes` must match time difference (if both times provided)

### On LineClearance submit:
- All 9 checklist items must have a result (YES/NO/NA)
- At least one signature (supervisor or incharge) must be provided

### On WasteLog approval:
- Previous level must be signed before current level can sign
- Signing user must have the corresponding permission
