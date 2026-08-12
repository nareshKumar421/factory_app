# Quality Control — Backend (`quality_control` Django app)

> Paired frontend doc: `C:/Users/gurpa/dev/FactoryFlow/docs/modules/qc.md`
> (repo `FactoryFlow`, path `docs/modules/qc.md`).
>
> This document is written from the **code** (models, services, views, serializers,
> signals, permissions). Older files in this folder (`status_flow.md`, `api.md`,
> `api_endpoints.md`, `permission.md`) are partially stale — trust this README and
> the source when they disagree. Notable drift: report/lot numbers are **manually
> entered**, not auto-generated; `QC_HOLD` is a real gate status; the module now
> also covers **Production QC** and **QC print documents**.

---

## Overview — what it does & who uses it

`quality_control` is the QA/lab side of the factory-operations platform. It has two
largely independent halves plus shared master data:

1. **Arrival-slip / Raw-Material QC (LIVE, in daily use).** When a vehicle is
   received at the raw-material gate, a security guard fills a **Material Arrival
   Slip** for each PO line item and submits it to QA. A lab user then creates a
   **Raw Material Inspection**, records parameter readings, and drives it through a
   two-step approval chain: **QA Chemist → QA Manager (QAM)**. The QAM's decision
   (Accept / Reject / Hold) is the operational verdict that gates goods receipt
   (GRPO) and vendor returns.

2. **Production QC (rolling out).** Per **ProductionRun**, QC records **In-Process**
   and **Final (FG)** QC sessions against a product's parameter set, submits a
   PASS/FAIL, and a QC approver approves/rejects. Production can *request* a Final
   QC (creates a draft shell); QC owns parameter selection and the verdict.

3. **Shared master data.** `MaterialType`, its `QCParameterMaster` rows, the
   SAP-item→material-type mapping, and per-company `QCPrintDocument` IDs.

Users (via Django auth groups, see [Permissions](#permissions--roles)):
`qc_store` (guards / store), `qc_chemist` (lab technicians + chemist sign-off),
`qc_manager` (QAM — holds every QC permission), plus a dedicated Production-QC group
holding the line-clearance perms. The module is **deliberately gated** so shop-floor
`production_execution` users (who hold `can_view_production_qc` for in-run checks) do
**not** see the whole QC module.

**Line-clearance QA** permissions (`can_view_line_clearance_qc`,
`can_approve_line_clearance_qc`) are *defined* on the `ProductionQCSession` model here,
but the line-clearance **data and API live in `production_execution`** — QC only
consumes/approves it. **Customer-Return QC** is a frontend-only prototype (browser
`localStorage`); it has **no backend model in this app**.

---

## Key concepts & entities

All models extend `gate_core.models.BaseModel` (adds `is_active`, `created_by`,
`updated_by`, `created_at`, `updated_at`; "delete" is soft = `is_active=False`).

| Entity | File | Notes |
|---|---|---|
| `MaterialType` | `models/material_type.py` | Company-scoped category defining a parameter set. `unique_together=(code, company)`. Custom perms `can_manage_material_types`, `can_manage_qc_parameters`. |
| `MaterialTypeSAPItem` | `models/material_type_sap_item.py` | Maps a SAP `item_code` → a material type. **A SAP code may map to several material types**; `unique_together=(company, item_code, material_type)`. `item_code` force-uppercased on save. |
| `QCParameterMaster` | `models/qc_parameter_master.py` | A test parameter for a material type. `standard_value` is **free text** (e.g. `"1.35±0.10"`, `"NLT 20"`, `"Blue"`). `min_value`/`max_value` exist but in practice are rarely filled. `parameter_type` ∈ NUMERIC/TEXT/BOOLEAN/RANGE. `is_mandatory`, `sequence`. `unique_together=(material_type, parameter_code)`. |
| `QCPrintDocument` | `models/qc_print_document.py` | Per-company document ID printed on QC reports. `document_key=RAW_MATERIAL_INSPECTION`. `unique_together=(company, document_key)`. |
| `MaterialArrivalSlip` | `models/material_arrival_slip.py` | `OneToOne` → `raw_material_gatein.POItemReceipt`. Status DRAFT/SUBMITTED/REJECTED. Methods `submit_to_qa`, `send_back_to_gate`, `reject_by_qa`. Perms `can_submit_arrival_slip`, `can_send_back_arrival_slip`. |
| `ArrivalSlipAttachment` | `models/arrival_slip_attachment.py` | COA / COQ file. `unique_together=(arrival_slip, attachment_type)`. |
| `RawMaterialInspection` | `models/raw_material_inspection.py` | `OneToOne` → arrival slip. The heart of RM QC (see below). |
| `InspectionParameterResult` | `models/inspection_parameter_result.py` | One reading per parameter. `is_within_spec` auto-derived on `save()` (see spec evaluation). `unique_together=(inspection, parameter_master)`. |
| `InspectionAttachment` | `models/inspection_attachment.py` | Lab files uploaded during inspection (`qc_attachments`). |
| `InspectionManagerDecisionLog` | `models/raw_material_inspection.py` | Append-only audit row per QAM decision (kept even when a decision is overturned). |
| `ProductionQCSession` | `models/production_qc_session.py` | Per `production_execution.ProductionRun`. `session_type` IN_PROCESS/FINAL, `session_number` auto-incremented per run, workflow DRAFT/SUBMITTED/APPROVED/REJECTED, `overall_result` PASS/FAIL. `unique_together=(production_run, session_number)`. Holds the production-QC + line-clearance perms. |
| `ProductionQCResult` | `models/production_qc_result.py` | Per-parameter reading in a session; mirrors `InspectionParameterResult`. |

### `RawMaterialInspection` — the fields that matter

- **Identifiers `report_no` (globally `unique`) and `internal_lot_no`** are
  **manually entered by QC** (the serializer requires them; `generate_report_no` /
  `generate_lot_no` static helpers exist but are only used by the Django admin
  fallback, not the API).
- **`workflow_status`** (`enums.InspectionWorkflowStatus`): DRAFT → SUBMITTED →
  QA_CHEMIST_APPROVED → QAM_APPROVED, with REJECTED as a terminal side-exit.
  (`COMPLETED` is a legacy terminal value.)
- **`final_status`** (`enums.InspectionStatus`): PENDING / ACCEPTED / REJECTED /
  HOLD — the QAM's operational verdict.
- **Decision fields**: `qa_chemist_decision` and `qam_decision`
  (`enums.InspectionDecision` = APPROVED/HOLD/REJECTED) plus actor, timestamp,
  remarks. `manager_decision` property = the QAM's decision.
- **`is_locked`**: set `True` once the QAM decides or the item is rejected;
  blocks further edits.
- **Computed properties**: `is_grpo_done` (a POSTED GRPO exists for this PO item →
  QC decision may no longer change), `is_rejected_qc_returned` /
  `rejected_qc_return_entry` (the rejected material already left the gate on a
  `RejectedQCReturnEntry`), `qc_stage` (simplified DRAFT/AWAITING_CHEMIST/
  AWAITING_MANAGER/DECIDED), `effective_final_status`.

---

## End-to-end flows

### Flow A — Raw-material inspection (happy path)

1. **Arrival slip created & submitted.** Guard `POST`s
   `/po-items/<po_item_id>/arrival-slip/` (create/update) then
   `POST /arrival-slips/<id>/submit/`. Submit is multipart: if the slip flags a COA
   or COQ, the matching file is required (unless one already exists from a prior
   submission). `submit_to_qa()` sets status SUBMITTED + `in_time_to_qa`, and the
   view nudges the `VehicleEntry` to `ARRIVAL_SLIP_SUBMITTED`. Signal notifies
   `qc_store`.
2. **Inspection created.** Lab user `POST`s
   `/arrival-slips/<slip_id>/inspection/`. The slip **must be SUBMITTED** (else 400).
   The view resolves the **material type from the SAP code**
   (`_resolve_material_type_for_inspection`): one candidate auto-resolves; multiple
   require the caller's `material_type_id`; none → validation error telling the user
   to link the SAP item first. It then resolves **which vendor's parameters apply**:
   the vendor is taken from the PO (`POReceipt.supplier_code`), never from the
   request, and `resolve_parameter_set` picks that vendor's `QCParameterSet` if one
   exists, otherwise the material type's default set. Sending a `vendor_code` asks
   to inspect against someone else's parameters — that needs
   `can_override_qc_vendor` **and** a `vendor_override_reason`, both recorded on the
   inspection. `report_no` uniqueness is pre-checked, then the save runs inside a
   **retry loop (up to 5)** so a concurrent duplicate collides on the DB constraint,
   rolls back, and retries rather than 500-ing. Parameter-result rows are synced
   from the resolved set's active parameters, with each definition snapshotted onto
   the row; `qc_attachments` files are stored. `update_entry_status(entry)`
   recomputes the gate status.
3. **Readings entered.** `POST /inspections/<id>/parameters/` bulk-updates results.
   Each `InspectionParameterResult.save()` auto-derives `is_within_spec` from the
   free-text spec (see [spec evaluation](#spec-evaluation)).
4. **Submit for approval.** `POST /inspections/<id>/submit/`. Guards: not locked;
   status must be DRAFT; **every mandatory parameter must have a `result_value`**;
   **if any parameter is out of spec, `remarks` is required** (a remark may be sent
   inline on submit). Moves to SUBMITTED; signal notifies `qc_chemist`.
5. **QA Chemist decision.** `POST /inspections/<id>/approve/chemist/` (alias
   `/chemist-decision/`). Requires status SUBMITTED. Records
   APPROVED/HOLD/REJECTED and moves to QA_CHEMIST_APPROVED; signal notifies
   `qc_manager`.
6. **QA Manager (QAM) decision.** `POST /inspections/<id>/approve/qam/` (alias
   `/manager-decision/`). Allowed from QA_CHEMIST_APPROVED **or** QAM_APPROVED (the
   QAM may revise). Sets `qam_decision`, maps it to `final_status`
   (APPROVED→ACCEPTED, REJECTED→REJECTED, HOLD→HOLD), sets `is_locked=True`, and
   appends an `InspectionManagerDecisionLog` row. On ACCEPTED → notify `grpo`
   ("Ready for GRPO"); on HOLD → notify `qc_store`; on REJECTED → notify `qc_store`
   + gate.
7. **Gate status settles.** After every step the view calls
   `update_entry_status(entry)`, which recomputes the `VehicleEntry.status` from the
   bottleneck across **all** PO items (see [rules](#business-rules--invariants)).
   When all items reach a final decision → `QC_COMPLETED`, and the raw-material gate
   group is notified that the entry can be completed.

### Flow B — Rejection & vendor return

- QAM (or chemist) rejects → `final_status=REJECTED`, `workflow_status=REJECTED`,
  `is_locked=True`, and the arrival slip is flipped to REJECTED (`reject_by_qa`).
- The item now appears on `GET /inspections/return-to-vendor/` (rejected **and** not
  yet returned). The **gate module** (`gate_core`) builds a `RejectedQCReturnEntry`
  + `RejectedQCReturnItem` (`OneToOne` to the inspection) for the physical gate-out.
- Once returned, `is_rejected_qc_returned` becomes true and the QAM can **no longer**
  revise the decision (guarded in `InspectionApproveQAMAPI`).

### Flow C — Send back to gate (correction before QC starts)

`POST /arrival-slips/<id>/send-back/`. Allowed only when the slip is SUBMITTED and
the inspection is **absent or still DRAFT**. Any draft inspection is soft-deleted
(`cancel_for_send_back` sets `is_active=False`, `is_locked=True`, and **detaches the
OneToOne** so a fresh inspection can be created later). Slip → DRAFT, entry →
`ARRIVAL_SLIP_REJECTED`, guard notified. If the inspection has already been
submitted to the chemist, send-back is refused ("use inspection rejection instead").

### Flow D — Production QC

1. **Session start.** `POST /production-qc/runs/<run_id>/sessions/` with a
   `material_type_id`, `session_type`, `checked_at`. Parameter rows are populated
   from the material type. Only **one FINAL** session per run.
2. **Production requests Final QC** (optional): `POST
   /production-qc/runs/<run_id>/request-final/` (needs
   `can_edit_production_run`/`can_complete_production_run`, run must be COMPLETED).
   Creates a **draft FINAL shell with `material_type=None`**; QC later picks the
   parameter set via the create endpoint, which attaches to that shell.
3. **Enter results** `POST /production-qc/sessions/<id>/results/` (DRAFT only).
4. **Submit** `POST /production-qc/sessions/<id>/submit/` with PASS/FAIL. Requires a
   material type + at least one result + all mandatory parameters filled. Locks the
   session (DRAFT→SUBMITTED).
5. **Approve / Reject** `.../approve/` or `.../reject/` (needs
   `can_approve_production_qc`; only from SUBMITTED; approve requires PASS/FAIL).

---

## Business rules & invariants

- **Company scoping.** Every endpoint uses DRF `HasCompanyContext` and filters on
  `request.company.company` (the single active company). There is **no
  `all_companies` cross-company read here** — QC is strictly single-company. Master
  data (`MaterialType`, SAP mappings, print docs) is per-company.
- **Arrival slip gate.** An inspection can only be created against a **SUBMITTED**
  slip. Re-submitting an already-SUBMITTED slip is refused.
- **`report_no` is globally unique and user-supplied.** Duplicates are rejected with
  a field error; concurrent collisions are absorbed by the retry loop
  (`MAX_INSPECTION_SAVE_RETRIES = 5`); if still colliding after retries → 400.
- **Material-type resolution** is driven by the SAP code, not free choice: 1
  candidate auto-resolves, N candidates require an explicit `material_type_id`, 0
  candidates block creation until the code is linked (`link-sap-item/`).
- **Submit guards:** all mandatory params filled; out-of-spec params need a remark.
- **QAM decision is final but revisable** — *until* committed downstream:
  `is_grpo_done` (a POSTED GRPO exists) **or** `is_rejected_qc_returned` (material
  left the gate) locks it (both re-checked server-side in `InspectionApproveQAMAPI`).
- **Locked inspections** reject edits, parameter updates, re-submit, and reject.
- **Gate-status is a bottleneck computation** (`compute_entry_status`): status
  reflects the *least-progressed* item. Any missing arrival-slip/inspection keeps the
  entry at `QC_PENDING`; any REJECTED (without all-terminal) → `QC_REJECTED`; any
  HOLD → `QC_HOLD`; all items ACCEPTED/REJECTED → `QC_COMPLETED`. `QC_COMPLETED`
  triggers a "gate can be completed" notification to `raw_material_gatein`.
- **Production QC:** one FINAL per run; the `request-final` shell may only be filled
  while it is DRAFT with `material_type=None`.

### Spec evaluation

`services/spec_evaluation.py` derives `is_within_spec` because specs are free text
and readings are typed into the text field. It parses tolerance (`235±5`, `+/-`,
`+-`), comparator (`NLT`/`MIN`/`>=` and `NMT`/`MAX`/`<=`), and dash ranges
(`58.6-61.7`), and pulls the first number out of labelled results (`"AVG-233"`→233).
Structured `min_value`/`max_value` win when present. **Non-numeric / visual specs
return `None`**, meaning "cannot auto-decide" — the inspector's manual pass/fail flag
is left untouched. Used by both `InspectionParameterResult` and `ProductionQCResult`.

---

## Integrations & cross-module boundaries

| Direction | Module | Boundary |
|---|---|---|
| Upstream | `raw_material_gatein` (`POItemReceipt`, `POReceipt`) | Arrival slips are `OneToOne` to PO items; the whole QC chain reaches the `VehicleEntry` via `arrival_slip.po_item_receipt.po_receipt.vehicle_entry`. |
| Up/down | `gate_core` / `driver_management` (`VehicleEntry`, `GateEntryStatus`) | `services/rules.py` is the single writer of QC-phase gate statuses. |
| Downstream | `gate_core` (`RejectedQCReturnEntry`/`Item`) | Rejected QC items are physically returned via a gate-out entry (`OneToOne` back to the inspection); it locks the QC decision. |
| Downstream | `grpo` | A POSTED GRPO for the PO item locks the QC decision (`is_grpo_done`); QAM ACCEPTED notifies the `grpo` group with a preview link. |
| Peer | `production_execution` (`ProductionRun`, line clearance) | Production QC sessions hang off runs; line-clearance perms live here but data lives there. |
| SAP (read-only) | `production_execution.services.sap_reader.ProductionOrderReader` | `SAPItemSearchAPI` searches the SAP **item master** to link SAP items to material types. On `SAPReadError` → **HTTP 503**. QC does **not** post anything to SAP. |
| Cross-cutting | `notifications` (FCM) | `signals.py` sends push on slip submit/send-back and every inspection workflow transition (see below). |

### Notifications (`signals.py`)

`post_save` signals fire **after commit** (`transaction.on_commit`). Every QC
notification is also delivered to the `raw_material_gatein` group in addition to its
primary target, so gate users stay in the loop. Failures are logged and swallowed —
they never break the QC action.

| Trigger | Primary group | Type |
|---|---|---|
| Slip submitted | `qc_store` | `ARRIVAL_SLIP_SUBMITTED` |
| Slip sent back | `raw_material_gatein` | `ARRIVAL_SLIP_SENT_BACK` |
| Inspection → SUBMITTED | `qc_chemist` | `QC_INSPECTION_SUBMITTED` |
| Inspection → QA_CHEMIST_APPROVED | `qc_manager` | `QC_CHEMIST_APPROVED` |
| QAM → ACCEPTED | `grpo` | `QC_QAM_APPROVED` |
| QAM → HOLD | `qc_store` | `QC_HOLD` |
| Rejected | `qc_store` (+ gate) | `QC_REJECTED` |
| Entry → QC_COMPLETED | `raw_material_gatein` | `QC_COMPLETED` |

---

## Real-world edge cases

- **Concurrent duplicate `report_no`.** *Trigger:* two lab users save inspections
  with the same manually-typed report number at once. *Behaviour:* pre-check +
  transactional retry loop (×5); loser retries, and if the number is genuinely taken
  it 400s with a field error. *Symptom:* "This report number is already in use."
  *Risk:* none data-wise; a rare double-submit shows the error.

- **SAP code maps to multiple material types.** *Trigger:* one SAP item legitimately
  belongs to >1 QC category. *Behaviour:* inspection creation demands
  `material_type_id`; the API returns all candidates via
  `/material-types/by-sap-item/<code>/`. *Symptom:* "select which one applies."
  *Risk:* picking the wrong type loads the wrong parameter set.

- **Unmapped SAP code.** *Trigger:* new item never linked. *Behaviour:* creation
  blocked with "Link SAP item … to a material type before creating QC entry"; user
  links via `link-sap-item/`. *Risk:* if SAP item-search is down, linking a brand-new
  code is blocked (existing mappings still work).

- **SAP down during item search.** *Trigger:* SAP/DI-API unreachable. *Behaviour:*
  `SAPItemSearchAPI` returns **503**. *Symptom:* "search unavailable" — cannot map
  new codes until SAP is back. *Risk:* new-material QC stalls at the mapping step.

- **Bill / PO item added after gate-in.** *Trigger:* a late PO line with no arrival
  slip yet. *Behaviour:* `compute_entry_status` sees `has_pending_prerequisite` and
  holds the entry at `QC_PENDING`; the gate cannot complete. *Symptom:* entry stuck
  "QC Pending" though other items are done. *Risk:* needs the new item's slip +
  inspection before completion.

- **Partial multi-item truck.** *Trigger:* item A rejected, item B still in review.
  *Behaviour:* entry sits at `QC_REJECTED` until **all** items reach a final
  decision, then flips to `QC_COMPLETED`. *Symptom:* "QC Rejected" banner while B is
  worked. *Risk:* none; matches the bottleneck rule.

- **QAM tries to overturn a committed decision.** *Trigger:* GRPO already posted, or
  rejected material already left the gate. *Behaviour:* `approve/qam/` 400s ("Decision
  is locked: GRPO has already been posted" / "the rejected material has already been
  sent out at the gate"). *Risk:* intended — prevents downstream inconsistency.

- **Send-back after chemist submission.** *Trigger:* QA tries to bounce a slip to the
  gate after the inspection was already submitted. *Behaviour:* 400 "Cannot send back
  — inspection has already been submitted. Use inspection rejection instead."

- **Deleted user (dangling FK).** *Trigger:* a `qa_chemist`/`qam` user row deleted
  outside the ORM leaves `SET_NULL` un-fired. *Behaviour:* serializers use
  `_safe_related` to return `null` instead of raising `DoesNotExist`. *Symptom:*
  blank approver name; list/detail endpoints don't 500.

- **Out-of-spec without a remark.** *Trigger:* submit an inspection whose reading is
  outside spec, no remark. *Behaviour:* 400 `{"remarks": ["A remark is required …"]}`.
  A submit-only user can pass the remark inline on the submit call.

- **Production Final QC requested twice.** *Trigger:* production requests FG QC when a
  FINAL session already exists. *Behaviour:* the existing session is returned (or, if
  it's a still-empty draft shell, QC's parameter selection attaches to it); a second
  *filled* FINAL is refused ("A Final QC session already exists for this run").

---

## Failure modes / what can break

| Failure | Operator-visible symptom | Where |
|---|---|---|
| SAP item master unreachable | 503 on SAP item search; can't link new codes | `SAPItemSearchAPI` |
| `report_no` collision persists after retries | 400 "report number already in use" | `InspectionCreateUpdateAPI` |
| Editing a locked inspection | 400 "Inspection is locked" | submit / parameters / approve / reject |
| Creating inspection before slip submitted | 400 "Arrival slip must be submitted first" | `InspectionCreateUpdateAPI` |
| Missing COA/COQ file on submit | 400 "Certificate … is required" | `ArrivalSlipSubmitAPI` |
| Approving out of order | 400 "QA Chemist must approve first" / "must be submitted first" | approval APIs |
| Notification backend down | QC action still succeeds; push silently missing (logged) | `signals.py` |
| Entry won't complete | Gate stuck at QC_PENDING/QC_REJECTED because an item lacks a final decision | `compute_entry_status` |

---

## Improvement opportunities & known gaps

- **No optimistic-lock / atomic sequence for `report_no`.** The read-max-then-insert
  helpers plus a retry loop work but aren't a true sequence; a DB sequence or
  `select_for_update` counter would be cleaner.
- **`min_value`/`max_value` are almost never populated**, so `is_within_spec` leans
  entirely on the free-text parser. Encouraging structured bounds would make
  pass/fail deterministic.
- **Customer-Return QC has no backend here** — it is entirely browser `localStorage`
  in the frontend and does not survive a device/browser change (see the frontend
  doc). A server model + API is the obvious next step.
- **Line-clearance perms defined on `ProductionQCSession`** but data lives in
  `production_execution` — a slightly surprising split worth a code comment/onboarding
  note (there is one in `module.config.tsx`).
- **`workflow_status=COMPLETED`** is legacy and unused by the current flow; consider
  removing to avoid confusion.
- **`MaterialArrivalSlip.po_item_receipt` / `RawMaterialInspection.arrival_slip` are
  `null=True`** ("temporarily nullable for migration") — long-lived nullable FKs that
  the code otherwise assumes are present.

---

## Permissions & roles

Access is **Django-permission based** (`permissions.py` wraps `has_perm`), not
role-string based. Custom permissions live on the model `Meta.permissions`.

| Permission (codename) | Guards |
|---|---|
| `add/change/view_materialarrivalslip` | Create / edit / view arrival slips |
| `can_submit_arrival_slip` | Submit slip to QA |
| `can_send_back_arrival_slip` | Bounce slip to gate |
| `add/change/view_rawmaterialinspection` | Create / edit / view inspections |
| `can_submit_inspection` | Submit inspection for approval |
| `can_approve_as_chemist` | QA Chemist decision + chemist queue |
| `can_approve_as_qam` | QA Manager decision + QAM queue |
| `can_reject_inspection` | Reject inspection |
| `can_manage_material_types` | Material-type master + SAP search |
| `can_manage_qc_parameters` | Parameter master + **print documents** |
| `can_view/create/submit/approve_production_qc` | Production QC lifecycle |
| `can_view/approve_line_clearance_qc` | Line-clearance QA (data in `production_execution`) |

**Auth groups** (migrations `0010`/`0012`, `0018`, `0025`):
- `qc_store` — arrival-slip add/change/view/submit + `view_rawmaterialinspection`.
- `qc_chemist` — view slips, inspection add/change/view/submit, `can_approve_as_chemist`.
- `qc_manager` — **all** `quality_control` permissions (the QAM).
- A dedicated Production-QC group carries the line-clearance perms; front-end nav is
  gated on **line-clearance** perms (not `can_view_production_qc`) so shop-floor users
  don't get the whole module. See the memory note *"Group perms vs frontend nav gating."*

---

## Developer file map

**Backend (`C:/Users/gurpa/dev/factory_app/quality_control/`)**
- `models/` — `material_type.py`, `material_type_sap_item.py`, `qc_parameter_master.py`,
  `qc_print_document.py`, `material_arrival_slip.py`, `arrival_slip_attachment.py`,
  `raw_material_inspection.py` (+ `InspectionManagerDecisionLog`),
  `inspection_parameter_result.py`, `inspection_attachment.py`,
  `production_qc_session.py`, `production_qc_result.py`. (`models.py` is an empty stub.)
- `enums.py` — arrival-slip/inspection/decision/workflow/parameter enums.
- `services/rules.py` — gate-status computation + QC-completed notification.
- `services/spec_evaluation.py` — free-text spec → `is_within_spec`.
- `views.py` — master data, arrival slips, inspections, approvals, status-based lists.
- `views_production_qc.py` — production QC session lifecycle.
- `serializers.py` — all read/write serializers (incl. `_safe_related` FK guard).
- `permissions.py` — DRF permission classes.
- `signals.py` — FCM notifications.
- `urls.py` — full endpoint map (prefix `/api/v1/quality-control/`).
- `admin.py` — Django admin (report/lot auto-gen fallback lives here).

**Frontend (`C:/Users/gurpa/dev/FactoryFlow/src/modules/qc/`)** — see the paired doc;
entry points: `module.config.tsx`, `api/`, `pages/`, `hooks/useInspectionPermissions.ts`.

---

## Related docs

- **Paired frontend doc:** `C:/Users/gurpa/dev/FactoryFlow/docs/modules/qc.md`.
- Older (partially stale) local docs: `status_flow.md`, `api.md`,
  `api_endpoints.md`, `permission.md` in this folder — useful for the gate-status
  transition table and notification detail, but verify against this README.
- Upstream/downstream: `raw_material_gatein`, `gate_core`, `grpo`,
  `production_execution` app docs (if present).
