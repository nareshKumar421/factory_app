# Order Processing — what was built

The orchestration layer between OMS and SAP:

```
OMS order → check SAP stock → shortfall → production requirement
          → BOM explosion → material requirement → procurement requirement
```

Branch `feat/oms-order-fulfilment`. **74 tests**, none touching OMS or SAP.
Verified against the live systems throughout.

---

## 1. What each system owns

| Data | Owner | How we treat it |
| --- | --- | --- |
| Orders, lines, customers | **OMS** | Mirrored, never written |
| Items, warehouses, stock, BOM, POs | **SAP** | Queried live, never stored |
| Workflow, requirements, audit | **Here** | Ours |

Nothing is written to OMS or SAP. Not one endpoint.

## 2. Findings that shaped it

Each came from the live systems and contradicted or completed the written spec.

### `qty` is the quantity — proven, not assumed

Checked against the `Quantity` field in **1,081 real SAP payload lines**:

| OMS column | Matched |
| --- | --- |
| **`qty`** | **1,021 (94.4%)** |
| `boxes` | 96 |
| `pcs` | 23 |

The spec says `pcs` is the piece count. It is the pack size.

### OMS holds two quantity conventions in one column

| | qty | pcs | boxes | meaning |
| --- | --- | --- | --- | --- |
| usual | 8000 | 20 | 400 | qty is PIECES, `boxes = qty ÷ pcs` |
| inverted | 40 | 16 | 640 | qty is CASES, `boxes = qty × pcs` |

Under the second, reading `qty` as pieces **understates the order 16-fold**, and
nothing in the row says which applies. Both are caught by one consistency check
and the line is **flagged, never corrected** — we cannot know which figure the
customer meant. 26 of 5,300 mirrored lines are affected.

### The stock is not where the orders are booked

| Warehouse | on hand | committed |
| --- | --- | --- |
| BH-BT | 210,496 | 0 |
| BH-PF | 171,582 | 84,010 |
| **GP-FG** ← what OMS books | **21,557** | **229,583** |

Checking the booking warehouse alone reported SHORT for *every* order — true, and
useless, because the goods exist one warehouse away. `OMS_WAREHOUSE_SOURCING`
fixes it, and is **empty by default**: whether Bahadurgarh can serve a GP-FG order
implies a transfer, which is an operational decision, not one this code may make.

Setting it changed the answer materially:

| | booking warehouse only | with sourcing |
| --- | --- | --- |
| AVAILABLE | 0 | **12** |
| SHORT | 18 | 1 |
| Open requirements | 20 | **13** |

### OMS switched from Quotations to Sales Orders in July 2026

| Month | Quotations | Sales Orders |
| --- | --- | --- |
| 2026-06 | 680 | 0 |
| 2026-07 | 517 | 303 |
| **2026-08** | **0** | **186** |

A SAP Sales Order commits stock; a Quotation does not. So availability reads
`OnHand − IsCommited` and we keep **no parallel ledger** — it would double-count
every pushed order. Only the ~204 orders with `sap_created = false` are netted off
locally, because SAP has never been told about those.

### Item codes are NOT unique across SAP companies

| Code | JIVO_OIL | JIVO_MART | JIVO_BEVERAGES |
| --- | --- | --- | --- |
| `FG0000324` | SESAME OIL 1 LTR | VEDAKA EXTRA VIRGIN 2 LTR | **PET BOTTLE 500 ML MINERAL** |

Asking the wrong company does not fail — it returns a real quantity for a
completely different product, and looks authoritative doing it. So the SAP company
is decided by **category**, and a category that is present but unmapped resolves
to nothing rather than falling through to the company map.

### BEVERAGES, resolved

OMS sends no `WarehouseCode` for BEVERAGES, so the warehouse was derived the same
way OIL's was — from where the stock and the commitments actually sit:

| Warehouse (JIVO_BEVERAGES) | on hand | committed |
| --- | --- | --- |
| **BH-FG** | **1,079,759** | **855,191** |
| DL-PS | 84,576 | 0 |

Commitments accumulating at BH-FG is what identifies it as the booking warehouse,
exactly as GP-FG is for OIL. **Derived, not confirmed** — worth a sanity check
with Beverages.

Effect: `NO_WAREHOUSE` went from **1,641 lines to 0**, and processing 40 orders
moved from `AVAILABLE=12, UNKNOWN=20` to `AVAILABLE=33, UNKNOWN=1`.

### Other live-schema facts the spec gets wrong

`is_auto_free` and `combo_source_code` do not exist. `delivery_date` is **text**.
`company` is a varchar holding `'1'` (Jivo Wellness) / `'2'` (Jivo Mart) — not
Oil/Beverages, and company 1 carries both categories. `sales_*_logs.order_id` is
a **varchar**, needing a numeric guard before any cast. `product_details` and
`sku` are empty.

## 3. Safety

The credential in use is a **postgres superuser** on a host carrying 17 databases,
including this application's own. Read-only is enforced three ways, redundantly:

1. `SET TRANSACTION READ ONLY` on every connection
2. `OmsReadOnlyRouter` refuses writes *and* migrations on the alias
3. No Django model is mapped to OMS — every read is raw SQL, so no ORM path
   exists that could write

**The replica role Harshit offered should replace this.** Rotate the password too;
it was shared in cleartext.

## 4. Running it

```bash
python manage.py sync_oms_orders --check        # connectivity only
python manage.py sync_oms_orders                # incremental pull
python manage.py process_orders --show-requirements
python manage.py plan_materials --show
python manage.py run_order_pipeline             # all of the above, for cron
```

Scheduled: sync 15 min → process 30 min → plan hourly (`jobs.register()`).
APScheduler, matching `maintenance/` — this project has no Celery.

### Settings

| Setting | Purpose |
| --- | --- |
| `OMS_DB_*` | The read-only OMS connection |
| `OMS_DB_TIMEZONE` | OMS stores naive timestamps; this names their zone |
| `OMS_SHIPPING_STATUSES` | Which statuses are real demand (`COMPLETED`) |
| `OMS_PIPELINE_STATUSES` | In-flight demand SAP has not been told about |
| `OMS_CATEGORY_WAREHOUSE` | `OIL=GP-FG`. BEVERAGES absent on purpose |
| `OMS_WAREHOUSE_SOURCING` | Which warehouses may supply another |
| `OMS_CATEGORY_SAP_COMPANY` | Which SAP database answers |

## 5. API

`/api/v1/order-processing/` — `dashboard`, `orders`, `orders/{id}`,
`orders/{id}/timeline`, `orders/{id}/check-stock`, `production`, `materials`,
`materials/plan`, `procurement`, `sync`.

Frontend: `/order-processing` (Overview · Orders · Planning).

## 6. What is NOT done

- **No SAP writes.** No purchase order, no production order, no reservation
  (Rule 11). Planning records only.
- **Fulfilment is not joined** to the existing dispatch flow.
- **Company → SAP database mapping is a default**, not confirmed.
- **BOM explosion is single-level by default.** Multi-level works via
  `--bom-depth` but has not been run against a real nested BOM.

## 7. Open questions

1. Replace the superuser with the read-only replica role.
2. **Confirm the derived BEVERAGES mapping** — `BH-FG` in `JIVO_BEVERAGES`. It is
   read from where stock and commitments sit, not from anyone's word.
4. The 26 lines with an inconsistent `qty` — bug at source, or expected?
5. 4 REJECTED orders have `sap_created = true`. Cancelled in SAP, or drift?
