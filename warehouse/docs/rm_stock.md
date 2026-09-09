# Raw Material Stock register — Backend

> Django app: `warehouse` · Base URL: `/api/v1/warehouse/rm-stock/`
> Frontend: `FactoryFlow/src/modules/warehouse/pages/rmStock/` →
> **Warehouse → Raw Material Stock** (`/warehouse/rm-stock`)

## What it is, and what it is not

The register records **the quantity of each raw material a store states is on
its floor**, item by item and warehouse by warehouse.

SAP already carries an on-hand figure for exactly that (`OITW.OnHand`), and this
does **not** replace it. It answers a different question: what the person
responsible for the store says is actually there, and as of when. The two
disagree in practice — receipts booked late, material issued but never posted,
stock moved between stores without a document — and until now there was nowhere
in the app to record the floor's own figure.

**Nothing here posts to SAP.** No goods receipt, no goods issue, no inventory
posting. The only SAP contact is the read that fills the item picker.

## Entities

`warehouse/models_rm_stock.py`:

- **`RawMaterialStock`** — one **live row per (company, warehouse_code,
  item_code)**: `qty` (18,3), `as_of_date`, `item_name`/`uom` (cached from SAP so
  the register still reads as something human when HANA is unreachable),
  `remarks`, `is_active`, `set_by`. Codes are upper-cased on save — a lower-case
  warehouse code would never match a `UserWarehouse` assignment, and a
  lower-case item code would quietly open a second row for the same material.
- **`RawMaterialStockEntry`** — the change trail: `action`
  (CREATED/UPDATED/REMOVED/RESTORED), `previous_qty` (**null**, not zero, on
  CREATED), `qty`, `as_of_date`, `remarks`, `changed_by`, `changed_at`. Company,
  warehouse and item are **denormalised onto every entry** so the trail stays
  readable after a row is removed, with the FK kept nullable for that case.

Why a register plus a trail, rather than a log of counts: the page's question is
"what is in the store now", so setting a quantity twice updates one row instead
of growing a list every reader then has to reduce. The figure that was replaced
is not lost — it is written to the trail first.

`as_of_date` is deliberately separate from `updated_at`: a keeper often types
Monday's count on Tuesday, and a register that cannot say which day a figure
belongs to cannot be reconciled against anything.

## API

All endpoints need `IsAuthenticated` + `HasCompanyContext` (the `Company-Code`
header), and are company-scoped through `request.company.company`.

| Method | Path | Permission | Notes |
|--------|------|-----------|-------|
| GET | `rm-stock/` | `can_view_rm_stock` | The register. Filters: `warehouse_code`, `search` (item code or name), `include_inactive=true`. Returns `{unrestricted, managed_warehouse_codes, rows}`. |
| POST | `rm-stock/` | `can_set_rm_stock` | Upsert one quantity. Body: `warehouse_code`, `item_code`, `qty`, optional `item_name`, `uom`, `as_of_date`, `remarks`. |
| GET | `rm-stock/items/` | `can_view_rm_stock` | The SAP-backed item picker (see below). |
| GET | `rm-stock/<id>/` | `can_view_rm_stock` | One row plus its full change trail. |
| DELETE | `rm-stock/<id>/` | `can_set_rm_stock` | Takes the item off the register (deactivates). |

The list returns the caller's warehouse scope **alongside** the rows, because
the screen needs both to decide which rows get a Set button; fetching them
separately would flash an editable table that then turns read-only.

### The item picker

`rm-stock/items/` reads SAP HANA: `OITM` where `ItmsGrpCod = 106`
(RAW MATERIAL) and `validFor = 'Y'`, search-filtered on code or name, capped
(default 50, max 200). Passing `warehouse_code` LEFT-joins `OITW` for that
warehouse so each item carries SAP's own `sap_on_hand` for context — LEFT, not
INNER, because an item SAP has never stocked there has no `OITW` row and must
still be pickable.

Item group **106**, not the `RM*` code prefix: the group is what SAP classifies
on and the prefix is only a naming convention.

It is a **separate endpoint on purpose**. It is the only path here that touches
HANA, so a HANA outage costs the picker (503) and not the register — which
answers from Postgres and keeps working.

## Rules

1. **Setting a quantity requires managing that warehouse.**
   `rm_stock_service.set_quantity` calls
   `warehouse_scope.assert_manages(..., action="set raw-material stock")`, so
   `can_set_rm_stock` is necessary but **not sufficient** — the keeper also needs
   an active `UserWarehouse` row for that warehouse (unassigned = blocked,
   superusers exempt; see [`warehouse_scope`](../services/warehouse_scope.py)).
2. **Reads are not warehouse-scoped**, deliberately. Visibility is unrestricted
   across this module — lists show everything, only actions are gated — and a
   register whose totals depend on who is looking is useless for reconciliation.
3. **Upsert, never refuse a duplicate.** "Set it to 40" must work the same
   whether or not somebody set it to 60 yesterday.
4. **Zero is a quantity; blank is not.** An emptied store is a fact worth
   recording, so `qty = 0` saves. Negatives are refused.
5. **A future `as_of_date` is refused** — a quantity cannot already be true
   tomorrow.
6. **Removal deactivates, never deletes** (same reasoning as `UserWarehouse`),
   and setting a removed item again **restores** it rather than being blocked —
   the alternative is a keeper who can see nothing wrong and cannot save.
7. `select_for_update` on the upsert, so two keepers saving the same item at once
   cannot both read the old figure and write a trail that disagrees with the row.

## Permissions & deploy

Two permissions on `RawMaterialStock`:

- `warehouse.can_view_rm_stock` — read the register (planning, supervisors).
- `warehouse.can_set_rm_stock` — state quantities (store keepers).

Groups: `python manage.py setup_rm_stock_groups` creates **RM Stock Keeper**
(both) and **RM Stock Viewer** (view only); `--list` shows what they hold.

Deploy checklist:

1. `manage.py migrate warehouse` (migration
   `0020_rawmaterialstock_rawmaterialstockentry_and_more`) — **both databases if
   the environment runs live + test**.
2. `manage.py setup_rm_stock_groups`, then grant the groups.
3. Confirm the intended keepers have a `UserWarehouse` assignment — otherwise
   they hold the permission, see the page, and every save is refused. The page
   says so in words rather than showing a dead button, and
   `manage.py report_warehouse_scope_gaps` lists them.

## File map

- `models_rm_stock.py` — `RawMaterialStock`, `RawMaterialStockEntry`.
- `services/rm_stock_service.py` — every write goes through here (the scope
  check lives here), plus the register query and the item search.
- `services/wms_hana_reader.py` — `search_items_in_group()`, the SAP read.
- `views_rm_stock.py`, `serializers_rm_stock.py`, `permissions.py`, `urls.py`.
- `management/commands/setup_rm_stock_groups.py`.
- `tests_rm_stock.py` — 26 tests (service rules + API permissions + a mocked
  HANA outage).

## Related

- [`warehouse/docs/README.md`](./README.md) — BOM issue / FG receipt.
- `warehouse/services/warehouse_scope.py` — the per-warehouse manager rules.
- Memory: warehouse-manager-scoping, rm-pm-fg-warehouse-redesign,
  cross-company-flow-boundary.
