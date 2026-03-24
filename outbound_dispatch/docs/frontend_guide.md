# Outbound Dispatch - Frontend Integration Guide

## Overview

The Outbound Dispatch module manages the full lifecycle of shipping finished goods to customers. This guide helps frontend developers integrate with the API.

---

## Authentication Setup

All requests require:
1. **JWT Token** in `Authorization: Bearer <token>` header
2. **Company Code** in `Company-Code: <code>` header (e.g., `JIVO_OIL`)

```javascript
const API_BASE = '/api/v1/outbound';

const headers = {
  'Authorization': `Bearer ${token}`,
  'Company-Code': companyCode,
  'Content-Type': 'application/json',
};
```

---

## Page Structure

### 1. Shipment List Page

**API:** `GET /shipments/?status=RELEASED&scheduled_date=2026-03-25`

**UI Elements:**
- Filter bar: Status dropdown, Date picker, Customer search
- Table with columns: SO#, Customer, Status, Scheduled Date, Dock Bay, Actions
- Status badges with colors:
  - `RELEASED` → Blue
  - `PICKING` → Orange
  - `PACKED` → Yellow
  - `STAGED` → Purple
  - `LOADING` → Indigo
  - `DISPATCHED` → Green
  - `CANCELLED` → Red

### 2. Shipment Detail Page

**API:** `GET /shipments/<id>/`

**UI Sections:**
- **Header**: SO#, Customer, Status, Scheduled Date
- **Items Table**: Item Code, Name, Ordered/Picked/Packed/Loaded Qty, Pick Status
- **Dock Assignment**: Bay number, Time slot (editable if RELEASED)
- **Action Buttons**: Context-sensitive based on current status

### 3. Pick Task Panel

**API:** `GET /shipments/<id>/pick-tasks/`

**UI Elements:**
- Task list with: Location, Item, Qty to Pick, Status, Assigned To
- Barcode scan input field
- Mark Complete / Short buttons

### 4. Dashboard Page

**API:** `GET /dashboard/`

**UI Elements:**
- KPI cards: Today's Dispatches, Scheduled Today, Active Bays
- Status breakdown chart (pie/donut)
- Bay utilisation gauge

---

## Workflow Flow (Step by Step)

The frontend should guide users through this sequence. Each step has a corresponding API call.

```
┌─────────────┐
│  RELEASED   │ → "Sync from SAP" button  (POST /shipments/sync/)
└──────┬──────┘
       │ "Assign Bay" → POST /shipments/<id>/assign-bay/
       │ "Generate Picks" → POST /shipments/<id>/generate-picks/
       ▼
┌─────────────┐
│   PICKING   │ → Pick task list (GET /shipments/<id>/pick-tasks/)
└──────┬──────┘   Update tasks (PATCH /pick-tasks/<id>/)
       │          Scan barcodes (POST /pick-tasks/<id>/scan/)
       │ "Confirm Pack" → POST /shipments/<id>/confirm-pack/
       ▼
┌─────────────┐
│   PACKED    │
└──────┬──────┘
       │ "Stage at Dock" → POST /shipments/<id>/stage/
       ▼
┌─────────────┐
│   STAGED    │ → "Link Vehicle" when carrier arrives
└──────┬──────┘   POST /shipments/<id>/link-vehicle/
       │
       │ "Inspect Trailer" → POST /shipments/<id>/inspect-trailer/
       │ "Load Truck" → POST /shipments/<id>/load/
       ▼
┌─────────────┐
│   LOADING   │
└──────┬──────┘
       │ "Supervisor Confirm" → POST /shipments/<id>/supervisor-confirm/
       │ "Generate BOL" → POST /shipments/<id>/generate-bol/
       │ "Dispatch" → POST /shipments/<id>/dispatch/
       ▼
┌─────────────┐
│ DISPATCHED  │ → Read-only. Show GI status.
└─────────────┘   GET /shipments/<id>/goods-issue/
```

---

## Action Button Visibility

Show/hide action buttons based on shipment status:

| Action | Visible When Status = |
|--------|----------------------|
| Assign Bay | RELEASED |
| Generate Picks | RELEASED |
| Confirm Pack | PICKING |
| Stage | PACKED |
| Link Vehicle | STAGED, LOADING |
| Inspect Trailer | STAGED (after vehicle linked) |
| Load Truck | STAGED, LOADING (after inspection) |
| Supervisor Confirm | LOADING |
| Generate BOL | LOADING |
| Dispatch | LOADING (after supervisor confirm + BOL) |
| Retry GI | LOADING (when GI failed) |

---

## Error Handling

```javascript
try {
  const response = await fetch(`${API_BASE}/shipments/${id}/dispatch/`, {
    method: 'POST',
    headers,
    body: JSON.stringify({ seal_number: sealNumber, branch_id: branchId }),
  });

  if (!response.ok) {
    const error = await response.json();

    if (response.status === 400) {
      // Validation error - show error.detail to user
      showToast(error.detail, 'error');
    } else if (response.status === 503) {
      // SAP unavailable - show retry option
      showToast('SAP is currently unavailable. Please retry.', 'warning');
    } else if (response.status === 502) {
      // SAP data error
      showToast(`SAP Error: ${error.detail}`, 'error');
    }
    return;
  }

  const data = await response.json();
  showToast('Shipment dispatched successfully!', 'success');
} catch (err) {
  showToast('Network error', 'error');
}
```

---

## Real-time Updates

For the dashboard and shipment list, poll every 30 seconds:

```javascript
useEffect(() => {
  const interval = setInterval(() => {
    fetchShipments();
  }, 30000);
  return () => clearInterval(interval);
}, []);
```

---

## Permissions

The backend enforces permissions via Django groups. The frontend should hide UI elements when the user lacks permission. Check the user's permissions from the `/api/v1/accounts/me/` endpoint.

| Permission | Controls |
|------------|----------|
| `outbound_dispatch.view_shipmentorder` | View shipments list/detail |
| `outbound_dispatch.can_sync_shipments` | Sync button |
| `outbound_dispatch.can_assign_dock_bay` | Assign bay form |
| `outbound_dispatch.can_execute_pick_task` | Pick task operations |
| `outbound_dispatch.can_inspect_trailer` | Trailer inspection form |
| `outbound_dispatch.can_load_truck` | Loading operations |
| `outbound_dispatch.can_confirm_load` | Supervisor confirm button |
| `outbound_dispatch.can_dispatch_shipment` | Dispatch button |
| `outbound_dispatch.can_view_outbound_dashboard` | Dashboard page |
