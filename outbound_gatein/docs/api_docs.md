# Outbound Gate Entry — API Documentation

Base URL: `/api/v1/outbound-gatein/`

All endpoints require `Authorization: Bearer <token>` and `Company-Code: <code>` headers.

---

## 1. Create Outbound Entry

**POST** `/gate-entries/{gate_entry_id}/outbound/`

Creates an outbound gate entry for a VehicleEntry of type `OUTBOUND`.

**Permissions:** `outbound_gatein.add_outboundgateentry`

**Request Body:**

```json
{
    "purpose": 1,
    "sales_order_ref": "1725096575",
    "customer_name": "ACME Corp",
    "customer_code": "C10001",
    "transporter_name": "Fast Logistics",
    "transporter_contact": "9876543210",
    "lr_number": "LR-2026-001",
    "vehicle_empty_confirmed": true,
    "trailer_type": "20ft Container",
    "trailer_length_ft": "20.00",
    "assigned_zone": "YARD",
    "assigned_bay": "",
    "expected_loading_time": "2026-03-25T14:00:00Z",
    "remarks": "Expected for SO dispatch"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `purpose` | int | No | FK to OutboundPurpose |
| `sales_order_ref` | string | No | SAP Sales Order number |
| `customer_name` | string | No | Customer name |
| `customer_code` | string | No | SAP Business Partner code |
| `transporter_name` | string | No | Transporter / logistics company |
| `transporter_contact` | string | No | Transporter phone |
| `lr_number` | string | No | Lorry Receipt number |
| `vehicle_empty_confirmed` | bool | No | Security confirmed vehicle is empty |
| `trailer_type` | string | No | Type of trailer |
| `trailer_length_ft` | decimal | No | Trailer length in feet |
| `assigned_zone` | string | No | `ZONE_C` or `YARD` (default: `YARD`) |
| `assigned_bay` | string | No | Bay number (1-30) |
| `expected_loading_time` | datetime | No | Expected loading start time |
| `remarks` | string | No | Free-text remarks |

**Success Response:** `201 Created`

```json
{
    "message": "Outbound gate entry created",
    "id": 42
}
```

**Error Responses:**

| Code | Condition |
|------|-----------|
| 400 | `entry_type` is not `OUTBOUND` |
| 400 | Gate entry is locked |
| 400 | Outbound entry already exists |
| 404 | Gate entry not found |

---

## 2. Read Outbound Entry

**GET** `/gate-entries/{gate_entry_id}/outbound/`

**Permissions:** `outbound_gatein.view_outboundgateentry`

**Success Response:** `200 OK`

```json
{
    "id": 42,
    "purpose": { "id": 1, "name": "Finished Goods Dispatch" },
    "sales_order_ref": "1725096575",
    "customer_name": "ACME Corp",
    "customer_code": "C10001",
    "transporter_name": "Fast Logistics",
    "transporter_contact": "9876543210",
    "lr_number": "LR-2026-001",
    "vehicle_empty_confirmed": true,
    "trailer_type": "20ft Container",
    "trailer_length_ft": "20.00",
    "assigned_zone": "YARD",
    "assigned_bay": "",
    "expected_loading_time": "2026-03-25T14:00:00Z",
    "arrival_time": "2026-03-25T10:30:00Z",
    "released_for_loading_at": null,
    "exit_time": null,
    "remarks": "Expected for SO dispatch",
    "created_at": "2026-03-25T10:30:00Z",
    "updated_at": "2026-03-25T10:30:00Z"
}
```

---

## 3. Update Outbound Entry

**PUT** `/gate-entries/{gate_entry_id}/outbound/update/`

Partial updates supported.

**Permissions:** `outbound_gatein.change_outboundgateentry`

**Request Body:** Any subset of the fields from Create.

**Success Response:** `200 OK` — Updated entry object.

---

## 4. Complete Gate Entry

**POST** `/gate-entries/{gate_entry_id}/complete/`

Completes and locks the gate entry.

**Permissions:** `outbound_gatein.can_complete_outbound_entry`

**Validations:**

- Entry type must be `OUTBOUND`
- Not already locked
- Security check must be submitted
- Outbound entry must exist
- Vehicle must be confirmed empty

**Success Response:** `200 OK`

```json
{
    "detail": "Outbound gate entry completed successfully"
}
```

---

## 5. Release Vehicle for Loading

**POST** `/gate-entries/{gate_entry_id}/release-for-loading/`

Marks the vehicle as released from the gate/yard and ready for dock loading.

**Permissions:** `outbound_gatein.can_release_for_loading`

**Validations:**

- Outbound entry must exist
- Vehicle must be confirmed empty
- Not already released

**Success Response:** `200 OK` — Updated entry object with `released_for_loading_at` timestamp.

---

## 6. List Available Vehicles (for Shipment Linking)

**GET** `/available-vehicles/`

Returns outbound vehicle entries that are confirmed empty and available for linking to a shipment. Used by the **outbound_dispatch** module's "Link Vehicle" dropdown.

**Permissions:** `outbound_gatein.view_outboundgateentry`

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `status` | string | — | Filter by VehicleEntry status (e.g., `COMPLETED`) |
| `exclude_linked` | string | `true` | Exclude vehicles already linked to active shipments |

**Success Response:** `200 OK`

```json
[
    {
        "id": 42,
        "vehicle_entry_id": 100,
        "entry_no": "GE-OB-001",
        "vehicle_number": "MH12XY9999",
        "driver_name": "Raj Kumar",
        "gate_status": "COMPLETED",
        "customer_name": "ACME Corp",
        "sales_order_ref": "1725096575",
        "assigned_zone": "ZONE_C",
        "assigned_bay": "22",
        "vehicle_empty_confirmed": true,
        "arrival_time": "2026-03-25T10:30:00Z",
        "released_for_loading_at": "2026-03-25T11:00:00Z"
    }
]
```

**Frontend Usage:** When calling `POST /api/v1/outbound/shipments/{id}/link-vehicle/`, pass the `vehicle_entry_id` from this response.

---

## 7. List Outbound Purposes (Dropdown)

**GET** `/purposes/`

**Permissions:** `outbound_gatein.view_outboundpurpose`

**Success Response:** `200 OK`

```json
[
    { "id": 1, "name": "Finished Goods Dispatch", "description": "", "is_active": true },
    { "id": 2, "name": "Sample Delivery", "description": "", "is_active": true }
]
```

---

## 8. Full View (gate_core)

**GET** `/api/v1/gate-core/outbound-gate-entry/{gate_entry_id}/`

Returns the complete outbound gate entry data including gate info, vehicle, driver, security check, and outbound details in a single combined response. Same pattern as the other gate entry full views.

**Permissions:** `gate_core.can_view_outbound_full_entry`
