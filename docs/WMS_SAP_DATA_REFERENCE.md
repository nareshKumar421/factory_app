# WMS - SAP HANA Data Reference

This document describes every SAP table, field, and query used by the WMS module. The WMS module is **READ-ONLY** — it does not insert, update, or delete any data in SAP.

---

## SAP Tables Used

### OITM — Item Master Data

| Field | Type | Description | Used In |
|-------|------|-------------|---------|
| `ItemCode` | string | Unique item code (e.g. `RM0000016`) | All queries |
| `ItemName` | string | Item description | Stock Overview, Item Detail, Dashboard |
| `ItmsGrpCod` | int | Item group code (FK to OITB) | Group filtering, Dashboard charts |
| `InvntryUom` | string | Inventory unit of measure (e.g. `LTR`, `KG`, `PCS`) | Stock Overview, Item Detail |
| `AvgPrice` | decimal | Average price (often 0 in this SAP instance) | Value calculation (fallback to LastPurPrc) |
| `LastPurPrc` | decimal | Last purchase price | Value calculation (primary), Item Detail |
| `MinLevel` | decimal | Minimum stock level | Stock status alerts (LOW/CRITICAL) |
| `MaxLevel` | decimal | Maximum stock level | Stock status alerts (OVERSTOCK) |

**Value Calculation:** `Stock Value = OnHand × CASE WHEN AvgPrice > 0 THEN AvgPrice ELSE LastPurPrc END`

This fallback was necessary because `AvgPrice` is 0 for all items in the current SAP instance. `LastPurPrc` provides the most recent purchase price.

---

### OITW — Item Warehouse Data (Stock Quantities)

| Field | Type | Description | Used In |
|-------|------|-------------|---------|
| `ItemCode` | string | FK to OITM | Joined on ItemCode |
| `WhsCode` | string | Warehouse code (FK to OWHS) | All stock queries |
| `OnHand` | decimal | Current quantity on hand | Stock levels |
| `IsCommited` | decimal | Quantity committed to orders | Committed qty |
| `OnOrder` | decimal | Quantity on order (incoming POs) | On order qty |
| `MinStock` | decimal | Warehouse-level min stock | Stock alerts |

**Derived Fields:**
- `Available = OnHand - IsCommited`
- `StockValue = OnHand × Price`

---

### OITB — Item Groups

| Field | Type | Description | Used In |
|-------|------|-------------|---------|
| `ItmsGrpCod` | int | Group code (PK) | Filter, Dashboard charts |
| `ItmsGrpNam` | string | Group name (e.g. `RAW MATERIAL`) | Display, Filter dropdown |

**Groups in Current Instance:**
- CONSUMABLES
- CONSUMABLES WITH INVENTORY
- FA CONSUMABLES
- FINISHED
- FIXED ASSETS
- PACKAGING MATERIAL
- RAW MATERIAL
- SALES BOM
- SEMI FINISHED GOODS
- TRADING ITEMS

---

### OWHS — Warehouses

| Field | Type | Description | Used In |
|-------|------|-------------|---------|
| `WhsCode` | string | Warehouse code (PK, e.g. `BH-BS`) | Warehouse list, Summary |
| `WhsName` | string | Warehouse full name | Display |
| `Inactive` | char | `Y`/`N` — inactive flag | Filter active only |

**Current Instance:** 46 warehouses across Bhakharpur (BH-*), Gujarat Plant (GP-*), Panipat (PB-*) etc.

---

### OINM — Inventory Audit (Movement History)

| Field | Type | Description | Used In |
|-------|------|-------------|---------|
| `ItemCode` | string | Item that moved | Movement list |
| `Warehouse` | string | Warehouse where movement occurred | Movement list |
| `InQty` | decimal | Quantity received/in | IN movements |
| `OutQty` | decimal | Quantity issued/out | OUT movements |
| `TransType` | int | SAP transaction type code | Movement type mapping |
| `DocDate` | date | Document date | Date filtering, sorting |
| `BASE_REF` | string | Base document reference | Reference display |
| `TransNum` | int | Transaction number | Sorting |
| `CreatedBy` | int | User who created the entry | Audit trail |

**Transaction Type Codes:**

| Code | SAP Document Type | Description |
|------|-------------------|-------------|
| 13 | OINV | AR Invoice (Sales) |
| 14 | ORIN | AR Credit Note |
| 15 | ODLN | Delivery Note |
| 16 | ORDN | Goods Return |
| 18 | OPCH | AP Invoice (Purchase) |
| 19 | ORPC | AP Credit Note |
| 20 | OPDN | Goods Receipt PO (GRPO) |
| 21 | ORPD | Return to Vendor |
| 59 | OIGN | Goods Receipt (non-PO) |
| 60 | OIGE | Goods Issue |
| 67 | OWTR | Inventory Transfer |
| 202 | OWOR | Production Order |

---

### OPDN — Goods Receipt PO (Header)

| Field | Type | Description | Used In |
|-------|------|-------------|---------|
| `DocEntry` | int | Internal document entry (PK) | Join to PDN1 |
| `DocNum` | int | Document number (visible) | Billing display |
| `DocDate` | date | Posting date | Date filtering |
| `CardCode` | string | Vendor code | Vendor filtering |
| `CardName` | string | Vendor name | Display |
| `CANCELED` | char | `Y`/`N` — canceled flag | Exclude canceled |

---

### PDN1 — Goods Receipt PO (Lines)

| Field | Type | Description | Used In |
|-------|------|-------------|---------|
| `DocEntry` | int | FK to OPDN | Join |
| `ItemCode` | string | Item received | Billing reconciliation |
| `Dscription` | string | Item description | Display |
| `Quantity` | decimal | Total quantity received | Received qty |
| `OpenCreQty` | decimal | Qty open for credit (not yet invoiced) | Unbilled qty calculation |
| `LineTotal` | decimal | Line total value | Received value |
| `WhsCode` | string | Target warehouse | Warehouse filter |

**Billing Reconciliation Logic:**
```
Billed Qty   = Quantity - OpenCreQty
Unbilled Qty = OpenCreQty
Billed Value = LineTotal × (1 - OpenCreQty / Quantity)
```

When an AP Invoice (OPCH) is created based on a GRPO, SAP automatically reduces `OpenCreQty` on the GRPO line. This is how we track billing without querying the AP Invoice tables directly.

---

## Data Flow Diagram

```
SAP HANA (Read-Only)
┌─────────────────────────────────────────────────────────┐
│                                                          │
│  OITM (Item Master)                                     │
│    ├── OITW (Warehouse Stock) ──── OWHS (Warehouses)    │
│    └── OITB (Item Groups)                                │
│                                                          │
│  OINM (Movement Audit Trail)                             │
│                                                          │
│  OPDN (GRPO Header) ── PDN1 (GRPO Lines)               │
│                                                          │
└─────────────────────────────────────────────────────────┘
         │ (HANA SQL queries via hdbcli)
         ▼
┌─────────────────────────────────────────────────────────┐
│  WMSHanaReader (warehouse/services/wms_hana_reader.py)  │
│                                                          │
│  Methods:                                                │
│  ├── get_dashboard_summary()    → OITM+OITW+OITB+OINM  │
│  ├── get_stock_overview()       → OITM+OITW+OITB        │
│  ├── get_item_detail()          → OITM+OITW+OITB        │
│  ├── get_stock_movements()      → OINM+OITM             │
│  ├── get_warehouse_summary()    → OITM+OITW+OWHS        │
│  ├── get_billing_overview()     → OPDN+PDN1             │
│  ├── get_warehouses()           → OWHS                  │
│  └── get_item_groups()          → OITB                  │
└─────────────────────────────────────────────────────────┘
         │ (JSON response)
         ▼
┌─────────────────────────────────────────────────────────┐
│  Django REST Views (warehouse/views_wms.py)             │
│  8 API endpoints at /api/v1/warehouse/wms/*             │
└─────────────────────────────────────────────────────────┘
         │ (HTTP/JSON)
         ▼
┌─────────────────────────────────────────────────────────┐
│  React Frontend (FactoryFlow)                           │
│  ├── WMS Dashboard     (charts + KPIs)                  │
│  ├── Stock Tracker      (table + filters + export)      │
│  ├── Billing Tracker    (reconciliation + charts)       │
│  └── Warehouse Compare  (cards + comparison charts)     │
└─────────────────────────────────────────────────────────┘
```

---

## HANA Connection Details

| Setting | Value | Source |
|---------|-------|--------|
| Host | `103.89.45.192` | `.env` → `HANA_HOST` |
| Port | `30015` | `.env` → `HANA_PORT` |
| User | `DSR` | `.env` → `HANA_USER` |
| Password | (see .env) | `.env` → `HANA_PASSWORD` |
| Driver | `hdbcli.dbapi` | Python SAP HANA client |

**Schema per Company:**

| Company Code | HANA Schema |
|-------------|-------------|
| `JIVO_OIL` | `JIVO_OIL_HANADB` |
| `JIVO_MART` | `JIVO_MART_HANADB` |
| `JIVO_BEVERAGES` | `JIVO_BEVERAGES_HANADB` |

The schema is resolved via `sap_client/registry.py` → `CompanyContext` → `HanaConnection`.

---

## What WMS Does NOT Do

The WMS module is strictly **read-only** for SAP data. It:

- Does **NOT** insert any records into SAP tables
- Does **NOT** update any records in SAP tables
- Does **NOT** delete any records in SAP tables
- Does **NOT** call SAP Service Layer APIs (POST/PATCH/DELETE)
- Does **NOT** write to PostgreSQL (no new Django models)

All data is queried live from SAP HANA and returned directly to the frontend. There is no local caching or data storage.

**Note:** Other existing warehouse features (BOM Requests, Material Issues, FG Receipts) DO write to SAP via the Service Layer. Those are documented in the existing warehouse module, not here.
