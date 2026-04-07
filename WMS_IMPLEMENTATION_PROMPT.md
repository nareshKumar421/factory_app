# WMS Implementation Prompt - FactoryFlow

## Objective

Build a full-featured Warehouse Management System (WMS) module inside FactoryFlow (Django + React) that provides **real-time stock tracking, billing quantity management, warehouse inventory visibility, and rich visual dashboards** — all integrated with SAP Business One via HANA reads and Service Layer writes.

---

## Scope Summary

| Area | What We Build | Data Source |
|------|---------------|-------------|
| Stock Tracking | Real-time on-hand, committed, ordered, available quantities per item per warehouse | SAP HANA (OITM, OITW) |
| Billing Quantities | Track billed vs unbilled stock, invoice-linked quantities, billing discrepancies | SAP HANA (OINV, INV1, OPCH, PCH1) |
| Warehouse Stock | Warehouse-wise stock breakdown, inter-warehouse comparison, min/max/reorder levels | SAP HANA (OITW) + PostgreSQL |
| Stock Movements | Inbound (GRPO, Production Receipt), Outbound (Goods Issue, Dispatch), Transfers | SAP HANA (OINM) + PostgreSQL |
| Visual Dashboards | Charts, KPIs, heatmaps, trend lines, alerts | Aggregated from all sources |

---

## Phase 1: Stock Tracking & Inventory Visibility

### 1.1 Stock Overview API

**Backend: `warehouse/views/stock_views.py`**

Build APIs that read from SAP HANA and return stock data:

```
GET /api/v1/wms/stock/overview/
    ?warehouse_code=&item_group=&search=&page=&page_size=
    Response: {
        summary: { total_items, total_value, total_on_hand, total_committed, total_available },
        items: [
            {
                item_code, item_name, item_group, uom,
                on_hand, committed, ordered, available,
                avg_price, total_value,
                warehouse_code, warehouse_name,
                last_movement_date,
                stock_status: "NORMAL" | "LOW" | "CRITICAL" | "OVERSTOCK" | "ZERO"
            }
        ],
        pagination: { total, page, page_size, pages }
    }
```

### 1.2 Item Detail API

```
GET /api/v1/wms/stock/items/{item_code}/
    Response: {
        item: { item_code, item_name, item_group, uom, avg_price, last_purchase_price },
        warehouse_breakdown: [
            { warehouse_code, warehouse_name, on_hand, committed, ordered, available, value }
        ],
        stock_summary: { total_on_hand, total_committed, total_available, total_value },
        reorder_info: { min_level, max_level, reorder_point, reorder_qty },
        recent_movements: [ last 20 movements ]
    }
```

### 1.3 Stock Movement History

```
GET /api/v1/wms/stock/movements/
    ?item_code=&warehouse_code=&movement_type=&from_date=&to_date=&page=
    Response: {
        movements: [
            {
                date, item_code, item_name, warehouse_code,
                movement_type: "IN" | "OUT" | "TRANSFER",
                transaction_type: "GRPO" | "GOODS_ISSUE" | "PRODUCTION_RECEIPT" | "TRANSFER" | "RETURN",
                quantity, direction, reference_doc, created_by
            }
        ]
    }
```

### 1.4 SAP HANA Queries (Backend: `warehouse/hana_readers/stock_reader.py`)

```sql
-- Stock overview across warehouses
SELECT T0."ItemCode", T0."ItemName", T0."ItmsGrpCod", T0."InvntryUom",
       T0."AvgPrice", T0."LastPurPrc",
       T1."WhsCode", T1."WhsName", T1."OnHand", T1."IsCommited", T1."OnOrder",
       (T1."OnHand" - T1."IsCommited") AS "Available",
       (T1."OnHand" * T0."AvgPrice") AS "StockValue",
       T0."MinLevel", T0."MaxLevel", T0."ReorderPnt", T0."ReorderQty"
FROM "{schema}"."OITM" T0
INNER JOIN "{schema}"."OITW" T1 ON T0."ItemCode" = T1."ItemCode"
LEFT JOIN "{schema}"."OWHS" T2 ON T1."WhsCode" = T2."WhsCode"
WHERE T1."OnHand" <> 0 OR T1."IsCommited" <> 0 OR T1."OnOrder" <> 0

-- Movement history from inventory audit
SELECT "ItemCode", "Warehouse", "InQty", "OutQty",
       "TransType", "CreatedBy", "DocDate", "Ref1", "Ref2",
       "DocNum", "BASE_REF"
FROM "{schema}"."OINM"
WHERE "ItemCode" = :item_code
ORDER BY "DocDate" DESC
```

---

## Phase 2: Billing Quantity Tracking

### 2.1 Purpose

Track the relationship between **stock received** vs **stock billed** (invoiced). This helps identify:
- Items received but not yet billed (pending invoices)
- Items billed but quantities don't match received quantities (discrepancies)
- Billing aging (how long items sit unbilled)

### 2.2 Billing Overview API

```
GET /api/v1/wms/billing/overview/
    ?warehouse_code=&vendor=&from_date=&to_date=&status=
    Response: {
        summary: {
            total_received_qty, total_billed_qty, total_unbilled_qty,
            total_received_value, total_billed_value, total_unbilled_value,
            discrepancy_count
        },
        items: [
            {
                item_code, item_name,
                grpo_qty, grpo_value,          -- received quantities
                invoice_qty, invoice_value,    -- billed quantities
                unbilled_qty, unbilled_value,  -- difference
                status: "FULLY_BILLED" | "PARTIALLY_BILLED" | "UNBILLED" | "OVERBILLED",
                last_grpo_date, last_invoice_date,
                aging_days                     -- days since receipt without full billing
            }
        ]
    }
```

### 2.3 Billing Detail Per Item

```
GET /api/v1/wms/billing/items/{item_code}/
    Response: {
        item: { item_code, item_name },
        receipts: [
            { grpo_doc_num, date, vendor, qty, value, warehouse }
        ],
        invoices: [
            { invoice_doc_num, date, vendor, qty, value, linked_grpo }
        ],
        reconciliation: {
            total_received, total_billed, variance, status
        }
    }
```

### 2.4 SAP HANA Queries for Billing

```sql
-- GRPO quantities (what was received)
SELECT T1."ItemCode", T1."Dscription", T1."Quantity", T1."WhsCode",
       T0."DocNum", T0."DocDate", T0."CardCode", T0."CardName"
FROM "{schema}"."OPDN" T0
INNER JOIN "{schema}"."PDN1" T1 ON T0."DocEntry" = T1."DocEntry"
WHERE T0."DocDate" BETWEEN :from_date AND :to_date

-- AP Invoice quantities (what was billed)
SELECT T1."ItemCode", T1."Dscription", T1."Quantity", T1."WhsCode",
       T0."DocNum", T0."DocDate", T0."CardCode", T0."CardName",
       T1."BaseRef", T1."BaseEntry", T1."BaseLine"
FROM "{schema}"."OPCH" T0
INNER JOIN "{schema}"."PCH1" T1 ON T0."DocEntry" = T1."DocEntry"
WHERE T0."DocDate" BETWEEN :from_date AND :to_date

-- Unbilled GRPOs (received but no invoice linked)
SELECT T1."ItemCode", T1."Dscription",
       T1."Quantity" AS "ReceivedQty",
       IFNULL(T1."Quantity" - T1."OpenCreQty", 0) AS "BilledQty",
       T1."OpenCreQty" AS "UnbilledQty",
       T0."DocNum", T0."DocDate", T0."CardName"
FROM "{schema}"."OPDN" T0
INNER JOIN "{schema}"."PDN1" T1 ON T0."DocEntry" = T1."DocEntry"
WHERE T1."OpenCreQty" > 0
```

---

## Phase 3: Warehouse Stock Management

### 3.1 Warehouse-Wise Dashboard

```
GET /api/v1/wms/warehouses/
    Response: {
        warehouses: [
            {
                warehouse_code, warehouse_name, location,
                total_items, total_value, total_on_hand,
                low_stock_count, critical_stock_count, overstock_count,
                utilization_pct,        -- if capacity is defined
                last_movement_date,
                pending_inbound: { count, qty },   -- pending GRPOs/transfers coming in
                pending_outbound: { count, qty }   -- pending issues/transfers going out
            }
        ]
    }
```

### 3.2 Warehouse Detail

```
GET /api/v1/wms/warehouses/{warehouse_code}/
    Response: {
        warehouse: { code, name, location, capacity },
        stock_summary: {
            total_items, total_value,
            by_item_group: [{ group, item_count, value, pct_of_total }]
        },
        stock_health: {
            normal: count, low: count, critical: count, overstock: count, zero: count
        },
        top_items_by_value: [ top 10 ],
        top_items_by_movement: [ top 10 most active ],
        slow_moving: [ items with no movement in 30+ days ],
        items: [ paginated full item list ]
    }
```

### 3.3 Stock Alerts & Notifications (PostgreSQL Model)

```python
class StockAlert(models.Model):
    ALERT_TYPES = [
        ('LOW_STOCK', 'Stock Below Minimum'),
        ('CRITICAL_STOCK', 'Stock Critically Low'),
        ('OVERSTOCK', 'Stock Exceeds Maximum'),
        ('ZERO_STOCK', 'Zero Stock'),
        ('NO_MOVEMENT', 'No Movement (Aging)'),
        ('BILLING_DISCREPANCY', 'Billing Mismatch'),
    ]

    alert_type          # Choice from above
    item_code
    item_name
    warehouse_code
    warehouse_name
    current_qty
    threshold_qty       # min/max level that triggered alert
    severity            # INFO, WARNING, CRITICAL
    is_acknowledged     # Has someone seen this?
    acknowledged_by     # FK -> User
    acknowledged_at
    is_resolved         # Auto-resolved when stock corrected
    resolved_at
    company             # FK -> Company
    created_at
```

---

## Phase 4: Visual Dashboards (Frontend)

### 4.1 Main WMS Dashboard (`/wms/dashboard`)

**Layout: Grid of cards + charts**

```
┌─────────────────────────────────────────────────────────────────────┐
│  WAREHOUSE MANAGEMENT SYSTEM                    [Warehouse: All v] │
├──────────┬──────────┬──────────┬──────────┬─────────────────────────┤
│  Total   │  Total   │  Low     │  Billing │                        │
│  Items   │  Value   │  Stock   │  Pending │    Stock Value Trend   │
│  1,245   │  45.2 Cr │  23      │  12      │    (Line Chart - 30d)  │
│          │          │  !! warn │          │                        │
├──────────┴──────────┴──────────┴──────────┤                        │
│                                           │                        │
│  Stock by Warehouse (Bar Chart)           │                        │
│  ████ WH01 - 450 items                    │                        │
│  ██████ WH02 - 520 items                  ├────────────────────────┤
│  ███ WH03 - 275 items                     │  Recent Movements      │
│                                           │  > RM001 IN +100 WH01  │
├───────────────────────────────────────────┤  > FG012 OUT -50 WH02  │
│                                           │  > RM045 XFER WH01→02 │
│  Stock Health Donut Chart                 │  > ...                 │
│  [Normal 78%][Low 12%][Critical 5%][OS 5%]│                        │
│                                           ├────────────────────────┤
├───────────────────────────────────────────┤  Active Alerts          │
│                                           │  ! RM023 LOW STOCK     │
│  Item Group Breakdown (Pie/Treemap)       │  !! FG001 ZERO STOCK   │
│  Raw Material: 45%                        │  ! PM005 OVERSTOCK     │
│  Finished Goods: 30%                      │  > View All (23)       │
│  Packing Material: 15%                    │                        │
│  Consumables: 10%                         │                        │
└───────────────────────────────────────────┴────────────────────────┘
```

**Charts to implement:**
1. **Stock Value Trend** - Line chart showing total stock value over 30/60/90 days
2. **Stock by Warehouse** - Horizontal bar chart comparing warehouses
3. **Stock Health Distribution** - Donut chart (Normal / Low / Critical / Overstock / Zero)
4. **Item Group Breakdown** - Treemap or pie chart by item group value
5. **Top 10 Items by Value** - Bar chart
6. **Movement Trend** - Area chart showing daily IN vs OUT quantities

### 4.2 Stock Tracker Page (`/wms/stock`)

**Features:**
- Searchable, filterable, sortable table of all items
- Columns: Item Code, Name, Group, Warehouse, On-Hand, Committed, Available, Value, Status
- Status badges: `NORMAL` (green), `LOW` (yellow), `CRITICAL` (red), `OVERSTOCK` (blue), `ZERO` (gray)
- Click row -> Item detail page with warehouse breakdown + movement history
- Filters: Warehouse, Item Group, Stock Status, Search
- Export to Excel button
- Visual: Sparkline mini-charts showing 7-day movement trend per item (optional)

### 4.3 Billing Tracker Page (`/wms/billing`)

**Features:**
- Summary cards: Total Received Value, Total Billed Value, Unbilled Value, Discrepancies
- Filterable table: Item, Received Qty, Billed Qty, Unbilled Qty, Status, Aging Days
- Status badges: `FULLY_BILLED` (green), `PARTIALLY_BILLED` (yellow), `UNBILLED` (red), `OVERBILLED` (purple)
- Click row -> Billing detail with receipt-to-invoice matching
- Charts:
  - **Billing Status Pie** - % fully billed vs partial vs unbilled
  - **Unbilled Aging Bar** - Items grouped by aging buckets (0-7d, 7-15d, 15-30d, 30+d)
  - **Monthly Billing Trend** - Received vs Billed amounts over months

### 4.4 Warehouse Comparison Page (`/wms/warehouses`)

**Features:**
- Card view: Each warehouse as a card showing key metrics
- Comparison bar charts: Side-by-side stock values, item counts, movement volumes
- Warehouse detail drill-down with full item listing
- Heatmap: Item availability across warehouses (rows = items, cols = warehouses, color = qty)

### 4.5 Alerts & Notifications Page (`/wms/alerts`)

**Features:**
- Real-time alert feed with severity indicators
- Filter by type, severity, warehouse, acknowledged/unacknowledged
- Acknowledge button to dismiss alerts
- Auto-resolve when stock levels return to normal
- Summary bar: X Critical, Y Warnings, Z Info

---

## Phase 5: Reports & Analytics

### 5.1 Stock Valuation Report

```
GET /api/v1/wms/reports/stock-valuation/
    ?warehouse_code=&item_group=&as_of_date=
    Response: {
        valuation_date,
        warehouses: [
            {
                warehouse_code, warehouse_name,
                groups: [
                    {
                        group_name, item_count,
                        total_qty, total_value,
                        items: [{ item_code, name, qty, avg_price, value }]
                    }
                ],
                warehouse_total_value
            }
        ],
        grand_total_value
    }
```

### 5.2 Movement Summary Report

```
GET /api/v1/wms/reports/movement-summary/
    ?warehouse_code=&from_date=&to_date=&group_by=daily|weekly|monthly
    Response: {
        periods: [
            {
                period: "2026-04-01",
                inbound_qty, inbound_value,
                outbound_qty, outbound_value,
                net_change_qty, net_change_value,
                transfer_in_qty, transfer_out_qty
            }
        ],
        totals: { ... }
    }
```

### 5.3 Slow/Non-Moving Stock Report

```
GET /api/v1/wms/reports/slow-moving/
    ?days_threshold=30&warehouse_code=
    Response: {
        items: [
            {
                item_code, item_name, warehouse_code,
                on_hand, value,
                last_movement_date, days_since_movement,
                category: "SLOW" | "NON_MOVING" | "DEAD_STOCK"
            }
        ],
        summary: { slow_count, non_moving_count, dead_count, total_locked_value }
    }
```

---

## Tech Stack & Libraries

### Backend (Django)
- Django REST Framework for APIs
- SAP HANA reader (existing `sap_client` module) for stock data
- APScheduler for periodic stock alert checks
- django-filter for query filtering
- openpyxl for Excel exports

### Frontend (React)
- **Recharts** or **Chart.js** (via react-chartjs-2) for all charts
- **TanStack Table** for data tables with sorting, filtering, pagination
- **React Query** (TanStack Query) for API data fetching & caching
- **Tailwind CSS** + **shadcn/ui** for UI components (match existing app style)
- **react-hot-toast** for notifications
- **date-fns** for date manipulation

---

## Implementation Order

| Step | Task | Priority |
|------|------|----------|
| 1 | Stock Overview API + HANA reader | HIGH |
| 2 | Stock Tracker frontend page with table | HIGH |
| 3 | Main WMS Dashboard with KPI cards | HIGH |
| 4 | Dashboard charts (stock by warehouse, health donut, group breakdown) | HIGH |
| 5 | Item Detail API + detail page | HIGH |
| 6 | Stock Movement History API + movement list | MEDIUM |
| 7 | Billing Overview API + HANA queries | MEDIUM |
| 8 | Billing Tracker frontend page | MEDIUM |
| 9 | Warehouse Comparison page | MEDIUM |
| 10 | Stock Alerts model + background checker | MEDIUM |
| 11 | Alerts frontend page | MEDIUM |
| 12 | Stock Value Trend chart (historical) | LOW |
| 13 | Reports (Valuation, Movement Summary, Slow-Moving) | LOW |
| 14 | Excel export for all tables | LOW |
| 15 | Movement trend sparklines | LOW |

---

## Models Summary (PostgreSQL - New)

```python
# warehouse/models.py (additions to existing)

class StockAlert(models.Model):
    """Tracks stock level alerts and billing discrepancies"""

class StockSnapshot(models.Model):
    """Daily stock snapshots for historical trend charts"""
    snapshot_date
    item_code, item_name, warehouse_code
    on_hand, committed, ordered, available
    value
    company

class BillingReconciliation(models.Model):
    """Tracks billing status per GRPO line"""
    grpo_doc_entry, grpo_doc_num, grpo_line
    item_code, item_name
    received_qty, received_value
    billed_qty, billed_value
    status  # UNBILLED, PARTIALLY_BILLED, FULLY_BILLED, OVERBILLED
    last_checked_at
    company
```

---

## API URL Structure

```
/api/v1/wms/
    stock/
        overview/                    # Stock overview with filters
        items/{item_code}/           # Item detail with warehouse breakdown
        movements/                   # Movement history
    billing/
        overview/                    # Billing reconciliation overview
        items/{item_code}/           # Billing detail per item
    warehouses/                      # Warehouse list with metrics
        {warehouse_code}/            # Warehouse detail
    alerts/                          # Stock alerts
        {id}/acknowledge/            # Acknowledge alert
    reports/
        stock-valuation/             # Stock valuation report
        movement-summary/            # Movement summary report
        slow-moving/                 # Slow/non-moving stock report
    dashboard/
        summary/                     # Dashboard KPIs & summary data
        charts/stock-trend/          # Historical stock value trend
        charts/movement-trend/       # Daily movement trend data
```

---

## Permissions

```python
# New permissions for WMS
warehouse.can_view_stock           # View stock levels
warehouse.can_view_billing         # View billing reconciliation
warehouse.can_manage_alerts        # Acknowledge/manage alerts
warehouse.can_view_reports         # Access reports
warehouse.can_export_data          # Export to Excel
warehouse.can_view_dashboard       # Access WMS dashboard
```

---

## Key Considerations

1. **SAP is source of truth** - All stock quantities come from SAP HANA reads. PostgreSQL stores only operational data (alerts, snapshots, reconciliation status).
2. **Performance** - Stock overview queries can be heavy. Use pagination, caching (5-min TTL), and limit default results.
3. **Stock Snapshots** - Run a daily scheduled job to capture stock levels for trend charts. Without snapshots, we can't show historical trends.
4. **Billing Reconciliation** - Run periodic sync (hourly/daily) to match GRPOs with AP Invoices and update reconciliation status.
5. **Alerts** - Run every 15 minutes via APScheduler. Use cooldown periods to avoid alert spam (existing StockAlertLog pattern).
6. **Frontend Charting** - Use consistent color scheme across all charts. Match FactoryFlow's existing design system.
