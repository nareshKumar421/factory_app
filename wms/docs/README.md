# Warehouse Ops (bin-level WMS) — Backend

> Django app: `wms` · mounted at `/api/v1/wms/` (`config/urls.py`)
> Frontend companion doc: [FactoryFlow `docs/modules/wms.md`](../../../FactoryFlow/docs/modules/wms.md)
> Absolute path: `C:/Users/gurpa/dev/FactoryFlow/docs/modules/wms.md`

> **Naming warning.** This `wms` app is the *self-contained, bin-level* Warehouse
> Ops system (warehouses → zones → cells → pallets → stock, all designed in the
> browser). It is **not** the SAP stock-analytics "WMS" — those endpoints live in
> the separate `warehouse` app under `/api/v1/warehouse/wms/...` (OITW/OITM/OINM
> dashboards, BOM requests, FG receipts). Two different things share the letters
> "WMS"; this doc is only about the `wms` app.

---

## Overview — what it does & who uses it

The `wms` app is an intentionally **thin, multi-tenant JSON persistence shim**.
All warehouse logic — layout design, occupancy, putaway suggestions, move
validation, picking strategy, the outbound audit, stock math — lives in the
**frontend** (`FactoryFlow/src/modules/wms`). The backend's only job is to store
each record the browser authors, verbatim, and share it across devices/users and
company boundaries.

Every record is a **camelCase JSON document** with a client-generated `id`
(a UUID, or the fixed `wms-settings` id for the settings singleton). The server
persists that document in a `data` JSONField and returns it unchanged, so the
round-trip is lossless and the schema never has to track the frontend types.

Who touches it:
- **The WMS frontend** via the storage adapter (`src/modules/wms/storage/apiAdapter.ts`) — the only real client.
- **Django admin** (`wms/admin.py`) — a rich, badge/filter/export console over the JSON docs, plus a read-only Dashboard overview page.
- **Ops/superusers** — for inspection, bulk export, and manual record repair.

Design consequence to internalise: **the server does no validation and enforces
no referential integrity.** It will happily store a pallet with a `currentLocationId`
that points at a deleted location, two pallets with the same license plate, or
negative stock. Correctness is the frontend's responsibility.

---

## Key concepts & entities

`WmsRecord` (abstract, `wms/models.py`) is the base every collection shares:

| Field | Meaning |
|-------|---------|
| `company` | FK to `company.Company` (`related_name='+'`). Every row is owned by exactly one company. |
| `record_id` | The client-generated `data['id']` (UUID) — or `wms-settings` for the settings singleton. Indexed. |
| `data` | The full camelCase document the frontend authored. |
| `created_at` / `updated_at` | Server timestamps (`auto_now_add` / `auto_now`). |

`Meta`: `unique_together = ('company', 'record_id')`, `ordering = ['created_at']`.
The uniqueness is **per company**, so two companies keep fully isolated data sets
and each company's `wms-settings` singleton never collides.

**Collections** — one concrete model per collection, all sharing the base. The
URL segment → model map (`COLLECTION_MODELS` in `wms/permissions.py`) is the single
source of truth, imported by the views:

| URL segment | Model | What the document holds |
|-------------|-------|-------------------------|
| `warehouses` | `Warehouse` | Grid dimensions, naming scheme, areas, `type` (`OWN`/`SAP`), `sapWarehouseCode`. |
| `zones` | `Zone` | A tinted, classified group of cells inside a warehouse. |
| `cellPurposes` | `CellPurpose` | What a cell *is* (storage / walkable path / damaged…); `holdsStock` flag. |
| `locations` | `Location` | A single bin/cell: code, barcode, coords, capacity, material rules, status. |
| `materials` | `Material` | Warehouse profile for an item (UoM, units/box, temp class, tracking flags). |
| `pallets` | `Pallet` | A license plate: item, box count, lot, expiry, `currentLocationId`, status. |
| `inventory` | `Inventory` | Stock of an item at a location, optionally on a pallet. |
| `movements` | `Movement` | Append-only audit log (RECEIVE/PUTAWAY/TRANSFER/PICK/OUTBOUND/ADJUSTMENT/AUDIT/CYCLE_COUNT). |
| `templates` | `Template` | A reusable warehouse blueprint (grid + zones). |
| `settings` | `Settings` | The per-company singleton (`wms-settings`): master enable flag + workflow modes. |

`Dashboard` (`wms/admin.py`) is a **proxy of `Movement`** used only to add a
read-only overview page to the admin — no schema of its own.

Two special collections:
- **`movements` is append-only** — never editable (`APPEND_ONLY_COLLECTIONS`).
- **`settings` is a singleton** — id `wms-settings`, with a first-run seed exception (below).

---

## End-to-end flows (as the server sees them)

The server is a generic CRUD surface (`wms/views.py`, `wms/urls.py`). Full paths
are `/api/v1/wms/<collection>/...`:

1. **List a collection** — `GET /wms/<collection>/`
   - Filters to `request.company.company`, orders by `created_at`.
   - `?warehouseId=<id>` → adds `data__warehouseId=<id>` (Postgres JSON lookup). Lets a single-warehouse screen avoid pulling every warehouse's rows (Jivo Oil had 5,197 locations across 3 warehouses). Collections without a `warehouseId` key (warehouses/materials/settings) simply match nothing extra.
   - `?limit=&offset=` → returns a `{results, count, offset, limit}` page. **With no `limit`, a bare JSON array is returned** (the default the adapter expects).

2. **Create / upsert one** — `POST /wms/<collection>/`
   - `_upsert` → `update_or_create(company, record_id, defaults={'data': record})`, keyed on `data['id']` (a UUID is generated server-side if absent). So POST is really an **idempotent upsert by id**, returning `201` with the stored doc.

3. **Bulk upsert** — `POST /wms/<collection>/bulk/`
   - Same upsert, for a list of records, inside a **single `transaction.atomic()`**. Non-dict items are skipped. This is the *only* endpoint that is atomic across multiple records. (Route declared before `<record_id>/` so `bulk` is never parsed as an id.)

4. **Get one** — `GET /wms/<collection>/<id>/` → the stored doc, or `404` (the adapter maps 404 → `null`).

5. **Patch one** — `PATCH /wms/<collection>/<id>/`
   - **Deep merge** (`_deep_merge`): nested dicts merge recursively; lists/scalars replace. This avoids a shallow-merge wiping sibling keys of a nested object (capacity, materialRules, namingScheme…). `id` is forced back to `record_id` (**immutable**).

6. **Delete one** — `DELETE /wms/<collection>/<id>/` → `204`.

**A frontend operation is usually several of these requests.** e.g. "receive &
put away" = `POST pallets` + `POST inventory` + `POST movements` (three separate
HTTP calls from `wmsStore.receiveStock`). The server does **not** wrap them in one
transaction — only `bulk/` is atomic, and only within one collection. See
[the frontend doc](../../../FactoryFlow/docs/modules/wms.md) for how each screen
composes these calls.

---

## Critical business rules & invariants

Enforced **server-side** (`wms/permissions.py`, `wms/views.py`):

- **Company scoping is mandatory.** `HasCompanyContext` (`company/permissions.py`) reads the `Company-Code` request header, looks up an active `UserCompany(user, company__code)`, and attaches `request.company`. **No/invalid header → `403`** (`"Company-Code header is missing."` / `"You do not have access to any companies."`). Every query is filtered by `request.company.company` — companies never see each other's data.
- **Reads are open** to any authenticated user with company context (`SAFE_METHODS` bypass in `WmsCollectionPermission`). There is **no per-collection view gate** on the API.
- **Writes require the Django model permission** `wms.<add|change|delete>_<model>` for the collection's model. Superusers bypass via `has_perm`.
- **`movements` is append-only.** `PUT`/`PATCH` on `movements` is always `403`; only a user with `wms.delete_movement` may delete one.
- **Settings first-run seed.** A `POST /wms/settings/` is allowed for *any* authenticated company user **iff** no `Settings` row exists yet for that company (`_settings_already_seeded`). Once seeded, changing it needs `wms.add_settings`/`wms.change_settings`. (This lets the frontend lazily seed defaults on first load without pre-granting the perm.)
- **`id` is immutable** across a PATCH.
- **Lossless round-trip** — documents are stored and returned verbatim; the API shape always equals what the frontend sent.

**NOT enforced server-side** (all delegated to the frontend — treat as invariants
the *client* must uphold, not guarantees the server gives):

- No referential integrity between collections (a pallet/inventory line may reference a deleted location; deleting a warehouse does not cascade its zones/locations server-side — the frontend does that in `deleteWarehouseCascade`).
- No stock arithmetic, capacity, material-rule, or negative-stock checks.
- No uniqueness of `licensePlate`, `code`, or `barcode` within a company.
- No server-side actor attribution — `userId`/`userName` on a movement are whatever the client put in the JSON.
- No optimistic-concurrency check — upsert and PATCH are **last-write-wins**.

---

## Integrations & cross-module boundaries

- **SAP: none, directly.** This app never posts to SAP. A warehouse document *may*
  carry `type: 'SAP'` and a `sapWarehouseCode`, but those are consumed **only by
  the frontend** to bridge pallet moves to/from the `barcode` module (which owns
  its own backend + SAP path). See the frontend doc's cross-module section.
- **`company` app** — `HasCompanyContext`, `UserCompany`, `Company` provide the multi-tenant scoping. This is the one hard backend dependency.
- **`auth` groups** — `WMS Admin` / `WMS Operator` (migration `0003_wms_access_groups`) carry the model permissions that gate writes.
- **The older `warehouse` app** — unrelated except for the shared "WMS" name and a
  route-prefix near-collision. `warehouse/urls.py` mounts `/wms/dashboard/`,
  `/wms/stock/...`, `/wms/warehouses/...` (SAP analytics) under the *warehouse* app.
  This `wms` app is a different mount (`/api/v1/wms/`). Don't conflate them.

Migrations of note: `0001_initial`, `0002_dashboard` (proxy), `0003_wms_access_groups`
(groups + perms), `0004_cellpurpose`, `0005_fold_zones_into_purposes`.

---

## Real-world edge cases

Each: **trigger → current behaviour → operator-visible symptom → risk/gap.**

- **Partial multi-write failure (network/DB drop mid-operation).**
  Trigger: `receiveStock`/`shipPallet`/`moveInventory` issue several POST/PATCH/DELETE calls; one succeeds, the next fails.
  Behaviour: the server has no cross-request transaction, so it persists the successful half and rejects the rest.
  Symptom: a pallet marked `SHIPPED` whose inventory still exists, or stock on the map with "no pallet", or a movement with no matching stock change.
  Risk/gap: silent data drift; only repairable from the frontend (Settings → *Reconcile pallet & stock links*, or the Admin console). No server-side integrity sweep exists.

- **Duplicate license plate.**
  Trigger: a plate is received twice (the frontend `makePallet` always creates a new row; the server upserts by `id`, not plate).
  Behaviour: two `Pallet` rows with the same `licensePlate`.
  Symptom: outbound/transfer scan resolves whichever the frontend finds first; the other becomes a phantom.
  Risk/gap: no server uniqueness constraint on business keys.

- **`warehouseId` filter on a collection that has no such key.**
  Trigger: `GET /wms/warehouses/?warehouseId=X` (warehouses store no `warehouseId`).
  Behaviour: `data__warehouseId=X` matches zero rows → empty list.
  Symptom: a screen that wrongly scopes such a call would show "no warehouses".
  Risk/gap: caller must only pass `warehouseId` for the per-warehouse collections.

- **Very large collection with no `limit`.**
  Trigger: `GET /wms/locations/` company-wide on a big site.
  Behaviour: full array (thousands of rows, multi-MB) in one response.
  Symptom: slow load / large transfer.
  Risk/gap: pagination and `warehouseId` scoping exist but are opt-in — a caller that forgets them pays the full cost.

- **Operator can read but not write.**
  Trigger: a `WMS Operator` opens a structural collection (e.g. tries to save a warehouse edit) — operators hold no `add/change_warehouse`.
  Behaviour: reads succeed, the write returns `403`.
  Symptom: "the page loads but Save fails." (The frontend also nav-gates these pages, so this mainly bites API/automation callers.)
  Risk/gap: read-everything, write-little is intentional but asymmetric.

- **Settings seed race.**
  Trigger: two devices first-load a brand-new company simultaneously; both see no settings and both `POST /wms/settings/`.
  Behaviour: both pass the "not yet seeded" bypass; `update_or_create` on the fixed `wms-settings` id means the second simply overwrites the first (same id).
  Symptom: none visible (idempotent by id).
  Risk/gap: benign because the singleton id is fixed; would not be if the seed used random ids.

- **Unknown collection.**
  Trigger: `GET /wms/widgets/`.
  Behaviour: `WmsCollectionPermission` lets it through, the view returns `404 {"error": "Unknown WMS collection 'widgets'."}`.

---

## Failure modes / what can break

- **`Company-Code` header missing or wrong** → every WMS call `403`. Operator symptom: the whole Warehouse Ops area fails to load ("no data / permission denied"). First thing to check when a user says "WMS is blank."
- **Write permission missing** → `403` on save while reads still work. Symptom: "I can see stock but can't receive/move/ship." Fix = add the user to `WMS Operator`/`WMS Admin`.
- **No cross-request atomicity** → half-applied receive/ship/transfer after a mid-operation error (see edge cases). Symptom: orphaned pallets/stock; needs the frontend reconcile tool.
- **Shared Postgres box** — per team memory, `factory_app` shares one Postgres instance across ~14 databases; a restart is a multi-app outage, and a "slow WMS API" is almost always a DRF/N+1 or a huge unpaginated list, not infra. The admin Dashboard/changelist do `COUNT`/`annotate` aggregates over full tables and can be slow on large `movements`/`inventory`.
- **Malformed body** → `400` (`"Expected a single record object."` / `"Expected a list of records."` / `"limit/offset must be integers."`).
- **Last-write-wins overwrite** — two users editing the same record; the later save silently clobbers the earlier (no version check).

---

## Improvement opportunities & known gaps

- **No transactional composite endpoint.** A "receive"/"ship"/"transfer" is several client calls; a single server endpoint (or a generic multi-collection `bulk` transaction) would remove the partial-failure class of bugs.
- **No referential integrity / validation.** Even lightweight server checks (does `warehouseId`/`locationId` exist? is `licensePlate` unique per company?) would catch client bugs before they persist.
- **Reads are entirely ungated.** Any authenticated company user can read every WMS collection. If some data should be operator-hidden, add a read gate.
- **Server-side actor attribution.** Movements trust client-supplied `userId`/`userName`; stamping `request.user` server-side would make the audit log trustworthy.
- **Optimistic concurrency** (an `If-Match`/version field) to stop silent overwrites.
- **Admin aggregate cost** on large `movements`/`inventory` (Dashboard + summary chips scan full tables).

---

## Permissions & roles

Two auth groups created by `0003_wms_access_groups`:

| | `WMS Admin` | `WMS Operator` |
|---|---|---|
| Structural writes (warehouses, zones, cellPurposes, locations, materials, templates, settings) | ✅ all `add/change/delete` | ❌ none |
| Pallets — `add/change/delete_pallet` | ✅ | ✅ |
| Inventory — `add/change/delete_inventory` | ✅ | ✅ |
| Movements — `add_movement` | ✅ | ✅ |
| Movements — `change/delete_movement` | ✅ (delete only; movements are never editable) | ❌ |
| Reads (any collection) | ✅ | ✅ (reads are not permission-gated in the API) |

Notes:
- `WMS Operator` has **no `view_*` permissions** — so the frontend gates operator pages on the *operational write* perms (`WMS_ACCESS`), not on `view_*`. See the frontend doc's Permissions section.
- On a fresh DB the migration calls `create_permissions` for the `wms` app before assigning, because the post-migrate hook hasn't run yet.
- Superusers bypass all of the above.

---

## Developer file map

**Backend (`C:/Users/gurpa/dev/factory_app/wms/`):**
- `models.py` — `WmsRecord` base + 10 collection models + `Dashboard` proxy.
- `views.py` — `WmsCollectionAPI` (list/create), `WmsBulkCreateAPI`, `WmsRecordAPI` (get/patch/delete); `_upsert`, `_deep_merge`.
- `permissions.py` — `COLLECTION_MODELS`, `WmsCollectionPermission`, append-only + settings-seed rules.
- `urls.py` — the three routes (`bulk/` before `<record_id>/`).
- `admin.py` — badge/filter/export admin + `DashboardAdmin` overview page.
- `apps.py` — `WmsConfig` (`verbose_name = 'Warehouse Ops (WMS)'`).
- `migrations/` — `0001`…`0005` (see above).
- `config/urls.py` (project) — `path("api/v1/wms/", include("wms.urls"))`.
- `company/permissions.py` (project) — `HasCompanyContext`.

**Frontend (`C:/Users/gurpa/dev/FactoryFlow/src/modules/wms/`):**
- `storage/apiAdapter.ts` — the REST client that speaks to this app (BASE `/wms`).
- `storage/adapter.types.ts` — `WmsCollectionMap` (collection → record type), `WMS_COLLECTIONS`.
- `store/wmsStore.ts` — the composite operations (`receiveStock`, `shipPallet`, …) that turn one user action into several API calls.
- `types/wms.types.ts` — the record shapes stored in `data`.

---

## Related docs

- **Frontend companion (required reading):** [`FactoryFlow/docs/modules/wms.md`](../../../FactoryFlow/docs/modules/wms.md) — screens, user journeys, offline/scanning behaviour, permission-gated navigation, cross-module barcode bridge, and the operator-visible failure UX.
- **Not this module (the SAP-analytics "WMS"):** `FactoryFlow/src/docs/WMS_ARCHITECTURE.md`, `WMS_API_REFERENCE.md`, and the `warehouse` app under `/api/v1/warehouse/wms/...`. Those describe SAP OITW/OITM stock dashboards, BOM requests, and FG receipts — a separate subsystem that predates this one and reuses the "WMS" name.
