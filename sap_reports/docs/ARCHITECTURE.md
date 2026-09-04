# SAP Reports — Architecture

## What this module is

The teams maintain shelves of reports inside **SAP's Query Manager** — saved SQL
queries filed under query categories (**Factory**, **General**, **GST R1**, …).
People open the SAP client, pick a report, type values into unlabelled prompt
boxes, and read the grid.

This module puts those same reports in the app, without re-implementing any of them.

The reports are not coded here. They are **discovered** from SAP: each company
database has an `OUQR` table holding every saved query (name, category, SQL text),
and a `OQCN` table naming the categories. A sync mirrors that into a local
catalogue — by default every report category, skipping SAP's own machinery
categories (System, E-Billing, `SAP_DASHBOARD_*`, `KPI_MOBILE*`, User Defined
Value, APPROVAL TEMPLATES), which hold dashboard feeds, approval-procedure
conditions and formatted searches rather than reports. Running a report executes
SAP's own SQL, read-only, with bound parameters.

The consequence worth stating plainly: **a report added or edited in SAP appears in
the app after a sync, with no code change and no release.** At the time of writing
that is 21 reports for Jivo Oil and 3 for Jivo Beverages.

## Why generic, not 21 hand-ported endpoints

Porting each query into Python would have meant 21 endpoints, 21 filter contracts
and 21 things to keep in step with whatever the factory team edits in SAP next
week. The queries are also not static — they are the team's working tools, and they
get edited.

The one real cost of the generic approach is that a saved query does not say what
its prompts *mean* (see below). That is a solvable problem; drift between 21 copies
of a query is not.

## The pieces

```
sap_reports/
  sql.py              normalise / read-only guard / prompt binding   (no DB, no SAP)
  parameters.py       infer what each prompt asks for; coerce values (no DB, no SAP)
  hana_reader.py      every HANA round-trip: catalogue, run, lookups
  models.py           SapReport, SapReportParameter, SapReportRun
  services/
    catalog.py        sync OUQR -> SapReport rows
    runner.py         run one report, cap the rows, audit it
    lookups.py        master-data options for a report's filters
  exports.py          result -> csv / xlsx
  views.py            the API
  management/commands/sync_sap_reports.py
```

## Four problems, and how each is handled

### 1. SAP's SQL is written to run *inside* SAP

A saved query's tables are unqualified (`FROM OINM T0`) because Query Manager runs
it with the company database as the current schema. Every report connection
therefore issues `SET SCHEMA "<company db>"` before executing. Without it, every
report fails on an unresolvable table name.

SAP also stores line breaks as bare carriage returns. HANA treats a CR as
whitespace so execution is unaffected, but anything that *displays* the SQL is
unreadable until `normalise_sql` runs, so the text is normalised on the way in.

### 2. A prompt says nothing about what it wants

SAP writes a prompt as a quoted placeholder:

```sql
WHERE T0."DocDate" BETWEEN '[%0]' AND '[%1]'
  AND T0."ItemCode" = '[%2]'
```

`[%0]` has no name, no type and no help text. Inside SAP the user is simply
expected to know that the first box wants `20260801` and the third an item code.
That expectation does not survive being moved into a web app.

So `parameters.py` reads the SQL around each placeholder and infers:

| Inferred from | Result |
|---|---|
| `T0."DocDate" >= '[%0]'` | a date, labelled **From date** |
| `... <= '[%1]'`, or the tail of `BETWEEN … AND '[%1]'` | a date, labelled **To date** |
| `"ItemCode"`, `"WhsCode"`, `"CardCode"`, `"ItmsGrpNam"` | item / warehouse / partner / item-group picker |
| `from OFCT T1 where T1."Name" = '[%0]'` | a fiscal period (the table disambiguates a meaningless column name) |
| `I."WhsCode" = /* T0."WhsCode" */ '[%0]'` | a warehouse — SAP's own prompt annotation, when present |
| `(T4."ItmsGrpNam" = '[%2]' OR '[%2]' = '')` | an **optional** filter; blank means "no filter" |
| nothing recognisable | free text |

Two cases needed more than reading the first occurrence:

- **A prompt used several ways.** `Purchase working Query` uses `[%0]` both as
  `DocDate < '[%0]'` (an opening-balance cut-off) and as the start of
  `BETWEEN '[%0]' AND '[%1]'`. Reading only the first occurrence labels the
  period's *start* "To date" — the opposite of what the user must type. So all
  occurrences are considered and a range use outranks a bare comparison.
- **Repeated labels.** `Honey special report FG` filters on
  `Warehouse IN ('[%0]', '[%1]', '[%2]', '[%3]')`. Four boxes captioned
  "Warehouse" are unusable, so duplicates are numbered.

Inference is a guess made once, at sync time, and stored. An admin can correct any
parameter; the correction sets `is_customised`, and no later sync overwrites it.

### 3. User input must never become SQL

Prompt values are never interpolated. `bind_prompts` replaces each `'[%N]'` —
quotes included — with a `?` marker and returns an ordered bind list, so a value
reaches HANA as a parameter and nothing else. A prompt used twice yields two
markers bound to the same value, so the caller still supplies one value per
position.

Prompts that SAP wrote quoted keep string semantics (a number is bound as
`'626080206'`, not `626080206`) so the comparison behaves exactly as it does inside
Query Manager — HANA casts it against the column, as it already did for SAP.

Separately, `assert_read_only` refuses anything that is not one read-only
statement: the leading keyword must be `SELECT`, `WITH` or `CALL`, there must be no
second statement, and no write keyword may appear outside a string literal or a
quoted identifier. The identifier exclusion matters — SAP reports really do select
`T0."UpdateDate" AS "Last Update"`, and a guard that read the word inside the
quotes as a statement would refuse good reports. The check runs at sync time *and*
again on the stored SQL immediately before execution.

### 4. These queries are big, and SAP is shared

Some reports scan years of `OINM` on a HANA box the whole business depends on.

- Every run is capped (5,000 rows on screen, 50,000 in an export, overridable per
  report). One row beyond the cap is fetched so the response can say honestly that
  the result was cut short instead of presenting a partial answer as complete.
- Report connections get a longer communication timeout than a normal API read,
  and still a finite one.
- Runs are never automatic. The frontend only auto-runs a report that takes no
  filters at all, where there is exactly one possible answer.
- Every run is recorded in `SapReportRun` — who, what filters, how many rows, how
  long, and the error if it failed. Results themselves are never stored: a report
  always shows what SAP holds now.

## The sync contract

`SapReport` has two halves, and the split is the whole design:

| SAP owns (overwritten every sync) | We own (never touched by a sync) |
|---|---|
| `sap_name`, `sap_category_*` | `slug`, `display_name`, `description` |
| `sql_text`, `sql_hash`, `statement_kind` | `is_enabled`, `sort_order`, `row_limit` |
| `is_runnable`, `not_runnable_reason` | parameter rows marked `is_customised` |

Other sync behaviour worth knowing:

- The **slug is stable**. It is generated once, from the name the query had when
  first seen, so renaming a query in SAP does not break saved links.
- Parameters are only re-inferred when the SQL actually changes (`sql_hash`).
- A query **deleted in SAP is flagged**, not deleted (`is_missing_in_sap`). Its run
  history is the only record of who pulled which numbers, and an accidental
  deletion in SAP is common enough that losing our side of it would be worse.
- Syncing one category never flags reports from another, so a Factory sync leaves
  the GST reports alone.
- A saved query that fails the read-only guard is still catalogued, with
  `is_runnable=False` and the reason, so an admin can see *why* it is not offered
  rather than wondering where it went.

## Local reports

The sync can only discover what lives in `OUQR`. Some reports the teams rely on
live elsewhere: the warehouse stock-audit sheet exists in SAP as a **Crystal
Report** ("Inventory Audit Report Manual", `RDOC` code RCRI0010) over a HANA
procedure, invisible to Query Manager — and the procedure's box/loose
arithmetic is wrong (`OnHand - OnHand/SalFactor2` is not a remainder, and it
reads *today's* `OITW."OnHand"` whatever dates were asked for).

Such reports are authored in `local_reports.py` instead and seeded with
`manage.py seed_local_sap_reports` (no SAP connection involved). A local report
is an ordinary `SapReport` row — same screen, prompts inferred from its SQL by
the same machinery, same exports, run audit and per-user access — with
`is_local=True`, which the sync honours in one place: `_flag_missing` never
marks a local report missing, because SAP not knowing it is its normal state.
Local reports carry the category **Factory App** (id −1) and internal keys at
`-900000` and below, far outside anything SAP assigns, so no `OUQR` row can
collide with them.

The seed honours the same ownership split as the sync: this codebase owns the
SQL (a re-seed refreshes it and re-infers prompts), while display names,
descriptions and customised parameter labels survive every re-seed. The SQL tab
shows the authored SQL exactly as it runs.

The one local report today is **Inventory Audit Report** — a corrected rebuild
of the Crystal original: opening balance per item/godown as on the From date,
every stock movement in the period (doc-type-prefixed document numbers), and
the box/loose split of finished-goods stock computed with `FLOOR`/`MOD` from
stock **as on the To date**. Item and Warehouse are real optional prompts,
which the Crystal report only faked client-side.

## Company scoping

A saved query lives in one company database and means nothing outside it, so every
report row is tied to a `Company` and every read is scoped to the `Company-Code`
header. `CompanyContext(company.code)` resolves the SAP connection, exactly as the
other SAP-backed modules do.

## Permissions

- `sap_reports.can_view_sap_reports` — list and run reports, export, see one
  report's own run history. What a viewer actually sees is further narrowed by
  per-user assignment (below).
- `sap_reports.can_manage_sap_reports` — sync from SAP, rename reports, correct
  filter labels, read the SQL, see unrunnable reports, read the company-wide audit
  feed, and assign report access.

## Per-user report access

`SapReportAccess` rows decide which reports a viewer sees — the same shape and
the same two rules as `warehouse.UserWarehouse` (see
`sap_reports/services/access.py`): **no assignment means no access**, so the
restriction cannot be bypassed by never configuring somebody; and
**administrators are exempt** (superusers and `can_manage_sap_reports`
holders), so the first deploy cannot lock out the people who do the assigning.
Every report endpoint resolves its report through the scoped queryset, so an
unassigned report 404s rather than leaking that it exists. Assignments are
managed on **Admin → SAP Report Access**, or via `/access/`.

## Verified against production SAP

At the time of writing, all 21 Factory reports in `JIVO_OIL` and all 3 in
`JIVO_BEVERAGES` were catalogued, and 18 of them (including the four that are
`CALL`s into HANA procedures) were executed successfully end-to-end against live
SAP. All 55 of their prompts were typed and labelled correctly by inference.
