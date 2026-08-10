# Order Processing — design

**Status: design only. No code written yet.** Waiting on OMS database credentials.

The ask, in one line:

> Pull an order from OMS → check the warehouse → if the stock is not there, raise a
> production plan for the shortfall.

---

## 1. What already exists (read this before building anything)

Four of the five pieces are already in this codebase. Only the join between them
is missing.

| Piece | Where it lives today | Reusable? |
| --- | --- | --- |
| **OMS** | External service, `harshit-jivo/OMS-*`, `http://103.89.45.75:8081`. `factory_app` already proxies it in `oms/` | **Yes** — but see §2 |
| **FG stock** | `warehouse/services/wms_hana_reader.py` (SAP OITW), and `marketplace.services.sap_gateway.oitw_onhand()` | **Yes** |
| **Production** | `production_execution.ProductionRun` — has `item_code`, `required_qty`, `line`, SAP `OWOR` doc entry, warehouse BOM approval | **Yes** |
| **Line capability** | `production_execution.LineSkuConfig` (SKU → line preset, rated speed) and `supply_chain.MaterialMachineMap` / `MachineCapacity` | **Yes** |
| **Order → stock → plan** | *Nothing* | **This is the new work** |

### The `oms` app is not an order store

`oms/` is a narrow proxy: `InvoiceApprovalAudit` plus an HTTP client for
`/api/invoice/all/`, `/api/invoice/<id>/update-status/` and
`/api/invoice/history/<id>/`. It exists so the factory can approve **AR invoices**
against physical stock.

**It has no orders in it.** So this is a new app, not an extension of `oms/` — and
it must not be named `oms` or the proxy breaks.

Proposed name: **`order_processing`**.

## 2. The one decision to make before coding: how we read OMS

You are giving me **database credentials**, but there is already a working **HTTP
client** against the same system. These are very different choices.

| | Direct DB read | Existing HTTP API |
| --- | --- | --- |
| Speed of build | Fast — SQL against real tables | Depends what endpoints exist |
| Coupling | **Tight.** Any OMS schema change breaks us silently | Loose — a contract |
| Fidelity | Sees everything, including fields the API hides | Only what is exposed |
| Writes back | Dangerous — bypasses OMS's own logic | Safe — OMS validates |
| Auth / audit | None. Rows appear with no trace of who read them | Uses the OMS service account |

**Recommendation: read via the database, write via the API — never the reverse.**

Reading orders directly is pragmatic and low-risk. But if this system ever needs
to tell OMS "this order is now in production" or "reserved", that must go through
the API, because writing into another system's tables behind its back is how two
systems quietly disagree forever.

If OMS has no such endpoint, we hold the state on **our** side and never write to
OMS at all. That is fine, and better than a hidden write.

**I need from you:** DB host/user/password, and confirmation of whether we ever
need to write anything back.

## 3. The flow

```
   OMS order
      │
      ├─ 1. PULL          import the order + its lines
      │
      ├─ 2. RESOLVE       OMS item code → SAP item code
      │
      ├─ 3. CHECK STOCK   free FG stock in the dispatch warehouse
      │                   free = on hand − committed to other orders
      │
      ├─ 4. SPLIT         per line:  fulfil now  /  short by N
      │
      ├─ 5a. AVAILABLE    → mark ready to dispatch
      │
      └─ 5b. SHORT        → raise a PRODUCTION REQUEST for the shortfall
                             │
                             ├─ which line can run it?
                             ├─ how long will it take?
                             ├─ can the plan absorb it?
                             └─ → ProductionRun (existing model)
```

### Step 3 — "is the qty available" is not one number

This is where naive versions go wrong. Available is **not** SAP on-hand. It must
be:

```
free = on hand
     − already committed to other confirmed orders
     − stock reserved for marketplace dispatches
     − anything below the safety floor, if a floor is enforced
```

Without that, two orders both see the same 500 cases and both get promised.

**The commitment ledger is the heart of this system.** Everything else is
plumbing.

### Step 5b — the production request

A shortfall does not become a `ProductionRun` immediately. It becomes a
**request**, which someone accepts. Reasons:

- Production runs are scheduled against real lines and shifts; an order does not
  get to jump that queue automatically.
- Several orders short of the same SKU should become **one** run, not three.
- A shortfall of 12 cases probably should not start a line at all.

So: `ProductionRequest` (ours) → reviewed/merged → `ProductionRun` (existing).

## 4. Proposed models

All in the new `order_processing` app.

| Model | Holds | Why it is ours and not OMS's |
| --- | --- | --- |
| `OmsOrder` | Mirror of one OMS order: number, customer, date, warehouse, status | We need our own status without writing to OMS |
| `OmsOrderLine` | Item, qty, UOM, and the resolved SAP item code | The SAP code is our resolution, not OMS's data |
| `OrderStockCheck` | One check run: when, by whom, what it found | An answer with no timestamp is not an answer |
| `OrderLineAvailability` | Per line: required, free, allocated, short | The row a person actually reads |
| `StockCommitment` | Qty of an item reserved for an order, until dispatched or released | **The ledger.** Stops double-promising |
| `ProductionRequest` | Item, shortfall qty, needed-by date, source orders, status | Several orders can point at one request |
| `ItemCodeMap` | OMS item code → SAP item code | Only if the codes differ — see §6 |

### Why `StockCommitment` is separate from the order

Because a commitment outlives the check that created it, and dies for its own
reasons: dispatched, cancelled, expired, released by hand. Modelling it as a field
on the order line means it silently persists when the order is cancelled, and
then nothing else can be promised that stock.

## 5. The states

```
OMS order      →  IMPORTED  →  CHECKED  →  ┬─ READY        (all lines covered)
                                           ├─ PARTIAL      (some covered)
                                           └─ SHORT        (nothing covered)

ProductionRequest  →  RAISED  →  ACCEPTED  →  PLANNED  →  DONE
                          └────→  REJECTED / MERGED
```

Nothing auto-advances past `CHECKED`. A person decides what to promise.

## 6. What I need from you

Blocking — I cannot build without these:

1. **OMS DB credentials** — host, port, database, user, password, and whether it
   is MySQL or Postgres.
2. **Do OMS item codes equal SAP item codes?** If yes, `ItemCodeMap` disappears
   and this gets much simpler. If no, I need one real example of each.
3. **Which warehouse** does an order dispatch from, and is it on the order or
   inferred?
4. **Does anything need writing back to OMS?** If yes, which endpoint.

Useful but not blocking:

5. One real order that went short, with what actually happened — the same way the
   supply-chain playbook's caps example anchored that build.
6. Who accepts a production request — a name or a role.

## 7. Build order

Each step is usable on its own, so nothing is wasted if priorities change.

| # | Step | Result |
| --- | --- | --- |
| 1 | OMS reader + `OmsOrder` / `OmsOrderLine` | Orders visible in factory_app |
| 2 | Item-code resolution | Lines carry SAP codes |
| 3 | Stock check against OITW | "Can we fulfil this?" answered |
| 4 | `StockCommitment` ledger | Two orders stop seeing the same stock |
| 5 | `ProductionRequest` from shortfalls | Shortfalls become work |
| 6 | Accept → `ProductionRun` | Joined to existing production |
| 7 | Frontend: order list → check → request | Usable by a person |

**Step 3 is the first genuinely useful stop.** If you want the smallest thing
that helps, we stop there and add the ledger once people trust it.

## 8. Deliberately out of scope

- Not replacing OMS. Orders are raised there.
- Not scheduling production. A request lands in the existing queue.
- Not touching SAP. Stock is read-only.
- Not allocating batches or FEFO. That is `barcode`/`warehouse` work.

## 9. The risk worth naming now

**Stock is read from SAP; commitments live here.** The moment those disagree —
someone dispatches outside this system, or SAP is edited directly — this system
promises stock that is gone.

Mitigations, in order of preference: re-check at dispatch rather than trusting an
old check; expire commitments after N days; show the age of the last check on
screen so nobody acts on a week-old answer.

I would rather build this in from the start than add it after the first
double-promise.

---

*Next: your answers to §6, then step 1.*
