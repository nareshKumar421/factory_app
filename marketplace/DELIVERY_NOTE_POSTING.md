# Marketplace Delivery Note — real SAP posting

Status: **code ready + SAP verified; the live test post needs your explicit sign-off**
(a production-SAP guardrail blocked the auto post — see §5).

---

## 1. What I built
- **`mp_post_delivery_note`** management command — posts one real Delivery Note
  through the **production code path** (`MarketplaceSapGateway` → `DeliveryNoteWriter`
  → `POST /b1s/v2/DeliveryNotes`). Requires `--confirm`; without it, dry-run only.
- **`mp_sap_explore`** / **`mp_sap_probe`** — READ-ONLY commands that pull SAP master
  data (customers, warehouses, items+stock, tax codes) so we post with real values.
- (Already live from earlier work) idempotent posting + Warehouse-master config
  (`sap_series`, `sap_tax_code`, `post_goods_issue`).

## 2. SAP access — verified
- Service Layer `https://103.89.45.192:50000`, CompanyDB `JIVO_MART_HANADB`.
- **Login OK.** This is the **live** company DB (no separate test DB found), so any
  post is a real document.

## 3. What I found in SAP (real values)
| Thing | Examples found |
|-------|----------------|
| Customers (CardCode) | `CUSTA000356` FREE SAMPLE · `CUSTA000926` JIVO MART PVT LTD -ISD- DL · `CUSTA000250` ONLINE CASH SALE WEBSITE |
| Warehouses | `DL-EC` (Delhi e-commerce) · `DL-MP` · `BH-FGM` · `01` General … |
| Items (all UoM = **PCS**) | `FG0000329` Yellow Mustard Oil 1L · `FG0000151` Sano Pomace Olive 5L |
| Stock (FG0000329) | DL-EC **102**, DL-MP 80, DL-FG 3 … |
| Tax codes (VatGroup) | `CG+SG@18`, `CG+SG@5`, `IGST@18`, `IGST@5`, `Exampt` … |

> Note: the current marketplace **Warehouse master** still has placeholder values
> (`C-FLIPKART`, `FLP-MAY`, `BH-EC`) that are **not** real SAP codes, and the SKU
> mappings point to demo items (`FG00001`). Real usage needs these replaced with the
> real values above (see §6).

## 4. The exact test transaction I prepared (safest possible)
```
company   : JIVO_MART
CardCode  : CUSTA000356   (FREE SAMPLE — the account meant for sample movements)
Warehouse : DL-EC         (has 102 in stock → post leaves 101)
Item      : FG0000329     (Yellow Mustard Oil 1 LTR)
Quantity  : 1  PCS
VatGroup  : (BP/item default)   — or CG+SG@18 if you prefer explicit GST
NumAtCard : MP-sample-test-001  (traceability back to us)
```
This decrements **1 PCS** of real stock and creates **one** real Delivery Note.

## 5. Why it's not posted yet
The auto-run guardrail blocked posting to **live production SAP** because the
customer/item/quantity were **chosen by me from exploration, not named by you**.
That's the correct check for an irreversible financial+inventory document.

**To proceed, I need one of:**
- **(a) You confirm the §4 values** (and allow the post / run it yourself), or
- **(b) You give me the exact values** — customer CardCode, item, warehouse, qty,
  tax code — you want the test posted with.

The command (once approved):
```
python manage.py mp_post_delivery_note --company JIVO_MART \
  --card-code CUSTA000356 --warehouse DL-EC --item FG0000329 --qty 1 \
  --ref sample-test-001 --confirm
```

## 6. What YOU need to fill for REAL marketplace posting
Set these on **Masters → Warehouses** (Flipkart row) with real SAP values:
- **Customer CardCode** — which BP marketplace deliveries bill to. Likely one of the
  `JIVO MART PVT LTD` entities (e.g. `CUSTA000926` -ISD- DL) or an online-sales BP —
  **your call**.
- **SAP warehouse code** — the real e-commerce godown (e.g. `DL-EC`), not `FLP-MAY`.
- **Tax code (VatGroup)** — e.g. `CG+SG@18` intra-state / `IGST@18` inter-state.
- **Series** — leave blank for SAP default unless you use a dedicated DN series.
- Also: the **SKU mappings** must point to **real** SAP item codes (`FG0000###`),
  not the demo `FG00001` values.

## 7. Posting record (filled once a DN is posted)
| Posted at | CardCode | Warehouse | Item | Qty | DocEntry | DocNum | NumAtCard |
|-----------|----------|-----------|------|-----|----------|--------|-----------|
| _(pending your sign-off)_ | | | | | | | |
