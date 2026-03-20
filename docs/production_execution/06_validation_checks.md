# Production Execution — Validation Checks & Business Rules

This document lists every validation check implemented in the production execution app, organized by module. For each check, the error message, HTTP status code, and file location are provided.

---

## Table of Contents

1. [Production Line Checks](#1-production-line-checks)
2. [Production Run Checks](#2-production-run-checks)
3. [Hourly Production Log Checks](#3-hourly-production-log-checks)
4. [Machine Breakdown Checks](#4-machine-breakdown-checks)
5. [Material Usage Checks](#5-material-usage-checks)
6. [Machine Runtime Checks](#6-machine-runtime-checks)
7. [Manpower Checks](#7-manpower-checks)
8. [Line Clearance Checks](#8-line-clearance-checks)
9. [Machine Checklist Checks](#9-machine-checklist-checks)
10. [Waste Approval Checks](#10-waste-approval-checks)
11. [Final QC Checks](#11-final-qc-checks)
12. [Resource Tracking Checks](#12-resource-tracking-checks)
13. [Auto-Calculations](#13-auto-calculations)
14. [State Machine Transitions](#14-state-machine-transitions)
15. [Permission Checks](#15-permission-checks)

---

## 1. Production Line Checks

| # | Check | Error Message | HTTP Status | File Location |
|---|-------|---------------|-------------|---------------|
| 1.1 | Line must exist and belong to company | `"Production line {id} not found."` | 400 | `services/production_service.py:98` |
| 1.2 | Line name must be unique per company | DB `unique_together = ('company', 'name')` | 400 | `models.py:92` |
| 1.3 | Soft delete — sets `is_active = False` instead of actual delete | N/A (design choice) | 204 | `services/production_service.py:92` |

---

## 2. Production Run Checks

| # | Check | What Happens | Error Message | HTTP Status | File Location |
|---|-------|-------------- |---------------|-------------|---------------|
| 2.1 | **Line must be active** to create a run | Run creation blocked | `"Production line '{name}' is not active."` | 400 | `services/production_service.py:208-209` |
| 2.2 | **COMPLETED run cannot be edited** | Update blocked | `"Cannot edit a COMPLETED run."` | 400 | `services/production_service.py:243-244` |
| 2.3 | **Cannot complete an already completed run** | Complete action blocked | `"Run is already completed."` | 400 | `services/production_service.py:258-259` |
| 2.4 | Run must exist and belong to company | Lookup fails | `"Production run {id} not found."` | 404 | `services/production_service.py:304-305` |
| 2.5 | Run number auto-incremented per `(company, sap_doc_entry, date)` | Automatic — no user input needed | N/A | `services/production_service.py:214-219` |
| 2.6 | Unique constraint on `(company, sap_doc_entry, date, run_number)` | DB constraint | DB error | N/A | `models.py:208` |

---

## 3. Hourly Production Log Checks

| # | Check | What Happens | Error Message | HTTP Status | File Location |
|---|-------|-------------- |---------------|-------------|---------------|
| 3.1 | **COMPLETED run — cannot edit logs** | Save/update blocked | `"Cannot edit logs of a COMPLETED run."` | 400 | `services/production_service.py:318-319` |
| 3.2 | Log must exist and belong to the run | Update fails | `"Production log {id} not found."` | 400 | `services/production_service.py:353-354` |
| 3.3 | `produced_cases` must be >= 0 | Serializer validation | Serializer error | 400 | `serializers.py:146` |
| 3.4 | `recd_minutes` must be 0–60 | Serializer validation | Serializer error | 400 | `serializers.py:148` |
| 3.5 | Unique constraint on `(production_run, time_start)` | Uses `update_or_create` — upserts on same slot | N/A | `models.py:270` |
| 3.6 | Auto-transition: DRAFT → IN_PROGRESS when logs are saved | Status changes automatically | N/A | `services/production_service.py:340-341` |

---

## 4. Machine Breakdown Checks

| # | Check | What Happens | Error Message | HTTP Status | File Location |
|---|-------|-------------- |---------------|-------------|---------------|
| 4.1 | **COMPLETED run — cannot add/edit/delete breakdowns** | Operation blocked | `"Cannot add breakdowns to a COMPLETED run."` / `"Cannot edit breakdowns of a COMPLETED run."` / `"Cannot delete breakdowns from a COMPLETED run."` | 400 | `services/production_service.py:377-378, 409-410, 438-439` |
| 4.2 | **Machine must belong to the same line as the run** | Add/update blocked | `"Machine does not belong to the same line as the run."` | 400 | `services/production_service.py:381-382, 421-422` |
| 4.3 | Machine must exist and belong to company | Lookup fails | `"Machine {id} not found."` | 400 | `services/production_service.py:142-143` |
| 4.4 | Breakdown must exist and belong to the run | Update/delete fails | `"Breakdown {id} not found."` | 400 | `services/production_service.py:416-417, 445-446` |
| 4.5 | `end_time` must be >= `start_time` | Serializer validation | `"end_time must be >= start_time."` | 400 | `serializers.py:183-184` |
| 4.6 | `breakdown_minutes` auto-calculated from `start_time`/`end_time` if not provided | Automatic | N/A | `services/production_service.py:385-388` |

---

## 5. Material Usage Checks

| # | Check | What Happens | Error Message | HTTP Status | File Location |
|---|-------|-------------- |---------------|-------------|---------------|
| 5.1 | **COMPLETED run — cannot edit materials** | Save/update blocked | `"Cannot edit materials of a COMPLETED run."` | 400 | `services/production_service.py:466-467, 491-492` |
| 5.2 | Material must exist and belong to the run | Update fails | `"Material usage {id} not found."` | 400 | `services/production_service.py:498-499` |
| 5.3 | `batch_number` must be 1–3 | Serializer validation | Serializer error | 400 | `serializers.py:210` |
| 5.4 | `opening_qty`, `issued_qty`, `closing_qty` must be >= 0 | Serializer validation | Serializer error | 400 | `serializers.py:206-208` |
| 5.5 | `wastage_qty` auto-calculated: `opening + issued - closing` | Automatic | N/A | `services/production_service.py:474, 514` |

---

## 6. Machine Runtime Checks

| # | Check | What Happens | Error Message | HTTP Status | File Location |
|---|-------|-------------- |---------------|-------------|---------------|
| 6.1 | **COMPLETED run — cannot edit runtime** | Save/update blocked | `"Cannot edit machine runtime of a COMPLETED run."` | 400 | `services/production_service.py:529-530, 554-555` |
| 6.2 | Runtime entry must exist and belong to run | Update fails | `"Machine runtime {id} not found."` | 400 | `services/production_service.py:559-560` |
| 6.3 | `runtime_minutes`, `downtime_minutes` must be >= 0 | Serializer validation | Serializer error | 400 | `serializers.py:231-232` |

---

## 7. Manpower Checks

| # | Check | What Happens | Error Message | HTTP Status | File Location |
|---|-------|-------------- |---------------|-------------|---------------|
| 7.1 | **COMPLETED run — cannot edit manpower** | Save/update blocked | `"Cannot edit manpower of a COMPLETED run."` | 400 | `services/production_service.py:578-579, 594-595` |
| 7.2 | Manpower entry must exist and belong to run | Update fails | `"Manpower entry {id} not found."` | 400 | `services/production_service.py:603-604` |
| 7.3 | Unique constraint on `(production_run, shift)` | Uses `update_or_create` — upserts on same shift | N/A | `models.py:371` |
| 7.4 | `worker_count` must be >= 0 | Serializer validation | Serializer error | 400 | `serializers.py:252` |

---

## 8. Line Clearance Checks

| # | Check | What Happens | Error Message | HTTP Status | File Location |
|---|-------|-------------- |---------------|-------------|---------------|
| 8.1 | **Only DRAFT clearances can be edited** | Edit blocked | `"Only DRAFT clearances can be edited."` | 400 | `services/production_service.py:665-666` |
| 8.2 | **Only DRAFT clearances can be submitted** | Submit blocked | `"Only DRAFT clearances can be submitted."` | 400 | `services/production_service.py:692-693` |
| 8.3 | **All 9 checklist items must have a result (not N/A)** before submit | Submit blocked | `"All checklist items must have a result. Item '{checkpoint}' is still N/A."` | 400 | `services/production_service.py:696-702` |
| 8.4 | **At least one signature required** (supervisor OR incharge) before submit | Submit blocked | `"At least one signature (supervisor or incharge) is required."` | 400 | `services/production_service.py:705-706` |
| 8.5 | **Only SUBMITTED clearances can be approved/rejected** by QA | Approve blocked | `"Only SUBMITTED clearances can be approved/rejected."` | 400 | `services/production_service.py:714-715` |
| 8.6 | Clearance must exist and belong to company | Lookup fails | `"Line clearance {id} not found."` | 400 | `services/production_service.py:659-660` |
| 8.7 | 9 standard checklist items auto-created on clearance creation | Automatic | N/A | `services/production_service.py:642-647` |

### Standard Clearance Checklist Items (auto-created)

1. Previous product, labels and packaging materials removed
2. Machine/equipment cleaned and free from product residues
3. Utensils, scoops and accessories cleaned and available
4. Packaging area free from previous batch coding material
5. Work area (tables, conveyors, floor) cleaned and sanitized
6. Waste bins emptied and cleaned
7. Required packaging material verified against BOM
8. Coding machine updated with correct product/batch details
9. Environmental conditions (temperature/humidity) within limits

---

## 9. Machine Checklist Checks

| # | Check | What Happens | Error Message | HTTP Status | File Location |
|---|-------|-------------- |---------------|-------------|---------------|
| 9.1 | Machine must exist and belong to company | Lookup fails | `"Machine {id} not found."` | 400 | `services/production_service.py:142-143` |
| 9.2 | Template must exist and belong to company | Lookup fails | `"Checklist template {id} not found."` | 400 | `services/production_service.py:183-184` |
| 9.3 | Unique constraint on `(machine, template, date)` | Bulk upsert uses `update_or_create` — duplicate same-day entries are updated | N/A | `models.py:493` |
| 9.4 | Checklist entry must exist and belong to company | Update fails | `"Checklist entry {id} not found."` | 400 | `services/production_service.py:801-802` |

---

## 10. Waste Approval Checks

Waste approval follows a **strict sequential order**: Engineer → AM → Store → HOD.

| # | Check | What Happens | Error Message | HTTP Status | File Location |
|---|-------|-------------- |---------------|-------------|---------------|
| 10.1 | **Engineer must sign before AM** | AM approval blocked | `"Engineer must sign before AM."` | 400 | `services/production_service.py:856-857` |
| 10.2 | **AM must sign before Store** | Store approval blocked | `"AM must sign before Store."` | 400 | `services/production_service.py:862-863` |
| 10.3 | **Store must sign before HOD** | HOD approval blocked | `"Store must sign before HOD."` | 400 | `services/production_service.py:869-870` |
| 10.4 | Invalid approval level | Operation rejected | `"Invalid approval level: {level}"` | 400 | `services/production_service.py:877-878` |
| 10.5 | Waste log must exist and belong to company | Lookup fails | `"Waste log {id} not found."` | 400 | `services/production_service.py:843-844` |

### Waste Approval Status Transitions

```
PENDING → (Engineer signs) → PARTIALLY_APPROVED → (AM signs) → (Store signs) → (HOD signs) → FULLY_APPROVED
```

---

## 11. Final QC Checks

| # | Check | What Happens | Error Message | HTTP Status | File Location |
|---|-------|-------------- |---------------|-------------|---------------|
| 11.1 | **Only one Final QC per run** (OneToOne) | Duplicate POST blocked | `"Final QC already exists. Use PATCH to update."` | 400 | `views.py:1575-1576` |
| 11.2 | `overall_result` must be one of: `PASS`, `FAIL`, `CONDITIONAL` | Serializer validation | Serializer error | 400 | `serializers.py:532` |
| 11.3 | In-Process QC `result` must be one of: `PASS`, `FAIL`, `NA` | Serializer validation | Serializer error | 400 | `serializers.py:516` |

---

## 12. Resource Tracking Checks

All resource types (Electricity, Water, Gas, Compressed Air, Labour, Machine Cost, Overhead) share common patterns:

| # | Check | What Happens | Error Message | HTTP Status | File Location |
|---|-------|-------------- |---------------|-------------|---------------|
| 12.1 | Run must exist and belong to company | Lookup fails | `"Production run {id} not found."` | 404 | `views.py` (all resource views) |
| 12.2 | Resource entry must exist and belong to the run | Lookup fails | `"Not found"` / DoesNotExist | 404 | `views.py` (detail views) |
| 12.3 | Serializer validation on numeric fields | Invalid data rejected | `"Invalid data."` | 400 | `serializers.py` |
| 12.4 | `total_cost` auto-calculated on save | Automatic — see below | N/A | `models.py` |
| 12.5 | Cost summary auto-recalculated after every resource CRUD | `recalculate_run_cost()` called | N/A | `services/cost_calculator.py` |

---

## 13. Auto-Calculations

These values are computed automatically — they are NOT user-editable:

| Calculation | Formula | Trigger | File Location |
|-------------|---------|---------|---------------|
| **Run number** | Last run number + 1 per `(company, sap_doc_entry, date)` | On run creation | `services/production_service.py:214-219` |
| **Wastage qty** | `opening_qty + issued_qty - closing_qty` | On material save/update | `services/production_service.py:474, 514` |
| **Breakdown minutes** | `(end_time - start_time)` in minutes (if not provided) | On breakdown add | `services/production_service.py:385-388` |
| **Run total production** | `SUM(logs.produced_cases)` | On log/breakdown save, on complete | `services/production_service.py:269-273` |
| **Run total PE minutes** | `SUM(logs.recd_minutes)` | On log/breakdown save, on complete | `services/production_service.py:269-273` |
| **Run total breakdown time** | `SUM(breakdowns.breakdown_minutes)` | On log/breakdown save, on complete | `services/production_service.py:277-278` |
| **Line breakdown time** | `SUM(breakdowns where type=LINE)` | On log/breakdown save, on complete | `services/production_service.py:280-283` |
| **External breakdown time** | `SUM(breakdowns where type=EXTERNAL)` | On log/breakdown save, on complete | `services/production_service.py:285-288` |
| **Unrecorded time** | `720 - total_PE_minutes - total_breakdown_time` (min 0) | On log/breakdown save, on complete | `services/production_service.py:291-294` |
| **Electricity total_cost** | `units_consumed * rate_per_unit` | On save | `models.py:580-582` |
| **Water total_cost** | `volume_consumed * rate_per_unit` | On save | `models.py:609-611` |
| **Gas total_cost** | `qty_consumed * rate_per_unit` | On save | `models.py:638-640` |
| **Compressed Air total_cost** | `units_consumed * rate_per_unit` | On save | `models.py:667-669` |
| **Labour total_cost** | `hours_worked * rate_per_hour` | On save | `models.py:696-698` |
| **Machine total_cost** | `hours_used * rate_per_hour` | On save | `models.py:725-727` |
| **Per-unit cost** | `total_cost / produced_qty` (0 if no production) | On any resource change | `services/cost_calculator.py:66` |

---

## 14. State Machine Transitions

### Production Run Status

```
DRAFT ──(any data added)──> IN_PROGRESS ──(complete_run)──> COMPLETED
                                                                │
                                                     (IMMUTABLE — no edits
                                                      to logs, breakdowns,
                                                      materials, runtime,
                                                      manpower allowed)
```

### Line Clearance Status

```
DRAFT ──(submit)──> SUBMITTED ──(QA approves)──> CLEARED
                        │
                        └──(QA rejects)──> NOT_CLEARED
```

**Gates:**
- DRAFT → SUBMITTED: All 9 items must have result + at least 1 signature
- SUBMITTED → CLEARED/NOT_CLEARED: QA user decision

### Waste Approval Status

```
PENDING ──(Engineer signs)──> PARTIALLY_APPROVED ──(AM signs)──> ... ──(Store signs)──> ... ──(HOD signs)──> FULLY_APPROVED
```

**Gate:** Each level requires the previous level's signature.

---

## 15. Permission Checks

Every API endpoint enforces permission classes. If the user lacks the required permission, the request is rejected with `403 Forbidden`.

| Area | Required Permission |
|------|-------------------|
| Manage production lines | `CanManageProductionLines` |
| Manage machines | `CanManageMachines` |
| Manage checklist templates | `CanManageChecklistTemplates` |
| View production runs | `CanViewProductionRun` |
| Create production runs | `CanCreateProductionRun` |
| Edit production runs | `CanEditProductionRun` |
| Complete production runs | `CanCompleteProductionRun` |
| View/edit production logs | `CanViewProductionLog` / `CanEditProductionLog` |
| View/create/edit breakdowns | `CanViewBreakdown` / `CanCreateBreakdown` / `CanEditBreakdown` |
| View/create/edit materials | `CanViewMaterialUsage` / `CanCreateMaterialUsage` / `CanEditMaterialUsage` |
| View/create machine runtime | `CanViewMachineRuntime` / `CanCreateMachineRuntime` |
| View/create manpower | `CanViewManpower` / `CanCreateManpower` |
| View/create line clearance | `CanViewLineClearance` / `CanCreateLineClearance` |
| Approve line clearance (QA) | `CanApproveLineClearanceQA` |
| View/create machine checklists | `CanViewMachineChecklist` / `CanCreateMachineChecklist` |
| View/create waste logs | `CanViewWasteLog` / `CanCreateWasteLog` |
| Approve waste (Engineer) | `CanApproveWasteEngineer` |
| Approve waste (AM) | `CanApproveWasteAM` |
| Approve waste (Store) | `CanApproveWasteStore` |
| Approve waste (HOD) | `CanApproveWasteHOD` |
| View reports/analytics | `CanViewReports` |
| All endpoints | `IsAuthenticated` + `HasCompanyContext` |

---

## Quick Reference — Common Error Scenarios

| Scenario | What Happens |
|----------|-------------|
| Try to create a run on an inactive line | **Blocked** — `"Production line '{name}' is not active."` |
| Try to add logs to a completed run | **Blocked** — `"Cannot edit logs of a COMPLETED run."` |
| Try to add breakdown with machine from different line | **Blocked** — `"Machine does not belong to the same line as the run."` |
| Try to submit clearance with items still N/A | **Blocked** — `"All checklist items must have a result."` |
| Try to submit clearance without any signature | **Blocked** — `"At least one signature is required."` |
| Try to edit a submitted clearance | **Blocked** — `"Only DRAFT clearances can be edited."` |
| AM tries to approve waste before engineer | **Blocked** — `"Engineer must sign before AM."` |
| Store tries to approve waste before AM | **Blocked** — `"AM must sign before Store."` |
| HOD tries to approve waste before store | **Blocked** — `"Store must sign before HOD."` |
| Try to POST Final QC when one already exists | **Blocked** — `"Final QC already exists. Use PATCH to update."` |
| Try to complete an already completed run | **Blocked** — `"Run is already completed."` |
| Any request without authentication | **Blocked** — `401 Unauthorized` |
| Any request without company context | **Blocked** — `403 Forbidden` |
| Any request without required permission | **Blocked** — `403 Forbidden` |
