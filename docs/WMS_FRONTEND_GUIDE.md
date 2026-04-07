# WMS Frontend Guide

## Pages & Routes

| Route | Page | Description |
|-------|------|-------------|
| `/wms` | WMS Dashboard | KPIs, charts, recent movements |
| `/wms/stock` | Stock Tracker | Searchable stock table with filters, pagination, export |
| `/wms/billing` | Billing Tracker | GRPO vs Invoice reconciliation with charts |
| `/wms/warehouses` | Warehouse Comparison | Per-warehouse cards and comparison charts |

All routes are registered in `src/modules/warehouse/module.config.tsx` and appear under the "WMS" sidebar section.

---

## File Structure

```
src/modules/warehouse/
├── api/
│   ├── wms.api.ts            # API client functions (axios calls)
│   ├── wms.queries.ts        # React Query hooks
│   ├── warehouse.api.ts      # Existing BOM/FG API (unchanged)
│   ├── warehouse.queries.ts  # Existing BOM/FG hooks (unchanged)
│   └── index.ts              # Re-exports everything
├── components/
│   └── ItemDetailModal.tsx   # Item detail popup with warehouse breakdown chart
├── pages/
│   ├── WMSDashboardPage.tsx       # Main WMS dashboard
│   ├── StockTrackerPage.tsx       # Stock table page
│   ├── BillingTrackerPage.tsx     # Billing reconciliation page
│   ├── WarehouseComparisonPage.tsx # Warehouse comparison page
│   ├── WarehouseDashboardPage.tsx  # Existing (unchanged)
│   ├── BOMRequestListPage.tsx      # Existing (unchanged)
│   ├── BOMRequestDetailPage.tsx    # Existing (unchanged)
│   └── FGReceiptListPage.tsx       # Existing (unchanged)
├── types/
│   ├── wms.types.ts          # All WMS TypeScript types
│   ├── warehouse.types.ts    # Existing types (unchanged)
│   └── index.ts              # Re-exports both
├── module.config.tsx          # Route + nav config (updated)
└── index.ts
```

---

## Pages Detail

### 1. WMS Dashboard (`/wms`)

**Component:** `WMSDashboardPage.tsx`

**Features:**
- Warehouse dropdown filter (filters all data)
- 4 KPI cards: Total Items, Total Value, Low Stock, Critical/Zero
- Stock Value by Warehouse (horizontal bar chart)
- Stock Health Distribution (donut chart: Normal/Low/Critical/Zero/Overstock)
- Stock by Item Group (pie chart)
- Top 10 Items by Value (horizontal bar chart)
- Recent Movements table (last 20)

**Data Hook:** `useWMSDashboard(warehouseCode?)`

**Charts Library:** Recharts (BarChart, PieChart, ResponsiveContainer)

---

### 2. Stock Tracker (`/wms/stock`)

**Component:** `StockTrackerPage.tsx`

**Features:**
- Search box (filters by item code or name)
- Warehouse dropdown filter
- Item Group dropdown filter
- Stock filter (With Stock / All / Zero Stock)
- Summary bar (Total Items, On Hand, Committed, Available, Value)
- Data table with columns: Code, Name, Group, Warehouse, On Hand, Committed, Available, Value, Status, Detail
- Status badges: NORMAL (green), LOW (amber), CRITICAL (red), OVERSTOCK (blue), ZERO (gray)
- Pagination (50 items per page)
- CSV export button
- Click eye icon → Item Detail Modal

**Data Hooks:** `useStockOverview(filters)`, `useWMSWarehouses()`, `useWMSItemGroups()`

---

### 3. Item Detail Modal

**Component:** `ItemDetailModal.tsx`

**Triggered by:** Eye icon click in Stock Tracker

**Features:**
- Item info (name, group, UoM, price)
- 4 summary cards (On Hand, Committed, Available, Value)
- Warehouse breakdown bar chart (On Hand / Committed / Available per warehouse)
- Recent movements table (last 20 for that item)

**Data Hooks:** `useItemDetail(itemCode)`, `useStockMovements({ item_code })`

---

### 4. Billing Tracker (`/wms/billing`)

**Component:** `BillingTrackerPage.tsx`

**Features:**
- Date range filter (From / To)
- Warehouse dropdown filter
- Status filter (Fully Billed / Partially Billed / Unbilled)
- Search box (client-side filter by item code/name)
- 6 summary cards: Received Value, Billed Value, Unbilled Value, Fully Billed count, Partial count, Unbilled count
- Billing Status Distribution (donut chart)
- Received vs Billed vs Unbilled (bar chart)
- Data table: Code, Name, Warehouse, Received Qty, Billed Qty, Unbilled Qty, Unbilled Value, Status
- CSV export button

**Data Hooks:** `useBillingOverview(filters)`, `useWMSWarehouses()`

---

### 5. Warehouse Comparison (`/wms/warehouses`)

**Component:** `WarehouseComparisonPage.tsx`

**Features:**
- Warehouse cards grid (code, name, items, value, low stock, critical/zero counts)
- Alert icon on warehouses with critical or zero stock
- Stock Value by Warehouse (horizontal bar chart, top 15)
- Stock Health by Warehouse (stacked horizontal bar: Low/Critical/Overstock/Zero)

**Data Hook:** `useWarehouseSummary()`

---

## React Query Keys

All WMS queries use the `['wms']` prefix for cache invalidation:

```typescript
WMS_QUERY_KEYS = {
  all:              ['wms'],
  dashboard:        ['wms', 'dashboard', warehouseCode],
  stockOverview:    ['wms', 'stock-overview', filters],
  itemDetail:       ['wms', 'item', itemCode],
  movements:        ['wms', 'movements', filters],
  warehouseSummary: ['wms', 'warehouse-summary'],
  billing:          ['wms', 'billing', filters],
  warehouses:       ['wms', 'warehouses'],
  itemGroups:       ['wms', 'item-groups'],
}
```

---

## Value Formatting

All pages use a shared `formatValue()` function:

| Range | Format | Example |
|-------|--------|---------|
| >= 1 Crore (10M) | `X.XX Cr` | `55.35 Cr` |
| >= 1 Lakh (100K) | `X.XX L` | `1.43 L` |
| >= 1000 | `X.X K` | `13.0 K` |
| < 1000 | Raw number | `456` |

Currency values are prefixed with `₹`.

---

## Dependencies Added

| Package | Version | Purpose |
|---------|---------|---------|
| `recharts` | 3.8.1 | Chart components (BarChart, PieChart, etc.) |

All other dependencies (React Query, axios, Tailwind, shadcn/ui, Lucide) were already installed.
