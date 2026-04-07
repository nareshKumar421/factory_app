# WMS API Reference

All endpoints require JWT authentication (`Authorization: Bearer <token>`) and the `Company-Code` header.

Base URL: `/api/v1/warehouse/wms/`

---

## 1. Dashboard

### `GET /wms/dashboard/`

Returns aggregated KPIs, chart data, and recent movements for the WMS dashboard.

**Query Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `warehouse_code` | string | No | Filter to a specific warehouse (e.g. `GP-FG`) |

**Response:**

```json
{
  "kpis": {
    "total_items": 1346,
    "total_on_hand": 18093080.34,
    "total_value": 553488141.54,
    "low_stock": 704,
    "critical_stock": 600,
    "zero_stock": 164,
    "overstock": 2
  },
  "stock_by_warehouse": [
    { "warehouse_code": "BH-LO", "items": 30, "value": 143160693.60 }
  ],
  "stock_by_group": [
    { "group_name": "RAW MATERIAL", "items": 47, "value": 180000000.00 }
  ],
  "top_items_by_value": [
    { "item_code": "RM0000016", "item_name": "CRUDE DEGUMMED RAPESEED OIL NEW", "quantity": 822392.96, "value": 92290747.12 }
  ],
  "stock_health": {
    "normal": 476,
    "low": 704,
    "critical": 600,
    "zero": 164,
    "overstock": 2
  },
  "recent_movements": [
    { "date": "2026-04-07", "item_code": "FG0000395", "item_name": "...", "warehouse": "GP-FG", "in_qty": 0, "out_qty": 4.0, "direction": "OUT", "quantity": 4.0 }
  ]
}
```

**SAP Tables Used:** OITM, OITW, OITB, OWHS, OINM (all READ-ONLY)

---

## 2. Stock Overview

### `GET /wms/stock/overview/`

Returns paginated stock data across items and warehouses with summary stats.

**Query Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `warehouse_code` | string | No | Filter by warehouse code |
| `item_group` | string | No | Filter by item group name |
| `search` | string | No | Search in item code or item name |
| `stock_filter` | string | No | `with_stock` (default), `all`, or `zero_stock` |
| `page` | int | No | Page number (default: 1) |
| `page_size` | int | No | Items per page (default: 50) |

**Response:**

```json
{
  "summary": {
    "total_items": 3840,
    "total_on_hand": 18093080.34,
    "total_committed": 9033833.65,
    "total_available": 9059246.69,
    "total_value": 553488141.54
  },
  "items": [
    {
      "item_code": "RM0000016",
      "item_name": "CRUDE DEGUMMED RAPESEED OIL NEW",
      "item_group": "RAW MATERIAL",
      "uom": "LTR",
      "warehouse_code": "BH-CRUDE",
      "on_hand": 822392.96,
      "committed": 0,
      "on_order": 0,
      "available": 822392.96,
      "avg_price": 112.22,
      "stock_value": 92290747.12,
      "min_level": 0,
      "max_level": 0,
      "last_purchase_price": 112.22,
      "stock_status": "NORMAL"
    }
  ],
  "pagination": {
    "total": 3840,
    "page": 1,
    "page_size": 50,
    "pages": 77
  }
}
```

**Stock Status Logic:**

| Status | Condition |
|--------|-----------|
| `ZERO` | on_hand = 0 |
| `CRITICAL` | min_level > 0 AND on_hand <= min_level * 0.5 |
| `LOW` | min_level > 0 AND on_hand <= min_level |
| `OVERSTOCK` | max_level > 0 AND on_hand >= max_level |
| `NORMAL` | Everything else |

**SAP Tables Used:** OITM, OITW, OITB (all READ-ONLY)

---

## 3. Item Detail

### `GET /wms/stock/items/{item_code}/`

Returns detailed stock info for a single item across all warehouses.

**Path Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `item_code` | string | SAP item code (e.g. `RM0000016`) |

**Response:**

```json
{
  "item": {
    "item_code": "RM0000016",
    "item_name": "CRUDE DEGUMMED RAPESEED OIL NEW",
    "item_group": "RAW MATERIAL",
    "uom": "LTR",
    "avg_price": 112.22,
    "last_purchase_price": 112.22,
    "min_level": 0,
    "max_level": 0
  },
  "warehouse_breakdown": [
    {
      "warehouse_code": "BH-CRUDE",
      "on_hand": 822392.96,
      "committed": 0,
      "on_order": 0,
      "available": 822392.96,
      "value": 92290747.12
    }
  ],
  "stock_summary": {
    "total_on_hand": 822392.96,
    "total_committed": 0,
    "total_available": 822392.96,
    "total_value": 92290747.12
  }
}
```

Returns `{ "item": null, "warehouse_breakdown": [], "stock_summary": {} }` if item not found. The view returns HTTP 404 in that case.

**SAP Tables Used:** OITM, OITW, OITB (all READ-ONLY)

---

## 4. Stock Movements

### `GET /wms/stock/movements/`

Returns stock movement history from the SAP inventory audit trail.

**Query Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `item_code` | string | No | Filter by item code |
| `warehouse_code` | string | No | Filter by warehouse |
| `from_date` | string | No | Start date (YYYY-MM-DD) |
| `to_date` | string | No | End date (YYYY-MM-DD) |
| `limit` | int | No | Max rows to return (default: 100) |

**Response:**

```json
{
  "movements": [
    {
      "date": "2026-03-31",
      "item_code": "RM0000016",
      "item_name": "CRUDE DEGUMMED RAPESEED OIL NEW",
      "warehouse_code": "BH-CRUDE",
      "in_qty": 0,
      "out_qty": 176043.78,
      "quantity": 176043.78,
      "direction": "OUT",
      "transaction_type": "GOODS_ISSUE",
      "reference": "",
      "doc_num": "12345",
      "created_by": "manager"
    }
  ]
}
```

**Transaction Type Mapping (SAP TransType -> Label):**

| SAP Code | Label | Description |
|----------|-------|-------------|
| 13 | AR_INVOICE | Sales Invoice |
| 14 | AR_CREDIT | Sales Credit Note |
| 15 | DELIVERY | Delivery Note |
| 16 | RETURN | Goods Return |
| 18 | AP_INVOICE | Purchase Invoice |
| 19 | AP_CREDIT | Purchase Credit Note |
| 20 | GRPO | Goods Receipt PO |
| 21 | RETURN_TO_VENDOR | Return to Vendor |
| 59 | GOODS_RECEIPT | Goods Receipt |
| 60 | GOODS_ISSUE | Goods Issue |
| 67 | TRANSFER | Inventory Transfer |
| 202 | PRODUCTION_ORDER | Production Order |

**SAP Tables Used:** OINM, OITM (all READ-ONLY)

---

## 5. Warehouse Summary

### `GET /wms/warehouses/summary/`

Returns stock health metrics per warehouse.

**Response:**

```json
{
  "warehouses": [
    {
      "warehouse_code": "BH-BS",
      "warehouse_name": "Bhakharpur Basement",
      "total_items": 363,
      "total_on_hand": 5000000,
      "total_value": 13011397,
      "low_stock_count": 79,
      "critical_stock_count": 54,
      "overstock_count": 0,
      "zero_stock_count": 12
    }
  ]
}
```

**SAP Tables Used:** OITM, OITW, OWHS (all READ-ONLY)

---

## 6. Billing Reconciliation

### `GET /wms/billing/overview/`

Compares GRPO received quantities against AP Invoice billed quantities. Identifies unbilled and partially billed items.

**Query Parameters:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `from_date` | string | No | GRPO from date (YYYY-MM-DD) |
| `to_date` | string | No | GRPO to date (YYYY-MM-DD) |
| `vendor` | string | No | Filter by vendor code (CardCode) |
| `warehouse_code` | string | No | Filter by warehouse |

**Response:**

```json
{
  "summary": {
    "total_received_qty": 12874405.03,
    "total_billed_qty": 11492694.69,
    "total_unbilled_qty": 1381710.34,
    "total_received_value": 839846060.14,
    "total_billed_value": 757676864.45,
    "total_unbilled_value": 82169195.69,
    "fully_billed_count": 156,
    "partially_billed_count": 56,
    "unbilled_count": 68
  },
  "items": [
    {
      "item_code": "PM0000235",
      "item_name": "CAPS 1 LTR WHITE AND YELLOW SM",
      "warehouse_code": "BH-BS",
      "received_qty": 500000,
      "received_value": 150000,
      "billed_qty": 135000,
      "billed_value": 40500,
      "unbilled_qty": 365000,
      "unbilled_value": 109500,
      "status": "PARTIALLY_BILLED",
      "first_grpo_date": "2026-01-15",
      "last_grpo_date": "2026-03-20"
    }
  ]
}
```

**Billing Status Logic:**

| Status | Condition |
|--------|-----------|
| `FULLY_BILLED` | unbilled_qty <= 0 |
| `PARTIALLY_BILLED` | billed_qty > 0 AND unbilled_qty > 0 |
| `UNBILLED` | billed_qty = 0 OR received_qty = 0 |

**SAP Tables Used:** OPDN (GRPO Header), PDN1 (GRPO Lines) (all READ-ONLY)

**How Billing Reconciliation Works:**
- `Quantity` on PDN1 = total received quantity
- `OpenCreQty` on PDN1 = quantity still open for credit (i.e., not yet invoiced)
- `Billed Qty = Quantity - OpenCreQty`
- SAP maintains this linkage automatically when AP Invoices are copied from GRPOs

---

## 7. Warehouse List (Dropdown)

### `GET /wms/warehouses/`

Returns active warehouses for filter dropdowns.

**Response:**

```json
{
  "warehouses": [
    { "code": "BH-BS", "name": "Bhakharpur Basement" },
    { "code": "GP-FG", "name": "Gujarat FG" }
  ]
}
```

**SAP Tables Used:** OWHS (READ-ONLY)

---

## 8. Item Groups (Dropdown)

### `GET /wms/item-groups/`

Returns item groups for filter dropdowns.

**Response:**

```json
{
  "item_groups": [
    { "code": 100, "name": "RAW MATERIAL" },
    { "code": 102, "name": "FINISHED" }
  ]
}
```

**SAP Tables Used:** OITB (READ-ONLY)

---

## Error Responses

All endpoints return errors in this format:

```json
{ "error": "Error message here" }
```

| Status Code | Meaning |
|-------------|---------|
| 401 | JWT token missing or expired |
| 403 | Company-Code header missing or user not assigned to company |
| 404 | Resource not found (e.g., invalid item code) |
| 500 | SAP HANA connection failure or query error |
