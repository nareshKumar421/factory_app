# Production Orders: production entries posted to SAP

> Django app: `production_orders` · Base URL: `/api/v1/production-orders/`
> FactoryFlow: `src/modules/production-orders` (`/production-orders`)
> Not `production_execution`. Its run screens and its "SAP Orders" page are
> separate and unchanged.

A production entry records what was made. It becomes one SAP production
order, in the steps SAP itself takes, each on its own page asking only what
that step needs. Each page saves a draft or posts its step:

| Step | Page asks for | SAP document | Right |
|--|--|--|--|
| Plan | product, boxes + loose pieces, posting date, remarks | `POST ProductionOrders`, planned, lines = the BOM | `can_create_production_orders` |
| Release | nothing | `PATCH ProductionOrders(n)` to `boposReleased` | `can_release_production_orders` |
| Issue | issue date, variety, batches of the batch-tracked lines (oldest first by default) | `POST InventoryGenExits`, every line at its planned quantity | `can_issue_production_orders` |
| Receipt | receipt date, line, oil code, production date (batch digits and expiry fill themselves) | `POST InventoryGenEntries`, the planned quantity under the batch | `can_receive_production_orders` |
| Close | closing date | `PATCH ProductionOrders(n)` to `boposClosed` with `ClosingDate` | `can_close_production_orders` |

Live SAP does the same: every one of 323 standard orders since 1 Sep 2026 was
first saved planned with its BOM lines, released about half a minute later,
then issued, received and closed within minutes (`AWOR`). The Service Layer
takes a new order only as planned (-10).

`can_view_production_orders` reads only. Every other right implies it.

Scope today is **Jivo Oil, finished-goods filling, standard orders**. That is
the bulk of the volume: about 235 a month, entered by Gautam (USER24). Special
orders (loose-oil conversions) and disassembly are not built yet.

## Changing an order already in SAP

The floor corrects orders while they are planned: in the 998 standard orders
since 1 July 2026, the product was swapped 120 times, the quantity changed 181
times, and orders were taken from released back to planned 66 times. No order
was changed after release, and none had a second issue or receipt. So two
changes are offered, each a `sap_postings` kind posted as the person:

* **Change the planned order** (`production_order.replan`, Plan right): a new
  product, quantity, date or remarks for an order still planned. A `PATCH`
  with `B1S-ReplaceCollectionsOnPatch`, so its lines become the new product's
  BOM. The new values travel in the posting's params and reach the entry only
  once SAP takes them.
* **Back to planned** (`production_order.unrelease`, Release right): a released
  order with nothing issued goes back to planned, to be changed and released
  again.

## SAP's own orders

`GET sap-orders/` (the "Orders in SAP" page) lists every production order in
SAP, newest first, read straight from OWOR, whether it was made here or in SAP
itself. It can be filtered by status, type, posting date and item or DocNum.
Each row shows who saved it (`UserSign`), how much of its components'
planned quantity has been issued (WOR1), and the entry here whose
`sap_order_entry` it is. It is read only, and any production-order right may
open it.

## Posting as the person

Every step logs into the Service Layer as the SAP user of the person who took
it, not as the shared `B1i`. There are two reasons:

* SAP should record who did it.
* SAP's `SBO_SP_TransactionNotification` checks the posting user:
  * Special orders are allowed for users 1/10/15 only.
  * Disassembly is allowed for 1/33/36/44 only.
  * A released standard order needs a "Sales Team" approval in
    `PRODUCTIONORDERSYNC` unless user 33 (Gautam) made it.

  `B1i` is user 2 and is on none of these lists.

To let someone post:

1. Link them to their SAP user on **SAP Identities** (`sap_client.SapApproverIdentity`, per company).
2. Add that SAP user's password to the server's `SAP_APPROVER_CREDENTIALS` env map on 117
   (`{"JIVO_OIL": {"USER24": "..."}}`). It is never stored in the database or sent to the browser.
3. Give them the rights for the steps they take.

`GET /me/` and the pages say what is missing when one of these is not done.

## What SAP enforces, and what the app checks first

Read from live SAP on 2026-10-09:

* **BOM**: a standard order must match the BOM exactly. The order is built line
  for line from `ITT1`, per-piece quantity = BOM quantity ÷ BOM size. The order
  step re-reads the BOM and refuses if it changed since the entry was saved.
* **Wastage**: the header UDF `U_WASTAGE` defaults to 'Y'. SAP then refuses any
  line with an empty `U_WASTAGE_QUANTITY` (20200001), so every line sends 0.
* **WIP vendor**: Oil's order screen fills `CardCode` = `VENDA001625` with a
  formatted search, so the payload sends it as `CustomerCode`.
* **Series**: per month and per document (`PRO`/`GI`/`GRE` + MMYY). SAP's
  default is a 2024 series, so each step resolves the month's series for the
  posting date.
* **Variety**: an Oil issue line must carry a distribution rule (60003). It is
  the product's `U_Sub_Group` matched to the OOCR name (97% of real lines), and
  can be overridden on the entry. `U_SchemeAgst` carries the same code; the SAP
  screen copies it.
* **Quantities and order of steps**:
  * The issue is exactly the planned quantities.
  * The receipt is exactly the planned quantity.
  * Issue comes before receipt, and closing needs both.
* **Stock caps**: SAP refuses an FG order, its close, or a receipt into BH-PF
  while finished stock is over its limit (350,000 L in BH-PF, among others; see
  `constants.STOCK_CAPS`). The entry shows the bars.
* **Batches**: a production receipt's batch number must be unique per item.
  * The batch is `<line><oil code> <MMYYDD> <NN>`, e.g. `L4006024 102608 01`;
    the app suggests the next free `NN`.
  * Expiry is the production date + 2 years − 1 day, and can be edited.
  * The oil code is typed by the floor; it comes from the receiving WhatsApp group.
* **Issue batches**: the batch-tracked oil is taken oldest released batch first
  (`OIBT`), unless batches were chosen on the entry. The Service Layer does not
  pick batches by itself.
* **Dates**: nothing can be dated in the future.

## Retries

Each step is a `sap_postings` kind (`production_order.create`, `.issue`,
`.receipt`, `.close`).

* **SAP not answering**: the step waits, and the worker sends it again as the
  same person.
* **Read-back before every write**:
  * The order is found by its Comments stamp `App PRD-…`.
  * The issue and receipt are found by the order they point at; SAP allows one
    of each.
  * The close is found by the order's status.

  So a retry after a lost answer records what SAP already has instead of posting
  twice.
* **Several steps at once**: when the person asked for several steps, a step
  that posts queues the next one for them. A run interrupted by an outage
  finishes by itself.

## Proven against SAP

On TEST (`TEST_JIVO_OIL_HANADB`), 2026-10-10, as USER24: plan + release
(order 1026202500, resource line and UDFs as sent), issue 1026606500 (variety
and `U_SchemeAgst` on every line, oldest batches), receipt 1026596500 (batch
with its dates; SAP's post-transaction step ran).

## Not yet proven against SAP

* The close: `boposClosed` with `ClosingDate`, and the variance SAP posts.
* Changing a planned order's product through the Service Layer (`ItemNo` in a
  `PATCH`, lines replaced), and back to planned (`boposPlanned`).
