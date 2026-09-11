# Non-Moving Raw Material Dashboard — API Reference

## Base URL

```
/api/v1/non-moving-rm/
```

## Authentication & Headers

All endpoints require the following headers:

```
Authorization: Bearer <jwt_token>
Company-Code: <company_code>    (e.g., JIVO_OIL, JIVO_MART, JIVO_BEVERAGES)
Content-Type: application/json
```

## Permissions

Users must have the `non_moving_rm.can_view_non_moving_rm` permission assigned.

---

## Endpoints

### 1. Item Groups Dropdown

```
GET /api/v1/non-moving-rm/item-groups/
```

Returns all SAP item groups from the OITB table. Use this to populate the item group dropdown filter.

**Parameters:** None

**Response (200):**

| Field               | Type   | Description                          |
|---------------------|--------|--------------------------------------|
| `data`              | array  | List of item groups                  |
| `data[].item_group_code` | int | SAP item group code (ItmsGrpCod) |
| `data[].item_group_name` | string | Item group name (ItmsGrpNam)   |
| `meta.total_groups` | int    | Total number of groups               |
| `meta.fetched_at`   | string | ISO 8601 timestamp of data fetch     |

---

### 2. Non-Moving RM Report

```
GET /api/v1/non-moving-rm/report/?age=<days>&item_group=<code>
```

Reads stock by movement age from the schema of the selected `Company-Code` and returns one row per item and warehouse, with summary aggregations.

**Query Parameters:**

| Parameter    | Type | Required | Min | Description                                  |
|-------------|------|----------|-----|----------------------------------------------|
| `age`       | int  | Yes      | 0   | Minimum days since last movement; `0` returns all stock |
| `item_group`| int  | No       | 0   | Item group code from OITB; omit or pass `0` for all groups |

The API returns only rows where `days_since_last_movement > age`. Use `age=0` to include all stock, including recently moved stock. This matches the Excel workbook's "more than N days" filter.

**Packing material is aged on production, not on movement.** For item group 105
(`PACKAGING MATERIAL`) `days_since_last_movement` counts from the last time the
ITEM was issued to a production order (`OINM.TransType` 60) or received from one
(59) — in any warehouse. Warehouse-to-warehouse transfers (67) do not reset it,
so a pallet restacked between godowns keeps its full age. Stock never consumed
falls back to the item's last non-transfer movement (its GRPO), then to
`OITM.CreateDate`.

Every other item group is unchanged: its age is still days since the last OINM
row of any kind in THAT warehouse. `movement_basis` says which rule a row used,
and `days_since_warehouse_movement` carries the old per-warehouse figure
alongside, so a restack is still visible on a packing-material row.

**Response (200):**

| Field                                  | Type    | Description                                    |
|----------------------------------------|---------|------------------------------------------------|
| `data`                                 | array   | List of non-moving items                       |
| `data[].branch`                        | string  | SAP branch code                                |
| `data[].item_code`                     | string  | SAP item code                                  |
| `data[].item_name`                     | string  | Item description                               |
| `data[].item_group_name`               | string  | Item group name                                |
| `data[].quantity`                       | float   | Current stock quantity                         |
| `data[].sub_group`                      | string  | Sub group (e.g., LABEL, CARTON, CAPS)          |
| `data[].value`                          | float   | Inventory value                                |
| `data[].last_movement_date`             | string  | Last movement date (YYYY-MM-DD HH:MM:SS)      |
| `data[].days_since_last_movement`       | int     | Days since last stock movement                 |
| `data[].consumption_ratio`              | float   | Consumption ratio percentage                   |
| `data[].movement_basis`                 | string  | `production` on packing material, `any` on everything else — which rule aged the row |
| `data[].last_warehouse_movement_date`   | string  | That warehouse's own last movement of any kind, transfers included |
| `data[].days_since_warehouse_movement`  | int     | Days since that warehouse's own last movement  |
| `summary.total_items`                   | int     | Total non-moving items                         |
| `summary.total_value`                   | float   | Sum of all item values                         |
| `summary.total_quantity`                | float   | Sum of all item quantities                     |
| `summary.by_branch`                     | array   | Branch-wise breakdown                          |
| `summary.by_branch[].branch`           | string  | Branch code                                    |
| `summary.by_branch[].item_count`       | int     | Number of items in this branch                 |
| `summary.by_branch[].total_value`      | float   | Total value for this branch                    |
| `summary.by_branch[].total_quantity`   | float   | Total quantity for this branch                 |
| `meta.age_days`                         | int     | Age filter used                                |
| `meta.item_group`                       | int     | Item group filter used                         |
| `meta.fetched_at`                       | string  | ISO 8601 timestamp                             |

---

## Error Responses

### 400 Bad Request
```json
{
  "detail": "Invalid query parameters.",
  "errors": {
    "age": ["This field is required."]
  }
}
```

### 401 Unauthorized
```json
{
  "detail": "Authentication credentials were not provided."
}
```

### 403 Forbidden
```json
{
  "detail": "You do not have permission to perform this action."
}
```

### 502 Bad Gateway
```json
{
  "detail": "SAP data error: Failed to retrieve non-moving RM data from SAP."
}
```

### 503 Service Unavailable
```json
{
  "detail": "SAP system is currently unavailable. Please try again later."
}
```

---

## HANA Data Reference

The report is computed by one query against the selected company's own schema. It used to call `JIVO_BEVERAGES_HANADB.REPORT_BP_NON_MOVING_RM(age, item_group)`; that procedure answered for all three companies at once, returned no warehouse — forcing the API to guess each item's warehouse by pro-rating against current stock — and eventually stopped answering at all, which the dashboard showed as "SAP data error".

Rows are one per **(item, warehouse)**, built from:

| Field                       | Source                                                                                                                              |
|-----------------------------|-------------------------------------------------------------------------------------------------------------------------------------|
| `quantity`                  | `OITW.OnHand`, restricted to `> 0` in warehouses that are not `OWHS.Inactive`                                                        |
| `value`                     | `quantity` x unit cost: `OITW.AvgPrice`, else `OITM.AvgPrice`, else `OITM.LastPurPrc`                                                |
| `last_movement_date`        | latest `OINM.DocDate` that moved `InQty` or `OutQty` **in that warehouse**; falls back to `OITM.CreateDate` when SAP never moved it   |
| `days_since_last_movement`  | `DAYS_BETWEEN(last_movement_date, CURRENT_DATE)`                                                                                     |
| `consumption_ratio`         | `OINM.OutQty` issued over the trailing 365 days as a percentage of `quantity`, so `0` means nothing left the warehouse all year       |
| `sub_group`                 | `OITM.U_Sub_Group` (blank in a company that does not have the UDF)                                                                    |
| `item_group_name`           | `OITB.ItmsGrpNam`                                                                                                                    |
| `branch`                    | the selected company (`JIVO_OIL` -> `OIL`, `JIVO_MART` -> `MART`, `JIVO_BEVERAGES` -> `BEV`); a schema is one branch                 |

Aging is per warehouse, not per item: a label consumed daily at `BH-PC` while an identical pallet sits untouched in another store still shows up for that other store, which is the stock the dashboard exists to find. `warehouse_summary` is the same rows added up per warehouse — nothing in it is estimated.

To read the same numbers straight from SAP, outside the API:

```
python manage.py check_non_moving_report --company JIVO_OIL --age 45
python manage.py check_non_moving_report --company JIVO_OIL --age 0 --show-sql
```

### OITB (Item Groups Table)

```sql
SELECT "ItmsGrpCod", "ItmsGrpNam" FROM OITB
```

**Known Item Groups:**

| Code | Name                |
|------|---------------------|
| 101  | CONSUMABLES         |
| 102  | FINISHED            |
| 105  | PACKAGING MATERIAL  |
| 106  | RAW MATERIAL        |
| 107  | TRADING ITEMS       |
| 109  | SALES BOM           |
| 110  | FIXED ASSETS        |
| 111  | LABORATORY          |
| 112  | FA CONSUMABLES      |
| 114  | CONSUMABLES         |
