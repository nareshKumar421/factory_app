# Goods Return — Backend (gate_core)

> Customer returns (a.k.a. "Goods Return") **and** rejected-QC material return to
> vendor, as the Django REST server sees them.
>
> Paired frontend doc: `C:/Users/gurpa/dev/FactoryFlow/docs/modules/goods-return.md`
>
> Status: **built, adoption pending.** Read the code, not folklore. The two flows
> that share the "goods return" label have **very different backends** — one is a
> real persisted gate-out record, the other is a browser-only prototype with a
> single read-only SAP touchpoint. This doc spells out which is which.

---

## Overview — what it does & who uses it

Two distinct "return" flows live under the Gate module. They look similar in the
UI but are wired completely differently on the server:

| Flow | What it is | Backend persistence | SAP |
| --- | --- | --- | --- |
| **Rejected-QC Return** | A truck taking QA-rejected raw material back **out** to the vendor. | Real: `RejectedQCReturnEntry` + `RejectedQCReturnItem` in `gate_core`. | None — posting done manually in SAP; reference typed in. |
| **Customer / Goods Return** | Finished goods coming **back in** from a customer against a SAP sales invoice. | **None.** Entirely in the operator's browser `localStorage`. | One read-only lookup of the original sales invoice. |

- **Rejected-QC Return** is used by gate/QC staff after Quality Control rejects a
  raw-material lot and the material must physically leave the plant. It produces a
  weighed, auditable gate-out record with a gatepass.
- **Customer / Goods Return** is a workflow prototype for receiving returned
  finished goods. The server's only involvement is fetching the referenced SAP
  invoice; everything the operator captures (vehicle, returned lines, attachments,
  QC decision, SAP GR number) is stored client-side. There is **no `CustomerReturn`
  model, table, endpoint, or serializer** in this codebase.

---

## Key concepts & entities

### Rejected-QC Return (persisted)

`gate_core/models/rejected_qc_return.py`

- **`RejectedQCReturnEntry`** — one gate-out event: a vehicle + driver leaving with
  one or more rejected lots.
  - `company` (FK `company.Company`, `PROTECT`) — owning company; strictly the
    active Company-Code at create time.
  - `entry_no` — unique, server-generated `RQC-YYYYMMDD-NNNN`
    (`generate_entry_no()`).
  - `vehicle`, `driver` (FK `PROTECT`).
  - Gate-out fields: `gate_out_date`, `out_time`, `challan_no`, `eway_bill_no`,
    `manual_sap_reference`, `security_name`, `remarks`.
  - Weighbridge: `gross_weight`, `tare_weight`, `net_weight` (auto = gross − tare in
    `save()`, `editable=False`), `weighbridge_slip_no`, `first_weighment_time`,
    `second_weighment_time`.
  - `gatepass_documents` — `JSONField(default=list)`; a list of **file-name strings**,
    not uploaded files (see failure modes).
  - `status` — `RejectedQCReturnStatus` (`DRAFT` / `COMPLETED` / `CANCELLED`),
    **defaults to `COMPLETED`**. There is no server-side draft lifecycle; rows are
    created already complete.
- **`RejectedQCReturnItem`** — one rejected lot on the entry.
  - `entry` (FK, `CASCADE`).
  - `inspection` — **`OneToOneField`** to `quality_control.RawMaterialInspection`
    (`PROTECT`, `related_name="rejected_qc_return_item"`). This one-to-one is the
    hard guarantee that a rejected inspection can be returned **once**.
  - Snapshotted-at-create fields: `gate_entry_no`, `report_no`, `internal_lot_no`,
    `item_name`, `supplier_name`, `quantity`, `uom`.

**Upstream source of returnable items:** QA-rejected inspections. An inspection is
eligible when `RawMaterialInspection.final_status == InspectionStatus.REJECTED` and
it has not already been returned (`rejected_qc_return_item__isnull=True`).

### Customer / Goods Return (not persisted here)

There is no server entity. The only server contract is a **read** of the original
SAP A/R invoice by document number, served by the **`dispatch_plans`** app and
reused verbatim by the Goods-Return screen:

- Endpoint: `GET /dispatch-plans/bills/by-number/<invoice_number>/`
  (`dispatch_plans/urls.py` → `DispatchBillByNumberAPI`).
- It returns the **original sales invoice** (customer, ship-to, GST/e-way, line
  items, totals) read live from SAP HANA — **not** a SAP return/credit document.
- The frontend then keeps all return state in `localStorage`
  (key `gate.customer-return.completed-entries`).

---

## End-to-end flows (as the server sees them)

### Flow A — Rejected-QC Return (happy path)

1. **Item discovery (read).** Client calls
   `GET /quality-control/inspections/return-to-vendor/`
   (`quality_control/views.py::InspectionReturnToVendorAPI`, perm `CanViewInspection`).
   Returns arrival slips whose inspection is `final_status=REJECTED` and not yet
   returned, **scoped to `request.company.company`**.
2. **Create (write).** Client POSTs to
   `POST /gate-core/rejected-qc-returns/`
   (`gate_core/views.py::RejectedQCReturnListCreateView.post`) with vehicle, driver,
   gate-out details, weighbridge weights, `gatepass_documents`, and `inspection_ids`.
3. **Server validation** (all inside `RejectedQCReturnCreateSerializer` +
   the view; see invariants below): weights sane, at least one gatepass doc, at
   least one inspection, every inspection resolvable **within the active company**,
   every inspection genuinely `REJECTED` and not already returned.
4. **Persist atomically.** In a `transaction.atomic()` block: create the
   `RejectedQCReturnEntry` (`status=COMPLETED`, `entry_no` generated) then one
   `RejectedQCReturnItem` per inspection, snapshotting the gate-entry / report / lot
   / item / supplier / qty / uom from the
   `inspection → arrival_slip → po_item_receipt → po_receipt → vehicle_entry` chain.
5. **Respond** `201` with `RejectedQCReturnEntrySerializer` (includes nested
   `items`, `net_weight`, `driver_name`, `vehicle_number`).
6. **SAP is out of band.** No SAP call happens. The actual goods-return / GRPO
   reversal is booked by a user directly in SAP; its reference is stored in
   `manual_sap_reference` (free text, unverified).

**Read back:** `GET /gate-core/rejected-qc-returns/` (list, company-scoped, optional
`from_date`/`to_date`) and `GET /gate-core/rejected-qc-returns/<id>/` (detail).

### Flow B — Customer / Goods Return (server's slice)

1. **Invoice lookup (read).** Operator types a SAP invoice number; client calls
   `GET /dispatch-plans/bills/by-number/<invoice_number>/`. Server (perm
   `CanLookupDispatchBill`) reads the invoice from SAP HANA
   (`DispatchPlansService.get_bill_by_number` → `hana_reader`), attaches any local
   dispatch `plan`, and returns the full bill via `DispatchBillDetailSerializer`.
2. **Everything else is client-side.** The server never learns that a return was
   started, weighed, QC'd, or "posted". Item lines, return quantities, reasons,
   attachments, QC accept/reject, factory-head decision, and the eventual SAP GR
   document number are all written to browser `localStorage`. See the frontend doc.

> Consequence: from the backend's perspective, a "customer return" is
> indistinguishable from someone simply looking up an invoice.

---

## Critical business rules & invariants

### Rejected-QC Return

- **Company boundary (single company, write-side D2).** The list filters
  `company=request.company.company`; create resolves inspections through
  `arrival_slip__po_item_receipt__po_receipt__vehicle_entry__company =
  request.company.company`. A rejected lot booked under company B is **invisible and
  un-returnable** while acting as company A. There is no `all_companies` aggregation
  here (unlike empty-vehicle-in). This matches the cross-company write boundary: the
  owning company is resolved from the record, not from a selector.
- **Only QA-rejected, once.** Each `inspection_id` must have
  `final_status == InspectionStatus.REJECTED`; otherwise `400 "Only QA-rejected QC
  items can be returned to vendor"` with an `invalid_items` list. If the inspection
  already has a `rejected_qc_return_item`, it is reported as
  `"<report_no> already returned"`. The `OneToOneField` is the DB-level backstop
  against a race.
- **All-or-nothing per lot.** `quantity` is snapshotted from
  `arrival_slip.billing_qty` — the **whole** rejected lot. There is no partial-quantity
  return of a single inspection.
- **Missing / unknown inspections.** If `len(inspections) != len(inspection_ids)`
  (some id not found or out of company scope), the whole request fails
  `400 "One or more selected QC inspections were not found"`.
- **Weighbridge mandatory.** `RejectedQCReturnCreateSerializer.validate` requires
  `gross_weight > 0`, `tare_weight >= 0`, and `tare_weight <= gross_weight`.
  `net_weight` is derived server-side, never trusted from the client.
- **Gatepass required.** `gatepass_documents` is `ListField(min_length=1)` — but see
  the failure modes: these are file **names**, not files.
- **Deduped ids.** `inspection_ids` is de-duplicated (`dict.fromkeys`) before use.
- **Atomicity.** Entry + all items are created in one transaction; a mid-way failure
  rolls back cleanly.

### Customer / Goods Return

- **No server invariants exist**, because nothing is persisted server-side. All
  guards (return-qty ≤ invoice-qty, at least one line, vehicle/driver required,
  attachments) are **client-side only** and trivially bypassable / losable. The one
  server rule is the invoice lookup's own contract (valid invoice number, SAP
  reachable, caller has `CanLookupDispatchBill`).

---

## Integrations & cross-module boundaries

- **Quality Control (`quality_control`)** — the item source for rejected-QC returns.
  - `InspectionReturnToVendorAPI` (`inspections/return-to-vendor/`) lists eligible
    rejected inspections.
  - **Gap:** neither the QC list nor the gate-core create endpoint enforces the
    **factory-head decision** `RETURN_TO_VENDOR`. Any `final_status=REJECTED`,
    not-yet-returned inspection is returnable, even if a factory head chose
    "Accept override" / "Scrap" / "Hold". The `RETURN_TO_VENDOR` decision
    (`quality_control` migration `0022_factory_head_decision`) is surfaced in the UI
    but is **advisory** on the return path.
  - The `RejectedQCReturnItem.inspection` `OneToOne` uses `PROTECT`, so a returned
    inspection cannot be hard-deleted from QC while the return exists.
- **Dispatch Plans (`dispatch_plans`)** — provides the SAP invoice read for customer
  returns. `DispatchBillByNumberAPI`, `DispatchPlansService.get_bill_by_number`,
  perm `CanLookupDispatchBill` (satisfied by `dispatch_plans.can_view_dispatch_plans`
  **or** `person_gatein.can_view_dashboard`). It reads SAP HANA directly
  (`dispatch_plans/hana_reader.py`).
- **SAP** —
  - Rejected-QC return: **no SAP posting.** The vendor return / GRPO reversal is a
    manual SAP action; reference captured as free text (`manual_sap_reference`). This
    makes the flow resilient to SAP outages but leaves SAP↔gate reconciliation manual.
  - Customer return: **read-only** SAP invoice lookup; no return/credit document is
    ever posted from this codebase. The customer-return "SAP GR done" step is a
    client-side text field.
- **Finance / Vehicle-management** — downstream of the *customer* flow only, and
  again via shared `localStorage` (credit note), not the server. See frontend doc.

---

## Real-world edge cases

Each: **trigger → current behaviour → operator-visible symptom → risk/gap.**

1. **Two clerks return the same rejected lot at once.**
   → First POST wins; the second fails the `already returned` guard, or (on a true
   race) the `OneToOne` raises `IntegrityError`.
   → Second clerk sees the entry fail with `"… already returned"` (or a 500 on the
   rare integrity race).
   → Risk: low — the DB constraint holds the line. The 500 path is ugly but safe.

2. **Acting as the wrong company.**
   → The return-to-vendor list and the create scope both filter to the active
   company. A lot rejected under a sibling company simply isn't in the list, and a
   hand-crafted POST with its `inspection_id` fails `"… not found"`.
   → Operator sees "No more Return to Vendor items", or a not-found error.
   → Risk: the classic "blank in sibling company" confusion. Correct behaviour, but
   the operator must switch Company-Code first.

3. **Missing weighbridge weight.**
   → Serializer rejects `gross_weight <= 0` / `tare_weight` issues.
   → `400` with `{"gross_weight": "Gross weight is required."}` etc.; the UI surfaces
   the message.
   → Risk: none server-side; the weighing is enforced.

4. **SAP down during a customer return.**
   → `DispatchBillByNumberAPI` catches `SAPConnectionError` → `503`, `SAPDataError`
   → `502`, and returns `404` when the invoice isn't found.
   → Operator sees "SAP system is currently unavailable…" / "SAP invoice … was not
   found." and cannot proceed past the invoice-search step.
   → Risk: the customer flow is hard-blocked without SAP; the rejected-QC flow is
   **not** (it never calls SAP).

5. **Customer return double-booked against one invoice.**
   → Nothing on the server prevents two returns against the same invoice, or a total
   returned quantity exceeding what was sold — there is no server record to check
   against.
   → Operator sees success both times.
   → Risk: **high** over-return / duplicate-credit exposure; only guarded by one
   clerk's browser session.

6. **Factory head said "Scrap"/"Accept override", gate still returns it.**
   → The rejected lot is `final_status=REJECTED` and not yet returned, so it appears
   in the return-to-vendor list and posts fine.
   → No symptom — it just succeeds.
   → Risk: gate can send to vendor material the factory head did not approve for
   vendor return (decision not enforced).

7. **Retry after a failed rejected-QC POST.**
   → On failure the frontend keeps its `localStorage` draft (not cleared until a
   `201`); the server created nothing (atomic rollback).
   → Operator can safely re-submit.
   → Risk: none; retry is clean.

8. **Vehicle arrival lifecycle (customer return).**
   → A returning customer vehicle creates **no** `VehicleEntry` / `VehicleArrival` /
   empty-vehicle-in record — the customer flow never touches those tables.
   → The truck is invisible on any "inside vehicles" board.
   → Risk: no gate/inside-truck tracking for returned-goods vehicles; reconciliation
   and security have no server trail.

---

## Failure modes / what can break

| Failure | Where | Symptom a human notices |
| --- | --- | --- |
| SAP unreachable / bad data (customer flow) | `DispatchBillByNumberAPI` | `503`/`502`; "SAP system is currently unavailable"; invoice step blocked |
| Invoice number wrong | same | `404` "SAP invoice … was not found" |
| Weight missing/invalid (rejected-QC) | `RejectedQCReturnCreateSerializer.validate` | `400` field error on gross/tare |
| No gatepass doc / no item (rejected-QC) | serializer `min_length=1` | `400` list/gatepass error |
| Inspection not `REJECTED` / already returned | `RejectedQCReturnListCreateView.post` | `400` "Only QA-rejected QC items…" + `invalid_items` |
| Inspection id out of company scope | same (`len` mismatch) | `400` "One or more … not found" |
| Concurrent duplicate return | `OneToOne` on `inspection` | second POST 400/500 |
| Customer-return data loss | **browser only** | entries vanish on clear-storage / other device / other user; **no server error, no recovery** |
| Attachments never stored | both flows | `gatepass_documents` holds only file **names**; the documents themselves are nowhere |

---

## Improvement opportunities & known gaps

- **Customer return has no backend at all.** No model, no persistence, no audit, no
  cross-user/cross-device visibility, no company scoping of the local data, no
  server enforcement of over-return. Productionising it means a real
  `CustomerReturn` model + endpoints, real file upload, and a SAP return/credit
  posting — none of which exist yet.
- **Attachments are file-names, not files** (both flows). `gatepass_documents` and
  the customer-flow attachment fields store `file.name` strings. There is **no
  upload** to the server or object store. A compliance/gatepass document referenced
  here cannot actually be retrieved.
- **Factory-head `RETURN_TO_VENDOR` decision is not enforced** on the rejected-QC
  return path (see edge case 6).
- **No dedicated Django permission** guards the rejected-QC endpoints — only
  `IsAuthenticated, HasCompanyContext`. Any authenticated user with a company context
  can create/read returns; the "gate" gating is purely frontend
  (`raw_material_gatein.view_poreceipt` / `add_poreceipt`). Backend and frontend
  permission models are out of step.
- **List endpoint ignores its own date filters' potential.** The rejected-QC list
  accepts `from_date`/`to_date`, but the frontend dashboard calls it with none and
  filters client-side.
- **`manual_sap_reference` is unvalidated free text** — no reconciliation against an
  actual SAP document.
- **Rejected-QC status enum is aspirational.** `DRAFT`/`CANCELLED` exist but the code
  only ever writes `COMPLETED`; there is no cancel/void endpoint.

---

## Permissions & roles

| Action | Endpoint | Backend requirement | Frontend gate (see `gate.permissions.ts`) |
| --- | --- | --- | --- |
| List/view rejected-QC returns | `GET /gate-core/rejected-qc-returns/[<id>/]` | `IsAuthenticated` + `HasCompanyContext` (no object perm) | `REJECTED_QC_RETURN.VIEW` = `raw_material_gatein.view_poreceipt` |
| Create rejected-QC return | `POST /gate-core/rejected-qc-returns/` | `IsAuthenticated` + `HasCompanyContext` | `REJECTED_QC_RETURN.CREATE` = `raw_material_gatein.add_poreceipt` |
| List return-to-vendor items | `GET /quality-control/inspections/return-to-vendor/` | `IsAuthenticated` + `HasCompanyContext` + `CanViewInspection` | (same rejected-QC gates) |
| Look up SAP invoice (customer return) | `GET /dispatch-plans/bills/by-number/<no>/` | `IsAuthenticated` + `HasCompanyContext` + `CanLookupDispatchBill` (`dispatch_plans.can_view_dispatch_plans` **or** `person_gatein.can_view_dashboard`) | `CUSTOMER_RETURN.VIEW/CREATE` = `person_gatein.can_view_dashboard` |

Note: because `CanLookupDispatchBill` is satisfied by `person_gatein.can_view_dashboard`
— the same permission that shows the Goods-Return screen — a gate user who can open
the customer-return page can also search invoices; there is **no** hidden 403 trap.

---

## Developer file map

**Backend (`C:/Users/gurpa/dev/factory_app`)**

- `gate_core/models/rejected_qc_return.py` — `RejectedQCReturnEntry`,
  `RejectedQCReturnItem`, `RejectedQCReturnStatus`, `generate_entry_no()`.
- `gate_core/views.py` — `RejectedQCReturnListCreateView` (list/create, ~L3646),
  `RejectedQCReturnDetailView` (detail, ~L3763).
- `gate_core/serializers.py` — `RejectedQCReturnItemSerializer` (~L771),
  `RejectedQCReturnEntrySerializer` (~L784), `RejectedQCReturnCreateSerializer`
  (~L808).
- `gate_core/urls.py` — `rejected-qc-returns/` routes (~L104-106).
- `gate_core/permissions.py` — no dedicated return permission (context only).
- `quality_control/views.py` — `InspectionReturnToVendorAPI` (~L1551).
- `quality_control/urls.py` — `inspections/return-to-vendor/` (~L184).
- `dispatch_plans/views.py` — `DispatchBillByNumberAPI` (~L467).
- `dispatch_plans/services.py` — `DispatchPlansService.get_bill_by_number` (~L339).
- `dispatch_plans/permissions.py` — `CanLookupDispatchBill` (~L44).
- `dispatch_plans/urls.py` — `bills/by-number/<str:invoice_number>/`.

**Frontend (`C:/Users/gurpa/dev/FactoryFlow`)** — see the paired doc for detail.

- Rejected-QC: `src/modules/gate/pages/rejectedMaterialPages/*`,
  `src/modules/gate/api/rejectedQcReturn/*`.
- Customer return: `src/modules/gate/pages/customerSalesFlow/CustomerReturn*.tsx`,
  `customerSalesFlow.storage.ts`, `src/modules/gate/api/customerReturnInvoice/*`.

---

## Related docs

- **Paired frontend doc:** `C:/Users/gurpa/dev/FactoryFlow/docs/modules/goods-return.md`
- `C:/Users/gurpa/dev/factory_app/gate_core/docs/sales_dispatch.md` — the outbound
  (dispatch-out) counterpart; shares the `customerSalesFlow` frontend folder and the
  same `DispatchBillByNumber` SAP lookup.
- `C:/Users/gurpa/dev/factory_app/gate_core/docs/README.md` — raw-material gate entry
  + QC workflow that produces the rejected inspections consumed here.
- Memory: *Cross-company flow boundary* (reads need `all_companies`, writes resolve
  company from the record — why rejected-QC returns are single-company).
