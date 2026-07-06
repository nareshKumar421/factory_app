# Database Index Migration — Deployment Guide

**Branch:** `perf/safe-config-wins`
**What this delivers:** 22 composite `Meta.indexes` across 13 models (Phase 2 of the
[Performance Improvement Roadmap](../PERFORMANCE_IMPROVEMENT_ROADMAP.md)), plus the
safe config wins already on this branch (persistent DB connections, GZip, logging).

These are **additive schema changes only** — new indexes. No columns are added,
altered, or dropped. No data is migrated. **No frontend contract changes.** The API
responses are byte-for-byte identical; only the database gets faster at the hot
filtered/ordered queries.

---

## ⚡ TL;DR — what YOU need to do on the server

1. **Take a DB snapshot / backup** (standard pre-migration hygiene).
2. Pull the branch and **run migrations** — they build indexes with
   `CREATE INDEX CONCURRENTLY`, so **writes are NOT blocked** during the build:
   ```bash
   git fetch && git checkout perf/safe-config-wins
   # no new Python packages to install for this change
   python manage.py migrate
   ```
3. **Restart the app** (to pick up the `settings.py` config changes — `CONN_MAX_AGE`,
   GZip, logging). The index migrations do **not** require a restart, but the settings
   changes do.
4. **Verify** a few indexes are in use (see "Verify" below).

That's the whole deploy. Everything else in this doc is context, safety notes, and
rollback.

---

## ⚠️ Important: how these migrations run (read before deploying)

Every index migration is written with:

```python
atomic = False
operations = [ AddIndexConcurrently(...), ... ]
```

`CREATE INDEX CONCURRENTLY` builds the index **without taking a write lock**, so the
app keeps serving traffic normally while each index builds. In exchange, there are
three rules you must respect:

1. **Do not wrap `migrate` in your own transaction.** Concurrent index creation
   cannot run inside a transaction block. Django already runs these migrations
   non-atomically; just don't add an outer `BEGIN/COMMIT` around `manage.py migrate`
   in your deploy script.

2. **Run the migration once, to completion.** If a `CONCURRENTLY` build is
   interrupted (deploy killed, connection dropped), Postgres can leave an **INVALID**
   index behind. It's harmless (the planner ignores it) but should be cleaned up.
   Detect and drop any invalid index, then re-run `migrate`:
   ```sql
   -- find invalid indexes
   SELECT indexrelid::regclass AS index_name
   FROM pg_index WHERE indisvalid = false;
   -- drop the specific one it names, e.g.:
   DROP INDEX CONCURRENTLY <index_name>;
   ```
   Then `python manage.py migrate` again — Django is idempotent about applied
   migrations and will only build what's missing.

3. **No parallel schema changes.** Don't run two `migrate` processes at once, and
   avoid other DDL on the same tables while these build.

**Timing:** on tables of a few hundred thousand rows each index builds in seconds to a
minute or two. Prefer a lower-traffic window if the tables are large, but a
**write-lock outage is not expected** because of `CONCURRENTLY`.

---

## What indexes are added (and why)

All are composites chosen to match the real filter + order-by patterns on
fast-growing tables. `-col` means the index column is stored descending to match a
`ORDER BY col DESC` / `ordering = ['-col']`.

| # | App / Model | Index columns | Serves |
|---|---|---|---|
| 1 | barcode `Box` | `(company, status, -created_at)` | box lists filtered by status, newest-first |
| 2 | barcode `ScanLog` | `(company, -scanned_at)` | scan history, newest-first |
| 3 | barcode `ScanLog` | `(company, entity_type)` | scans filtered by entity type |
| 4 | production_execution `ProductionRun` | `(company, status, -date)` | run queues by status, by date |
| 5 | production_execution `ProductionRun` | `(sap_doc_entry)` | near-unique SAP doc point lookup |
| 6 | production_execution `LineClearance` | `(company, -date)` | line-clearance lists by date |
| 7 | warehouse `BOMRequest` | `(company, status)` | BOM request status queue |
| 8 | warehouse `BOMRequest` | `(company, -created_at)` | BOM request lists, newest-first |
| 9 | warehouse `FinishedGoodsReceipt` | `(company, status)` | FG-receipt status queue |
| 10 | warehouse `FinishedGoodsReceipt` | `(company, -created_at)` | FG-receipt lists, newest-first |
| 11 | docking_admin `DockingScanSkipRequest` | `(company, status)` | skip-request approval queue |
| 12 | docking_admin `DockingScanSkipRequest` | `(company, -requested_at)` | skip-request lists, newest-first |
| 13 | docking_admin `DockingPartialScanRequest` | `(company, status)` | partial-scan approval queue |
| 14 | docking_admin `DockingPartialScanRequest` | `(company, -requested_at)` | partial-scan lists, newest-first |
| 15 | quality_control `MaterialArrivalSlip` | `(status, submitted_at)` | arrival-slip tabs by status |
| 16 | quality_control `ProductionQCSession` | `(workflow_status, session_type)` | QC session queues |
| 17 | person_gatein `EntryLog` | `(status, entry_time)` | "who's inside" |
| 18 | person_gatein `EntryLog` | `(labour, status)` | per-labour open-entry guard |
| 19 | person_gatein `EntryLog` | `(visitor, status)` | per-visitor open-entry guard |
| 20 | notifications `Notification` | `(recipient, is_read, -created_at)` | `?is_read=` filtered inbox, newest-first |
| 21 | driver_management `VehicleEntry` | `(company, status)` | shared gate log filtered by status |
| 22 | driver_management `VehicleEntry` | `(company, entry_type)` | shared gate log filtered by entry type |

Notes:
- **`MaterialArrivalSlip`, `ProductionQCSession`, `EntryLog`** are **not** company-scoped
  (no `company` field), so their indexes intentionally omit `company`.
- **`EntryLog`** already had single-column indexes on `status`, `entry_time`,
  `person_type`; we only add the composites above.
- **`Notification`** already had `(recipient, -created_at)` and `(recipient, is_read)`;
  the new triple `(recipient, is_read, -created_at)` is a superset that fully serves
  the filtered-inbox query. The old `(recipient, is_read)` is now technically a prefix
  of the new index and could be dropped later, but we left it in place to keep this
  change strictly additive.
- **`VehicleEntry`** is the shared, unbounded gate log that all 6 gate-in apps filter —
  the two indexes here are the single biggest win in this batch.

---

## Verify (after migrate)

1. **Confirm the indexes exist and are valid:**
   ```sql
   SELECT indexrelid::regclass AS name, indisvalid
   FROM pg_index
   WHERE indexrelid::regclass::text LIKE ANY (ARRAY[
     'box_%','scanlog_%','prodrun_%','lineclr_%','bomreq_%','fgr_%',
     'dockskip_%','dockpart_%','matslip_%','prodqc_%','entrylog_%',
     'notif_recip_read_created_idx','vehentry_%'
   ]);
   ```
   Every row should show `indisvalid = t`.

2. **Prove a seq-scan became an index-scan** on a hot query, e.g. the shared gate log:
   ```sql
   EXPLAIN (ANALYZE, BUFFERS)
   SELECT * FROM driver_management_vehicleentry
   WHERE company_id = <id> AND status = 'DRAFT'
   ORDER BY created_at DESC LIMIT 50;
   ```
   Expect `Index Scan using vehentry_co_status_idx` (not `Seq Scan`).

3. Cross-check total time dropped for that query in `pg_stat_statements` (if enabled
   per Phase 0.3).

---

## Rollback

Fully reversible. To remove all indexes from this batch:

```bash
python manage.py migrate barcode <prev>
python manage.py migrate production_execution <prev>
python manage.py migrate warehouse <prev>
python manage.py migrate docking_admin <prev>
python manage.py migrate quality_control <prev>
python manage.py migrate person_gatein <prev>
python manage.py migrate notifications <prev>
python manage.py migrate driver_management <prev>
```

(Replace `<prev>` with the migration number that preceded this batch in each app —
`python manage.py showmigrations <app>` lists them; migrate to the one just above the
new one.) Reverse also uses `DROP INDEX CONCURRENTLY`, so unblocked.

Config-change rollback (no redeploy needed, env only):
- `DB_CONN_MAX_AGE=0` — disables persistent connections.
- Remove `GZipMiddleware` from `MIDDLEWARE` to disable compression.
- `LOG_LEVEL=WARNING` — quieter logs.

---

## What is deliberately NOT in this change

Per the "must not affect frontend, must be safe to deploy" constraint:

- **No pagination** (Phase 1.3) — would change API response shape and break the
  frontend.
- **No new Python packages** (orjson / silk / Sentry / Celery) — nothing new to
  `pip install` on the server.
- **No ORM/business-logic rewrites** (Phase 2b) — those change runtime behavior and
  carry more risk; can be a separate, reviewed PR.
