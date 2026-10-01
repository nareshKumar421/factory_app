# Blowing module (preform → bottle)

Bottle-making from preform on blowing machines, one machine per company
(JIVO_OIL, JIVO_BEVERAGES). Run-based, mirroring `production_execution`.

Source of truth for the data model: `FactoryFlow/docs.local/Linear.xlsx`
(sheet *Linear Data*) — each row is one `BlowingRun`.

## Concepts

- **BlowingMachine / PreformSpec / BlowingRateConfig** — company-scoped master data.
  The latest active rate config effective on a run's date is *snapshotted* onto the run,
  so historical runs keep the rates they were costed at.
- **BlowingRun** — one machine + preform-spec session on a date (`run_number` is
  sequential per company per date). Operators enter readings/counts; quantity fields
  (`machine_units`, `total_units`, `preform_used_g`, `rejection_pct`, `total_manpower`)
  are computed in `save()`.
- **BlowingRunCost** — auto-computed via `services/cost_calculator.py`.

## Cost formulas (see cost_calculator.compute_run_cost)

```
operator_cost   = operator_count * operator_rate_per_day
labour_cost     = (contract_labour + own_labour) * labour_rate_per_day
electricity_cost= total_units * electricity_rate_per_unit
wastage_cost    = rejection_pcs * (preform_gram / 1000) * preform_rate_per_kg
total_cost      = operator + labour + wastage + electricity
scrap_bottle    = rejection_pcs * scrap_rate_per_bottle
net_cost        = total_cost - (scrap_bottle + scrap_carton_value)
blowing/bottle  = net_cost / total_counter_production
per_bottle_cost = blowing/bottle + packing_rate_per_bottle
```

Verified against Linear.xlsx row 1 in `blowing/tests.py`.

## Shift sheet (`services/shift_sheet.py`)

The person who books runs is not at the machine for every shift — the line runs
through the night — so a missed shift reaches them as the floor's Excel: a date
line, a header (`sku · shift · total production · labour company/outside · total
electricity · utility · wastage`), then a SKU line with a day / night line under it.
FactoryFlow's **Blowing Shift Sheet** page takes it in those columns, typed or
uploaded, many dates and SKUs at once, and books each row as a COMPLETED run.

- `POST shift-sheet/parse/` reads an uploaded `file` into rows (SKU matched to a
  preform spec by make + gram). Figures with no shift are listed as `ignored`, never
  guessed. The workbook is read *not* read-only: read-only mode returns values hidden
  under a merged cell, and the floor's reused file keeps an old date under its merged
  date line.
- `POST shift-sheet/` with `{machine_id, rows}` plans without writing — run number,
  meter readings, cost, duplicates. With `commit: true` it re-plans under a lock on
  the machine and books every NEW row, or nothing if any row needs fixing (400 with
  the plan). Needs both `can_create_blowing_run` and `can_complete_blowing_run`.

Rules: a shift the app already has (same date, shift and preform) is skipped — the
floor's figures outrank the sheet's — unless the row says `add_anyway`. A run's shift
is read off its remarks (`… night shift`), else its first segment's start (06:00–18:00
IST = day), else when it was opened; **never its run number**, which is a per-date
sequence (29 Sep 2026's night shift is run 1). The sheet gives units, so a row starts
the meter where the run before it (date, then shift order) stopped. `created_at` is
09:00 IST for a day shift and 20:00 IST for a night shift; the warehouse status is
APPROVED, since there is no preform request for a shift that has already run.

## SAP (v1 = read-only)

`services/sap_reader.BlowingItemReader` reuses `production_execution`'s
`ProductionOrderReader` for item pickers (preform = all items; bottle = produced only).
No stock postings yet — `BlowingRun.sap_preform_item_code` / `sap_bottle_item_code`
are reserved for a later Goods Issue / Goods Receipt phase.

## Setup

```
python manage.py migrate blowing
python manage.py setup_blowing_groups     # role groups: Operator / Supervisor / HOD
```

Endpoints are under `/api/v1/blowing/`.
