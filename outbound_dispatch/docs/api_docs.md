# Outbound Dispatch API Documentation

**Base URL:** `/api/v1/outbound/`

**Authentication:** JWT Bearer Token (via `Authorization: Bearer <token>`)

**Required Header:** `Company-Code: <company_code>` (e.g., `JIVO_OIL`)

---

## 1. List Shipments

**GET** `/shipments/`

List all shipment orders with optional filters.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| status | string | Filter by status: RELEASED, PICKING, PACKED, STAGED, LOADING, DISPATCHED, CANCELLED |
| scheduled_date | date | Filter by scheduled dispatch date (YYYY-MM-DD) |
| customer_code | string | Filter by SAP customer code |

**Response:** `200 OK`
```json
[
  {
    "id": 1,
    "sap_doc_entry": 123,
    "sap_doc_num": 456,
    "customer_code": "C10001",
    "customer_name": "Acme Corp",
    "carrier_name": "FastFreight",
    "scheduled_date": "2026-03-25",
    "dock_bay": "22",
    "status": "RELEASED",
    "bill_of_lading_no": "",
    "seal_number": "",
    "total_weight": null,
    "item_count": 3,
    "vehicle_entry_no": null,
    "created_at": "2026-03-24T10:00:00Z"
  }
]
```

**Permission:** `outbound_dispatch.view_shipmentorder`

---

## 2. Shipment Detail

**GET** `/shipments/<id>/`

Get full shipment details including all line items.

**Response:** `200 OK`
```json
{
  "id": 1,
  "sap_doc_entry": 123,
  "sap_doc_num": 456,
  "customer_code": "C10001",
  "customer_name": "Acme Corp",
  "ship_to_address": "123 Industrial Park",
  "carrier_code": "TR001",
  "carrier_name": "FastFreight",
  "scheduled_date": "2026-03-25",
  "dock_bay": "22",
  "dock_slot_start": "2026-03-25T08:00:00Z",
  "dock_slot_end": "2026-03-25T10:00:00Z",
  "status": "RELEASED",
  "vehicle_entry_no": null,
  "bill_of_lading_no": "",
  "seal_number": "",
  "total_weight": null,
  "notes": "",
  "items": [
    {
      "id": 1,
      "sap_line_num": 0,
      "item_code": "FG-001",
      "item_name": "Jivo Oil 1L",
      "ordered_qty": "100.000",
      "picked_qty": "0.000",
      "packed_qty": "0.000",
      "loaded_qty": "0.000",
      "uom": "BTL",
      "warehouse_code": "WH01",
      "batch_number": "B20260301",
      "weight": "1.200",
      "pick_status": "PENDING"
    }
  ],
  "created_at": "2026-03-24T10:00:00Z",
  "updated_at": "2026-03-24T10:00:00Z"
}
```

---

## 3. Sync from SAP

**POST** `/shipments/sync/`

Sync open Sales Orders from SAP HANA into the system.

**Request Body (optional):**
```json
{
  "customer_code": "C10001",
  "from_date": "2026-03-01",
  "to_date": "2026-03-31"
}
```

**Response:** `200 OK`
```json
{
  "created_count": 5,
  "updated_count": 2,
  "skipped_count": 1,
  "total_from_sap": 8,
  "errors": []
}
```

**Permission:** `outbound_dispatch.can_sync_shipments`

---

## 4. Assign Dock Bay

**POST** `/shipments/<id>/assign-bay/`

Assign a Zone C dock bay (19-30) and time slot.

**Request Body:**
```json
{
  "dock_bay": "22",
  "dock_slot_start": "2026-03-25T08:00:00Z",
  "dock_slot_end": "2026-03-25T10:00:00Z"
}
```

**Permission:** `outbound_dispatch.can_assign_dock_bay`

---

## 5. Pick Tasks

### List Pick Tasks
**GET** `/shipments/<id>/pick-tasks/`

### Generate Pick Wave
**POST** `/shipments/<id>/generate-picks/`

Generates pick tasks for all items. Shipment must be in RELEASED status.

### Update Pick Task
**PATCH** `/pick-tasks/<id>/`

```json
{
  "assigned_to": 5,
  "status": "COMPLETED",
  "actual_qty": "98.000",
  "scanned_barcode": "FG-001-B20260301"
}
```

### Record Scan
**POST** `/pick-tasks/<id>/scan/`

```json
{
  "barcode": "FG-001-B20260301"
}
```

**Permission:** `outbound_dispatch.can_execute_pick_task`

---

## 6. Confirm Pack

**POST** `/shipments/<id>/confirm-pack/`

Marks shipment as PACKED. All items must be picked or marked SHORT.

---

## 7. Stage Shipment

**POST** `/shipments/<id>/stage/`

Marks shipment as STAGED. Shipment must be PACKED.

---

## 8. Link Vehicle

**POST** `/shipments/<id>/link-vehicle/`

Link an arriving carrier vehicle to the shipment.

```json
{
  "vehicle_entry_id": 42
}
```

---

## 9. Inspect Trailer

**POST** `/shipments/<id>/inspect-trailer/`

Record trailer inspection. Vehicle must be linked first.

```json
{
  "trailer_condition": "CLEAN",
  "trailer_temp_ok": true,
  "trailer_temp_reading": "4.50"
}
```

**Permission:** `outbound_dispatch.can_inspect_trailer`

---

## 10. Load Truck

**POST** `/shipments/<id>/load/`

Record pallet loading. Trailer must be inspected first.

```json
{
  "items": [
    {"item_id": 1, "loaded_qty": "98.000"},
    {"item_id": 2, "loaded_qty": "50.000"}
  ]
}
```

**Permission:** `outbound_dispatch.can_load_truck`

---

## 11. Supervisor Confirm

**POST** `/shipments/<id>/supervisor-confirm/`

Supervisor final confirmation of loading.

**Permission:** `outbound_dispatch.can_confirm_load`

---

## 12. Generate BOL

**POST** `/shipments/<id>/generate-bol/`

Auto-generates Bill of Lading number and returns BOL data.

**Response:**
```json
{
  "bol_number": "BOL-456-202603251030",
  "shipment_id": 1,
  "customer_name": "Acme Corp",
  "ship_to_address": "123 Industrial Park",
  "carrier_name": "FastFreight",
  "scheduled_date": "2026-03-25",
  "total_weight": "117.600",
  "items": [...]
}
```

---

## 13. Dispatch

**POST** `/shipments/<id>/dispatch/`

Seal trailer, post Goods Issue to SAP, mark as DISPATCHED.

```json
{
  "seal_number": "SEAL-20260325-001",
  "branch_id": 1
}
```

**Validations:**
- Shipment must be in LOADING status
- Supervisor must have confirmed
- BOL must be generated
- Seal number is required

**Permission:** `outbound_dispatch.can_dispatch_shipment`

---

## 14. Goods Issue Status

**GET** `/shipments/<id>/goods-issue/`

Get Goods Issue posting status for a shipment.

---

## 15. Retry Goods Issue

**POST** `/shipments/<id>/goods-issue/retry/`

Retry a failed Goods Issue posting.

```json
{
  "branch_id": 1
}
```

**Permission:** `outbound_dispatch.can_post_goods_issue`

---

## 16. Dashboard

**GET** `/dashboard/`

Get outbound dashboard KPIs.

**Response:**
```json
{
  "total_shipments": 50,
  "by_status": {
    "RELEASED": 10,
    "PICKING": 5,
    "PACKED": 3,
    "STAGED": 2,
    "LOADING": 1,
    "DISPATCHED": 28,
    "CANCELLED": 1
  },
  "today_dispatched": 4,
  "today_scheduled": 6,
  "zone_c_active_bays": 3,
  "zone_c_bay_utilisation_pct": 25.0
}
```

**Permission:** `outbound_dispatch.can_view_outbound_dashboard`

---

## Error Responses

All endpoints return standard error format:

```json
{
  "detail": "Error description here"
}
```

| Status Code | Meaning |
|-------------|---------|
| 400 | Bad Request / Validation Error |
| 401 | Unauthorized (missing/invalid token) |
| 403 | Forbidden (missing company code or permission) |
| 404 | Not Found |
| 502 | SAP Data Error |
| 503 | SAP Unavailable |
