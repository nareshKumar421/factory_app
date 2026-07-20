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
