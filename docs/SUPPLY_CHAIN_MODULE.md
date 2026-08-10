# Smart Supply Chain — the `supply_chain` module

What was built for the August 2026 brief, why it is this shape, and what is still
open. Companion to `SUPPLY_CHAIN_SYSTEM_BRIEF.md`, which reads the brief itself.

Branch: `feat/supply-chain-system`.

---

## 1. The finding that shaped everything

**Most of the brief already exists.** Before writing anything, the seven-step
chain was checked against the codebase:

| Brief step | Status |
| --- | --- |
| 1 Demand signal | **Exists** — `sales_planning_requirement` |
| 2 Stock floor | **Exists** — returned as `min_stock` |
| 3 FG gap | **Exists** — returned as `required_qty` |
| 4 BOM explosion | **Exists** — returned as `base_required_qty` |
| 5 Material requirement | **Exists** — plus `open_po_qty` / `net_shortage_qty` |
| 6 Lead-time timing | **Did not exist anywhere** |
| 7 Capacity feasibility | **Did not exist anywhere** |

`sales_planning_requirement` calls the HANA procedure
`SALES PLANNING VS REQUIREMENT_WEEKLY` and stores one row per item with the
demand, the floor, the stock, the exploded requirement, what is already on
order, and the net shortage. It even solves the open-PO netting the brief never
mentions.

So this module **does not re-implement steps 1–5**. Re-deriving numbers the ERP
already computes would create a second source of truth — precisely the problem
the brief is trying to end. It reads those rows and adds the two steps that turn
a requirement list into the alarm system the brief actually asks for.

That is also the honest answer to "how much of this is new": less than the brief
implies, and the missing part is the part that matters.

## 2. What was built

New Django app `supply_chain`, registered in `config/settings.py` and mounted at
`/api/v1/supply-chain/`.

### Reference data — the three template sheets

Nothing in the system held these, and steps 6 and 7 are impossible without them.

| Model | Template sheet | Owner |
| --- | --- | --- |
| `MaterialLeadTime` | 1. Lead Times | Procurement (Packaging + Oils/RM) |
| `MachineCapacity` | 2. Machine Capacities | Production / Infrastructure |
| `MaterialMachineMap` | 3. Material-Machine Map | Production |

`MachineCapacity.effective_capacity_units()` derives capacity from the shift
pattern rather than storing it, mirroring the template's own formula — a stored
copy goes stale the first time someone edits a shift.

### `SupplyChainPolicy` — the brief's open questions, made settable

Every field is a decision the brief raises but does not settle. They are
configuration rather than constants because each one changes every number
downstream and the business has not chosen yet:

| Field | Default | The question it answers |
| --- | --- | --- |
| `floor_percent` | 35 | The brief's headline number |
| `floor_basis` | `MONTHLY_AVERAGE` | 35% of the monthly average, or of the three-month total? **The two differ by 3×** |
| `urgency_window_days` | 7 | How near is "order now" rather than "scheduled" |
| `use_net_of_open_po` | `True` | Net off material already on order |
| `apply_moq_rounding` | `True` | Round orders up to a placeable quantity |
| `include_changeover_in_capacity` | `True` | Charge changeover against line hours |

`SupplyChainPolicy.for_company()` returns an unsaved default rather than `None`,
so every screen works before an admin has configured anything.

### Step 6 — `planning.material_alarms()`

Per material: the shortage, its lead time, and therefore the **date the order
must be placed**. That date is what makes it an alarm rather than a report.

```
order_by  = plan period start − lead time days
alarm     = OVERDUE      order_by has passed
            ORDER_NOW    order_by within the urgency window
            SCHEDULED    later, with the date stated
            NO_LEAD_TIME no reference data on file
            COVERED      nothing to order
```

Sorted most urgent first, so the top of the list is literally what to order
today. Order quantities round up to MOQ — the template collects MOQ and the
brief never uses it, so a requirement of 10 against an MOQ of 500 would
otherwise be raised as an order nobody can place.

`NO_LEAD_TIME` is deliberately ranked above `SCHEDULED`. A material we cannot
time is not a low-priority material; it is the reference-data gap the template
exists to close, and burying it would hide the very thing that needs chasing.

### Step 7 — `planning.capacity_check()`

Per line: usable hours against required hours, with utilisation, shortfall and
the alternates available.

Worked in **hours, not units**. Two SKUs on one line can have different output
rates, so summing their units and comparing that to a capacity number compares
nothing meaningful. Changeover is charged at one per SKU scheduled on the line —
the template collects changeover minutes and the brief's step 7 never uses them,
and a check that ignores them passes plans the floor cannot run. There is a test
for exactly that case: 399.5 hours of work fits in 400 raw hours and does not fit
once the line has to change over.

SKUs with no machine, or no output rate, are reported in `unmapped_skus` rather
than dropped, and force `feasible: false` — an unrunnable SKU must not be
silently excluded from a feasibility answer.

### Template import — `services/template_import.py`

Reads the workbook, prefers `openpyxl`, falls back to a standard-library reader
(the existing one in `marketplace.services.amazon_sheet` handles only the first
sheet). Upserts rather than replaces, because the three sheets are owned by
different departments and arrive at different times.

Two traps in the real workbook, both handled:

1. **Example rows.** The template's grey italic rows carry real-looking codes
   (`PM-CAP-26`, `M-01`, `FG0000030`) and fictional suppliers (`e.g. ABC Caps Pvt
   Ltd`). Loading them seeds the reference set with suppliers that do not exist.
   They are detected, counted and skipped.
2. **Blank rows carrying a filled-down formula.** Machine Capacities fills
   "Effective Monthly Capacity" down as a formula, so ~20 empty rows carry a
   cached value and read as populated. **Parsing the real file returned 24
   machines where there are 4.** Rows are now keyed off their identity column.

That second bug was only found by running the parser against the actual
workbook, not the fixtures.

### API

| Endpoint | Purpose |
| --- | --- |
| `GET /dashboard/` | Steps 6 + 7 plus a headline — the "single brain" |
| `GET /procurement/` | Step 6 alone |
| `GET /capacity/` | Step 7 alone |
| `GET|PUT /policy/` | Read/change the open-question settings |
| `GET /reference/lead-times|machines|sku-machines/` | The three datasets |
| `POST /reference/upload/` | Upload the template workbook |
| `GET /reference/imports/` | Upload history with warnings |

Permissions: `can_view_supply_chain` to read, `can_manage_supply_chain_reference`
to upload or change policy. All reads are company-scoped.

## 3. Delivery, floor enforcement and the frontend

The first cut stopped at computed alarms. These close the gaps it left.

### Alarms are now delivered

`AlarmSubscription` decides who is told, because the brief never says. Each row
targets everyone holding a Django permission — how the rest of this codebase
addresses a department — and picks its own thresholds: overdue, order-now,
missing-lead-time, over-capacity, and optionally only packaging or only raw
material, so a packaging buyer is never paged about bulk oil.

Sends are **fingerprinted**. A supply-chain alarm is a standing condition, not an
event: an overdue order stays overdue every day until someone places it, and
re-sending it nightly is exactly how a notification channel gets muted. The
digest covers the item, its state and its order-by date but deliberately NOT the
quantity, so a shortage drifting by a few units is not treated as news.
`AlarmDispatch` records every send; `--force` overrides, `--dry-run` builds
everything and sends nothing.

One department's delivery failure is caught and logged so it cannot silence
another's alarm, and a failed send writes no dispatch row, so it retries next run.

    python manage.py send_supply_chain_alarms --company JIVO_OIL [--dry-run]

### The floor is now enforced

`SalesTrend` holds three months of actual sales per item — the figure the brief's
35% applies to, which nothing in the system held, and without which the
procedure's `min_stock` could not be checked against the brief's rule at all.

With `floor_source=POLICY` (the default) the requirement is recomputed as
`base demand + policy floor − stock` for any item that has a trend on file.
Items without one keep the procedure's numbers **untouched** — enabling the
policy must not silently move numbers for items it cannot recompute.

`floor_audit()` then names the items whose buffer has eroded: policy floor
against ERP minimum, largest divergence first. "Buffers erode unnoticed" is one
of the five problems the brief names; this is what noticing looks like.

    python manage.py load_sales_trend --company JIVO_OIL --csv trend.csv

### Steps 3 and 5 — settled with evidence

The brief contradicts itself: step 3 adds the floor to demand, step 5 subtracts
it. That lives in the HANA procedure, which cannot be edited from here — but it
does not need to be, because the question is **answerable from the data the
procedure already returns**. It gives the demand (`base_required_qty`), the floor
(`min_stock`), the stock (`stock_in_hand`) and its own answer (`required_qty`),
so both readings can be computed and compared against what it actually said.

`floor_convention_audit()` does exactly that and reports a verdict per row plus
an overall majority. Rows whose floor is zero cannot tell the readings apart and
are reported INDETERMINATE rather than counted for either side — without that
guard the audit would claim a verdict from data carrying no information.

### Frontend

`src/modules/dashboards/supply-chain/` in FactoryFlow, routed at
`/dashboards/supply-chain` behind `supply_chain.can_view_supply_chain`.

A headline row of four tiles (needs ordering today, no lead time on file, lines
over capacity, buffers below policy), each coloured only when it needs action so
a healthy chain reads quiet. Then three tabs: Procurement (the action list,
urgent first, showing MOQ whenever the order quantity differs from the shortage),
Production capacity (per-line utilisation with changeover called out), and Stock
buffers (the floor audit plus the convention verdict).

Template upload and "Send alarms" are gated on
`can_manage_supply_chain_reference`. When every material lacks a lead time the
page says so explicitly rather than showing an empty, healthy-looking table.

## 4. Verified end to end on the real template

The whole chain, run against the actual workbook:

```
STEP 1  import real template   lead times 0 | machines 4 | sku-map 5 | examples skipped 5
STEP 3  sales trend loaded     5 items
STEP 5  floor convention       ADDITIVE  {checked 10, additive 10, subtractive 0}
          worked example FG0000011: demand 18000 + floor 5000 - stock 14000 = 9000
          (ERP said 9000; subtractive would have been 0)
STEP 6  procurement
          RM-OIL-MUS   short 175      order 180      lead 45d  by 2026-07-16  OVERDUE
          PM-BTL-1L    short 80000    order 100000   lead 30d  by 2026-07-31  OVERDUE
          PM-CAP-26    short 185000   order 200000   lead 21d  by 2026-08-09  OVERDUE
          PM-SHRINK    short 9000     order 9000     lead   -                 NO_LEAD_TIME
          PM-LBL-JIVO  short 205000   order 300000   lead 12d  by 2026-08-18  SCHEDULED
          PM-CTN-20    short 10750    order 15000    lead  7d  by 2026-08-23  SCHEDULED
STEP 7  capacity               M-01 9.2h/414.5h · M-02 7.8h/415h · M-03 7.1h/206.5h — all fit
STEP 8  alarm delivery         Packaging + Oils each sent to 4 users
          "Supply chain: 1 overdue" / "OVERDUE: PM-CAP-26 — order 200000 Pcs by 2026-08-09"
STEP 9  re-run                 0 notification calls — "unchanged since last send"
```

**`lead times 0` is still the real finding.** Every row on the template's Lead
Times sheet is an example; Procurement has not returned theirs. The lead times in
the run above come from the seed command, not the workbook.

### Tests

56 backend tests, all passing. Frontend adds zero type errors (144 before and
after — the pre-existing baseline) and lints clean.

## 5. What is still NOT built

Stated plainly so nobody assumes otherwise:

- **WIP / already-scheduled production** is not netted off the FG gap.
- **The sales trend loads from CSV**, not from the ERP. The figure exists in
  HANA; wiring the pull is a small job that needs a live connection.
- **Alarms go through in-app / FCM notifications only** — no email, no WhatsApp.
- **No alternate-machine routing.** Alternates are surfaced per line, but nothing
  reassigns a SKU when its primary is full.
- **Required-by is the plan period start** for every material. Real staging (caps
  needed later than bottles) needs a production schedule that does not exist yet.
- **The HANA procedure itself is unchanged.** The convention audit reports what it
  does; correcting it, if the verdict is not what Planning intends, is their call.

## 6. Next

1. Chase the **Lead Times sheet** — step 6 is inert without it, and every row on
   the sheet as circulated is still an example.
2. Confirm `floor_basis` with Planning. One field, a 3× difference.
3. Create the `AlarmSubscription` rows per department and schedule
   `send_supply_chain_alarms` daily.
4. Point `load_sales_trend` at the ERP instead of a CSV.
