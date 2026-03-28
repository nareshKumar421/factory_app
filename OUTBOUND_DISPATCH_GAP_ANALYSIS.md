# Outbound Dispatch - Gap Analysis

**Date:** 2026-03-28
**Module:** `outbound_dispatch`
**Purpose:** Compare expected dispatch flow against current implementation

---

## Expected Flow vs Implementation Status

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 1 | Picklist | IMPLEMENTED | Pick wave generation, task assignment, barcode scanning, short-pick handling |
| 2 | Approval | PARTIAL | Only supervisor confirmation exists. No multi-level approval chain, no approval/rejection workflow, no approval history |
| 3 | Invoice (datewise filter, summary) | NOT IMPLEMENTED | No invoice model, no generation, no datewise filtering, no summary reports. Only a `BPL_IDAssignedToInvoice` field passed to SAP |
| 4 | Dispatch tracking (is dispatched, which dispatched) | PARTIAL | Status goes to DISPATCHED but no explicit `is_dispatched` flag, no `dispatched_at` timestamp, no "which vehicle dispatched" query |
| 5 | Vehicle sitting 3+ days (not dispatching) | NOT IMPLEMENTED | Vehicle is linked to shipment but no idle-time tracking, no alerts, no "sitting X days" report |
| 6 | Gatepass (weights kilos & liters) | PARTIAL | Weight in kg tracked on items. No liter/volume tracking. No gatepass document generation or gatepass number |
| 7 | Receivings: Warehouse to dispatch | NOT IMPLEMENTED | Warehouse code stored on items for picking, but no formal receiving-from-warehouse workflow or confirmation |
| 8 | Receivings: Receiving from party | NOT IMPLEMENTED | No functionality exists. This may belong in an inbound module |

---

## What IS Implemented (Current Flow)

```
SAP Sales Order Sync
    |
    v
ShipmentOrder (RELEASED)
    |-- assign dock bay
    v
Pick Wave Generation (PICKING)
    |-- assign tasks, scan barcodes, record actual qty
    v
Confirm Pack (PACKED)
    |
    v
Stage Shipment (STAGED)
    |-- link vehicle, inspect trailer
    v
Record Loading (LOADING)
    |-- total_weight calculated (kg only)
    v
Supervisor Confirm
    |
    v
Generate BOL + Dispatch (DISPATCHED)
    |
    v
SAP Goods Issue Posting
```

**Models:** ShipmentOrder, ShipmentOrderItem, PickTask, OutboundLoadRecord, GoodsIssuePosting
**API Endpoints:** ~19 endpoints covering the full pick-pack-ship cycle
**Dashboard:** Status counts and zone utilization KPIs

---

## Detailed Gaps

### 1. Approval Workflow
- **Current:** Single `supervisor_confirmed` boolean on OutboundLoadRecord
- **Missing:** Multi-level approval chain, approval request/rejection states, configurable approval rules, approval audit trail

### 2. Invoice Generation
- **Current:** Nothing
- **Missing:** Invoice model, datewise invoice generation, invoice summary view, invoice filtering by date range, invoice-to-shipment linkage

### 3. Dispatch Status Tracking
- **Current:** ShipmentOrder.status = DISPATCHED (generic)
- **Missing:** `dispatched_at` dedicated timestamp field, `is_dispatched` boolean for quick filtering, report of "which shipments dispatched today/this week"

### 4. Vehicle Sitting Tracking
- **Current:** `loading_started_at` and `loading_completed_at` timestamps exist on OutboundLoadRecord; `released_for_loading_at` on OutboundGateEntry
- **Missing:** Idle time calculation, alert for vehicles sitting 3+ days, vehicle status dashboard, automated notifications

### 5. Gatepass with Weights (Kilos & Liters)
- **Current:** `weight` field on ShipmentOrderItem (kg), `total_weight` computed on ShipmentOrder
- **Missing:** Liter/volume field, gatepass document model, gatepass number generation, gatepass print/PDF, weight validation against vehicle capacity

### 6. Receivings: Warehouse to Dispatch
- **Current:** `warehouse_code` stored on items, used as pick location
- **Missing:** Formal receiving workflow from warehouse, goods handover confirmation, receiving timestamp, warehouse-to-staging transfer record

### 7. Receivings: From Party
- **Current:** Nothing in outbound_dispatch
- **Missing:** Entire receiving-from-party workflow. This likely belongs in a separate inbound/receiving module

---

## Summary

**3 of 8 features fully or mostly implemented:** Picklist, basic dispatch flow, basic weight tracking
**5 of 8 features missing or only partially implemented:** Approval workflow, invoice generation, vehicle sitting alerts, gatepass documents, receivings

The core pick-pack-ship-dispatch pipeline works end-to-end with SAP integration. The major gaps are in reporting (invoices, vehicle idle time), document generation (gatepass), and receiving workflows.
