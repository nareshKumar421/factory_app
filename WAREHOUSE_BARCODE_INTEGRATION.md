# Warehouse ↔ Barcode Integration + SAP Warehouse Support

This document explains the two modules involved, how they connect today, and the
plan to (1) add SAP warehouse codes to warehouse creation, (2) support two
warehouse types (own vs SAP) that behave differently during transfer, and
(3) route **every stock transfer through the barcode module**.

---

## 1. The modules

### 1a. Warehouse Ops ("WMS") — the *own warehouse* designer
Frontend: `FactoryFlow/src/modules/wms/`  · Backend: `factory_app/wms/`

- Lets a user **design an internal warehouse**: a grid of **locations/bins**
  (columns × rows × levels), grouped into zones, each location carrying
  capacity / material rules / status.
- Frontend-driven; every record (warehouse, zone, location, pallet, inventory,
  movement, template, settings) is a camelCase JSON document persisted through a
  single storage adapter.
- Persistence is now **backend-only** (`storage/apiAdapter.ts` → Django `wms`
  app at `/api/v1/wms/<collection>/`), company-scoped.
- Key types (`src/modules/wms/types/wms.types.ts`):
  - `Warehouse { id, code, name, description, enabled, columns, rows, levels, namingScheme, … }`
  - `WarehouseLocation { id, warehouseId, zoneId, code, barcode, column, row, level, type, capacity, … }`
- Create flow: `pages/WmsDesignerPage.tsx` collects name/code/grid/naming, then
  `services/factories.ts:makeWarehouse` + `generateLayout` create the warehouse
  and all its locations in one action.
- **Today there is no `sapWarehouseCode` and no warehouse `type` (own vs SAP).**

### 1b. Barcode — the *physical stock* system of record
Frontend: `FactoryFlow/src/modules/barcode/`  · Backend: `factory_app/barcode/`

- Tracks real stock as **Boxes** and **Pallets**, each with:
  - `current_warehouse` (CharField, a flat warehouse **code** e.g. `FG01`)
  - `current_bin` (CharField, an app-managed bin string — **currently unused/blank**)
- Transfer/move flows (all internal, **no SAP document posting today**):
  - **Pallet Move** — `pages/PalletMovePage.tsx` → `POST /barcode/pallets/{id}/move/`
    `{ to_warehouse, notes }` → sets `pallet.current_warehouse` (+ its boxes),
    writes a `PalletMovement` (`movement_type='MOVE'`, `from/to_warehouse`,
    `from/to_bin`, `sap_transfer_doc_entry`).
  - **Godown Transfer** — `pages/PalletTransferPage.tsx` → bulk Pallet Move.
  - **Box Transfer** — `pages/BoxTransferPage.tsx` → `POST /barcode/transfers/box/`
    `{ box_ids, to_warehouse, to_pallet_id? }` → sets each `box.current_warehouse`,
    writes a `BoxMovement` (with `from/to_bin` fields already present).
  - **Intercompany Transfer** — company→company, optional SAP posting.
- The destination warehouse dropdown already exists and is populated from
  `useWMSWarehouses()` (`@/modules/warehouse/api`, the SAP-HANA `OWHS` list via
  `/api/v1/warehouse/wms/warehouses/`). Destination is a **flat code, no bin**.
- Movement models **already have `from_bin` / `to_bin` columns**
  (`barcode/models.py` `PalletMovement`, `BoxMovement`) — they are simply not set
  by the transfer flows yet. This is the natural hook for location/bin support.

### 1c. SAP warehouse codes (the dropdown source)
- Endpoint: `GET /api/v1/po/warehouses/` → `sap_client.ActiveWarehouseListAPI`
  → reads SAP HANA `OWHS` where `Inactive='N'`.
- Response shape: `[{ "warehouse_code": "WH01", "warehouse_name": "Main" }, …]`.
- Already consumed by GRPO via `useWarehouses()` and the reusable
  `grpo/components/WarehouseSelect.tsx` (searchable, value = `warehouse_code`).

---

## 2. How they connect today

- **Separate inventory domains.** Barcode owns physical Box/Pallet stock; Warehouse
  Ops owns a *designed layout* (locations) and its own inventory records. They are
  not linked.
- The barcode transfer pages move stock **to a warehouse code** (SAP `OWHS` code)
  with **no concept of an internal location/bin**.
- Warehouse Ops can design an internal warehouse with bins, but those bins are
  **not used by the barcode transfer** and there is no link from a designed
  warehouse to an SAP `OWHS` code.

**The gap:** when stock is transferred into an internally-managed ("own") warehouse,
the operator should be told *which bin* — that bin data exists in Warehouse Ops but
the barcode transfer never asks for it.

---

## 3. Integration plan

### 3.1 Data model — link a warehouse to SAP + give it a type
Add two fields to the Warehouse Ops `Warehouse` record (JSON doc; no DB migration
needed since the backend stores documents):

```ts
type WarehouseType = 'OWN' | 'SAP';
interface Warehouse {
  …existing…
  type: WarehouseType;            // OWN = has internal locations; SAP = external, no locations
  sapWarehouseCode: string | null; // the SAP OWHS code this warehouse maps to
}
```

- **OWN**: designed grid of locations (existing flow) **+** an SAP code it maps to.
- **SAP**: no grid/locations — just the external SAP warehouse (code + name).

### 3.2 Create-warehouse flow (Warehouse Ops)
`pages/WmsDesignerPage.tsx`:
- Add a **warehouse type** selector (Own / SAP).
- Add an **SAP warehouse code dropdown** populated from `/api/v1/po/warehouses/`
  (reuse the GRPO `WarehouseSelect` / `useWarehouses` query).
- **Own** → keep the grid designer; save `type='OWN'`, `sapWarehouseCode=<picked>`,
  and generate locations as today.
- **SAP** → hide the grid; save a `Warehouse` with `type='SAP'`,
  `sapWarehouseCode=<picked>`, **no locations**.

### 3.3 Route every stock transfer through the barcode module
The barcode module remains the single place a transfer happens. Make its
destination selection **location-aware**:

- Build the destination list from the SAP warehouses **plus** Warehouse-Ops "own"
  warehouses (read `useWmsCollection('warehouses')`).
- When the chosen destination is an **OWN** warehouse → show a **location/bin
  picker** (its Warehouse-Ops locations) and **require** a selection before the
  transfer can complete.
- When the chosen destination is a **SAP** warehouse → **no location prompt**;
  transfer directly to the warehouse code.
- Send the chosen location code as `to_bin` to the transfer API.

Pages affected: `PalletMovePage.tsx`, `PalletTransferPage.tsx`, `BoxTransferPage.tsx`.

### 3.4 Barcode backend — persist the bin
- `PalletMoveSerializer` / `BoxTransferSerializer`: accept optional `to_bin`.
- `barcode_service.move_pallet` / `transfer_boxes`: set `current_bin` on the
  moved box/pallet and `to_bin` on the `PalletMovement` / `BoxMovement` record.
- (The columns already exist; this only wires them through.)

### 3.5 Confirmed decisions
1. **No SAP document posting on transfer** — transfers update internal barcode
   stock + movement audit (matching today's behaviour). "SAP support" here means
   *selecting/targeting SAP warehouse codes*, not creating SAP stock-transfer
   documents. (Can be added later via the existing `sap_transfer_doc_entry` field.)
2. **Both warehouse types carry an SAP code** (own = SAP code + internal bins;
   SAP = SAP code only).
3. **Barcode is the transfer entry point**: the location-aware destination logic
   is added to the three barcode transfer pages (Pallet Move, Godown Transfer,
   Box Transfer). The Warehouse Ops Transfer page is kept for moving stock between
   bins *inside* a single own warehouse.
4. The barcode module stays the system of record for physical stock; Warehouse Ops
   provides the location structure for "own" warehouses.

---

## 4. Implementation status & test results

Implemented:
- WMS: `Warehouse.type` (`OWN`/`SAP`) + `sapWarehouseCode`; create flow has a type
  selector + SAP-code dropdown; SAP type skips the grid/locations; warehouses list
  renders SAP warehouses sensibly.
- Barcode: `useDestinationBins` hook bridges to Warehouse Ops; Pallet Move, Godown
  Transfer prompt for a location when the destination is an own warehouse (required)
  and go direct for SAP warehouses; Box Transfer carries the target pallet's bin.
- Barcode backend: `to_bin` accepted on pallet move + box transfer, persisted on the
  box/pallet `current_bin` and the `PalletMovement` / `BoxMovement` records.

Test results:
- Frontend: **88/88** wms + barcode tests pass; new code type-clean.
- Backend: new `to_bin` tests pass (pallet move bin, SAP move clears bin, box
  transfer bin). The only failing backend tests are 3 pre-existing
  `BarcodeDispatchWorkflowTests` SAP-sync tests — confirmed failing on the clean
  baseline too (environment-dependent, unrelated to this change).

## 4b. Test plan
- **Backend (barcode):** pallet move / box transfer accept and persist `to_bin`;
  movement records carry `to_bin`.
- **Backend (wms):** warehouse documents round-trip the new `type` /
  `sapWarehouseCode` fields (already covered by the generic JSON CRUD).
- **Frontend (wms):** create-warehouse builds `type` + `sapWarehouseCode`; SAP type
  skips location generation.
- **Frontend (barcode):** destination = own warehouse requires a location before
  transfer; destination = SAP warehouse transfers with no location.
- Run full `wms` + `barcode` test suites (frontend + backend) and typecheck.
