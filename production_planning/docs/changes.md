# Production Planning — Change Log

All notable changes to the `production_planning` module are recorded here in reverse-chronological order.

---

## 2026-03-06

### SAP B1 Service Layer — Production Order Posting Fixes

**File:** `production_planning/services.py`
**File:** `sap_client/service_layer/production_order_writer.py`

#### Problems fixed

| # | Error | Root Cause | Fix |
|---|-------|-----------|-----|
| 1 | `Property 'PlannedStartDate' of 'ProductionOrder' is invalid` | Wrong SAP field name | Renamed to `StartDate` |
| 2 | `Property 'Status' of 'ProductionOrder' is invalid` | SAP uses enum field | Changed to `ProductionOrderStatus: "boposPlanned"` |
| 3 | `Property 'BranchID' of 'ProductionOrder' is invalid` | `BranchID` does not exist on `ProductionOrders` entity | Removed from payload |
| 4 | `DocNum` and `DocEntry` were `None` after successful post | SAP response uses `DocumentNumber` and `AbsoluteEntry`, not `DocNum`/`DocEntry` | Writer now maps `AbsoluteEntry → DocEntry` and `DocumentNumber → DocNum` |
| 5 | `ProductionOrderLines` causing incorrect data | SAP auto-creates BOM lines from the item's production BOM — sending them manually overrides SAP's BOM | Removed `ProductionOrderLines` from payload |

#### Correct SAP B1 Service Layer payload (POST /b1s/v2/ProductionOrders)

```json
{
  "ItemNo": "FG0000004",
  "PlannedQuantity": 1000.0,
  "DueDate": "2026-03-06",
  "StartDate": "2026-03-06",
  "ProductionOrderStatus": "boposPlanned",
  "Remarks": "Optional remarks",
  "Warehouse": "WH-01"
}
```

> **Note:** `ProductionOrderLines` are intentionally omitted. SAP B1 automatically
> populates BOM components from the item's production BOM (OITT/ITT1) when `ItemNo`
> is provided. Do not override them.

#### DueDate restriction

SAP B1 enforces a **Posting Date Range** at the company level. If the DueDate falls
outside this range, SAP returns:

```
Date deviates from due date range [OWOR.DueDate]
```

**Resolution:** In SAP B1 → *Administration → System Initialization → Company Details
→ Posting Periods*, extend the allowed range to include the target dates.

---

### SAP HANA — Finished Goods Filter Fix

**File:** `production_planning/sap/item_reader.py`

#### Problem
`?type=finished` was using `SellItem = 'Y'` which matched raw materials and
semi-finished items that can also be sold. BOM components were empty when those
items were selected.

#### Fix
Changed the filter to check if the item has a **production BOM defined in OITT**:

```sql
-- Old (wrong — includes raw materials)
WHERE T0."SellItem" = 'Y'

-- New (correct — only items with a production BOM)
WHERE EXISTS (SELECT 1 FROM "schema"."OITT" B WHERE B."Code" = T0."ItemCode")
```

The `make_item` flag in the response now reflects true BOM existence.

---

### SAP HANA — OITM Column Name Fixes

**File:** `production_planning/sap/item_reader.py`

| Wrong column | Correct column | Table |
|---|---|---|
| `T0."ItmsGrpNam"` | `T1."ItmsGrpNam"` via `LEFT JOIN OITB` | `OITM` does not have this column; it's on `OITB` |
| `T0."MakeItem"` | Replaced with `EXISTS(OITT)` subquery | `MakeItem` column does not exist in this SAP B1 installation |

**UoM table column names** (`OUOM`):

| Wrong | Correct |
|---|---|
| `"Code"` | `"UomCode"` |
| `"Name"` | `"UomName"` |

---

### SAP HANA — BOM Reader (New Feature)

**File:** `production_planning/sap/bom_reader.py` *(new)*

Reads production BOM from SAP HANA and calculates material requirements + shortages.

**Actual ITT1 column names** (discovered by querying `SYS.TABLE_COLUMNS`):

| Field | Wrong assumption | Actual column |
|---|---|---|
| Component item code | `ItemCode` | `Father` |
| Quantity per unit | `Qty` | `Quantity` |
| Unit of measure | — | `Uom` (directly on ITT1) |
| Component name | — | `ItemName` (directly on ITT1) |

**Tables used:**

| Table | Purpose |
|---|---|
| `OITT` | BOM header — keyed by finished-good `Code` |
| `ITT1` | BOM lines — `Father` = component, `Quantity` = qty/unit |
| `OITM` | Component item master — `OnHand` for total stock |

---

### New API Endpoint — BOM Auto-Detect

**File:** `production_planning/views.py`, `urls.py`, `serializers.py`

```
GET /api/v1/production-planning/dropdown/bom/
    ?item_code=FG0000004
    &planned_qty=1000
```

Returns BOM components scaled to `planned_qty` with `available_stock` and
`shortage_qty` calculated from live SAP HANA data. See `docs/frontend.md § 5.4`
for full API contract.

---

### Module Redesign (Earlier in Session)

**Files:** `models.py`, `services.py`, `serializers.py`, `views.py`, `permissions.py`,
`migrations/0003_redesign_production_plan.py`

#### Key design changes

- **Plan lifecycle:** `DRAFT → OPEN (SAP posted) → IN_PROGRESS (weekly plan added) → COMPLETED`
- **SAP sync status:** `NOT_POSTED / POSTED / FAILED` tracked separately from plan status
- Plans are now created **locally first** (DRAFT), then **posted to SAP** explicitly via `POST /<id>/post-to-sap/`
- Removed `imported_by`, `imported_at`, `customer_code`, `customer_name`, `issued_qty`
- Added `sap_posting_status`, `sap_error_message`, `uom`, `warehouse_code`, `created_by`
- `@transaction.atomic` removed from `post_to_sap` — the decorator was rolling back the `FAILED` status save when the SAP exception was re-raised

#### Test fixes

- `Token` auth (DRF authtoken) replaced with `force_authenticate` + `Company-Code` header — project uses JWT (`rest_framework_simplejwt`)
- `make_user()` updated to use email-based `create_user(email, password, full_name, employee_code)`
- Mock paths changed from `production_planning.services.HanaItemReader` to `production_planning.sap.item_reader.HanaItemReader` (local import inside method body)
- `CompanyContext` also mocked in dropdown tests to prevent SAP registry lookup

**Test result:** 92/92 passing (`python manage.py test production_planning --keepdb`)
