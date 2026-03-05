# Quality Control - API Endpoints

Base URL: `/api/v1/quality-control/`

All endpoints require `Authorization: Bearer <token>` header and `Company-Code: <code>` header.

---

## Material Type APIs

### List / Create Material Types
```
GET  /material-types/
POST /material-types/
```
**Permission:** `CanManageMaterialTypes`

**POST Body:**
```json
{
    "code": "RM-STEEL",
    "name": "Steel Raw Material",
    "description": "Carbon steel plates"
}
```

### Get / Update / Delete Material Type
```
GET    /material-types/{material_type_id}/
PUT    /material-types/{material_type_id}/
DELETE /material-types/{material_type_id}/
```
**Permission:** `CanManageMaterialTypes`
DELETE is soft-delete (sets `is_active=False`).

---

## QC Parameter APIs

### List / Create Parameters for a Material Type
```
GET  /material-types/{material_type_id}/parameters/
POST /material-types/{material_type_id}/parameters/
```
**Permission:** `CanManageQCParameters`

**POST Body:**
```json
{
    "parameter_name": "Tensile Strength",
    "parameter_code": "TS-001",
    "standard_value": "400 MPa",
    "parameter_type": "NUMERIC",
    "min_value": 380.0,
    "max_value": 420.0,
    "uom": "MPa",
    "sequence": 1,
    "is_mandatory": true
}
```

### Get / Update / Delete Parameter
```
GET    /parameters/{parameter_id}/
PUT    /parameters/{parameter_id}/
DELETE /parameters/{parameter_id}/
```
**Permission:** `CanManageQCParameters`

---

## Material Arrival Slip APIs

### List Arrival Slips
```
GET /arrival-slips/
GET /arrival-slips/?status=SUBMITTED
GET /arrival-slips/?status=REJECTED
```
**Permission:** `CanViewArrivalSlip`

**Query Params:**
| Param    | Type   | Required | Values                          |
|----------|--------|----------|---------------------------------|
| `status` | string | No       | `DRAFT`, `SUBMITTED`, `REJECTED` |

### Create / Update Arrival Slip for PO Item
```
GET  /po-items/{po_item_id}/arrival-slip/
POST /po-items/{po_item_id}/arrival-slip/
```
**Permission:** `CanManageArrivalSlip`

**POST Body:**
```json
{
    "particulars": "Steel plates 10mm",
    "arrival_datetime": "2026-02-13T10:30:00Z",
    "weighing_required": true,
    "party_name": "ABC Steel Pvt Ltd",
    "billing_qty": 5000.000,
    "billing_uom": "KG",
    "truck_no_as_per_bill": "MH12AB1234",
    "commercial_invoice_no": "INV-2026-001",
    "eway_bill_no": "EWB123456",
    "bilty_no": "BLT-001",
    "has_certificate_of_analysis": true,
    "has_certificate_of_quantity": false,
    "remarks": ""
}
```

### Get Arrival Slip by ID
```
GET /arrival-slips/{slip_id}/
```
**Permission:** `CanViewArrivalSlip`

### Submit Arrival Slip to QA
```
POST /arrival-slips/{slip_id}/submit/
```
**Permission:** `CanSubmitArrivalSlip`

**Content-Type:** `multipart/form-data`

**Form Fields (files):**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `certificate_of_analysis` | file | Conditional | Required if `has_certificate_of_analysis` is `true` on the arrival slip |
| `certificate_of_quantity` | file | Conditional | Required if `has_certificate_of_quantity` is `true` on the arrival slip |

Both attachments are optional by default. They become required only when the corresponding boolean flag was set to `true` during arrival slip creation/update. Any file format is accepted.

On resubmission (after rejection), existing attachments of the same type are replaced.

**Updates:**
- Arrival slip status -> `SUBMITTED`
- Vehicle entry status -> `ARRIVAL_SLIP_SUBMITTED`

**Error Responses:**

| Status | Message |
|--------|---------|
| 400 | `Already submitted` |
| 400 | `Certificate of Analysis attachment is required when has_certificate_of_analysis is true.` |
| 400 | `Certificate of Quantity attachment is required when has_certificate_of_quantity is true.` |

---

## Inspection List APIs (Status-Based)

### List All Inspections (Master List)
```
GET /inspections/
GET /inspections/?workflow_status=SUBMITTED
GET /inspections/?final_status=REJECTED
GET /inspections/?from_date=2026-01-01&to_date=2026-02-13
GET /inspections/?workflow_status=QAM_APPROVED&final_status=ACCEPTED
```
**Permission:** `CanViewInspection`

**Query Params:**
| Param             | Type   | Required | Values                                                    |
|-------------------|--------|----------|-----------------------------------------------------------|
| `workflow_status` | string | No       | `DRAFT`, `SUBMITTED`, `QA_CHEMIST_APPROVED`, `QAM_APPROVED`, `REJECTED` |
| `final_status`    | string | No       | `PENDING`, `ACCEPTED`, `REJECTED`, `HOLD`                 |
| `from_date`       | date   | No       | `YYYY-MM-DD` format                                       |
| `to_date`         | date   | No       | `YYYY-MM-DD` format                                       |

All filters are optional and can be combined.

### List Pending Inspections (Arrival Slips awaiting QC)
```
GET /inspections/pending/
```
**Permission:** `CanViewInspection`

Returns arrival slips that are `SUBMITTED` or `REJECTED`, with their inspection status.
Excludes fully approved (`QAM_APPROVED`) inspections.

**Response:**
```json
[
    {
        "arrival_slip_id": 1,
        "inspection_id": 1,
        "entry_no": "GE-20260204-0001",
        "report_no": "RPT-20260203-0001",
        "internal_lot_no": "LOT-20260203-0001",
        "item_name": "Steel Raw Material",
        "party_name": "ABC Steel Pvt Ltd",
        "billing_qty": "5000.000",
        "billing_uom": "KG",
        "workflow_status": "SUBMITTED",
        "final_status": "PENDING",
        "material_type_name": "Steel Raw Material",
        "created_at": "2026-02-13T10:00:00Z",
        "submitted_at": "2026-02-13T10:30:00Z"
    }
]
```
`internal_lot_no` is `null` when inspection is not created yet.

### List Inspections Awaiting Chemist Approval
```
GET /inspections/awaiting-chemist/
```
**Permission:** `CanApproveAsChemist`

Returns inspections with `workflow_status = SUBMITTED`.
This is the QA Chemist's work queue.

### List Inspections Awaiting QAM Approval
```
GET /inspections/awaiting-qam/
```
**Permission:** `CanApproveAsQAM`

Returns inspections with `workflow_status = QA_CHEMIST_APPROVED`.
This is the QA Manager's work queue.

### List Completed Inspections
```
GET /inspections/completed/
GET /inspections/completed/?final_status=ACCEPTED
GET /inspections/completed/?final_status=REJECTED
```
**Permission:** `CanViewInspection`

Returns inspections with `workflow_status = QAM_APPROVED`.

**Query Params:**
| Param          | Type   | Required | Values                            |
|----------------|--------|----------|-----------------------------------|
| `final_status` | string | No       | `ACCEPTED`, `REJECTED`, `HOLD`    |

### List Rejected Inspections
```
GET /inspections/rejected/
```
**Permission:** `CanViewInspection`

Returns inspections with `workflow_status = REJECTED`.
Includes `rejected_by`, `rejected_by_name`, and `rejected_at` in response.

---

## Inspection CRUD APIs

### Create / Update Inspection for Arrival Slip
```
GET  /arrival-slips/{slip_id}/inspection/
POST /arrival-slips/{slip_id}/inspection/
```
**Permission:** `CanManageInspection`

Arrival slip must be in `SUBMITTED` status. Uses get-or-create pattern.

**POST Body:**
```json
{
    "inspection_date": "2026-02-13",
    "description_of_material": "Carbon Steel Plate 10mm",
    "sap_code": "MAT-001",
    "supplier_name": "ABC Steel Pvt Ltd",
    "manufacturer_name": "XYZ Manufacturing",
    "supplier_batch_lot_no": "BATCH-2026-001",
    "unit_packing": "Bundle",
    "purchase_order_no": "PO-2026-0001",
    "invoice_bill_no": "INV-001",
    "vehicle_no": "MH12AB1234",
    "material_type_id": 1,
    "remarks": ""
}
```

Auto-generates `report_no` and `internal_lot_no`.
If `material_type_id` is provided, auto-creates parameter results from master.

### Get Inspection by ID
```
GET /inspections/{inspection_id}/
```
**Permission:** `CanViewInspection`

### Update Parameter Results
```
GET  /inspections/{inspection_id}/parameters/
POST /inspections/{inspection_id}/parameters/
```
**Permission:** `CanManageInspection`

**POST Body:**
```json
{
    "results": [
        {
            "parameter_master_id": 1,
            "result_value": "405",
            "result_numeric": 405.0000,
            "is_within_spec": true,
            "remarks": ""
        }
    ]
}
```

---

## Workflow Action APIs

### Submit Inspection for Chemist Review
```
POST /inspections/{inspection_id}/submit/
```
**Permission:** `CanSubmitInspection`

No body required. Validates all mandatory parameters have results.

**Response:** Returns full updated `RawMaterialInspection` object, including `report_no` and `internal_lot_no`.
```json
{
    "id": 1,
    "report_no": "RPT-20260213-0001",
    "internal_lot_no": "LOT-20260213-0001",
    "workflow_status": "SUBMITTED",
    "final_status": "PENDING"
}
```

**Status Changes:**
- Inspection: `workflow_status` DRAFT -> SUBMITTED
- Vehicle Entry: updated via `update_entry_status()`

### QA Chemist Approval
```
POST /inspections/{inspection_id}/approve/chemist/
```
**Permission:** `CanApproveAsChemist`

**Body:**
```json
{
    "remarks": "Results verified and acceptable"
}
```

**Response:** Returns full updated `RawMaterialInspection` object, including `internal_lot_no`.
```json
{
    "id": 1,
    "report_no": "RPT-20260213-0001",
    "internal_lot_no": "LOT-20260213-0001",
    "workflow_status": "QA_CHEMIST_APPROVED",
    "final_status": "PENDING"
}
```

**Status Changes:**
- Inspection: `workflow_status` SUBMITTED -> QA_CHEMIST_APPROVED
- Vehicle Entry: updated via `update_entry_status()`

### QA Manager Approval
```
POST /inspections/{inspection_id}/approve/qam/
```
**Permission:** `CanApproveAsQAM`

**Body:**
```json
{
    "remarks": "Approved for production use",
    "final_status": "ACCEPTED"
}
```

| final_status | Description               |
|--------------|---------------------------|
| `ACCEPTED`   | Material accepted (default) |
| `REJECTED`   | Material rejected          |
| `HOLD`       | Material on hold           |

**Response:** Returns full updated `RawMaterialInspection` object, including `internal_lot_no`.
```json
{
    "id": 1,
    "report_no": "RPT-20260213-0001",
    "internal_lot_no": "LOT-20260213-0001",
    "workflow_status": "QAM_APPROVED",
    "final_status": "ACCEPTED",
    "is_locked": true
}
```

**Status Changes:**
- Inspection: `workflow_status` QA_CHEMIST_APPROVED -> QAM_APPROVED, `is_locked = True`
- Vehicle Entry: updated via `update_entry_status()` (-> `QC_COMPLETED` if all items done)

### Reject Inspection
```
POST /inspections/{inspection_id}/reject/
```
**Permission:** `CanRejectInspection`

**Body:**
```json
{
    "remarks": "Tensile strength below acceptable range"
}
```

**Response:** Returns full updated `RawMaterialInspection` object, including `internal_lot_no`.
```json
{
    "id": 1,
    "report_no": "RPT-20260213-0001",
    "internal_lot_no": "LOT-20260213-0001",
    "workflow_status": "REJECTED",
    "final_status": "REJECTED",
    "is_locked": true
}
```

**Allowed from:** `SUBMITTED` or `QA_CHEMIST_APPROVED` workflow status.

**Status Changes:**
- Inspection: `workflow_status` -> REJECTED, `final_status` -> REJECTED, `is_locked = True`
- Arrival Slip: `status` -> REJECTED
- Vehicle Entry: updated via `update_entry_status()`
- Tracks: `rejected_by`, `rejected_at`

---

## Permissions Reference

| Permission                          | Used By                          |
|-------------------------------------|----------------------------------|
| `can_manage_material_types`         | Material Type CRUD               |
| `can_manage_qc_parameters`          | QC Parameter CRUD                |
| `view_materialarrivalslip`          | List/view arrival slips          |
| `add/change_materialarrivalslip`    | Create/edit arrival slips        |
| `can_submit_arrival_slip`           | Submit arrival slip to QA        |
| `view_rawmaterialinspection`        | List/view inspections            |
| `add/change_rawmaterialinspection`  | Create/edit inspections          |
| `can_submit_inspection`             | Submit for chemist review        |
| `can_approve_as_chemist`            | Chemist approval + queue         |
| `can_approve_as_qam`               | QAM approval + queue             |
| `can_reject_inspection`             | Reject inspection                |
