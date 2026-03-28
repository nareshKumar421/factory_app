# BOM Auto-Fetch — Frontend Integration Guide

> When creating a production run, BOM (Bill of Materials) components are automatically fetched from SAP and populated as material usage records. This guide covers the API endpoints, data flow, and frontend integration.

---

## Table of Contents

1. [Overview](#1-overview)
2. [How It Works](#2-how-it-works)
3. [API Endpoints](#3-api-endpoints)
4. [Frontend Integration](#4-frontend-integration)
5. [Data Mapping](#5-data-mapping)
6. [Error Handling](#6-error-handling)
7. [Edge Cases](#7-edge-cases)

---

## 1. Overview

### Before (Manual Flow)
1. User creates a production run
2. User manually adds each material one by one via `POST /runs/{id}/materials/`

### After (Auto-Fetch Flow)
1. User creates a production run with `sap_doc_entry` or `product` (item code)
2. System **automatically fetches BOM** from SAP and creates all material records
3. User can still edit/add/remove materials after creation

### Priority Rules

| Scenario | Behavior |
|----------|----------|
| `materials` array provided in request | Manual materials used, **no BOM fetch** |
| `sap_doc_entry` provided (no manual materials) | BOM fetched from **SAP Production Order** (WOR1 table) |
| Only `product` provided (no sap_doc_entry, no materials) | BOM fetched from **SAP Item BOM Master** (OITT/ITT1 tables) |
| Neither `sap_doc_entry` nor `product` provided | No materials created |
| SAP connection fails | Run created successfully, **no materials** (non-blocking) |

---

## 2. How It Works

```
Frontend                         Backend                              SAP HANA
───────                         ───────                              ────────

1. User selects a Product
   or SAP Production Order
          │
          ▼
2. (Optional) Preview BOM
   GET /sap/bom/?item_code=FG-001  ──►  Fetch from OITT/ITT1  ──►  Returns components
          │
          ▼
3. Create Run
   POST /runs/                     ──►  create_run()
   {                                       │
     sap_doc_entry: 12345,                 │  No manual materials?
     line_id: 1,                           │
     date: "2026-03-28",                   ▼
     product: "FG-001"                auto_populate_materials_from_bom()
   }                                       │
                                           ├── sap_doc_entry? → WOR1
                                           └── product only?  → OITT/ITT1
                                           │
                                           ▼
                                    Create ProductionMaterialUsage records
                                           │
                                           ▼
4. Response  ◄────────────────────  Run + auto-filled materials
```

---

## 3. API Endpoints

### 3.1 Preview BOM for an Item (NEW)

Preview the BOM components before creating a run. Useful for showing the user what materials will be auto-populated.

```
GET /api/v1/production-execution/sap/bom/?item_code={ItemCode}
```

**Query Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `item_code` | string | Yes | SAP Item Code of the finished good |

**Response (200):**

```json
{
  "item_code": "FG-001",
  "component_count": 3,
  "components": [
    {
      "ItemCode": "RM-001",
      "ItemName": "Refined Oil",
      "PlannedQty": 500.0,
      "UomCode": "LTR"
    },
    {
      "ItemCode": "PKG-001",
      "ItemName": "5L PET Bottle",
      "PlannedQty": 1000.0,
      "UomCode": "PCS"
    },
    {
      "ItemCode": "PKG-002",
      "ItemName": "Shrink Wrap Roll",
      "PlannedQty": 50.0,
      "UomCode": "MTR"
    }
  ]
}
```

**Error Responses:**

| Status | Condition | Response |
|--------|-----------|----------|
| 400 | Missing or empty `item_code` | `{"detail": "item_code query parameter is required."}` |
| 503 | SAP connection failure | `{"detail": "Failed to fetch BOM for item FG-001: ..."}` |

---

### 3.2 Create Production Run (UPDATED)

The existing create run endpoint now **auto-fetches BOM** when no manual materials are provided.

```
POST /api/v1/production-execution/runs/
```

**Request Body:**

```json
{
  "sap_doc_entry": 12345,
  "line_id": 1,
  "date": "2026-03-28",
  "product": "FG-001",
  "rated_speed": "150.00",
  "machine_ids": [1, 2],
  "labour_count": 5
}
```

> **Note:** Omit the `materials` field (or pass an empty array) to trigger auto-fetch.

**Response (201):**

```json
{
  "id": 42,
  "sap_doc_entry": 12345,
  "run_number": 1,
  "date": "2026-03-28",
  "product": "FG-001",
  "status": "DRAFT",
  "...": "..."
}
```

After creation, fetch materials to see the auto-populated BOM:

```
GET /api/v1/production-execution/runs/42/materials/
```

```json
[
  {
    "id": 1,
    "material_code": "RM-001",
    "material_name": "Refined Oil",
    "opening_qty": "500.000",
    "issued_qty": "100.000",
    "closing_qty": "0.000",
    "wastage_qty": "600.000",
    "uom": "LTR",
    "created_at": "2026-03-28T10:00:00Z",
    "updated_at": "2026-03-28T10:00:00Z"
  },
  {
    "id": 2,
    "material_code": "PKG-001",
    "material_name": "5L PET Bottle",
    "opening_qty": "1000.000",
    "issued_qty": "0.000",
    "closing_qty": "0.000",
    "wastage_qty": "1000.000",
    "uom": "PCS",
    "created_at": "2026-03-28T10:00:00Z",
    "updated_at": "2026-03-28T10:00:00Z"
  }
]
```

### 3.3 Existing Endpoints (unchanged)

These endpoints continue to work as before for manual material management:

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/runs/{run_id}/materials/` | List all materials for a run |
| POST | `/runs/{run_id}/materials/` | Add a material manually |
| PATCH | `/runs/{run_id}/materials/{id}/` | Update a material |
| DELETE | `/runs/{run_id}/materials/{id}/` | Delete a material |

---

## 4. Frontend Integration

### 4.1 Recommended UI Flow

```
Step 1: User selects a Product / SAP Production Order
                    │
                    ▼
Step 2: Call GET /sap/bom/?item_code=... to preview BOM
                    │
                    ▼
Step 3: Show BOM preview table to user
        ┌────────────────────────────────────────┐
        │  BOM Components for FG-001             │
        │  ┌──────────┬──────────┬───────┬─────┐ │
        │  │ Code     │ Name     │ Qty   │ UOM │ │
        │  ├──────────┼──────────┼───────┼─────┤ │
        │  │ RM-001   │ Oil      │ 500   │ LTR │ │
        │  │ PKG-001  │ Bottle   │ 1000  │ PCS │ │
        │  │ PKG-002  │ Wrap     │ 50    │ MTR │ │
        │  └──────────┴──────────┴───────┴─────┘ │
        │  These materials will be auto-added     │
        │  [Create Run]                           │
        └────────────────────────────────────────┘
                    │
                    ▼
Step 4: POST /runs/ (without materials array)
        → BOM auto-populated on backend
                    │
                    ▼
Step 5: GET /runs/{id}/materials/ to show populated materials
        → User can edit closing_qty, add more, or delete
```

### 4.2 Example: React Integration

```javascript
// Step 1: Preview BOM when user selects an item
const previewBOM = async (itemCode) => {
  const response = await api.get('/production-execution/sap/bom/', {
    params: { item_code: itemCode }
  });
  return response.data; // { item_code, component_count, components }
};

// Step 2: Create run (BOM will be auto-populated)
const createRun = async (runData) => {
  // Do NOT include 'materials' to trigger auto-fetch
  const response = await api.post('/production-execution/runs/', {
    sap_doc_entry: runData.sapDocEntry,   // optional
    line_id: runData.lineId,
    date: runData.date,
    product: runData.product,             // item code
    rated_speed: runData.ratedSpeed,
    machine_ids: runData.machineIds,
    labour_count: runData.labourCount,
  });
  return response.data;
};

// Step 3: Fetch auto-populated materials
const getMaterials = async (runId) => {
  const response = await api.get(`/production-execution/runs/${runId}/materials/`);
  return response.data;
};

// Full flow
const handleCreateRun = async () => {
  // Optional: show preview first
  const bom = await previewBOM(selectedItemCode);
  setBomPreview(bom.components);

  // Create run — materials auto-populated from BOM
  const run = await createRun(formData);

  // Fetch and display the auto-populated materials
  const materials = await getMaterials(run.id);
  setMaterials(materials);
};
```

### 4.3 Manual Override

If the user wants to provide custom materials (skip auto-fetch), include the `materials` array:

```javascript
const createRunWithManualMaterials = async (runData) => {
  const response = await api.post('/production-execution/runs/', {
    line_id: runData.lineId,
    date: runData.date,
    product: runData.product,
    materials: [
      {
        material_code: 'CUSTOM-001',
        material_name: 'Custom Material',
        opening_qty: 100,
        issued_qty: 0,
        uom: 'KG',
      }
    ],
  });
  return response.data;
};
```

---

## 5. Data Mapping

### SAP WOR1 (Production Order Components) → MaterialUsage

| SAP WOR1 Field | MaterialUsage Field | Notes |
|----------------|---------------------|-------|
| `ItemCode` | `material_code` | Component item code |
| `ItemName` | `material_name` | Component name |
| `PlannedQty` | `opening_qty` | BOM required quantity |
| `IssuedQty` | `issued_qty` | Already issued from warehouse |
| `UomCode` | `uom` | Unit of measure |
| — | `closing_qty` | Set to 0, updated during production |
| — | `wastage_qty` | Calculated: opening + issued - closing |

### SAP OITT/ITT1 (Item BOM Master) → MaterialUsage

| SAP ITT1 Field | MaterialUsage Field | Notes |
|----------------|---------------------|-------|
| `Code` | `material_code` | Component item code |
| `Name` | `material_name` | Component name |
| `Quantity` | `opening_qty` | BOM required quantity |
| — | `issued_qty` | Set to 0 (no production order context) |
| `Uom` | `uom` | Unit of measure |

---

## 6. Error Handling

### SAP Connection Failure

The BOM auto-fetch is **non-blocking**. If SAP is down:

- The production run is **still created successfully**
- Materials list will be **empty**
- A warning is logged server-side
- Frontend should handle empty materials gracefully

```javascript
const run = await createRun(formData);
const materials = await getMaterials(run.id);

if (materials.length === 0 && formData.sapDocEntry) {
  // Show info: "Could not fetch BOM from SAP. You can add materials manually."
  showWarning('BOM could not be loaded. Add materials manually.');
}
```

### BOM Preview Endpoint Errors

| Status | Action |
|--------|--------|
| 200 + empty components | Show "No BOM found for this item" |
| 400 | Show "Please select an item first" |
| 503 | Show "SAP is currently unavailable. You can still create the run." |

---

## 7. Edge Cases

| Scenario | Behavior |
|----------|----------|
| Item has no BOM in SAP | Run created, zero materials |
| SAP connection times out | Run created, zero materials (non-blocking) |
| User provides `materials: []` (empty array) | Treated as "no manual materials" → auto-fetch triggers |
| User provides manual materials AND sap_doc_entry | Manual materials used, no auto-fetch |
| Duplicate item codes in BOM | Each BOM line creates a separate material record |
| User deletes auto-fetched material then re-adds manually | Works normally via existing material CRUD endpoints |
| Run status is COMPLETED | Materials cannot be modified (existing behavior) |

---

## Quick Reference

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/sap/bom/?item_code=FG-001` | GET | Preview BOM components for an item |
| `/sap/orders/` | GET | List released SAP production orders |
| `/sap/orders/{doc_entry}/` | GET | Get production order detail + components |
| `/sap/items/?search=oil` | GET | Search SAP item master |
| `/runs/` | POST | Create run (auto-fetches BOM if no materials given) |
| `/runs/{id}/materials/` | GET | List materials (including auto-fetched) |
| `/runs/{id}/materials/` | POST | Add material manually |
| `/runs/{id}/materials/{id}/` | PATCH | Update material |
| `/runs/{id}/materials/{id}/` | DELETE | Delete material |

> All URLs are relative to the base URL: `/api/v1/production-execution/`
