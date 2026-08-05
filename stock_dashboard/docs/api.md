# Stock Dashboard API

The stock dashboard API powers the frontend Stock Benchmark page. It reads SAP HANA directly and does not write application database rows.

## Endpoints

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/api/v1/dashboards/stock/` | Stock benchmark rows, pagination, and meta counts |
| GET | `/api/v1/dashboards/stock/as-of/` | Experimental SAP movement reconstruction for a prior posting date |
| GET | `/api/v1/dashboards/stock/<item_code>/warehouses/` | Per-warehouse detail for an expanded grouped item |

All endpoints require:

- JWT authentication
- `Company-Code` header
- `can_view_stock_dashboard` permission

## Query Parameters

`GET /api/v1/dashboards/stock/`

| Parameter | Type | Description |
|-----------|------|-------------|
| `search` | string | Case-insensitive match against item code, item name, or warehouse code. |
| `warehouse` | comma-separated string | Warehouse codes. Two or more warehouses switch the service to grouped item rows. |
| `item_group` | string | SAP item group name from `OITB.ItmsGrpNam`, for example `PACKAGING MATERIAL`. |
| `status` | comma-separated string | Allowed values: `healthy`, `low`, `critical`, `unset`. The `unset` value is displayed as No Benchmark Set. When the default operational set `healthy,low,critical` is used without a movement filter excluding slow rows, slow rows with a benchmark are still returned with no stock status so the Movement filter owns slow-moving visibility. |
| `movement_status` | comma-separated string | Allowed values: `recent`, `slow`. Omit to include all movement states. |
| `sort_by` | string | `item_code`, `item_name`, `warehouse`, `on_hand`, `min_stock`, `health_ratio`. The `min_stock` sort is the Benchmark column. |
| `sort_dir` | string | `asc` or `desc`. |
| `page` | integer | Page number, minimum 1. |
| `page_size` | integer | Page size, minimum 1, maximum 200. |

`GET /api/v1/dashboards/stock/as-of/`

Supports the same filters as the live Stock Benchmark endpoint, plus:

| Parameter | Type | Description |
|-----------|------|-------------|
| `as_of_date` | date | Required. SAP posting date to reconstruct through, formatted `YYYY-MM-DD`. Future dates are rejected. |

This endpoint is a proof path for historical Stock Benchmark data. It reconstructs `on_hand`, last consumption date, movement status, health ratio, and stock status from SAP movement history up to the selected date. Benchmark (`MinStock`), item name, UOM, and item group still come from current SAP master data.

`GET /api/v1/dashboards/stock/<item_code>/warehouses/`

| Parameter | Type | Description |
|-----------|------|-------------|
| `warehouse` | comma-separated string | Required warehouse list for the per-warehouse breakdown. |

## SAP HANA Sources

| SAP table | Usage |
|-----------|-------|
| `OITW` | Item warehouse stock, `OnHand`, benchmark (`MinStock`), warehouse code |
| `OITM` | Item name, inventory UOM, inventory item flag |
| `OITB` | Item group name for material type filtering |
| `OINM` | Inventory audit trail for item-level outbound consumption history |

Outbound consumption is taken from `OINM` rows with `OutQty > 0` and transaction types:

| TransType | Meaning |
|-----------|---------|
| `15` | Delivery |
| `60` | Goods Issue |
| `202` | Production Order |

Stock and benchmark quantities are limited to the selected warehouses. Movement age is item-level: recent consumption in any warehouse prevents the item from being classified as slow-moving in Stock Benchmark.

## Stock Status Rules

The backend owns stock status so filtering, returned rows, grouped rows, and meta counts stay consistent.
Rows classified as `slow` movement do not receive a stock health status and are not counted as Healthy, Low, Critical, or No Benchmark Set. Benchmarked slow rows remain visible in the default Stock Benchmark view as no-status rows; slow rows with no benchmark stay out of the default operational status view.

| Status | Rule |
|--------|------|
| `healthy` | Not slow-moving, benchmark is set, and `OnHand >= Benchmark` |
| `low` | Not slow-moving, benchmark is set, `OnHand < Benchmark`, and `OnHand >= Benchmark * 0.6` |
| `critical` | Not slow-moving, benchmark is set, and `OnHand < Benchmark * 0.6` |
| `unset` | Not slow-moving and benchmark is zero |

The SAP field behind Benchmark is `MinStock`; the API field remains `min_stock` for compatibility. Stock health, health ratio, and health sorting use Benchmark only.

## Movement Rules

`SLOW_MOVING_DAYS` is currently `30`.

| Movement | Rule |
|----------|------|
| `recent` | Last outbound consumption is within 30 days. |
| `slow` | No outbound consumption exists or the last outbound consumption is older than 30 days. |

Consumption age is checked by item code across SAP inventory movement, not only the selected Stock Benchmark warehouses.

## As-Of Reconstruction Rules

The experimental as-of endpoint uses SAP `OINM` as an inventory audit trail:

```text
as_of_on_hand = current OITW.OnHand - SUM(OINM.InQty - OINM.OutQty where DocDate > as_of_date)
```

Movement age is also calculated as of the selected posting date:

```text
last_consumption_date = MAX(OINM.DocDate where OutQty > 0 and DocDate <= as_of_date)
days_since_last_consumption = DAYS_BETWEEN(last_consumption_date, as_of_date)
```

Limitations:

- The endpoint reconstructs by SAP posting date, not exact time of day.
- Benchmark (`OITW.MinStock`) is current unless SAP change-log support is added later.
- Item names, UOMs, and item groups are current SAP master data.
- Historical open production demand is not reconstructed in this proof endpoint.
- The response uses ungrouped item-warehouse rows so warehouse filtering can be validated directly.

## Grouped Rows

If two or more warehouses are selected, the service returns grouped item rows:

- `on_hand` is summed across selected warehouses.
- Benchmark (`min_stock`) is summed across selected warehouses.
- `warehouse` is displayed as `<n> warehouses`.
- `warehouse_count` contains the number of contributing warehouse rows.
- `has_warning` is set when a child warehouse has a worse status than the aggregate row.
- A grouped row can be Healthy while still showing `has_warning=true` when an individual child warehouse is Low or Critical.

## Meta Counts

The response meta contains:

| Field | Description |
|-------|-------------|
| `total_items` | Number of rows after the active backend filters, including no-status slow rows when they are visible. |
| `healthy_count` | Count of non-slow rows classified as Healthy. |
| `low_stock_count` | Count of non-slow rows classified as Low. |
| `critical_stock_count` | Count of non-slow rows classified as Critical. |
| `warehouses` | Distinct warehouse list from SAP. |
| `page`, `page_size`, `total_pages` | Pagination metadata. |

The frontend Stock Benchmark page uses a separate pinned stats query for the top cards. That query uses default Packing Material, warehouses `BH-BS,BH-PM,BH-PC`, statuses `healthy,low,critical`, and movement `recent`.

## Tests

Run the stock dashboard test suite with:

```powershell
$env:DEBUG='False'; .\.venv\Scripts\python.exe manage.py test stock_dashboard
```
