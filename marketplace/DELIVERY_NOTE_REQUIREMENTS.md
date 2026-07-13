# Marketplace Delivery Note — what I need from you

To make the SAP **Delivery Note** posting production-perfect (real posting, correct
values, no duplicates), here's exactly what I need from your side. Items are grouped
and marked **[BLOCKER]** (can't post correctly without it) or **[IMPROVES]** (works
without, but needed to be fully correct).

## Where we are today
- Posting pipeline is built and **idempotent**: on confirm we post a SAP Delivery
  Note (FG stock out) via `POST /b1s/v2/DeliveryNotes` and a Goods Issue (packing
  material) via `InventoryGenExits`. Each document's id is saved the instant it's
  created, so a retry never creates a duplicate. Failures don't block dispatch.
- It currently runs against **simulated SAP** in dev (`MARKETPLACE_SIMULATE_SAP`).
  The line payload today sends only `ItemCode`, `Quantity`, `WarehouseCode`
  (+`NumAtCard`/`Comments`). Everything below is what's needed to send the *right*
  document to *real* SAP.

---

## 1. SAP environment & access  — to validate real posting
- **[BLOCKER]** A **SAP test/sandbox company** (Service Layer URL + a test CompanyDB)
  where I can post real Delivery Notes without touching live data.
- **[BLOCKER]** Service Layer **credentials** for that test company (user/password),
  or confirmation the existing `SL_URL` / `SL_USER` / `SL_PASSWORD` / `COMPANY_DB`
  point at a safe environment I can post into.
- **[IMPROVES]** Is the SAP cert self-signed? (We currently skip TLS verification —
  confirm that's intended, or give me the CA cert.)

## 2. The "customer" on the delivery note
- **[BLOCKER]** Which **SAP Business Partner (CardCode)** should the marketplace
  Delivery Note be billed to? i.e. what represents "Flipkart" as a customer.
  - one BP for all Flipkart orders? per-warehouse? per-state (for GST)?
- Today this comes from `MarketplaceWarehouse.sap_customer_card_code` — I need the
  **real value(s)** to put there.

## 3. Warehouse / stock source
- **[BLOCKER]** The real **SAP warehouse code(s)** goods ship from for the
  marketplace (JIVO_MART) — e.g. is it `FLP-MAY` (Mayapuri) or something else.
- **[IMPROVES]** Is that warehouse **bin-managed** in SAP? If yes, Delivery Note /
  Goods Issue lines need `DocumentLinesBinAllocations` — I'll need the bin logic
  (which bin, or "let SAP auto-allocate").

## 4. Line-level fields (I must not guess these)
- **[BLOCKER]** **Unit of measure**: should `Quantity` post in the item's inventory
  UoM, or a sales UoM? If multi-UoM, I need `UoMEntry`/`MeasureUnit` and how to
  derive it. (Our combos explode 1 SKU → several FG items; confirm each posts in
  its own base UoM.)
- **[BLOCKER for value]** Should the Delivery Note carry **price**
  (`UnitPrice`/`GrossPrice`)? If yes, which figure — Flipkart selling price,
  invoice amount, or a fixed price list? (We have `unit_price`, `invoice_amount`,
  `tax_amount`, `hsn_code` per order line.)
- **[BLOCKER for tax]** **Tax code** per line (`VatGroup` / `TaxCode`) — GST needs
  this. One code for all, or by HSN / state? We store `hsn_code`.
- **[IMPROVES]** Document **numbering Series** — should these post to a specific
  Series, or the default?
- **[IMPROVES]** Any **mandatory UDFs** (`U_*`) on Delivery Note header/lines in
  your SAP setup? List them and their required values.

## 5. Document flow / business rules
- **[BLOCKER]** Does the Delivery Note need to feed an **A/R Invoice** afterwards
  (value/GST flow), or is the DN alone enough and billing stays internal?
- **[IMPROVES]** Should the Delivery Note link to a **base Sales Order** in SAP
  (`BaseType`/`BaseEntry`/`BaseLine`), or is it a standalone delivery? (Today it's
  standalone.)
- **[IMPROVES]** Packing material: today it's a **separate Goods Issue**. Confirm
  that's right, or whether PM should be lines on the same delivery / handled by a
  BOM at the FG item instead.
- **[IMPROVES]** What **DocDate** to use — dispatch date (today) or the marketplace
  order date?

## 6. A reference document  — the fastest way to get this right
- **[BLOCKER]** One **real, correct marketplace Delivery Note created manually in
  SAP** (screenshot or the JSON from the Service Layer). I'll mirror its exact
  fields. This single item resolves most of section 4–5 at once.

## 7. Duplicate-safety at the SAP boundary (I'll build; needs your env)
- We're idempotent on our side. To also survive a **lost response** (SAP created the
  DN but the reply timed out), I want to **query SAP for an existing Delivery Note
  by `NumAtCard = <order_id>` before posting** and adopt it if present. I can build
  this once I have the test environment (section 1) to confirm the query/field.

---

## Quickest path
If you can give me just these four, I can wire and validate real posting end-to-end:
1. A **safe SAP test environment + credentials** (§1)
2. The **CardCode** for the marketplace customer (§2)
3. The **warehouse code** + whether it's bin-managed (§3)
4. **One sample Delivery Note** from SAP to mirror (§6)

Everything else (price/tax/UoM/series) I can slot in from that sample.
