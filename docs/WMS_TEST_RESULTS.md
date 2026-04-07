# WMS Test Results

**Date:** 2026-04-07
**Company:** JIVO_OIL (schema: JIVO_OIL_HANADB)
**HANA Host:** 103.89.45.192:30015

---

## Backend API Tests (16/16 PASSED)

| # | Test | Result | Time | Details |
|---|------|--------|------|---------|
| 1 | GET /wms/warehouses/ | PASS | 0.10s | 46 warehouses returned |
| 2 | GET /wms/item-groups/ | PASS | 0.11s | 10 groups returned |
| 3 | GET /wms/stock/overview/ (no filters) | PASS | 0.25s | 3,840 items, Value: 553,488,142 |
| 4 | GET /wms/stock/overview/?warehouse_code=BH-LO | PASS | 0.20s | 30 items in BH-LO |
| 5 | GET /wms/stock/overview/?search=OLIVE | PASS | 0.24s | 511 OLIVE items |
| 6 | GET /wms/stock/items/RM0000016/ | PASS | 0.12s | CRUDE DEGUMMED RAPESEED OIL, 1 warehouse |
| 7 | GET /wms/stock/items/FAKE999/ | PASS | - | Correctly returns null |
| 8 | GET /wms/stock/movements/?item_code=RM0000016 | PASS | 0.61s | 10 movements |
| 9 | GET /wms/stock/movements/ (date+warehouse filter) | PASS | 0.65s | 10 movements in GP-FG |
| 10 | GET /wms/dashboard/ | PASS | 1.08s | KPIs, 36 WH charts, 7 groups, 10 top items, 20 movements |
| 11 | GET /wms/dashboard/?warehouse_code=GP-FG | PASS | 1.46s | 276 items in GP-FG |
| 12 | GET /wms/warehouses/summary/ | PASS | 0.13s | 39 warehouses with health metrics |
| 13 | GET /wms/billing/overview/ (no filters) | PASS | 0.27s | 1,682 items, Unbilled: 124,464,744 |
| 14 | GET /wms/billing/overview/ (date range) | PASS | 0.12s | 280 items, Unbilled: 82,169,196 |
| 15 | GET /wms/billing/overview/?warehouse_code=BH-LO | PASS | 0.21s | 0 items (no GRPOs to BH-LO) |
| 16 | Cross-company (JIVO_MART) | PASS | 0.13s | 28 warehouses in JIVO_MART |

---

## Frontend Build Tests

| Test | Result | Details |
|------|--------|---------|
| TypeScript compilation (`tsc --noEmit`) | PASS | 0 errors |
| Vite production build | PASS | Built in 12.59s |
| Django URL resolution (all 8 endpoints) | PASS | All resolve correctly |
| Django import check (views + reader) | PASS | All modules import OK |

---

## Data Snapshot (JIVO_OIL, 2026-04-07)

### Stock KPIs
- Total unique items: 1,346
- Total stock on hand: 18,093,080 units
- Total stock value: ₹55.35 Cr
- Low stock items: 704
- Critical stock items: 600
- Zero stock items: 164
- Overstock items: 2

### Top Warehouses by Value
1. BH-LO: ₹14.32 Cr (30 items)
2. BH-FA: ₹10.98 Cr (269 items)
3. BH-CRUDE: ₹9.23 Cr (3 items)
4. GP-FG: ₹6.59 Cr (142 items)
5. BH-EC: ₹3.12 Cr (110 items)

### Top Items by Value
1. RM0000016 (CRUDE DEGUMMED RAPESEED OIL NEW): ₹9.23 Cr
2. RM0000002 (CANOLA COLD PRESS LOOSE OIL OLD): ₹4.09 Cr
3. RM0000012 (EXTRA LIGHT OLIVE LOSSE OIL IMPORTED): ₹3.79 Cr

### Billing Reconciliation (Jan-Apr 2026)
- Total received value: ₹83.98 Cr
- Total billed value: ₹75.77 Cr
- Unbilled value: ₹8.22 Cr
- Fully billed items: 156
- Partially billed items: 56
- Unbilled items: 68
