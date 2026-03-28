# Non-Moving Raw Material Dashboard — Frontend Integration Guide

## Overview

This dashboard displays raw materials that have not moved (no consumption/receipt) for a specified number of days. It helps inventory managers identify dead stock and take action.

---

## API Base URL

```
/api/v1/non-moving-rm/
```

## Authentication

All endpoints require:

1. **JWT Token** — `Authorization: Bearer <token>`
2. **Company Code Header** — `Company-Code: JIVO_OIL`

---

## Endpoints

### 1. GET `/api/v1/non-moving-rm/item-groups/`

**Purpose:** Fetch item groups for the dropdown filter. Call this on page load.

**Headers:**
```
Authorization: Bearer <token>
Company-Code: JIVO_OIL
```

**Response:**
```json
{
  "data": [
    { "item_group_code": 101, "item_group_name": "CONSUMABLES" },
    { "item_group_code": 102, "item_group_name": "FINISHED" },
    { "item_group_code": 105, "item_group_name": "PACKAGING MATERIAL" },
    { "item_group_code": 106, "item_group_name": "RAW MATERIAL" },
    { "item_group_code": 107, "item_group_name": "TRADING ITEMS" },
    { "item_group_code": 109, "item_group_name": "SALES BOM" },
    { "item_group_code": 110, "item_group_name": "FIXED ASSETS" },
    { "item_group_code": 111, "item_group_name": "LABORATORY" },
    { "item_group_code": 112, "item_group_name": "FA CONSUMABLES" },
    { "item_group_code": 114, "item_group_name": "CONSUMABLES" }
  ],
  "meta": {
    "total_groups": 10,
    "fetched_at": "2026-03-28T10:30:00+00:00"
  }
}
```

---

### 2. GET `/api/v1/non-moving-rm/report/?age=45&item_group=105`

**Purpose:** Fetch the non-moving raw material report. Call when user selects filters and clicks "Search" / "Apply".

**Query Parameters:**

| Parameter    | Type | Required | Description                                      | Example |
|-------------|------|----------|--------------------------------------------------|---------|
| `age`       | int  | Yes      | Minimum days since last movement                 | 45      |
| `item_group`| int  | Yes      | Item group code (from item-groups dropdown)       | 105     |

**Headers:**
```
Authorization: Bearer <token>
Company-Code: JIVO_OIL
```

**Response:**
```json
{
  "data": [
    {
      "branch": "PM00000081",
      "item_code": "ITEM-001",
      "item_name": "LABEL 1 KG GOLD FULL",
      "item_group_name": "PACKAGING MATERIAL",
      "quantity": 26116.0,
      "litres": 22186.0,
      "sub_group": "LABEL",
      "value": 24203.0,
      "last_movement_date": "2020-03-19 12:00:00",
      "days_since_last_movement": 2200,
      "consumption_ratio": 46.5
    }
  ],
  "summary": {
    "total_items": 32,
    "total_value": 1250000.50,
    "total_quantity": 450000.0,
    "by_branch": [
      {
        "branch": "PM00000081",
        "item_count": 20,
        "total_value": 800000.0,
        "total_quantity": 300000.0
      },
      {
        "branch": "PM00000082",
        "item_count": 12,
        "total_value": 450000.50,
        "total_quantity": 150000.0
      }
    ]
  },
  "meta": {
    "age_days": 45,
    "item_group": 105,
    "fetched_at": "2026-03-28T10:30:00+00:00"
  }
}
```

---

## Error Responses

| Status | Meaning                    | When                                          |
|--------|----------------------------|-----------------------------------------------|
| 400    | Bad Request                | Missing or invalid query parameters            |
| 401    | Unauthorized               | Missing or expired JWT token                   |
| 403    | Forbidden                  | Missing Company-Code header or no permission   |
| 502    | Bad Gateway                | SAP HANA query error                           |
| 503    | Service Unavailable        | SAP HANA connection failed                     |

**Error format:**
```json
{
  "detail": "Invalid query parameters.",
  "errors": {
    "age": ["This field is required."],
    "item_group": ["This field is required."]
  }
}
```

---

## Recommended UI Layout

### Filters Section (Top)
```
┌─────────────────────────────────────────────────────────────┐
│  Age (Days) [Dropdown/Input]    Item Group [Dropdown]  [Search] │
│  Suggested ages: 30, 45, 60, 90, 180, 365                      │
└─────────────────────────────────────────────────────────────┘
```

### Summary Cards (Below Filters)
```
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│ Total Items  │  │ Total Value  │  │ Total Qty    │
│     32       │  │  ₹12,50,000  │  │  4,50,000    │
└──────────────┘  └──────────────┘  └──────────────┘
```

### Branch-wise Summary (Optional Chart)
Show a bar chart or pie chart using `summary.by_branch` data grouped by branch with value/count.

### Data Table (Main Content)
Display `data` array in a sortable, filterable table with columns:

| Column               | Field                    | Notes                    |
|----------------------|--------------------------|--------------------------|
| Branch               | `branch`                 |                          |
| Item Code            | `item_code`              |                          |
| Item Name            | `item_name`              |                          |
| Group                | `item_group_name`        |                          |
| Quantity             | `quantity`               | Format with commas       |
| Litres               | `litres`                 | Format with commas       |
| Sub Group            | `sub_group`              |                          |
| Value (₹)            | `value`                  | Currency format          |
| Last Movement        | `last_movement_date`     | Format as DD/MM/YYYY     |
| Days Since Movement  | `days_since_last_movement`| Color code: >180=red, >90=orange, else yellow |
| Consumption Ratio    | `consumption_ratio`      | Percentage format        |

---

## Implementation Example (React + Axios)

```jsx
import { useState, useEffect } from 'react';
import axios from 'axios';

const API_BASE = '/api/v1/non-moving-rm';

function NonMovingRMDashboard() {
  const [itemGroups, setItemGroups] = useState([]);
  const [age, setAge] = useState(45);
  const [itemGroup, setItemGroup] = useState('');
  const [report, setReport] = useState(null);
  const [loading, setLoading] = useState(false);

  // Load item groups on mount
  useEffect(() => {
    axios.get(`${API_BASE}/item-groups/`, {
      headers: {
        'Authorization': `Bearer ${token}`,
        'Company-Code': companyCode,
      }
    }).then(res => {
      setItemGroups(res.data.data);
      if (res.data.data.length > 0) {
        setItemGroup(res.data.data[0].item_group_code);
      }
    });
  }, []);

  // Fetch report
  const fetchReport = async () => {
    setLoading(true);
    try {
      const res = await axios.get(`${API_BASE}/report/`, {
        params: { age, item_group: itemGroup },
        headers: {
          'Authorization': `Bearer ${token}`,
          'Company-Code': companyCode,
        }
      });
      setReport(res.data);
    } catch (err) {
      // Handle 400, 502, 503 errors
      console.error(err.response?.data?.detail);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      {/* Filters */}
      <select value={age} onChange={e => setAge(Number(e.target.value))}>
        {[30, 45, 60, 90, 180, 365].map(d => (
          <option key={d} value={d}>{d} days</option>
        ))}
      </select>

      <select value={itemGroup} onChange={e => setItemGroup(Number(e.target.value))}>
        {itemGroups.map(g => (
          <option key={g.item_group_code} value={g.item_group_code}>
            {g.item_group_name}
          </option>
        ))}
      </select>

      <button onClick={fetchReport} disabled={loading}>
        {loading ? 'Loading...' : 'Search'}
      </button>

      {/* Summary Cards */}
      {report && (
        <div className="summary-cards">
          <div>Total Items: {report.summary.total_items}</div>
          <div>Total Value: ₹{report.summary.total_value.toLocaleString()}</div>
          <div>Total Qty: {report.summary.total_quantity.toLocaleString()}</div>
        </div>
      )}

      {/* Data Table */}
      {report && (
        <table>
          <thead>
            <tr>
              <th>Branch</th>
              <th>Item Code</th>
              <th>Item Name</th>
              <th>Quantity</th>
              <th>Value</th>
              <th>Last Movement</th>
              <th>Days</th>
              <th>Consumption Ratio</th>
            </tr>
          </thead>
          <tbody>
            {report.data.map((item, i) => (
              <tr key={i}>
                <td>{item.branch}</td>
                <td>{item.item_code}</td>
                <td>{item.item_name}</td>
                <td>{item.quantity.toLocaleString()}</td>
                <td>₹{item.value.toLocaleString()}</td>
                <td>{item.last_movement_date}</td>
                <td>{item.days_since_last_movement}</td>
                <td>{item.consumption_ratio}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
```

---

## Company Codes

| Code             | Description       |
|------------------|-------------------|
| `JIVO_OIL`       | Jivo Oil          |
| `JIVO_MART`      | Jivo Mart         |
| `JIVO_BEVERAGES` | Jivo Beverages    |

---

## Notes

- The `age` parameter is the minimum number of days since the item's last stock movement.
- The `item_group` parameter corresponds to `ItmsGrpCod` in SAP B1's OITB table.
- `consumption_ratio` is calculated by the stored procedure — it indicates the consumption pattern of the item.
- `value` is the total inventory value of the non-moving item.
- All dates are returned in `YYYY-MM-DD HH:MM:SS` format from the report.
