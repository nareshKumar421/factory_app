# Dispatch Flow — Field Redundancy Audit (Plans → Sales Dispatch Out)

## Context

Across the dispatch flow the **same upstream values** (SAP bill data + Vehicle/Driver/Transporter
masters) are autofilled and then **saved again** on each downstream record. Much of this is
read-only display data that could be **read through to its source** instead of stored.

Concrete example (the Empty-Vehicle-In gate-in screen): `Document Reference =
"Dispatch 626060408"` and `Document Notes = "Customers: VDH ORGANIC P LTD\nWeight: 274.649 kg"`
are **built on the frontend from the bill and saved onto `EmptyVehicleGateIn`** — even though the
doc number, customer, and weight already live on the `DispatchPlan` / SAP bill / cover. The same
customer string ends up stored in **four** places (plan, gate-in notes, gate-out, gate-out
document).

This doc lists every such field from `DispatchPlan` → `EmptyVehicleGateIn` →
`SalesDispatchGateOut`/`Document`/`Item`, with its source and a verdict.

## Three tiers (how to read the verdicts)

| Tier | Meaning | Verdict |
|---|---|---|
| 🔴 **Redundant** | Source is a **local FK** on the same record (or trivially derivable from one). Stored copy duplicates live local data. | **Drop the column; read through the FK** (or compute in the serializer). |
| 🟡 **Local SAP cache** | SAP-only value with **no other local source**; the row *is* the local cache that downstream reads instead of re-querying slow SAP. | **Keep** — it's the local source of truth. |
| 🟢 **Audit snapshot** | Point-in-time capture for a printed/legal artifact (gatepass) that must reflect dispatch-time state even if masters/SAP change later. | **Keep** — immutability is the point. (Partial read-through from the linked plan is possible for overlapping fields if you don't need immutability.) |

---

## 1. `DispatchPlan` (Plans / Vehicle Linking)

Populated in `dispatch_plans/services.py`: `_apply_master_data` (masters) and
`_invoice_defaults_from_bill` (SAP bill).

### 🔴 Transport snapshot — read through the plan's own FKs
The plan already has `plan.vehicle`, `plan.transporter`, `plan.driver`. These 10 columns just
copy those masters (`_apply_master_data`, services.py ~665-785):

| Column | Source | Read-through |
|---|---|---|
| `vehicle_no` | `plan.vehicle.vehicle_number` | ✅ |
| `transporter_name` | `plan.transporter.name` | ✅ |
| `transporter_gstin` | `plan.transporter.gstin` | ✅ |
| `contact_person` | `plan.transporter.contact_person` | ✅ |
| `mobile_no` | `plan.transporter.mobile_no` | ✅ |
| `driver_name` | `plan.driver.name` | ✅ |
| `driver_mobile_no` | `plan.driver.mobile_no` | ✅ |
| `driver_license_no` | `plan.driver.license_no` | ✅ |
| `driver_id_proof_type` | `plan.driver.id_proof_type` | ✅ |
| `driver_id_proof_number` | `plan.driver.id_proof_number` | ✅ |

> Verdict: **Redundant.** Serialize them from the FKs (`select_related("vehicle","transporter","driver")`) and drop the columns. The plan is in-progress data, not a printed artifact, so no immutability need.

### 🟡 Invoice / SAP cache — keep (this is the local source of truth)
SAP-only, no other local source; everything downstream reads these from the plan, not SAP
(`_invoice_defaults_from_bill`, services.py ~355-368):

`invoice_number`, `invoice_weight`, `invoice_amount`, `customer_code`, `customer_name`,
`place_of_supply`, `product_variety`, `total_litres`, `eway_bill`, `effective_month`,
`budget_delivery_point`.

> Verdict: **Keep.** Dropping these would force a live SAP query on every read.

### ⚪ Primary data (not snapshots) — ignore
`bilty_no`, `bilty_date`, `freight`, `total_freight`, `kanta_weight`, `sac_*`,
`service_location_*` are operator-entered.

---

## 2. `EmptyVehicleGateIn` (Gate → Empty Vehicle In)

### 🔴 `document_reference` + `document_notes` (reason = DISPATCH) — pure round-trip
Built **on the frontend** from the bill and posted back to be saved
(`emptyVehicleInDispatch.ts` `buildDispatchDocumentReference` / `buildDispatchDocumentNotes`,
then `EmptyVehicleInNewPage.tsx` → create payload → `gate_core/views.py:441-442`):

| Field | Stored string | Built from | Now lives on |
|---|---|---|---|
| `document_reference` | `"Dispatch {doc_nums}"` | bill `doc_num` | plan `sap_invoice_doc_num` + the gate-in's **covers** (`sap_doc_entry`) |
| `document_notes` | `"Customers: …\nBilty: …\nBoxes: …\nWeight: … kg"` | bill `card_name`, `sap_bilty_no`, `total_boxes`, `total_weight` | plan `customer_name`, `bilty_no`, `invoice_weight` (+ SAP for boxes) |

> Verdict: **Redundant** for DISPATCH. Nothing downstream reads them — they're cosmetic. With the
> `EmptyVehicleGateInCover` rows now linking a gate-in to its exact bills, both strings can be
> **computed on read** from the covered plans. Drop the stored values for DISPATCH (keep the
> frontend builder for display only).

### 🟡 `sap_*` fields (reason = BST only) — keep
`sap_doc_entry/num/date`, `sap_from/to_warehouse`, `sap_reference`, `sap_comments`,
`sap_line_count`, `sap_total_quantity` are populated **only for BST**
(`apply_sap_transfer_to_empty_gate_in`) — a genuine SAP snapshot of the transfer that entered.

> Verdict: **Keep** (BST). Not populated for DISPATCH, so no DISPATCH redundancy here.

---

## 3. `SalesDispatchGateOut` / `…Document` / `…Item` (Sales Dispatch Out / Docking)

Populated at docking creation in `views_sales_dispatch.py`
`SalesDispatchGateOutListCreateView.post` via `_header_snapshot`, `_document_snapshot`,
`_transport_snapshot`, `_create_items`.

### 🟢 Transport snapshot on the gate-out (10 cols) — audit snapshot, but read-through-able
`_transport_snapshot` (views_sales_dispatch.py ~1198-1211) copies the same masters the gate-out
already FKs (`gate_out.vehicle`, `.driver`, `.transporter`):
`vehicle_no`, `transporter_name`, `transporter_gstin`, `transporter_contact_person`,
`transporter_mobile_no`, `driver_name`, `driver_mobile_no`, `driver_license_no`,
`driver_id_proof_type`, `driver_id_proof_number`.

> Verdict: **Keep if you need gatepass immutability** (the printed gatepass should show who was on
> the truck at dispatch even if a master is edited later). **Otherwise 🔴 read-through the FKs.**
> Decision point for you — see Recommendations.

### 🟢 SAP snapshot on the gate-out + document (header/document) — audit snapshot
`_header_snapshot` / `_document_snapshot` (views_sales_dispatch.py ~1143-1196) capture the SAP
document at docking time: `sap_doc_*`, `sap_branch_*`, `sap_reference`, `sap_comments`,
`customer_code/name`, `ship_to_*`, `place_of_supply`, `bp_gstin`, `eway_bill`,
`from/to_warehouse`, `warehouses`, `item_summary`, `base_refs`, `total_quantity/litres/boxes/weight`.
`SalesDispatchGateOutItem` mirrors the SAP lines (`item_code/name`, `quantity`, `uom`, `rate`,
`line_total`, `gross_total`, warehouses, `base_*`, `tax_code`, totals).

> Verdict: **Keep.** SAP-only + must be frozen for the gatepass. **But** the subset that *also*
> exists on the linked plan (`customer_name`, `customer_code`, `place_of_supply`, `eway_bill`,
> `total_weight`, `total_litres`, invoice amount) is 🔴 **double-stored** — if you don't need the
> gate-out frozen independently of the plan, read those from `gate_out.dispatch_plan`.

---

## Summary — what's safe to stop saving

| # | Where | Fields | Verdict | Effort |
|---|---|---|---|---|
| 1 | `EmptyVehicleGateIn` (DISPATCH) | `document_reference`, `document_notes` | 🔴 Drop; compute from covers on read | Low |
| 2 | `DispatchPlan` | 10 transport snapshot cols | 🔴 Drop; serialize from `vehicle`/`transporter`/`driver` FKs | Medium (touch serializers + any readers of the columns) |
| 3 | `SalesDispatchGateOut` | 10 transport snapshot cols | 🟢/🔴 Keep **or** read-through — your call on gatepass immutability | Medium |
| 4 | `SalesDispatchGateOut`/`Document` | plan-overlapping SAP cols (`customer_*`, `place_of_supply`, `eway_bill`, `total_weight/litres`) | 🟢/🔴 Keep **or** read from `dispatch_plan` — your call | Medium |
| — | `DispatchPlan` invoice/SAP cache | `invoice_*`, `customer_*`, `total_litres`, … | 🟡 Keep (local SAP cache) | — |
| — | `EmptyVehicleGateIn` BST `sap_*` | — | 🟡 Keep (BST snapshot) | — |
| — | All SAP-detail on gate-out/items | `ship_to_*`, `branch_*`, `warehouses`, `item_*`, `base_*` | 🟢 Keep (audit + SAP-only) | — |

## Recommendation

- **Do now (clear wins, no downside):** #1 and #2 — they're in-progress/cosmetic data with a local
  source, no audit role. Drop the columns and read through FKs / covers.
- **Decide deliberately:** #3 and #4 hinge on **one question — must a printed gatepass stay frozen
  if a master record or SAP doc is edited after dispatch?**
  - If **yes** (legal/audit) → keep the gate-out snapshots as-is.
  - If **no** → read transport from the FKs and the plan-overlapping fields from `dispatch_plan`,
    keeping only the genuinely SAP-only detail (`ship_to_*`, `branch_*`, items) as the frozen set.

## How to migrate a 🔴 field safely (pattern)

1. Add the value to the relevant serializer as a `SerializerMethodField` / FK-sourced field
   (read-through), keeping the stored column temporarily.
2. Point all readers (frontend, exports, gatepass PDF) at the serializer field.
3. Stop writing the column (remove from the snapshot helper / create payload).
4. Drop the column in a later migration once nothing reads it.

> The cover model (`EmptyVehicleGateInCover`) and the existing FKs already give every 🔴 field a
> live local source, so none of these requires a SAP call to read.
