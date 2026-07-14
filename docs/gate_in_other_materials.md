# Gate-In — Other Material Types (Backend)

Daily Needs / Canteen, Construction, Fixed Assets, Maintenance, and Person (Visitor/Labour/Contractor) gate entries.

> **Audience:** new backend devs + technical managers.
> **Scope:** Django apps `daily_needs_gatein`, `construction_gatein`, `fixed_asset_gatein`, `maintenance_gatein`, `person_gatein`, plus the shared header (`driver_management.VehicleEntry`) and `gate_core` plumbing these ride on.
> **Paired frontend doc:** [`FactoryFlow/docs/modules/gate-in-other.md`](../../FactoryFlow/docs/modules/gate-in-other.md).
> Trust this doc + the code over older per-app `README.md`/`docs/api.md` files in each app folder — those predate several changes described here.

---

## Overview — what it does & who uses it

The factory gate records every vehicle and person that enters. Raw-material inward is heavy (PO/SAP GRPO/QC/weighbridge). Everything **else** that rolls or walks through the gate is handled by the five apps documented here — deliberately **light-weight gate passes** with no SAP posting and no weighbridge/QC step:

| App | Entry type | What comes in | Who files it |
|-----|-----------|---------------|--------------|
| `daily_needs_gatein` | `DAILY_NEED` | Canteen food, consumables, routine supplies | Gate operator / canteen supervisor |
| `construction_gatein` | `CONSTRUCTION` | Civil/building material from contractors | Gate operator / site engineer |
| `fixed_asset_gatein` | `FIXED_ASSET` | Capital assets, machinery, furniture, tools | Gate operator / stores |
| `maintenance_gatein` | `MAINTENANCE` | Spare parts, tools, service material | Gate operator / maintenance store |
| `person_gatein` | *(none — standalone)* | Visitors, labour, contractors (people, not goods) | Gate/security operator |

**Two distinct architectures live here:**

1. **Material gate-ins (daily/construction/fixed-asset/maintenance)** hang a detail record off a shared `VehicleEntry` header (a vehicle drove in). Lifecycle: `DRAFT → COMPLETED (+ locked)`.
2. **Person gate-in** is a completely separate model (`EntryLog`) with **no `VehicleEntry`** and **no company scoping**. Lifecycle: `IN → OUT` (or `CANCELLED`). It also carries the "who is inside" board, multiple gates, and visitor/labour/contractor masters.

None of these five flows touch SAP for their core path. The only external-system side effect is the maintenance app's optional **receive-spare** action, which posts into the internal `maintenance` store stock (still not SAP).

---

## Key concepts & entities

### Shared header — `VehicleEntry` (driver_management/models/vehicle_entry.py)
The "gate root". Extends `gate_core.GateEntryBase` (which adds `entry_no` unique, `status`, `is_locked` + `BaseModel` audit fields). Key fields: `company` (FK, PROTECT), `vehicle`, `driver`, `entry_type` (choice), `entry_no` (unique, ≤30 chars), `status` (`GateEntryStatus`, default `DRAFT`), `is_locked`, `remarks`.

- `entry_type` choices include `DAILY_NEED`, `MAINTENANCE`, `CONSTRUCTION`, `FIXED_ASSET` (plus raw-material/BST/empty-vehicle/sales-dispatch/job-work used by other modules).
- The detail record for each material type is a `OneToOneField` back to this header (`daily_need_entry`, `construction_entry`, `fixed_asset_entry`, `maintenance_entry`).
- **Lock protection:** `GateEntryBase.save()` re-reads the row and raises a plain `ValueError("Gate entry is locked and cannot be modified")` if `is_locked` is already `True`. This is a hard backstop *below* the DRF layer.

### Material detail models
- **`DailyNeedGateEntry`** + **`DailyNeedGateEntryItem`** (line items) + **`CategoryList`** lookup. Captures supplier, receiving `Department`, bill/challan numbers, canteen supervisor, contact. Legacy single `material_name`/`quantity`/`unit` fields are mirrored from the first line item.
- **`ConstructionGateEntry`** + **`ConstructionMaterialCategory`**. Auto-generates `work_order_number` = `WO-YYYY-NNN`. Has `security_approval` (`PENDING`/`APPROVED`/`REJECTED`, default `PENDING`), `site_engineer`, `contractor_name`, `material_description`.
- **`FixedAssetGateEntry`** (header) + **`FixedAssetItem`** (one row per asset, with `serial_number`). Auto-generates `work_order_number` = `FA-YYYY-NNN`.
- **`MaintenanceGateEntry`** + **`MaintenanceType`**. Auto-generates `work_order_number` = `WO-YYYY-NNN`, has `urgency_level` (`NORMAL`/`HIGH`/`CRITICAL`), `equipment_id`, `part_number`. Optionally linked to the `maintenance` app's asset/work-order/spare via `MaintenanceGateLink`.

### Person gate-in models (person_gatein/models.py)
- **`EntryLog`** — the core table. `status` (`IN`/`OUT`/`CANCELLED`, default `IN`), `person_type` (FK PROTECT), nullable `visitor`/`labour` FKs, **snapshot** `name_snapshot`/`photo_snapshot` (audit-safe copy so the log survives master edits/deletes), `gate_in` (PROTECT), `gate_out` (nullable), `entry_time`/`actual_entry_time`/`exit_time`, `purpose`, `vehicle_no`, `approved_by`.
- Masters: **`Visitor`** (with `blacklisted` flag), **`Labour`** (belongs to a `Contractor`, has `permit_valid_till`, `skill_type`), **`Contractor`**, **`PersonType`** (Visitor/Labour), **`Gate`** (multiple physical gates, `is_active`).
- **No `company` field anywhere in this app** — EntryLog data is global across all companies (see Cross-module boundaries).

### Company context — `HasCompanyContext` (company/permissions.py)
Every endpoint requires a `Company-Code` HTTP header. The permission resolves the active `UserCompany` and attaches `request.company` (a `UserCompany`); the actual `Company` is `request.company.company`. Material gate-ins scope all reads/writes by `company=request.company.company`. Person gate-in requires the header but **does not filter by it**.

---

## End-to-end flows (server view)

### Flow A — Material gate-in (daily / construction / fixed-asset / maintenance)

1. **Create the header.** `POST /api/v1/vehicle-management/vehicle-entries/` (`VehicleEntryListCreateAPI`). Body supplies `entry_no`, `vehicle`, `driver`, `entry_type`, `remarks`; `company` is injected from the header context; `status` defaults to `DRAFT`. Returns `{id, entry_no, status}`. **`entry_no` is client-supplied** (the frontend generates `GE-YYYY-XXXX`) — the server only enforces uniqueness.
2. **(Security check)** A `security_check` record is attached to the header via the `security_checks` app (same `gate_entry_id`). Not enforced by the completion service for these light types, but the frontend always saves one.
3. **Attach the material detail.** `POST /api/v1/<app>/gate-entries/<gate_entry_id>/<type>/`:
   - Loads the header by `id + company`, rejects if `entry_type` mismatches (`400 "Invalid entry type. Expected …"`) or `is_locked` (`400 "Gate entry is locked"`).
   - Daily-need view **upserts** (re-POST updates the existing detail). Construction/fixed-asset/maintenance return `400 "… already exists"` on a second POST and expose a separate `…/update/` PUT.
   - Serializer validates and saves with `vehicle_entry=header, created_by=user`. Construction/fixed/maintenance return the generated `work_order_number`.
4. **Upload the bill.** `POST /api/v1/gate-core/gate-attachments/<gate_entry_id>/` creates a `GateAttachment` (FK to the header). At least one is **mandatory** before completion.
5. **Complete (lock).** `POST /api/v1/<app>/gate-entries/<gate_entry_id>/complete/`:
   - The **view** first checks `GateAttachment.objects.filter(gate_entry=entry).exists()` → `400 "Bill upload is required before completing this … entry"` if none.
   - Then calls the app's `complete_*_gate_entry(vehicle_entry)` service inside `@transaction.atomic`.
   - On success: `status = "COMPLETED"`, `is_locked = True`, saved. Response `200 "… completed successfully"`.

### Flow B — Construction completion rules (the strict one)
`complete_construction_gate_entry` enforces, in order: not already locked → `entry_type == CONSTRUCTION` → detail exists → `security_approval == "APPROVED"` → non-empty `site_engineer` → `material_category` set → non-empty `contractor_name`. Any failure raises DRF `ValidationError` → `400` with the message.

### Flow C — Fixed-asset completion rules
`complete_fixed_asset_gate_entry`: not locked → `entry_type == FIXED_ASSET` → detail exists → non-empty `supplier_name` → `items.exists()` (≥1 asset line).

### Flow D — Daily-need / Maintenance completion rules
Minimal: not locked → correct `entry_type` → detail exists. (Bill attachment is enforced by the view, not the service.)

### Flow E — Maintenance receive-spare (optional downstream)
`POST /api/v1/maintenance-gatein/gate-entries/<id>/maintenance/receive-spare/` (`MaintenanceGateReceiveSpareAPI`). Requires a `MaintenanceGateLink` with a `spare` set. Guards: link exists, spare linked, not already `RECEIVED`; if `qc_required` and `qc_status` not `ACCEPTED`/`WAIVED` it sets the link to `BLOCKED` and returns `400`. On success it creates a `MaintenanceSpareReceipt`, increments `MaintenanceSpare.current_stock`, writes a `SpareMovement` (RECEIPT), and marks the link `RECEIVED`. **Internal store stock only — not SAP.**

### Flow F — Person gate-in (EntryLog lifecycle)
1. **Enter.** `POST /api/v1/person-gatein/entry/create/` (`EntryService.create_entry`). Requires exactly one of `visitor`/`labour` + `gate_in` + `person_type`. **Blocks a second open entry** for the same person (`"Person already inside"`). Snapshots the name; creates `EntryLog(status="IN")`. Fires a "person entered" notification on commit.
2. **Exit.** `POST /api/v1/person-gatein/entry/<id>/exit/` sets `status="OUT"`, `exit_time=now`, optional `gate_out`. Rejects if not currently `IN`. Fires a "person exited" notification.
3. **Cancel.** `POST /entry/<id>/cancel/` — only from `IN`, appends the reason to remarks, sets `CANCELLED`.
4. **Bulk labour.** `entry/bulk-create/` and `entry/bulk-exit/` process a whole contractor's crew with **partial success** — already-inside labour are skipped (not errored), the rest processed; the response lists per-labour `created`/`exited`/`skipped` + reason.
5. **Boards & lookups.** `entry/inside/` (who is inside now), `entries/` (date/status/gate filters), `entries/search/`, `dashboard/` (live counts, gate-wise, person-type-wise, >8h-inside flag), per-visitor/labour history, `check-status/`.

---

## Critical business rules & invariants

- **Company scoping (material):** header + detail are always fetched with `company=request.company.company`. A record from a sibling company returns `404` on that company's `Company-Code`. See the "cross-company flow boundary" memory note — reads that must span companies need an explicit `all_companies` opt-in, which these material apps do **not** implement.
- **Company scoping (person): none.** `EntryLog`/`Visitor`/`Labour`/`Gate` have no company column; the whole factory shares one person register regardless of the active `Company-Code`.
- **Bill/document mandatory to complete:** all four material completions are blocked until ≥1 `GateAttachment` exists on the header.
- **Lock is terminal:** completion sets `is_locked=True`. After that, any save on the header raises `ValueError` at the model layer; the DRF views also pre-check `is_locked` and return `400`. There is **no un-lock / re-open endpoint**.
- **Entry-type gating:** each detail create/update/complete rejects a header whose `entry_type` doesn't match the app.
- **`entry_no` uniqueness is global** across all entry types and companies (single unique column on the shared table).
- **Construction approval is self-service:** `security_approval` is a plain field on the construction detail set through the same create/update payload the gate operator fills — there is no separate security-officer endpoint or role gate. "APPROVED" is whatever the operator picked.
- **Work-order namespaces overlap:** construction & maintenance both mint `WO-YYYY-NNN`, each from its own table's max. Numbers are unique within an app but the *same* string can exist as both a construction and a maintenance WO. Fixed assets use the distinct `FA-YYYY-NNN`.
- **Person "one open entry" invariant:** a visitor/labour cannot have two `IN` rows simultaneously.
- **Snapshots protect the audit trail:** `EntryLog.name_snapshot`/`photo_snapshot` are copied at entry time so deleting/renaming a master doesn't rewrite history.

---

## Integrations & cross-module boundaries

- **`gate_core`** — owns `GateEntryBase`, `GateAttachment`, `UnitChoice`, the `GateEntryStatus` enum, and the human-readable **FullView** read endpoints (`DailyNeedGateEntryFullView`, `MaintenanceGateEntryFullView`, `ConstructionGateEntryFullView` at `/api/v1/gate-core/<type>-gate-entry/<id>/`). Those FullViews are what the frontend Review pages read.
- **`vehicle_management` / `driver_management`** — the shared `VehicleEntry` header, list/count/by-status endpoints used by the material dashboards.
- **`security_checks`** — attaches a `security_check` to the header (surfaced in FullView).
- **`accounts.Department`** — receiving department on daily-need & maintenance details.
- **`maintenance` app** — `MaintenanceGateLink`/`MaintenanceSpare`/`MaintenanceSpareReceipt`/`SpareMovement` for the receive-spare flow. This is the only path that writes stock.
- **`notifications`** — `daily_needs_gatein` notifies the `daily_needs_gatein` auth group on detail creation; `person_gatein` notifies the `person_gatein` group on person enter/exit. Both use `transaction.on_commit` so a rolled-back save never sends.
- **SAP: not involved.** Unlike raw-material gate-in, none of these five apps create a GRPO or call `SAPClient`. Managers should not expect SAP documents for canteen/construction/asset/maintenance/person entries.

---

## Real-world edge cases

Each: **trigger → current behaviour → operator-visible symptom → risk/gap.**

- **Duplicate `entry_no` (client collision).** Two operators (or two tabs) create a header in the same second → both compute `GE-YYYY-XXXX` from the last 4 digits of the timestamp → **trigger** identical `entry_no`. **Behaviour:** the second `INSERT` violates the unique constraint. **Symptom:** "Failed to save gate entry" on Step 1 for the second user. **Risk:** low-probability but real; the number space is only the last 4 timestamp digits and is client-generated. Server does not retry/regenerate.

- **Bill added after the fact / skipped.** Operator files the detail but forgets the bill, then hits Complete. **Behaviour:** completion view returns `400 "Bill upload is required before completing this … entry"`; nothing is locked. **Symptom:** completion button errors; entry stays editable in `DRAFT`. **Risk:** none to data — but the entry lingers un-completed until someone uploads the bill.

- **Construction stuck un-completable.** `security_approval` left `PENDING`/`REJECTED`, or `site_engineer` blank. **Behaviour:** `complete_construction_gate_entry` raises `400 "Security approval is PENDING. Must be APPROVED…"` (or the site-engineer/category/contractor message). **Symptom:** contractor material is physically inside but the gate pass can't be closed. **Risk:** approval is self-selected by the same operator, so the control is a formality; conversely a genuinely-pending case has no separate approver workflow to unblock it.

- **Person never marked out ("stuck inside").** A visitor/labour leaves but the gate misses the exit scan. **Behaviour:** the `IN` row persists; a re-entry attempt raises `"Person already inside"`; the dashboard's ">8h inside" counter climbs. **Symptom:** operator can't re-admit the person and the inside board is wrong. **Risk:** no auto-timeout/auto-exit exists; only a manual `exit`/`cancel` clears it.

- **Cross-company person visibility.** Person entered while company A is active; another user switches to company B. **Behaviour:** because `EntryLog` isn't company-scoped, B sees and can exit A's person. **Symptom:** the inside board shows people from every company at once. **Risk:** intended (one physical gate) but surprising to anyone expecting company isolation; also means person counts aren't per-company.

- **Editing a completed entry.** Any code path that saves a locked header. **Behaviour:** DRF views return `400 "Gate entry is locked"`; if something bypasses the view check, `GateEntryBase.save()` raises a bare `ValueError` → unhandled `500`. **Symptom:** either a clean 400 or (rarely) a server error. **Risk:** the model-level guard is a `ValueError`, not a DRF exception, so it isn't rendered as a friendly 400.

- **Maintenance critical spare without QC.** Receive-spare called on a `qc_required` critical spare whose `qc_status` isn't `ACCEPTED`/`WAIVED`. **Behaviour:** link set to `BLOCKED`, `400 "QC must be accepted or waived…"`; stock unchanged. **Symptom:** the spare shows blocked and won't receive into store. **Risk:** correct guard, but there's no in-app QC-accept action on the gate side — it depends on the `maintenance` module.

- **Abandoned wizard = orphan DRAFT header.** Operator creates the header at Step 1, then closes the tab. **Behaviour:** a `DRAFT` `VehicleEntry` (possibly with a security check but no material detail) persists. **Symptom:** it shows on the dashboard as an incomplete/`DRAFT` entry forever. **Risk:** these material apps expose **no delete endpoint** (unlike raw-material), so orphans accumulate and can only be cleaned via Django admin.

---

## Failure modes / what can break

- **SAP outage:** *no effect* on these flows — they never call SAP. (If a manager reports "gate is down for daily needs during a SAP outage", the cause is elsewhere.)
- **Missing bill attachment:** completion blocked with a clear 400 (see edge cases).
- **Duplicate `entry_no`:** header create fails with a constraint error surfaced as "Failed to save gate entry".
- **Wrong `entry_type` on the header:** detail create returns `400 "Invalid entry type. Expected …"` — usually means the header was created for the wrong material type.
- **Locked entry writes:** 400 from the view, or a 500 `ValueError` from the model backstop.
- **Person "already inside" wall:** blocks legitimate re-entry until a missed exit is corrected.
- **Notification failures:** swallowed and logged (`logger.error`) inside the signal handlers — they never break the enter/exit/create transaction.
- **Receive-spare double-post:** guarded by the `RECEIVED` status check; a retry after a successful receive returns `400 "already been received"`.

---

## Improvement opportunities & known gaps

- **Server-side `entry_no` generation** would remove the client collision window entirely.
- **No delete/cancel for material gate-ins** → orphan `DRAFT` headers accumulate; add a guarded delete like `RawMaterialGateEntryDeleteAPI`.
- **Construction approval has no maker/checker split** — the operator both fills and approves. A separate `can_approve_construction_security` permission + endpoint would make the `security_approval` field meaningful.
- **Person gate-in is un-scoped by company** — if per-company person registers are ever needed, `EntryLog`/masters need a company column and filtering; today it's a single global register.
- **No auto-exit / stale-inside sweeper** for persons who never scanned out.
- **`GateEntryBase.save()` raises `ValueError`** rather than a DRF `ValidationError`, so the lock backstop can surface as a 500.
- **Overlapping `WO-YYYY-NNN` namespace** between construction and maintenance is confusing; prefix them distinctly (e.g. `CN-`/`MN-`).
- **`person_gatein` views catch bare `Exception` → 400** with the raw string, which can leak internals and masks 500-class errors as 400s.

---

## Permissions & roles

All use Django model/custom permissions checked by small DRF `BasePermission` classes in each app's `permissions.py`. Codenames (backend truth; the frontend mirrors these in `gate.permissions.ts`):

| Area | View | Create | Complete | Notes |
|------|------|--------|----------|-------|
| Daily needs | `daily_needs_gatein.view_dailyneedgateentry` | `add_dailyneedgateentry` | `can_complete_daily_need_entry` | `view_categorylist` for the category dropdown |
| Construction | `construction_gatein.view_constructiongateentry` | `add_constructiongateentry` | `can_complete_construction_entry` | `change_…` for the update PUT |
| Fixed asset | `fixed_asset_gatein.view_fixedassetgateentry` | `add_fixedassetgateentry` | `can_complete_fixed_asset_entry` | |
| Maintenance | `maintenance_gatein.view_maintenancegateentry` | `add_maintenancegateentry` | `can_complete_maintenance_entry` | receive-spare needs `change_…` |
| FullView (gate_core) | `can_view_daily_need_full_entry`, `can_view_maintenance_full_entry`, `can_view_construction_full_entry` | — | — | read-only review payloads |
| Person entry | `person_gatein.view_entrylog` | `add_entrylog` | — | `can_exit_entry`, `can_cancel_entry`, `can_search_entry`, `can_view_dashboard` |
| Person masters | `view_visitor`/`view_labour`/`view_contractor`/`view_gate`/`view_persontype` | `add_*` | — | `can_manage_gate`, `can_manage_person_type` for lookups |

Every endpoint also requires `IsAuthenticated` + `HasCompanyContext` (valid `Company-Code` header). Note the frontend gate **dashboard** and several unrelated gate flows are gated on `person_gatein.can_view_dashboard` — granting/removing it affects more than the person module (see the "group perms vs frontend nav gating" memory note).

---

## Developer file map

**Backend (this repo — `C:/Users/gurpa/dev/factory_app`):**
- Shared header: `driver_management/models/vehicle_entry.py`, `vehicle_management/views.py` (`VehicleEntryListCreateAPI`), `vehicle_management/serializers.py`, `vehicle_management/urls.py`.
- Base + attachments + enums: `gate_core/models/gate_entry.py`, `gate_core/models/gate_attachments.py`, `gate_core/enums.py`, `gate_core/views.py` (search `…FullView`).
- Daily needs: `daily_needs_gatein/{models,serializers,views,urls,permissions,signals,notifications}.py`, `daily_needs_gatein/services/daily_need_completion.py`.
- Construction: `construction_gatein/{models,serializers,views,urls,permissions}.py`, `construction_gatein/services/construction_completion.py`.
- Fixed asset: `fixed_asset_gatein/{models,serializers,views,urls,permissions}.py`, `fixed_asset_gatein/services/fixed_asset_completion.py`.
- Maintenance: `maintenance_gatein/{models,serializers,views,urls,permissions}.py`, `maintenance_gatein/services/maintenance_completion.py` (+ `maintenance/` app for spares).
- Person: `person_gatein/{models,serializers,views,urls,permissions,signals,notifications}.py`, `person_gatein/services/entry_service.py`.
- Company context: `company/permissions.py` (`HasCompanyContext`).
- Routing root: `config/urls.py`.

**Frontend (`C:/Users/gurpa/dev/FactoryFlow`):** `src/modules/gate/` — see the paired doc below for the file map.

---

## Related docs
- **Paired frontend doc:** [`FactoryFlow/docs/modules/gate-in-other.md`](../../FactoryFlow/docs/modules/gate-in-other.md)
- Per-app legacy docs (partially stale): `construction_gatein/README.md`, `daily_needs_gatein/docs/`, `maintenance_gatein/docs/`, `person_gatein/docs/`.
- Adjacent flows: `gate_core/docs/README.md`, `docs/permissions_and_groups.md`.
