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

## 3. Verified against real data

`python manage.py seed_supply_chain_demo --company JIVO_OIL` loads the
template's own machines and SKUs plus a sample plan, so the dashboard can be
demonstrated before any department returns their sheet — which is what the
brief's "working dashboard built and demonstrated for review" needs.

Importing the **real** `JIVO Supply Chain Reference Template.xlsx` gives:

```
lead times 0 | machines 4 | sku-map 5 | examples skipped 5 | warnings: none
```

and the resulting dashboard:

```
=== PROCUREMENT (step 6) ===
  material         shortage  order qty  lead     order by         alarm
  RM-OIL-MUS            110        120    45   2026-07-16       OVERDUE
  PM-BTL-1L            5000       5000    30   2026-07-31       OVERDUE
  PM-CAP-26           90000     100000    21   2026-08-09       OVERDUE
  PM-SHRINK            9000       9000     -            -  NO_LEAD_TIME
  PM-LBL-JIVO        130000     200000    12   2026-08-18     SCHEDULED
  PM-CTN-20            6500      10000     7   2026-08-23     SCHEDULED

=== PRODUCTION CAPACITY (step 7) ===
  line    usable h  needed h   util%  skus  feasible
  M-01      414.50      9.20     2.2     2  True
  M-02      415.00      7.83     1.9     1  True
  M-03      206.50      7.06     3.4     1  True
```

**`lead times 0` is the real finding.** Every row on the Lead Times sheet is
still an example — Procurement has not filled theirs in. Machines and the SKU map
are genuinely populated. Since step 6 is entirely lead-time driven, procurement
alarms cannot run for real until that sheet comes back.

### Tests

35 tests, all passing (`manage.py test supply_chain --settings=config.sqlite_test_settings`),
covering the 3× floor ambiguity, MOQ rounding, changeover, every alarm state,
open-PO netting, per-SKU-rate hour summing, unmapped SKUs, company isolation,
forecast scoping, the example-row trap, the formula-row trap, and the seed.

Nothing touches SAP or HANA — the module reads rows already in Postgres, so the
whole chain is testable offline.

## 4. What is NOT built

Stated plainly so nobody assumes otherwise:

- **No frontend.** The API is complete and the branch exists in FactoryFlow, but
  no dashboard page was written. This is backend only.
- **No alarm delivery.** Alarms are computed and returned; nothing emails or
  notifies. `notifications/` is the obvious home, but the brief never says who is
  alarmed, through which channel, or at what threshold.
- **The floor is not enforced end to end.** `SupplyChainPolicy.stock_floor()` is
  implemented and tested, but `min_stock` still comes from the HANA procedure.
  Whether the procedure's floor is the brief's 35% is unverified, and reconciling
  the two needs someone who knows the procedure.
- **Steps 3 and 5 still contradict each other** on whether the floor is added or
  subtracted. That lives in the procedure, not here.
- **WIP / already-scheduled production** is not netted off the FG gap.
- **No alternate-machine routing.** Alternates are surfaced per line, but nothing
  reassigns a SKU when its primary is full.
- **Required-by date is the plan period start** for every material. Real staging
  (caps needed later than bottles) would need a production schedule that does not
  exist yet.

## 5. Next

1. Chase the **Lead Times sheet** — step 6 is inert without it.
2. Confirm `floor_basis` with Planning. It is a one-field change and a 3×
   difference.
3. Decide alarm delivery, then wire `notifications/`.
4. Build the dashboard page against the existing API.
