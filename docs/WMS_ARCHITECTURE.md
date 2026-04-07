# WMS Architecture

## System Overview

```
┌──────────────────────────────────────────────────────────────┐
│                     SAP Business One                         │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  HANA Database                                        │   │
│  │  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐       │   │
│  │  │ OITM │ │ OITW │ │ OITB │ │ OWHS │ │ OINM │       │   │
│  │  │ Item │ │Stock │ │Group │ │Wareh.│ │ Move │       │   │
│  │  └──────┘ └──────┘ └──────┘ └──────┘ └──────┘       │   │
│  │  ┌──────┐ ┌──────┐                                    │   │
│  │  │ OPDN │ │ PDN1 │                                    │   │
│  │  │ GRPO │ │Lines │                                    │   │
│  │  └──────┘ └──────┘                                    │   │
│  └──────────────────────────────────────────────────────┘   │
│              ↑ hdbcli (port 30015)                            │
└──────────────┼───────────────────────────────────────────────┘
               │
┌──────────────┼───────────────────────────────────────────────┐
│  Django Backend (factory_app)                                 │
│              │                                                │
│  ┌───────────▼──────────────────────────────────────────┐    │
│  │  sap_client/                                          │    │
│  │  ├── registry.py      Company → HANA config mapping   │    │
│  │  ├── context.py        CompanyContext wrapper          │    │
│  │  └── hana/connection.py  HanaConnection (hdbcli)      │    │
│  └──────────────────────────────────────────────────────┘    │
│              │                                                │
│  ┌───────────▼──────────────────────────────────────────┐    │
│  │  warehouse/services/wms_hana_reader.py                │    │
│  │  WMSHanaReader class                                  │    │
│  │  ├── get_dashboard_summary()                          │    │
│  │  ├── get_stock_overview()                             │    │
│  │  ├── get_item_detail()                                │    │
│  │  ├── get_stock_movements()                            │    │
│  │  ├── get_warehouse_summary()                          │    │
│  │  ├── get_billing_overview()                           │    │
│  │  ├── get_warehouses()                                 │    │
│  │  └── get_item_groups()                                │    │
│  └──────────────────────────────────────────────────────┘    │
│              │                                                │
│  ┌───────────▼──────────────────────────────────────────┐    │
│  │  warehouse/views_wms.py                               │    │
│  │  8 Django REST Framework APIViews                     │    │
│  │  Auth: JWT + Company-Code header                      │    │
│  └──────────────────────────────────────────────────────┘    │
│              │                                                │
│  ┌───────────▼──────────────────────────────────────────┐    │
│  │  warehouse/urls.py                                    │    │
│  │  /api/v1/warehouse/wms/*                              │    │
│  └──────────────────────────────────────────────────────┘    │
└──────────────┼───────────────────────────────────────────────┘
               │ HTTP/JSON (localhost:8000)
               │
┌──────────────┼───────────────────────────────────────────────┐
│  React Frontend (FactoryFlow)                                 │
│              │                                                │
│  ┌───────────▼──────────────────────────────────────────┐    │
│  │  core/api/client.ts (axios + JWT interceptors)        │    │
│  └──────────────────────────────────────────────────────┘    │
│              │                                                │
│  ┌───────────▼──────────────────────────────────────────┐    │
│  │  modules/warehouse/api/                               │    │
│  │  ├── wms.api.ts       API client functions            │    │
│  │  └── wms.queries.ts   React Query hooks               │    │
│  └──────────────────────────────────────────────────────┘    │
│              │                                                │
│  ┌───────────▼──────────────────────────────────────────┐    │
│  │  modules/warehouse/pages/                             │    │
│  │  ├── WMSDashboardPage      Charts + KPIs              │    │
│  │  ├── StockTrackerPage      Table + Filters + Export   │    │
│  │  ├── BillingTrackerPage    Reconciliation + Charts    │    │
│  │  └── WarehouseComparisonPage  Cards + Comparison      │    │
│  └──────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────┘
```

---

## Multi-Company Support

The WMS supports multiple SAP companies. The company is determined by the `Company-Code` header sent by the frontend (set during company selection after login).

```
Request Header: Company-Code: JIVO_OIL
       ↓
Django middleware → request.company.company.code = "JIVO_OIL"
       ↓
WMSHanaReader("JIVO_OIL")
       ↓
CompanyContext("JIVO_OIL") → schema = "JIVO_OIL_HANADB"
       ↓
All SQL queries use: FROM "JIVO_OIL_HANADB"."OITM" ...
```

---

## Authentication Flow

```
1. User logs in → JWT access token stored in IndexedDB
2. Frontend sends: Authorization: Bearer <token>
3. Frontend sends: Company-Code: JIVO_OIL
4. Django JWT auth validates token
5. HasCompanyContext permission checks company assignment
6. View calls _get_reader(request) → WMSHanaReader(company_code)
```

---

## Key Design Decisions

### 1. No PostgreSQL Models
The WMS reads everything live from SAP HANA. No local database tables are needed. This ensures data is always fresh and avoids sync issues.

### 2. No SAP Service Layer Writes
The WMS is purely read-only. Other warehouse features (BOM Requests, Material Issues, FG Receipts) handle SAP writes through the Service Layer — those are separate from WMS.

### 3. Price Fallback (AvgPrice → LastPurPrc)
The SAP instance has `AvgPrice = 0` for all items. The WMS uses `CASE WHEN AvgPrice > 0 THEN AvgPrice ELSE LastPurPrc END` in all value calculations.

### 4. Server-Side Pagination for Stock
Stock overview can return 3,800+ rows. Pagination is handled server-side (SAP HANA query returns all, Python slices by page). This is acceptable because the total query time is ~0.25s.

### 5. Client-Side Filtering for Billing
Billing table supports client-side search and status filter (applied after the API response). The full result is typically 200-300 items, small enough for client-side filtering.

---

## Performance

Measured on production SAP HANA instance (JIVO_OIL):

| API | Response Time | Data Size |
|-----|---------------|-----------|
| Dashboard | ~1.0s | 5 queries in sequence |
| Stock Overview | ~0.25s | 3,840 items → paginated |
| Item Detail | ~0.12s | Single item lookup |
| Movements | ~0.6s | Up to 100 movements |
| Warehouse Summary | ~0.13s | 39 warehouses |
| Billing Overview | ~0.12-0.27s | 280 items (date-filtered) |
| Warehouse List | ~0.10s | 46 warehouses |
| Item Groups | ~0.11s | 10 groups |

All queries are within acceptable limits for a dashboard application.

---

## Files Modified (Existing)

| File | Change |
|------|--------|
| `warehouse/urls.py` | Added 8 new WMS URL patterns |
| `src/config/constants/api.constants.ts` | Added 8 WMS endpoint constants |
| `src/modules/warehouse/types/index.ts` | Added WMS types export |
| `src/modules/warehouse/api/index.ts` | Added WMS API exports |
| `src/modules/warehouse/module.config.tsx` | Added 4 WMS routes + sidebar nav |

## Files Created (New)

| File | Purpose |
|------|---------|
| `warehouse/services/wms_hana_reader.py` | SAP HANA reader (8 methods) |
| `warehouse/views_wms.py` | 8 REST API views |
| `src/modules/warehouse/types/wms.types.ts` | TypeScript types |
| `src/modules/warehouse/api/wms.api.ts` | API client functions |
| `src/modules/warehouse/api/wms.queries.ts` | React Query hooks |
| `src/modules/warehouse/pages/WMSDashboardPage.tsx` | Dashboard page |
| `src/modules/warehouse/pages/StockTrackerPage.tsx` | Stock tracker page |
| `src/modules/warehouse/pages/BillingTrackerPage.tsx` | Billing tracker page |
| `src/modules/warehouse/pages/WarehouseComparisonPage.tsx` | Warehouse comparison page |
| `src/modules/warehouse/components/ItemDetailModal.tsx` | Item detail modal |
