# Outbound Gate Entry — Frontend Integration Guide

## Overview

The `outbound_gatein` module handles **empty vehicles arriving at the factory gate** for outbound dispatch. It sits between the factory gate (security) and the `outbound_dispatch` module (warehouse loading).

---

## Workflow

```
Vehicle arrives     Security creates       Gate officer fills     Security check      Complete gate
at factory gate  →  VehicleEntry          →  Outbound Entry    →  submitted        →  entry (lock)
(empty truck)       (type: OUTBOUND)          + confirms empty      ↓
                                                                  Release for loading
                                                                    ↓
                                                              Link to Shipment
                                                           (outbound_dispatch)
```

---

## Screens to Build

### 1. Gate Entry Creation (existing flow)

Use the existing `VehicleEntryListCreateAPI` at `POST /api/v1/vehicle-management/gate-entries/` with `entry_type: "OUTBOUND"`.

```js
const payload = {
    vehicle: vehicleId,
    driver: driverId,
    entry_type: "OUTBOUND",
    entry_no: "GE-OB-2026-001",  // auto-generate or manual
    remarks: "Empty vehicle for FG dispatch"
};
```

### 2. Outbound Entry Form

After creating the VehicleEntry, show the outbound details form.

**API:** `POST /api/v1/outbound-gatein/gate-entries/{gate_entry_id}/outbound/`

**Form Fields:**

| Field | Input Type | Notes |
|-------|-----------|-------|
| Purpose | Dropdown | Fetch from `GET /api/v1/outbound-gatein/purposes/` |
| Sales Order Ref | Text | Optional — SAP SO number |
| Customer Name | Text | Auto-fill if SO ref provided |
| Customer Code | Text | Auto-fill if SO ref provided |
| Transporter Name | Text | Transport company |
| Transporter Contact | Phone | Transport company phone |
| LR Number | Text | Lorry receipt |
| Vehicle Empty Confirmed | Checkbox | **Required for completion** |
| Trailer Type | Text | e.g., "20ft Container", "Open Body" |
| Trailer Length (ft) | Number | Optional |
| Assigned Zone | Dropdown | `YARD` (default), `ZONE_C` |
| Assigned Bay | Text | Bay number if zone is ZONE_C |
| Expected Loading Time | DateTime | When loading is expected to start |
| Remarks | Textarea | Free text |

### 3. Outbound Gate Entry List

Use `GET /api/v1/vehicle-management/gate-entries/?entry_type=OUTBOUND&from_date=...&to_date=...`

**Table Columns:**
- Entry No
- Vehicle Number
- Driver Name
- Customer
- Status (badge)
- Empty Confirmed (icon)
- Arrival Time
- Released At
- Actions

### 4. Link Vehicle Dropdown (in outbound_dispatch)

When the user clicks "Link Vehicle" on a shipment, fetch available vehicles:

```js
// Fetch available outbound vehicles
const response = await fetch(
    "/api/v1/outbound-gatein/available-vehicles/",
    { headers: { "Company-Code": companyCode, Authorization: `Bearer ${token}` } }
);
const vehicles = await response.json();

// Render dropdown
vehicles.map(v => ({
    label: `${v.vehicle_number} — ${v.driver_name} (${v.entry_no})`,
    value: v.vehicle_entry_id,
}));

// On select, link vehicle to shipment
await fetch(`/api/v1/outbound/shipments/${shipmentId}/link-vehicle/`, {
    method: "POST",
    body: JSON.stringify({ vehicle_entry_id: selectedVehicleEntryId }),
});
```

**Dropdown shows:** Vehicle Number, Driver Name, Entry No, Customer (if available)

**Filters applied automatically:**
- Only `OUTBOUND` type entries
- Only vehicles confirmed empty
- Excludes vehicles already linked to active shipments

---

## Action Button Visibility

| Action | When Visible |
|--------|-------------|
| Create Outbound Entry | VehicleEntry exists, type=OUTBOUND, not locked, no outbound_entry |
| Edit Outbound Entry | outbound_entry exists, VehicleEntry not locked |
| Confirm Empty | outbound_entry exists, `vehicle_empty_confirmed` is false |
| Complete Gate Entry | Security check submitted + outbound_entry exists + empty confirmed |
| Release for Loading | `vehicle_empty_confirmed` is true, `released_for_loading_at` is null |
| Link to Shipment | Vehicle in available-vehicles list |

---

## Permissions Mapping

| Action | Permission Code |
|--------|----------------|
| Create outbound entry | `outbound_gatein.add_outboundgateentry` |
| View outbound entry | `outbound_gatein.view_outboundgateentry` |
| Edit outbound entry | `outbound_gatein.change_outboundgateentry` |
| Complete gate entry | `outbound_gatein.can_complete_outbound_entry` |
| Release for loading | `outbound_gatein.can_release_for_loading` |
| View purposes dropdown | `outbound_gatein.view_outboundpurpose` |
| View full gate entry | `gate_core.can_view_outbound_full_entry` |

---

## Error Handling

All errors return `{ "detail": "..." }` with appropriate HTTP status codes.

| HTTP Code | Meaning |
|-----------|---------|
| 400 | Validation error (wrong entry type, already exists, not empty, etc.) |
| 401 | Not authenticated |
| 403 | Missing Company-Code header or insufficient permissions |
| 404 | Gate entry or outbound entry not found |
