# Order Processing — design

Pull an order from OMS → check the warehouse → raise a production plan for
whatever stock cannot cover.

**Status: design. No code yet.** Updated against Harshit Singh's OMS
specification (10 Aug 2026).

---

## 1. What already exists

Four of the five pieces are already in this codebase. Only the join is missing.

| Piece | Where | Reuse |
| --- | --- | --- |
| OMS | External Django/PostgreSQL app. Already proxied here for **invoice approval** (`oms/`) | Read its DB |
| FG stock | `warehouse/services/wms_hana_reader.py`, `marketplace.services.sap_gateway.oitw_onhand()` (SAP OITW) | Yes |
| Production | `production_execution.ProductionRun` — `item_code`, `required_qty`, `line`, SAP OWOR link | Yes |
| Line capability | `production_execution.LineSkuConfig`, `supply_chain.MaterialMachineMap` | Yes |
| **Order → stock → plan** | *nothing* | **The new work** |

The existing `oms/` app holds **no orders** — it is `InvoiceApprovalAudit` plus an
HTTP client for invoice approve/reject. This is a separate app and must not be
named `oms`, or that proxy breaks.

## 2. THE question that decides this whole system

> **Does OMS create a SAP Sales Order, or only a Sales Quotation?**

The spec says orders are pushed "as a Sales Quotation (and, where configured, a
Sales Order)". That "where configured" carries the entire design.

In SAP B1, `OITW.IsCommited` is driven by open **Sales Orders**. A **Quotation
commits nothing**. So:

| If OMS creates | SAP's committed figure | What we must do |
| --- | --- | --- |
| **Sales Order** | Already includes OMS demand | Read `OnHand − IsCommited` from SAP. **Do not keep our own ledger — it would double-count.** |
| **Quotation only** | Knows nothing about these orders | SAP cannot answer "is it available". **Our ledger is the only source of truth.** |

Get this wrong in either direction and every availability answer is wrong — either
double-counting demand, or promising stock three times over.

**And regardless of the answer:** an order approved in OMS but not yet pushed
(`sap_created = false`) is demand SAP has never heard of. Those always need
counting on our side.

**Blocking question for Harshit:** per company (Oil / Beverages / Mart), is the
Sales Order flow enabled, or Quotation only?

## 3. What the spec settled

| Question | Answer | Consequence |
| --- | --- | --- |
| Item codes | `order_items.item_code` **is** `OITM.ItemCode` | **No mapping table needed.** Whole model dropped |
| Database | PostgreSQL | Mirrors the existing optional `ai_readonly` alias |
| Incremental pull | `orders.updated_at` watermark, `orders.id` / `order_items.id` keys | Straightforward sync |
| Warehouse | Resolved from `order_items.category` (OIL / BEVERAGES / MART) | **Per line, not per order** — see §4 |
| Branch | `orders.dispatch_from_id` = SAP `BPL_ID` | Not the stock location |
| Reconciliation | `sales_quotation_logs` holds the exact payload + DocEntry/DocNum | Best source for "what SAP actually got" |

### Warehouse is per line, and it is not the branch

`dispatch_from_id` is the **business place** (BPL, a GST/branch concept).
`WarehouseCode` is resolved separately from the line's **category** — `GP-FG` for
OIL in the sample payload.

So one order can draw on several warehouses. The stock check is **per line against
that line's warehouse**, never one warehouse per order.

**I need the category → warehouse map** OMS uses. If it is a table, we read it; if
hard-coded, we need the values.

## 4. Three traps in the data

**Free/scheme lines are real stock.** `is_auto_free` lines carry a quantity at
zero price and physically ship. Excluding them under-counts demand on exactly the
fast-moving SKUs that run short.

**Never derive quantities.** The spec is explicit:

> *"`pcs` is in individual bottles (not cartons). Multiplying by the carton
> configuration in the item name will overstate volume — please read `ltrs`/`boxes`
> directly rather than deriving them."*

So we store `qty`, `pcs`, `boxes` and `ltrs` as given, and never compute one from
another. The remaining question is which unit **SAP stock** is held in, so the
comparison is like-for-like — `product_details.sales_factor` exists for this, and
`marketplace` already hit the same conversion problem.

**Same order number, different SAP company.** Oil and Beverages route to
different company databases. The stock check must hit the right one, driven by
`orders.company`. `factory_app` is already company-scoped, so this is wiring, not
new design.

## 5. Which orders consume stock

Not all of them. An order in `RATE_APPROVAL` may never ship; a `REJECTED` one
never will. Counting everything makes the factory look permanently short.

**I need from Harshit:** the full list of `order_statuses.code` values, and which
mean *this will ship*.

Working assumption until then: statuses at or past auditor approval, excluding
rejected and cancelled.

## 6. The flow

```
   OMS (PostgreSQL replica)
      │  incremental pull on orders.updated_at
      ▼
   1. MIRROR        orders + order_items into our tables
      │
   2. FILTER        only statuses that mean "will ship"
      │
   3. CHECK STOCK   per line, per warehouse (from category), per company
      │             free = OnHand − committed        (see §2)
      │
   4. SPLIT         per line: coverable now / short by N
      │
      ├─ covered  → READY
      └─ short    → PRODUCTION REQUEST
                       │
                       ├─ merge with other orders short of the same SKU
                       ├─ which line runs it (LineSkuConfig)
                       └─ accepted → ProductionRun (existing model)
```

### A shortfall is a request, not a run

It does not become a `ProductionRun` automatically:

- Runs are scheduled against real lines and shifts. An order does not jump the queue.
- Three orders short of the same SKU should become **one** run.
- A 12-case shortfall probably should not start a line at all.

So: `ProductionRequest` (ours) → reviewed and merged → `ProductionRun` (existing).

## 7. Models

| Model | Holds |
| --- | --- |
| `OmsOrder` | Mirror: `oms_id`, `order_number`, card code/name, company, dispatch branch, status, `delivery_date`, `sap_created`, `sap_doc_number`, `updated_at` |
| `OmsOrderLine` | `oms_id`, `item_code` (= SAP), category, resolved warehouse, `qty`/`pcs`/`boxes`/`ltrs` as given, `is_auto_free` |
| `OmsSyncRun` | One pull: watermark from/to, counts, errors. A sync with no record is unauditable |
| `StockCheck` | One check: when, by whom, which SAP company, what it found |
| `LineAvailability` | Per line: required, free, allocatable, short |
| `StockCommitment` | Qty reserved for an order until dispatched or released — **only if §2 says quotation-only** |
| `ProductionRequest` | Item, shortfall, needed-by, source orders, status |
| `CategoryWarehouse` | Category → SAP warehouse, mirroring OMS's own resolution |

`StockCommitment` is its own record, not a field on the line, because a commitment
dies for its own reasons — dispatched, cancelled, expired. As a field it silently
outlives a cancelled order and locks stock nothing can use.

## 8. Access mode — what to ask for

Harshit offered three. **Ask for option 1, but with the curated views**, which he
also offered:

> *"a dedicated role with SELECT restricted to the tables above (or to a set of
> curated views such as `v_order_header`, `v_order_lines`, `v_sap_payload`)"*

Views over raw tables, because:

- A view is a **contract**. Raw tables mean any OMS refactor silently breaks us.
- It scopes exactly what we read — no accidental access to user or scheme tables.
- It keeps the "read the DB, write only via the API" boundary clean.

Read-only, on the **replica**, never the primary.

Wired like the existing optional `ai_readonly` alias — configured only when
`OMS_DB_NAME` is set, so nothing changes for anyone who has not set it:

```python
OMS_DB_NAME = config('OMS_DB_NAME', default='')
if OMS_DB_NAME:
    DATABASES['oms_readonly'] = { ... }
```

### Suggested reply to Harshit

| His question | Answer |
| --- | --- |
| (a) Access mode | **Read-only role on the replica, over curated views** (`v_order_header`, `v_order_lines`), not raw tables |
| (b) Refresh frequency | Every 15 min via `updated_at` watermark. Hourly is enough to start |
| (c) Source IP | *(the factory_app server's public IP)* |
| (d) Backfill | Open orders only, plus 90 days of history for the shortfall pattern |

Plus the four things we need back:

1. Per company — **Sales Order flow, or Quotation only?** (§2)
2. The **category → warehouse** map
3. The full `order_statuses.code` list, and which mean "will ship"
4. Confirmation we never write to OMS — if we must, which endpoint

## 9. Build order

| # | Step | Usable result |
| --- | --- | --- |
| 1 | `oms_readonly` alias + reader over the views | Orders visible here |
| 2 | Mirror models + incremental sync command | Orders queryable, auditable |
| 3 | Category → warehouse + company routing | Each line knows where to look |
| 4 | Stock check against SAP OITW | **"Can we fulfil this?" answered** |
| 5 | Commitment ledger *(only if quotation-only)* | Two orders stop seeing the same stock |
| 6 | `ProductionRequest` from shortfalls | Shortfalls become work |
| 7 | Accept → `ProductionRun` | Joined to existing production |
| 8 | Frontend: orders → check → request | Usable by a person |

**Step 4 is the first genuinely useful stop.**

## 10. Out of scope

- Not replacing OMS. Orders are raised there.
- Not writing to OMS. Read-only unless §8.4 says otherwise.
- Not scheduling production. A request joins the existing queue.
- Not touching SAP. Stock is read-only.
- Not batch/FEFO allocation — that is `barcode`/`warehouse` work.

## 11. The risk to design for now

Stock is read from SAP; orders live in OMS; commitments would live here. **Three
systems, one number.** The moment someone dispatches outside this flow, or edits
SAP directly, we promise stock that is gone.

Built in from the start, not bolted on later:

- **Re-check at dispatch**, never trust an old check
- **Expire commitments** after N days
- **Show the age of the last check** on screen, so nobody acts on a week-old answer
- **Reconcile against `sales_quotation_logs`** — it holds what SAP actually
  received, which is the only way to catch our mirror drifting from reality

---

*Blocked on §8's four questions. Step 1 can start as soon as credentials land.*
