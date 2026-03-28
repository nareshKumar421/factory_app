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

Calls the `REPORT_BP_NON_MOVING_RM` stored procedure and returns item-level data with summary aggregations.

**Query Parameters:**

| Parameter    | Type | Required | Min | Description                                  |
|-------------|------|----------|-----|----------------------------------------------|
| `age`       | int  | Yes      | 1   | Minimum days since last movement              |
| `item_group`| int  | Yes      | -   | Item group code from OITB (from dropdown)     |

**Response (200):**

| Field                                  | Type    | Description                                    |
|----------------------------------------|---------|------------------------------------------------|
| `data`                                 | array   | List of non-moving items                       |
| `data[].branch`                        | string  | SAP branch/warehouse code                      |
| `data[].item_code`                     | string  | SAP item code                                  |
| `data[].item_name`                     | string  | Item description                               |
| `data[].item_group_name`               | string  | Item group name                                |
| `data[].quantity`                       | float   | Current stock quantity                         |
| `data[].litres`                         | float   | Quantity in litres                             |
| `data[].sub_group`                      | string  | Sub group (e.g., LABEL, CARTON, CAPS)          |
| `data[].value`                          | float   | Inventory value                                |
| `data[].last_movement_date`             | string  | Last movement date (YYYY-MM-DD HH:MM:SS)      |
| `data[].days_since_last_movement`       | int     | Days since last stock movement                 |
| `data[].consumption_ratio`              | float   | Consumption ratio percentage                   |
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
    "age": ["This field is required."],
    "item_group": ["This field is required."]
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

## Stored Procedure Reference

### REPORT_BP_NON_MOVING_RM

```sql
CALL "REPORT_BP_NON_MOVING_RM"(<Age>, <ItemGroup>)
```

**Parameters:**
- `Age` (INT): Number of days since last movement
- `ItemGroup` (INT): Item group code from OITB

**Returns:**

| Column                  | Type     | Description                         |
|-------------------------|----------|-------------------------------------|
| Branch                  | VARCHAR  | Branch/plant code                   |
| ItemCode                | VARCHAR  | SAP item code                       |
| ItemName                | VARCHAR  | Item description                    |
| ItmsGrpNam              | VARCHAR  | Item group name                     |
| Quantity                | DECIMAL  | Current stock quantity               |
| Litres                  | DECIMAL  | Quantity in litres                   |
| U_Sub_Group             | VARCHAR  | Sub group classification             |
| Value                   | DECIMAL  | Inventory value                      |
| LastMovementDate        | DATETIME | Date of last stock movement          |
| DaysSinceLastMovement   | INT      | Number of days since last movement   |
| ConsumptionRatio        | DECIMAL  | Consumption ratio percentage         |

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
