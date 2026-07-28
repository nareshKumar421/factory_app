# Amazon Marketplace — Module Guide

Amazon fulfilment inside the `marketplace` app. Amazon reuses the **same downstream
flow as Flipkart** (import → resolve → warehouse issue → packing → outward scan →
confirm/DN → returns → reconciliation) but has its **own, deliberately separate order-sheet
parser** so changes to one channel never affect the other.

> Shared module reference: [`README.md`](./README.md) · Verified against code on **2026-07-28**.
> Where this doc and the code disagree, the code wins.

---

## 1. Design principle — full isolation from Flipkart

The two channels are separated at the **parser** layer, not just by a `channel` column:

| Layer | Flipkart | Amazon |
|---|---|---|
| Order-sheet parser | `services/order_sheet.py` | `services/amazon_sheet.py` **(separate file)** |
| Column mapping | Flipkart CSV headers | `AMAZON_COLUMNS` (MTR headers) |
| Dispatcher | `parse_rows_for(channel, …)` in `services/order_import_service.py` routes by `channel` |
| Everything after parse | **Identical, shared code** (resolve, issue, packing, dispatch, DN, returns) |

`parse_rows_for(MarketplaceChannel.AMAZON, …)` → Amazon parser;
`parse_rows_for(MarketplaceChannel.FLIPKART, …)` → Flipkart parser. Data is scoped by the
`channel` field on every model (`MarketplaceChannel.AMAZON = "AMAZON"`), so masters, orders,
dispatches and reports for the two channels never mix.

> **Isolation is under test** — `test_flipkart_unaffected_by_amazon_channel` asserts a Flipkart
> import still uses the Flipkart parser.

---

## 2. Input formats

Both **`.xlsx`** and **`.csv`** are accepted (upload is multipart; the channel is sent with the
file). The source sheet is the Amazon **MTR (Merchant Tax Report)**.

`.xlsx` parsing has **two paths** and needs **no server dependency**:
1. **openpyxl** if installed (`requirement.txt` pins `openpyxl>=3.1`).
2. **stdlib fallback** (`zipfile` + `xml.etree.ElementTree`) when openpyxl is absent — it reads the
   sheet XML directly and converts **Excel serial dates** to text.

> Both paths are tested: `test_amazon_xlsx_parses` (openpyxl) and
> `test_amazon_xlsx_parses_without_openpyxl` (openpyxl forced off).

---

## 3. Column mapping (`AMAZON_COLUMNS`)

The Amazon parser maps MTR columns → the canonical fields the shared flow expects
(`services/amazon_sheet.py`):

| Canonical field | Amazon (MTR) column | Notes |
|---|---|---|
| `order_id` | `Order Id` | Anchor id (`MarketplaceOrder.order_id`) |
| `order_item_id` | `Shipment Item Id` | |
| `shipment_id` | `Shipment Id` | |
| **`tracking`** | **`Shipment Id`** | **The scan key** — see §4 |
| `ordered_on` | `Order Date` | Excel-serial dates converted |
| `dispatch_by` | `Shipment Date` | Excel-serial dates converted |
| `order_state` | `Transaction Type` | Drives cancellation — see §5 |
| `order_type` | `Fulfillment Channel` | Always `AFN` (Amazon-fulfilled) |
| **`fsn`** | **`Asin`** | Amazon product key — **primary mapping key** |
| `sku` | `Sku` | Mapping fallback key |
| `product` | `Item Description` | |
| `quantity` | `Quantity` | |
| `hsn` | `Hsn/sac` | |
| `unit_price` | `Principal Amount` | |
| `invoice_amount` | `Invoice Amount` | |
| `cgst / igst / sgst` | `Cgst Tax / Igst Tax / Sgst Tax` | |
| `buyer` / `ship_to` / `city` | `Ship To City` | MTR carries no buyer name; ship-to city is the label |
| `state` | `Ship To State` | |
| `pin` | `Ship To Postal Code` | |

---

## 4. Scanning — what the operator scans at Outward

**Scan key = Amazon `Shipment Id` (e.g. `A09…`).** It is mapped to `tracking`, and stored on
`MarketplaceOrderLine.tracking_id`. Outward scanning uses the shared
`POST /dispatches/scan/` (scan-by-tracking) endpoint, which matches the scanned value against
`tracking_id`. This is the **same scan-then-confirm gate as Flipkart**: an order cannot be
confirmed until every line is scanned (`NOT_SCANNED` blocks confirm).

> Verified: `test_amazon_import_creates_orders` asserts the imported line's
> `tracking_id == Shipment Id`.

---

## 5. SKU / product resolution & cancellations

- **Mapping key priority:** `Asin` (→ `fsn`) first, then `Sku` (→ `marketplace_sku`) as fallback
  (`mappings.get(fsn) or mappings.get(sku)`). Configure `SkuMapping` rows with
  `channel = AMAZON`, keyed by ASIN (or SKU).
- **Cancellations:** an order whose `Transaction Type` is `cancel`, `refund` or `return`
  (`_CANCEL_TYPES`) is flagged `is_cancelled` on import and shown in the CANCELLED lane — it is not
  dispatched.

> Verified: `test_amazon_cancel_transaction_flags_cancelled`.

---

## 6. Posted-DN items CSV (both channels)

After a Delivery Note is posted, operators can download a per-DN items CSV:

```
GET /marketplace/delivery-notes/<doc_entry>/export.csv?channel=AMAZON
```

Returns an attachment with the DN header (number/date/warehouse), each item (name / UOM / HSN /
quantity required), the orders + channel it covers, and the SAP customer / branch / amount. The
export is HANA-free (built from resolve + order lines + warehouse master) and falls back to the
warehouse master's `sap_warehouse_code` when the order's is blank.

---

## 7. End-to-end flow (Amazon)

```
Upload MTR (.xlsx/.csv, channel=AMAZON)
  └─ parse_rows_for(AMAZON) → amazon_sheet.parse_amazon_rows
      └─ ingest() → OrderImportBatch(channel=AMAZON) + MarketplaceOrder/Line
          └─ Resolve (ASIN/SKU → FG/combo)  ── unmapped blocks the batch
              └─ Warehouse Issue request → approve → issue → receive
                  └─ Packing (by item group) → mark packed
                      └─ Outward: scan every Shipment Id  ── NOT_SCANNED blocks confirm
                          └─ Confirm → SAP Delivery Note (+ Goods Issue) + internal JI bill
                              └─ Posted-DN items CSV (optional)
                              └─ Returns (scan) → Reconciliation
```

Frontend: every marketplace page (Import, Packing, Issues, Delivery Notes, Batch detail) carries a
**Flipkart / Amazon toggle** (`MpChannelSelect`); the selected channel scopes the data and is sent
with each request.

---

## 8. Operator setup required before first Amazon import

Amazon config is **per-company and initially empty** — the code is live but the masters must be
created (all with `channel = AMAZON`):

1. **`MarketplaceSettings`** — Amazon channel settings for the company.
2. **`MarketplaceWarehouse`** — SAP godown (`sap_warehouse_code`) + business partner
   (`sap_customer_card_code`, used as the DN `CardCode`).
3. **`SkuMapping`** — one row per ASIN (or SKU) → FG item / combo.
4. (Optional) **`ComboDefinition`** for any Amazon combo/bundle SKUs.

Until these exist, imported Amazon orders stay **unmapped** and cannot flow past Resolve.

---

## 9. Tests

`marketplace/tests_sheet_flow.py → AmazonSheetTests`:

| Test | Asserts |
|---|---|
| `test_amazon_csv_parses_to_canonical` | CSV → canonical fields; `Shipment Id→tracking`, `Asin→fsn` |
| `test_amazon_import_creates_orders` | `ingest()` creates `channel=AMAZON` batch/order; line `tracking_id` = Shipment Id |
| `test_amazon_cancel_transaction_flags_cancelled` | `Transaction Type=Cancel` → `is_cancelled` |
| `test_amazon_xlsx_parses` | `.xlsx` via openpyxl |
| `test_amazon_xlsx_parses_without_openpyxl` | `.xlsx` via stdlib fallback + Excel-serial date conversion |
| `test_flipkart_unaffected_by_amazon_channel` | Isolation — Flipkart still uses the Flipkart parser |

Run: `python manage.py test marketplace --settings=config.sqlite_test_settings`
(full suite: **319 tests, OK**).
