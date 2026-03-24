# FACTORY DOCKING SYSTEM

## Outbound Module — Requirements & Implementation Document

**Version 1.0 | March 2026 | Logistics & Operations**

---

## Table of Contents

1. [Introduction & Scope](#1-introduction--scope)
2. [Current System State (Inbound - Already Built)](#2-current-system-state-inbound---already-built)
3. [Outbound Module - Requirements](#3-outbound-module---requirements)
   - 3.1 [Data Models](#31-data-models)
   - 3.2 [Process Flow & Steps](#32-process-flow--steps)
   - 3.3 [API Endpoints](#33-api-endpoints)
   - 3.4 [Business Rules & Validations](#34-business-rules--validations)
   - 3.5 [SAP Integration](#35-sap-integration)
4. [Roles & Permissions](#4-roles--permissions)
5. [Database Schema Summary](#5-database-schema-summary)
6. [KPIs & Monitoring](#6-kpis--monitoring)
7. [Implementation Priority & Phases](#7-implementation-priority--phases)

---

## 1. Introduction & Scope

This document defines requirements for the **Outbound** module of the Factory Docking System. The Inbound module (raw_material_gatein, quality_control, grpo apps) is already built and live. This document covers only the Outbound (Dispatch) module.

| Module | Status | Description |
|--------|--------|-------------|
| Inbound (Gate-In) | **DONE** | Vehicle entry, PO receipt, arrival slip, QC inspection, GRPO posting |
| Outbound (Dispatch) | **TO BUILD** | Pick, pack, stage, load, dispatch finished goods to customers |

---

## 2. Current System State (Inbound - Already Built)

| Django App | Purpose | Key Models |
|------------|---------|------------|
| `driver_management` | Vehicle entry/exit at gate | VehicleEntry |
| `raw_material_gatein` | PO receipt and item tracking | POReceipt, POItemReceipt |
| `quality_control` | Arrival slip + QC inspection | MaterialArrivalSlip, RawMaterialInspection |
| `grpo` | Post goods receipt to SAP | GRPOPosting, GRPOLinePosting |
| `sap_client` | SAP B1 Service Layer integration | SAPClient (service) |

**Architecture patterns to follow:** DRF-based APIs, service layer for business logic, SAP integration via sap_client, company-scoped models with CompanyAwareManager, role-based permissions via Django groups.

---

## 3. Outbound Module - Requirements

Handles dispatching finished goods to customers. Starts when shipment orders are released from SAP and ends when the loaded truck departs. **New Django app: `outbound_dispatch`.**

### 3.1 Data Models

#### ShipmentOrder

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| company | FK -> Company | Yes | Company scope |
| sap_doc_entry | IntegerField | Yes | SAP Sales Order DocEntry |
| sap_doc_num | IntegerField | Yes | SAP Sales Order DocNum |
| customer_code | CharField(50) | Yes | SAP Business Partner code |
| customer_name | CharField(200) | Yes | Customer display name |
| ship_to_address | TextField | No | Delivery address |
| carrier_code | CharField(50) | No | Assigned carrier code |
| carrier_name | CharField(200) | No | Carrier name |
| scheduled_date | DateField | Yes | Planned dispatch date |
| dock_bay | CharField(10) | No | Assigned bay (Zone C: 19-30) |
| dock_slot_start | DateTimeField | No | Booked slot start |
| dock_slot_end | DateTimeField | No | Booked slot end |
| status | CharField(20) | Yes | RELEASED / PICKING / PACKED / STAGED / LOADING / DISPATCHED / CANCELLED |
| vehicle_entry | FK -> VehicleEntry | No | Link to carrier vehicle |
| bill_of_lading_no | CharField(50) | No | BOL number |
| seal_number | CharField(50) | No | Trailer seal number |
| total_weight | DecimalField | No | Total load weight (kg) |
| notes | TextField | No | Notes |
| created_at / updated_at | DateTimeField | Auto | Timestamps |
| created_by | FK -> User | No | Creator |

#### ShipmentOrderItem

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| shipment_order | FK -> ShipmentOrder | Yes | Parent order |
| sap_line_num | IntegerField | Yes | SAP SO line number |
| item_code | CharField(50) | Yes | SAP Item code |
| item_name | CharField(200) | Yes | Item description |
| ordered_qty | DecimalField | Yes | Quantity ordered |
| picked_qty | DecimalField | No | Quantity picked (default 0) |
| packed_qty | DecimalField | No | Quantity packed (default 0) |
| loaded_qty | DecimalField | No | Quantity loaded (default 0) |
| uom | CharField(20) | Yes | Unit of measure |
| warehouse_code | CharField(20) | Yes | Source warehouse |
| batch_number | CharField(50) | No | Batch number |
| weight | DecimalField | No | Item weight (kg) |
| pick_status | CharField(20) | Yes | PENDING / PICKED / SHORT |

#### PickTask

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| shipment_item | FK -> ShipmentOrderItem | Yes | Item being picked |
| assigned_to | FK -> User | No | Assigned operative |
| pick_location | CharField(50) | Yes | Warehouse bin/slot |
| pick_qty | DecimalField | Yes | Quantity to pick |
| actual_qty | DecimalField | No | Quantity actually picked |
| status | CharField(20) | Yes | PENDING / IN_PROGRESS / COMPLETED / SHORT |
| picked_at | DateTimeField | No | Pick timestamp |
| scanned_barcode | CharField(100) | No | Barcode scanned |

#### OutboundLoadRecord

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| shipment_order | OneToOne -> ShipmentOrder | Yes | Shipment being loaded |
| trailer_condition | CharField(20) | Yes | CLEAN / DAMAGED / REJECTED |
| trailer_temp_ok | BooleanField | No | Temperature check passed |
| trailer_temp_reading | DecimalField | No | Temperature reading |
| inspected_by | FK -> User | No | Inspector |
| inspected_at | DateTimeField | No | Inspection timestamp |
| loading_started_at | DateTimeField | No | Loading start |
| loading_completed_at | DateTimeField | No | Loading end |
| loaded_by | FK -> User | No | Loader |
| supervisor_confirmed | BooleanField | No | Supervisor confirmed |
| supervisor | FK -> User | No | Supervisor |
| confirmed_at | DateTimeField | No | Confirmation timestamp |

#### GoodsIssuePosting

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| shipment_order | OneToOne -> ShipmentOrder | Yes | Shipment posted |
| sap_doc_entry | IntegerField | No | SAP Goods Issue DocEntry |
| sap_doc_num | IntegerField | No | SAP Goods Issue DocNum |
| status | CharField(20) | Yes | PENDING / POSTED / FAILED |
| posted_at | DateTimeField | No | Posting timestamp |
| posted_by | FK -> User | No | User who posted |
| error_message | TextField | No | Error details if failed |
| retry_count | IntegerField | No | Retry attempts |

---

### 3.2 Process Flow & Steps

| # | Step | Responsible | Description |
|---|------|-------------|-------------|
| 01 | Orders Released | ERP (Auto) | Production completion triggers ERP to release shipment orders. System syncs Sales Orders from SAP via Service Layer. |
| 02 | Pick Wave Created | System (Auto) | System generates PickTask records for each ShipmentOrderItem. Tasks assigned to warehouse operatives. |
| 03 | Picking | Warehouse Team | Operatives follow pick routes. Each item scanned. picked_qty updated. Short picks flagged. |
| 04 | Pack & Label | Pack Team | Goods consolidated onto pallets, wrapped, labelled. Weight recorded. packed_qty updated. Status -> PACKED. |
| 05 | Stage at Dock | Warehouse Team | Packed pallets moved to staging lane. 100% completion confirmed. Status -> STAGED. |
| 06 | Carrier Arrives | Gate Agent | Carrier checks in via VehicleEntry. Routed to Zone C bay. vehicle_entry linked. |
| 07 | Inspect Trailer | Dock Team | Trailer checked for cleanliness and temperature. OutboundLoadRecord created. |
| 08 | Load Truck | Dock Team | Pallets loaded and scanned. loaded_qty updated. Status -> LOADING. |
| 09 | Final Check & BOL | Dock Supervisor | Supervisor confirms. BOL generated. Driver signs. |
| 10 | Seal & Dispatch | Dock Operator | Trailer sealed. Goods Issue posted to SAP. Status -> DISPATCHED. |

---

### 3.3 API Endpoints

**Base URL:** `/api/v1/outbound/`

| Method | Endpoint | Purpose | Permission |
|--------|----------|---------|------------|
| GET | `shipments/` | List shipment orders with filters | Dock Supervisor, Logistics Manager |
| GET | `shipments/<id>/` | Get shipment detail with items | All dock roles |
| POST | `shipments/sync/` | Sync Sales Orders from SAP | System / Admin |
| POST | `shipments/<id>/assign-bay/` | Assign dock bay and time slot | Dock Planner |
| GET | `shipments/<id>/pick-tasks/` | List pick tasks | Warehouse Team |
| POST | `shipments/<id>/generate-picks/` | Generate pick wave | Dock Supervisor |
| PATCH | `pick-tasks/<id>/` | Update pick task | Warehouse Team |
| POST | `pick-tasks/<id>/scan/` | Record barcode scan | Warehouse Team |
| POST | `shipments/<id>/confirm-pack/` | Confirm packing complete | Pack Team |
| POST | `shipments/<id>/stage/` | Mark as staged | Warehouse Team |
| POST | `shipments/<id>/link-vehicle/` | Link carrier vehicle | Gate Agent |
| POST | `shipments/<id>/inspect-trailer/` | Record trailer inspection | Dock Team |
| POST | `shipments/<id>/load/` | Record pallet loading | Dock Team |
| POST | `shipments/<id>/supervisor-confirm/` | Supervisor confirmation | Dock Supervisor |
| POST | `shipments/<id>/generate-bol/` | Generate Bill of Lading | Dock Supervisor |
| POST | `shipments/<id>/dispatch/` | Seal and dispatch | Dock Operator |
| GET | `dashboard/` | Outbound dashboard and KPIs | Logistics Manager |

---

### 3.4 Business Rules & Validations

1. Dock bay must be Zone C (bays 19-30) only.
2. Pick tasks require ShipmentOrder status = RELEASED.
3. Cannot move to PACKED unless all items picked or marked SHORT (supervisor approved).
4. Cannot move to STAGED unless status = PACKED.
5. Vehicle must be linked before trailer inspection.
6. Trailer must pass inspection (CLEAN) before loading. DAMAGED requires supervisor override.
7. loaded_qty cannot exceed packed_qty.
8. Supervisor confirmation required before dispatch.
9. BOL and seal_number must be recorded before dispatch.
10. Goods Issue SAP posting must succeed before status = DISPATCHED.
11. All timestamps auto-recorded, not editable.

---

### 3.5 SAP Integration

#### Sync Sales Orders (from SAP)

Pull released Sales Orders: `GET /b1s/v1/Orders?$filter=DocumentStatus eq 'bost_Open'`. Map to ShipmentOrder and ShipmentOrderItem. Use `sap_client` app.

#### Post Goods Issue (to SAP)

On dispatch: `POST /b1s/v1/InventoryGenExits` with loaded items, quantities, warehouses, batches. Follow `grpo/services.py` pattern.

---

## 4. Roles & Permissions

Map to Django Groups (following existing pattern in `setup_groups` script).

| Role | Outbound Permissions |
|------|---------------------|
| Dock Supervisor | Full access: manage shipments, override inspections, confirm loads, approve short picks |
| Dock Planner | Assign bays and time slots to shipments |
| Warehouse Team | Execute pick tasks, pack goods, stage at dock |
| Pack Team | Confirm packing complete |
| Dock Team / Operator | Inspect trailers, load trucks, seal and dispatch |
| Gate Agent | Link arriving carrier vehicle to shipment |
| Logistics Manager | View dashboard and KPIs, review all shipments |

---

## 5. Database Schema Summary

### outbound_dispatch app

| Model | Table | Key Relations |
|-------|-------|---------------|
| ShipmentOrder | outbound_dispatch_shipmentorder | FK: company, vehicle_entry, created_by |
| ShipmentOrderItem | outbound_dispatch_shipmentorderitem | FK: shipment_order |
| PickTask | outbound_dispatch_picktask | FK: shipment_item, assigned_to |
| OutboundLoadRecord | outbound_dispatch_outboundloadrecord | O2O: shipment_order; FK: inspected_by, loaded_by, supervisor |
| GoodsIssuePosting | outbound_dispatch_goodsissueposting | O2O: shipment_order; FK: posted_by |

---

## 6. KPIs & Monitoring

| KPI | Target |
|-----|--------|
| Average dock turnaround time (carrier arrival to departure) | Under 60 minutes |
| On-time outbound dispatches | 97% or above |
| Pick accuracy (items picked correctly first time) | 99.5% or above |
| Packing accuracy | 99.5% or above |
| Dock bay utilisation (Zone C) | 75% - 90% |
| Order fulfilment rate (shipped vs ordered qty) | 98% or above |
| SAP Goods Issue posting success rate | 99% or above |

---

## 7. Implementation Priority & Phases

### Phase 1: Outbound Core (HIGH)

- Create `outbound_dispatch` app with models and migrations
- ShipmentOrder sync from SAP Sales Orders
- Pick task generation and picking workflow APIs
- Packing and staging APIs
- Carrier arrival integration with `driver_management`

### Phase 2: Outbound Dispatch (HIGH)

- Trailer inspection and loading APIs
- Supervisor confirmation flow
- BOL generation (PDF)
- Goods Issue posting to SAP
- Outbound dashboard API

### Phase 3: Polish & Testing (MEDIUM)

- KPI reporting endpoints
- Barcode scanner integration testing
- Load testing for concurrent dock operations
- Admin panel registration for all new models

---

*Factory Docking System | Outbound Requirements | v1.0 | 2026*
