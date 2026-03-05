# Quality Control API Documentation

## Overview

This module handles the quality control workflow for raw material inspection at Jivo Wellness factory gates.

## Data Flow

```
POItemReceipt
      ↓ (OneToOne)
MaterialArrivalSlip (Security Guard fills)
      ↓ (OneToOne)
RawMaterialInspection (QA fills)
      ↓ (ForeignKey)
InspectionParameterResult (Dynamic parameters)
```

## Authentication

All endpoints require JWT authentication. Include the token in the Authorization header:
```
Authorization: Bearer <access_token>
```

All endpoints also require the `Company-Code` header:
```
Company-Code: <company_code>
```

---

## Permissions

The API uses Django's permission-based access control. Assign these permissions to users via Django Admin or programmatically.

### Available Permissions

| Permission | Codename | Description |
|------------|----------|-------------|
| Can create arrival slip | `can_create_arrival_slip` | Create material arrival slips |
| Can edit arrival slip | `can_edit_arrival_slip` | Edit material arrival slips |
| Can submit arrival slip | `can_submit_arrival_slip` | Submit arrival slips to QA |
| Can view arrival slip | `can_view_arrival_slip` | View material arrival slips |
| Can create inspection | `can_create_inspection` | Create raw material inspections |
| Can edit inspection | `can_edit_inspection` | Edit raw material inspections |
| Can submit inspection | `can_submit_inspection` | Submit inspections for approval |
| Can view inspection | `can_view_inspection` | View raw material inspections |
| Can approve as chemist | `can_approve_as_chemist` | Approve as QA Chemist |
| Can approve as QAM | `can_approve_as_qam` | Approve as QA Manager |
| Can reject inspection | `can_reject_inspection` | Reject inspections |
| Can manage material types | `can_manage_material_types` | Manage material type master data |
| Can manage QC parameters | `can_manage_qc_parameters` | Manage QC parameter definitions |

---

## API Endpoints

### Material Type APIs

#### List/Create Material Types
```
GET  /api/v1/quality-control/material-types/
POST /api/v1/quality-control/material-types/
```

**Permission:** `can_manage_material_types`

**POST Request Body:**
```json
{
    "code": "CAP_BLUE",
    "name": "Cap Blue Plain",
    "description": "Blue plain caps for water bottles"
}
```

#### Get/Update/Delete Material Type
```
GET    /api/v1/quality-control/material-types/{material_type_id}/
PUT    /api/v1/quality-control/material-types/{material_type_id}/
DELETE /api/v1/quality-control/material-types/{material_type_id}/
```

---

### QC Parameter APIs

#### List/Create QC Parameters
```
GET  /api/v1/quality-control/material-types/{material_type_id}/parameters/
POST /api/v1/quality-control/material-types/{material_type_id}/parameters/
```

**Permission:** `can_manage_qc_parameters`

**POST Request Body:**
```json
{
    "parameter_code": "WEIGHT",
    "parameter_name": "Weight",
    "standard_value": "1.35±0.10",
    "parameter_type": "RANGE",
    "min_value": 1.25,
    "max_value": 1.45,
    "uom": "grams",
    "sequence": 1,
    "is_mandatory": true
}
```

**Parameter Types:**
- `NUMERIC` - Single numeric value
- `TEXT` - Text/descriptive value
- `BOOLEAN` - Pass/Fail
- `RANGE` - Numeric range with min/max validation

#### Get/Update/Delete QC Parameter
```
GET    /api/v1/quality-control/parameters/{parameter_id}/
PUT    /api/v1/quality-control/parameters/{parameter_id}/
DELETE /api/v1/quality-control/parameters/{parameter_id}/
```

---

### Material Arrival Slip APIs

#### List All Arrival Slips
```
GET /api/v1/quality-control/arrival-slips/
```

**Permission:** `can_view_arrival_slip`

**Query Parameters:**
- `status` - Filter by status (DRAFT, SUBMITTED, REJECTED)

#### Create/Update/Get Arrival Slip for PO Item
```
GET  /api/v1/quality-control/po-items/{po_item_id}/arrival-slip/
POST /api/v1/quality-control/po-items/{po_item_id}/arrival-slip/
```

**Permission:** `can_create_arrival_slip` or `can_edit_arrival_slip` (POST), `can_view_arrival_slip` (GET)

**POST Request Body:**
```json
{
    "particulars": "Cap Blue Plain (Water)",
    "arrival_datetime": "2026-02-03T10:30:00Z",
    "weighing_required": false,
    "party_name": "V Form Techno Packs Ltd",
    "billing_qty": 1500000,
    "billing_uom": "Pcs",
    "truck_no_as_per_bill": "HR46F-3009",
    "commercial_invoice_no": "3266",
    "eway_bill_no": "393180169231",
    "bilty_no": "3158",
    "has_certificate_of_analysis": true,
    "has_certificate_of_quantity": true,
    "remarks": ""
}
```

**Response:**
```json
{
    "id": 1,
    "po_item_receipt": 1,
    "po_item_code": "ITEM-001",
    "item_name": "Cap Blue Plain",
    "po_receipt_id": 1,
    "vehicle_entry_id": 1,
    "entry_no": "GE-20260204-0001",
    "particulars": "Cap Blue Plain (Water)",
    "arrival_datetime": "2026-02-03T10:30:00Z",
    "weighing_required": false,
    "party_name": "V Form Techno Packs Ltd",
    "billing_qty": "1500000.000",
    "billing_uom": "Pcs",
    "in_time_to_qa": null,
    "truck_no_as_per_bill": "HR46F-3009",
    "commercial_invoice_no": "3266",
    "eway_bill_no": "393180169231",
    "bilty_no": "3158",
    "has_certificate_of_analysis": true,
    "has_certificate_of_quantity": true,
    "status": "DRAFT",
    "is_submitted": false,
    "submitted_at": null,
    "submitted_by": null,
    "submitted_by_name": null,
    "remarks": "",
    "attachments": [],
    "created_at": "2026-02-04T10:00:00Z",
    "updated_at": "2026-02-04T10:00:00Z"
}
```

#### Get Arrival Slip by ID
```
GET /api/v1/quality-control/arrival-slips/{slip_id}/
```

**Permission:** `can_view_arrival_slip`

#### Submit Arrival Slip to QA
```
POST /api/v1/quality-control/arrival-slips/{slip_id}/submit/
```

**Content-Type:** `multipart/form-data`

**Permission:** `can_submit_arrival_slip`

**Form Fields (files):**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `certificate_of_analysis` | file | Conditional | Required if `has_certificate_of_analysis` is `true` on the arrival slip |
| `certificate_of_quantity` | file | Conditional | Required if `has_certificate_of_quantity` is `true` on the arrival slip |

Both attachments are optional by default. They become required only when the corresponding boolean flag was set to `true` during arrival slip creation/update. Any file format is accepted.

On resubmission (after rejection), existing attachments of the same type are replaced.

**Response:** Returns the updated arrival slip with `status: "SUBMITTED"` and `attachments` array:
```json
{
    "id": 1,
    "status": "SUBMITTED",
    "attachments": [
        {
            "id": 1,
            "file": "/media/arrival_slip_attachments/coa_report.pdf",
            "attachment_type": "CERTIFICATE_OF_ANALYSIS",
            "uploaded_at": "2026-02-20T10:30:00Z"
        },
        {
            "id": 2,
            "file": "/media/arrival_slip_attachments/coq_report.pdf",
            "attachment_type": "CERTIFICATE_OF_QUANTITY",
            "uploaded_at": "2026-02-20T10:30:00Z"
        }
    ],
    "..."
}
```

**Error Responses:**

| Status | Message |
|--------|---------|
| 400 | `Already submitted` |
| 400 | `Certificate of Analysis attachment is required when has_certificate_of_analysis is true.` |
| 400 | `Certificate of Quantity attachment is required when has_certificate_of_quantity is true.` |

---

### Raw Material Inspection APIs

#### List Pending Arrival Slips for QA Inspection
```
GET /api/v1/quality-control/inspections/pending/
```

**Permission:** `can_view_inspection`

**Response (list item):**
```json
[
    {
        "arrival_slip_id": 1,
        "inspection_id": 1,
        "entry_no": "GE-20260204-0001",
        "report_no": "RPT-20260203-0001",
        "internal_lot_no": "LOT-20260203-0001",
        "item_name": "Cap Blue Plain",
        "party_name": "V Form Techno Packs Ltd",
        "billing_qty": "1500000.000",
        "billing_uom": "Pcs",
        "workflow_status": "SUBMITTED",
        "final_status": "PENDING",
        "material_type_name": "Cap Blue Plain",
        "created_at": "2026-02-04T10:00:00Z",
        "submitted_at": "2026-02-04T10:10:00Z"
    }
]
```
`internal_lot_no` is `null` when inspection is not created yet.

#### Create/Update/Get Inspection for Arrival Slip
```
GET  /api/v1/quality-control/arrival-slips/{slip_id}/inspection/
POST /api/v1/quality-control/arrival-slips/{slip_id}/inspection/
```

**Permission:** `can_create_inspection` or `can_edit_inspection` (POST), `can_view_inspection` (GET)

**POST Request Body:**
```json
{
    "inspection_date": "2026-02-03",
    "description_of_material": "Cap Blue Plain (Water)",
    "sap_code": "CAP-001",
    "supplier_name": "V Form Techno Packs Ltd",
    "manufacturer_name": "V Form Techno Packs Ltd",
    "supplier_batch_lot_no": "B2026-001",
    "unit_packing": "1500000 Pcs",
    "purchase_order_no": "PO-2026-001",
    "invoice_bill_no": "3266",
    "vehicle_no": "HR46F-3009",
    "material_type_id": 1,
    "remarks": ""
}
```

**Response:**
```json
{
    "id": 1,
    "arrival_slip": 1,
    "arrival_slip_id": 1,
    "arrival_slip_status": "SUBMITTED",
    "po_item_receipt_id": 1,
    "po_item_code": "ITEM-001",
    "item_name": "Cap Blue Plain",
    "vehicle_entry_id": 1,
    "entry_no": "GE-20260204-0001",
    "report_no": "RPT-20260203-0001",
    "internal_lot_no": "LOT-20260203-0001",
    "inspection_date": "2026-02-03",
    "description_of_material": "Cap Blue Plain (Water)",
    "sap_code": "CAP-001",
    "supplier_name": "V Form Techno Packs Ltd",
    "manufacturer_name": "V Form Techno Packs Ltd",
    "supplier_batch_lot_no": "B2026-001",
    "unit_packing": "1500000 Pcs",
    "purchase_order_no": "PO-2026-001",
    "invoice_bill_no": "3266",
    "vehicle_no": "HR46F-3009",
    "material_type": 1,
    "material_type_name": "Cap Blue Plain",
    "final_status": "PENDING",
    "qa_chemist": null,
    "qa_chemist_name": null,
    "qa_chemist_approved_at": null,
    "qa_chemist_remarks": "",
    "qam": null,
    "qam_name": null,
    "qam_approved_at": null,
    "qam_remarks": "",
    "workflow_status": "DRAFT",
    "is_locked": false,
    "remarks": "",
    "parameter_results": [
        {
            "id": 1,
            "parameter_master": 1,
            "parameter_code": "WEIGHT",
            "parameter_name": "Weight",
            "standard_value": "1.35±0.10",
            "parameter_type": "RANGE",
            "min_value": "1.2500",
            "max_value": "1.4500",
            "uom": "grams",
            "result_value": "",
            "result_numeric": null,
            "is_within_spec": null,
            "remarks": ""
        }
    ],
    "created_at": "2026-02-04T10:00:00Z",
    "updated_at": "2026-02-04T10:00:00Z"
}
```

#### Get Inspection by ID
```
GET /api/v1/quality-control/inspections/{inspection_id}/
```

**Permission:** `can_view_inspection`

#### Update Parameter Results
```
GET  /api/v1/quality-control/inspections/{inspection_id}/parameters/
POST /api/v1/quality-control/inspections/{inspection_id}/parameters/
```

**Permission:** `can_create_inspection` or `can_edit_inspection`

**POST Request Body:**
```json
{
    "results": [
        {
            "parameter_master_id": 1,
            "result_value": "Avg-1.40",
            "result_numeric": 1.40,
            "is_within_spec": true,
            "remarks": ""
        },
        {
            "parameter_master_id": 2,
            "result_value": "Same",
            "is_within_spec": true,
            "remarks": ""
        }
    ]
}
```

#### Submit Inspection for Approval
```
POST /api/v1/quality-control/inspections/{inspection_id}/submit/
```

**Permission:** `can_submit_inspection`

**Response:** Returns full updated inspection object (same shape as create/get inspection response), including `report_no` and `internal_lot_no`.
```json
{
    "id": 1,
    "report_no": "RPT-20260203-0001",
    "internal_lot_no": "LOT-20260203-0001",
    "workflow_status": "SUBMITTED",
    "final_status": "PENDING"
}
```

---

### Approval APIs

#### QA Chemist Approval
```
POST /api/v1/quality-control/inspections/{inspection_id}/approve/chemist/
```

**Permission:** `can_approve_as_chemist`

**Request Body:**
```json
{
    "remarks": "Approved by QA Chemist"
}
```

**Response:** Returns full updated inspection object, including `internal_lot_no`.
```json
{
    "id": 1,
    "report_no": "RPT-20260203-0001",
    "internal_lot_no": "LOT-20260203-0001",
    "workflow_status": "QA_CHEMIST_APPROVED",
    "final_status": "PENDING"
}
```

#### QA Manager Approval
```
POST /api/v1/quality-control/inspections/{inspection_id}/approve/qam/
```

**Permission:** `can_approve_as_qam`

**Request Body:**
```json
{
    "remarks": "Approved by QA Manager",
    "final_status": "ACCEPTED"
}
```

**Final Status Options:**
- `ACCEPTED` - Material accepted
- `REJECTED` - Material rejected
- `HOLD` - Material on hold

**Response:** Returns full updated inspection object, including `internal_lot_no`.
```json
{
    "id": 1,
    "report_no": "RPT-20260203-0001",
    "internal_lot_no": "LOT-20260203-0001",
    "workflow_status": "QAM_APPROVED",
    "final_status": "ACCEPTED",
    "is_locked": true
}
```

#### Reject Inspection
```
POST /api/v1/quality-control/inspections/{inspection_id}/reject/
```

**Permission:** `can_reject_inspection`

**Request Body:**
```json
{
    "remarks": "Rejected due to quality issues"
}
```

**Response:** Returns full updated inspection object, including `internal_lot_no`, and sends back to security guard.
```json
{
    "id": 1,
    "report_no": "RPT-20260203-0001",
    "internal_lot_no": "LOT-20260203-0001",
    "workflow_status": "REJECTED",
    "final_status": "REJECTED",
    "is_locked": true
}
```

---

## Workflow Status States

### Arrival Slip Status
| Status | Description |
|--------|-------------|
| `DRAFT` | Initial state, can be edited |
| `SUBMITTED` | Submitted to QA |
| `REJECTED` | Rejected by QA, sent back to security |

### Inspection Workflow Status
| Status | Description |
|--------|-------------|
| `DRAFT` | Initial state, can be edited |
| `SUBMITTED` | Submitted for QA Chemist approval |
| `QA_CHEMIST_APPROVED` | Approved by QA Chemist, awaiting QAM |
| `QAM_APPROVED` | Approved by QA Manager |
| `COMPLETED` | Workflow completed |

### Inspection Final Status
| Status | Description |
|--------|-------------|
| `PENDING` | Awaiting inspection result |
| `ACCEPTED` | Material accepted |
| `REJECTED` | Material rejected |
| `HOLD` | Material on hold |

---

## Complete Workflow

```
1. Security Guard creates Arrival Slip for each PO item
   POST /po-items/{id}/arrival-slip/

2. Security Guard submits to QA
   POST /arrival-slips/{id}/submit/
   → Gate Entry status: ARRIVAL_SLIP_SUBMITTED

3. QA creates Inspection linked to Arrival Slip
   POST /arrival-slips/{id}/inspection/
   → Gate Entry status: QC_PENDING

4. QA fills parameter results
   POST /inspections/{id}/parameters/

5. QA submits for approval
   POST /inspections/{id}/submit/
   → Gate Entry status: QC_IN_REVIEW

6. QA Chemist approves
   POST /inspections/{id}/approve/chemist/
   → Gate Entry status: QC_AWAITING_QAM

7. QA Manager approves
   POST /inspections/{id}/approve/qam/
   → Gate Entry status: QC_COMPLETED (when all items approved)

8. Security Guard completes gate entry
```

### Rejection Flow
```
QA rejects inspection
POST /inspections/{id}/reject/
→ Inspection: final_status=REJECTED, is_locked=true
→ Arrival Slip: status=REJECTED, is_submitted=false
→ Gate Entry status: ARRIVAL_SLIP_REJECTED

Security Guard updates arrival slip and resubmits
POST /po-items/{id}/arrival-slip/
POST /arrival-slips/{id}/submit/
→ Workflow restarts
```

---

## Error Responses

### 400 Bad Request
```json
{
    "detail": "Arrival slip already submitted"
}
```

### 403 Forbidden
```json
{
    "detail": "You do not have permission to perform this action."
}
```

### 404 Not Found
```json
{
    "detail": "Not found."
}
```
