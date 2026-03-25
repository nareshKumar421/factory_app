# Outbound Dispatch — SAP Integration Reference

This document describes exactly what the `outbound_dispatch` app **reads from** and **writes to** SAP Business One via HANA and the Service Layer.

---

## 1. Reading from SAP HANA (Sales Orders)

**Purpose:** Sync open Sales Orders into Django as `ShipmentOrder` records.

**Triggered by:** `POST /api/v1/outbound/shipments/sync/`

**SAP Tables Queried:**

| Alias | Table  | Description |
|-------|--------|-------------|
| T0    | `ORDR` | Sales Order header |
| T1    | `RDR1` | Sales Order line items |

### HANA SQL Query

```sql
SELECT
    T0."DocEntry"           AS doc_entry,
    T0."DocNum"             AS doc_num,
    T0."CardCode"           AS customer_code,
    T0."CardName"           AS customer_name,
    T0."Address2"           AS ship_to_address,
    T0."DocDueDate"         AS due_date,
    T0."BPLId"              AS branch_id,
    T0."Comments"           AS comments,
    T1."LineNum"            AS line_num,
    T1."ItemCode"           AS item_code,
    T1."Dscription"         AS item_name,
    T1."Quantity"           AS ordered_qty,
    T1."DelivrdQty"         AS delivered_qty,
    (T1."Quantity" - T1."DelivrdQty") AS remaining_qty,
    IFNULL(T1."unitMsr", '') AS uom,
    IFNULL(T1."WhsCode", '') AS warehouse_code
FROM "{schema}"."ORDR" T0
INNER JOIN "{schema}"."RDR1" T1
    ON T0."DocEntry" = T1."DocEntry"
WHERE T0."DocStatus" = 'O'
    AND T0."CANCELED" = 'N'
    AND (T1."Quantity" - T1."DelivrdQty") > 0
ORDER BY T0."DocDueDate" ASC, T0."DocNum" ASC, T1."LineNum" ASC
```

### Filters (WHERE clauses)

| Condition | Always Applied? | Description |
|-----------|-----------------|-------------|
| `DocStatus = 'O'` | Yes | Only open (not closed/cancelled) orders |
| `CANCELED = 'N'` | Yes | Exclude cancelled orders |
| `Quantity - DelivrdQty > 0` | Yes | Only lines with remaining quantity |
| `CardCode = ?` | Optional | Filter by specific customer BP code |
| `DocDueDate >= ?` | Optional | Filter from date |
| `DocDueDate <= ?` | Optional | Filter to date |
| `ItemCode = ?` | Optional | Filter by specific item code |

### Fields Read → Django Mapping

| HANA Field (ORDR) | Django Field (`ShipmentOrder`) |
|--------------------|-------------------------------|
| `DocEntry` | `sap_doc_entry` |
| `DocNum` | `sap_doc_num` |
| `CardCode` | `customer_code` |
| `CardName` | `customer_name` |
| `Address2` | `ship_to_address` |
| `DocDueDate` | `scheduled_date` |
| `BPLId` | `sap_branch_id` |
| `Comments` | *(used for logging only)* |

| HANA Field (RDR1) | Django Field (`ShipmentOrderItem`) |
|--------------------|-----------------------------------|
| `LineNum` | `sap_line_num` |
| `ItemCode` | `item_code` |
| `Dscription` | `item_name` |
| `Quantity - DelivrdQty` | `ordered_qty` (remaining qty) |
| `unitMsr` | `uom` |
| `WhsCode` | `warehouse_code` |

### Sync Behaviour

- If a Sales Order (`sap_doc_entry` + `company`) **does not exist** in Django → creates new `ShipmentOrder` with status `RELEASED`
- If it **already exists** and status is `RELEASED` → updates items (re-sync quantities)
- If it **already exists** and status is beyond `RELEASED` (e.g., `PICKING`, `LOADING`) → **skips** (no overwrite of in-progress work)

### Code Location

- Reader: `sap_client/hana/sales_order_reader.py` → `HanaSalesOrderReader`
- Sync logic: `outbound_dispatch/services/sap_sync_service.py` → `SAPSyncService.sync_sales_orders()`

---

## 2. Writing to SAP Service Layer (Goods Issue)

**Purpose:** Post a Goods Issue document (`InventoryGenExits`) to SAP when a shipment is dispatched.

**Triggered by:** `POST /api/v1/outbound/shipments/{id}/dispatch/`

**SAP Endpoint:**

```
POST {SL_URL}/b1s/v2/InventoryGenExits
```

### JSON Payload Sent to SAP

```json
{
    "Comments": "App: FactoryApp v2 | SO: 1725096575 | BOL: BOL-20260324-100 | Seal: SEAL-001",
    "BPL_IDAssignedToInvoice": 1,
    "DocumentLines": [
        {
            "ItemCode": "FG0000194",
            "Quantity": "100.000",
            "WarehouseCode": "WH01",
            "BaseEntry": 15969,
            "BaseLine": 0,
            "BaseType": 17,
            "BatchNumbers": [
                {
                    "BatchNumber": "BATCH-001",
                    "Quantity": "100.000"
                }
            ]
        }
    ]
}
```

### Payload Fields Explained

#### Header Level

| JSON Field | Source | Description |
|------------|--------|-------------|
| `Comments` | Auto-generated | Traceability string: app name, SO number, BOL number, seal number |
| `BPL_IDAssignedToInvoice` | `branch_id` param | SAP branch/plant ID (optional, sent only if provided) |

#### DocumentLines (one per shipped item)

| JSON Field | Source (Django) | Description |
|------------|----------------|-------------|
| `ItemCode` | `ShipmentOrderItem.item_code` | SAP item code |
| `Quantity` | `ShipmentOrderItem.loaded_qty` | Actual loaded quantity (not ordered qty) |
| `WarehouseCode` | `ShipmentOrderItem.warehouse_code` | Source warehouse for stock deduction |
| `BaseEntry` | `ShipmentOrder.sap_doc_entry` | Links Goods Issue line to Sales Order header |
| `BaseLine` | `ShipmentOrderItem.sap_line_num` | Links to specific Sales Order line |
| `BaseType` | `17` (hardcoded) | SAP object type for Sales Order |
| `BatchNumbers` | `ShipmentOrderItem.batch_number` | Batch allocation (only if batch number exists) |

### Linking Logic (BaseEntry / BaseLine / BaseType)

The `BaseEntry`, `BaseLine`, and `BaseType` fields create a **document linkage** in SAP B1:

```
Goods Issue Line  →  links to  →  Sales Order Line
   BaseEntry      =                 ORDR.DocEntry
   BaseLine       =                 RDR1.LineNum
   BaseType       =                 17 (Sales Order object type)
```

This linkage:
- Automatically reduces the Sales Order's open quantity in SAP
- Creates a document trail (SO → Goods Issue) visible in SAP
- Enables SAP reporting on fulfillment rates

**Note:** `BaseEntry`/`BaseLine` are only included when the shipment was synced from a SAP Sales Order. Manually created shipments (if any) would not include these fields.

### Items Included / Excluded

- Only items where `loaded_qty > 0` are included in the Goods Issue
- If no items have loaded quantities, the posting fails with: `"No loaded quantities to post"`

### SAP Response (Success — HTTP 201)

```json
{
    "DocEntry": 777,
    "DocNum": 888,
    "DocTotal": 50000.0,
    ...
}
```

| Response Field | Stored In (Django) | Description |
|----------------|--------------------|-------------|
| `DocEntry` | `GoodsIssuePosting.sap_doc_entry` | SAP document internal ID |
| `DocNum` | `GoodsIssuePosting.sap_doc_num` | SAP document display number |

### Error Handling & Retry

| Scenario | HTTP Code | Action |
|----------|-----------|--------|
| Success | 201 | `GoodsIssuePosting.status = POSTED`, shipment → `DISPATCHED` |
| Validation error | 400 | `GoodsIssuePosting.status = FAILED`, error saved, retry_count++ |
| Auth error | 401/403 | `GoodsIssuePosting.status = FAILED`, "SAP authentication failed" |
| Connection error | N/A | `GoodsIssuePosting.status = FAILED`, "SAP system unavailable" |
| Timeout | N/A | `GoodsIssuePosting.status = FAILED`, retry_count++ |

**Retry endpoint:** `POST /api/v1/outbound/shipments/{id}/goods-issue/retry/`
- Can only retry postings in `FAILED` status
- Deletes old `GoodsIssuePosting` record and creates a fresh attempt
- On success, shipment status is updated to `DISPATCHED`

### Code Location

- Writer: `sap_client/service_layer/goods_issue_writer.py` → `GoodsIssueWriter.create()`
- Dispatch logic: `outbound_dispatch/services/outbound_service.py` → `OutboundService._post_goods_issue()`
- Client entry point: `sap_client/client.py` → `SAPClient.create_goods_issue()`

---

## 3. SAP Authentication

### HANA (Direct SQL)

- Protocol: hdbcli (SAP HANA Python driver)
- Credentials: `HANA_HOST`, `HANA_PORT`, `HANA_USER`, `HANA_PASSWORD` from `.env`
- Schema: per company from `COMPANY_DB` setting (e.g., `JIVO_OIL` → schema name)

### Service Layer (REST API)

- Login: `POST {SL_URL}/b1s/v2/Login`
- Payload: `{ "CompanyDB": "...", "UserName": "...", "Password": "..." }`
- Auth type: Cookie-based session (`B1SESSION`, `ROUTEID`)
- Credentials: `SL_URL`, `SL_USER`, `SL_PASSWORD` from `.env`
- SSL: Self-signed cert (`verify=False`)
- Timeout: 10s for login, 30s for document posting

---

## 4. Data Flow Summary

```
┌─────────────┐       HANA SQL        ┌─────────────────────┐
│  SAP HANA   │ ─────────────────────→ │  Django             │
│  ORDR/RDR1  │   Read Sales Orders    │  ShipmentOrder      │
│             │                        │  ShipmentOrderItem  │
└─────────────┘                        └─────────┬───────────┘
                                                  │
                                    Warehouse picks, packs,
                                    loads, supervisor confirms
                                                  │
                                                  ▼
┌─────────────┐    Service Layer POST  ┌─────────────────────┐
│  SAP B1     │ ←───────────────────── │  Django             │
│  Inventory  │   InventoryGenExits    │  GoodsIssuePosting  │
│  Gen Exits  │                        │                     │
└─────────────┘                        └─────────────────────┘
```

**Read direction:** SAP HANA → Django (Sales Orders → ShipmentOrders)
**Write direction:** Django → SAP Service Layer (Dispatch → Goods Issue)
