# SAP Reports — API Reference

Base path: `/api/v1/sap-reports/`

Every endpoint requires:

- `Authorization: Bearer <jwt>`
- `Company-Code: <company code>` — reports belong to one SAP company database
- `sap_reports.can_view_sap_reports`

Endpoints marked **(manage)** additionally require `sap_reports.can_manage_sap_reports`.

---

## Catalogue

### `GET /reports/`

The company's reports.

| Query param | Meaning |
|---|---|
| `search` | matches the SAP name, the friendly name, or the description |
| `include_hidden` | **(manage)** also return switched-off, unrunnable and deleted-in-SAP reports so they can be fixed |

```json
{
  "data": [
    {
      "slug": "stock-transfer-report",
      "title": "Stock Transfers",
      "sap_name": "STOCK TRANSFER  REPORT",
      "display_name": "Stock Transfers",
      "description": "Transfers between warehouses, with litres and boxes.",
      "sap_category_name": "Factory",
      "statement_kind": "SELECT",
      "parameter_count": 4,
      "is_enabled": true,
      "is_runnable": true,
      "not_runnable_reason": "",
      "is_missing_in_sap": false,
      "sort_order": 0,
      "last_run_at": "2026-08-22T14:31:02+05:30",
      "last_synced_at": "2026-08-22T09:00:00+05:30"
    }
  ],
  "meta": {
    "company": "JIVO_OIL",
    "total": 21,
    "categories": ["Factory"],
    "can_manage": false
  }
}
```

`statement_kind` is `CALL` for a report that wraps a HANA procedure. It behaves
like any other report; it is surfaced because its columns are only known once it
has run.

### `GET /reports/<slug>/`

One report, plus the filters it asks for. Same fields as above, with:

```json
{
  "data": {
    "...": "...",
    "row_limit": null,
    "effective_row_limit": 5000,
    "sap_changed_at": "2026-07-28T00:00:00+05:30",
    "parameters": [
      {
        "position": 0,
        "label": "Warehouse",
        "kind": "WAREHOUSE",
        "is_required": true,
        "default_value": "",
        "help_text": "SAP field \"WhsCode\"",
        "has_lookup": true,
        "occurrences": 1,
        "is_customised": false
      },
      {
        "position": 2,
        "label": "From date",
        "kind": "DATE",
        "is_required": true,
        "default_value": "",
        "help_text": "SAP field \"DocDate\"",
        "has_lookup": false,
        "occurrences": 1,
        "is_customised": false
      }
    ]
  }
}
```

`position` is the `N` in SAP's `[%N]` placeholder, and it is the key values are
sent under. `kind` is one of `DATE`, `TEXT`, `NUMBER`, `ITEM`, `WAREHOUSE`,
`BUSINESS_PARTNER`, `ITEM_GROUP`, `PERIOD`. `has_lookup` says whether the options
endpoint can fill a picklist for it.

### `PATCH /reports/<slug>/` **(manage)**

Edit what this app owns. Anything SAP owns (its name, its SQL) is absent by
design — editing it here would be overwritten by the next sync.

```json
{
  "display_name": "Stock Transfers",
  "description": "Transfers between warehouses.",
  "is_enabled": true,
  "sort_order": 10,
  "row_limit": 20000,
  "parameters": [
    { "position": 0, "label": "From warehouse", "kind": "WAREHOUSE", "is_required": true }
  ]
}
```

Every parameter in `parameters` is marked `is_customised`, which protects it from
future syncs. Returns the updated report detail.

### `GET /reports/<slug>/sql/` **(manage)**

The saved query's SQL, for diagnosing a failing report.

```json
{
  "data": {
    "slug": "stock-transfer-report",
    "sap_name": "STOCK TRANSFER  REPORT",
    "sql_text": "SELECT ...",
    "sql_hash": "9f2c…",
    "statement_kind": "SELECT"
  }
}
```

---

## Running

### `POST /reports/<slug>/run/`

```json
{
  "parameters": { "0": "BH-FG", "1": "BH-BT", "2": "2026-07-01", "3": "2026-08-22" },
  "row_limit": 5000
}
```

Values are keyed by parameter `position`. Dates are accepted as `YYYY-MM-DD`
(also `YYYYMMDD`, `DD/MM/YYYY`, `DD-MM-YYYY`) and sent to SAP in its own format.
An optional parameter may be omitted or blank. `row_limit` is optional and capped
at 50,000.

```json
{
  "columns": [
    { "key": "DocNum", "label": "DocNum", "type": "number" },
    { "key": "DocDate", "label": "DocDate", "type": "date" },
    { "key": "ltrs", "label": "ltrs", "type": "number" }
  ],
  "rows": [
    [626080206, "2026-08-22", 1080.0]
  ],
  "meta": {
    "report": "stock-transfer-report",
    "title": "Stock Transfers",
    "company": "JIVO_OIL",
    "row_count": 1,
    "row_limit": 5000,
    "was_truncated": false,
    "duration_ms": 279,
    "executed_at": "2026-08-22T14:31:02.511+05:30",
    "parameters": [
      { "position": 0, "label": "From warehouse", "kind": "WAREHOUSE", "value": "BH-FG" }
    ]
  }
}
```

Notes for a consumer:

- **Columns are runtime data.** Do not hard-code them; a report's author can change
  them in SAP at any time. Rows are value arrays in `columns` order.
- **`key` is unique, `label` is what SAP said.** SAP reports do select the same
  alias twice, so a duplicate heading gets a `(2)` suffix in `key` only.
- **`was_truncated: true` means the answer is incomplete** — the row ceiling cut it
  short. Say so on screen; do not present it as a full result.
- `type` is `text` / `number` / `date`, for alignment and formatting.

### `POST /reports/<slug>/export/`

Same body plus `"export_format": "xlsx" | "csv"` (default `xlsx`). Returns the file
with a `Content-Disposition` filename; `Access-Control-Expose-Headers` is set so a
browser `fetch` can read it. Exports are allowed 50,000 rows.

The `.xlsx` carries a second **Filters** sheet recording the report, company, run
time, row count and the filter values — a report mailed on without its filters is a
number without a question.

### `GET /reports/<slug>/parameters/<position>/options/?search=BH`

Picklist for one filter, from the company's own master data (warehouses `OWHS`,
item groups `OITB`, fiscal periods `OFCT`, items `OITM`, business partners `OCRD`).
Capped at 100 rows, so pass `search` for the big ones.

```json
{
  "data": [{ "value": "BH-FG", "label": "Bhakharpur Finished Goods" }],
  "meta": { "kind": "WAREHOUSE", "label": "Warehouse" }
}
```

A free-text or numeric parameter returns `{"data": []}` — render a plain input.

---

## History

### `GET /reports/<slug>/runs/`

The last 50 runs of one report: who ran it, with which filters, how many rows, how
long, and the error if it failed.

### `GET /runs/` **(manage)**

The same feed for the whole company — the audit trail of who pulled which numbers.

---

## Administration

### `GET /categories/` **(manage)**

SAP's own query categories in this company database, so an admin can see what else
could be synced.

```json
{
  "data": [{ "category_id": 22, "category_name": "Factory", "query_count": 21 }],
  "meta": { "company": "JIVO_OIL", "default_category": "Factory" }
}
```

### `POST /sync/` **(manage)**

Mirror SAP's saved queries into the catalogue.

```json
{ "category": "Factory", "all_categories": false, "dry_run": false }
```

All fields optional; the default is the `Factory` category. `dry_run` reports what
would change without writing.

```json
{
  "data": {
    "company": "JIVO_OIL",
    "category": "Factory",
    "found_in_sap": 21,
    "created": ["EXP DATE"],
    "updated": [],
    "unchanged": ["FINISHED", "PENDING DISPATCH"],
    "not_runnable": [],
    "missing_in_sap": [],
    "dry_run": false
  }
}
```

The same job runs from the command line:

```bash
python manage.py sync_sap_reports
python manage.py sync_sap_reports --company JIVO_OIL --category Factory
python manage.py sync_sap_reports --all-categories --dry-run
```

---

## Errors

| Status | When | Body |
|---|---|---|
| `400` | a filter is missing or malformed, or the request body is invalid | `{"detail": "'From date' is required."}` |
| `403` | missing permission, or no `Company-Code` header | `{"detail": "…"}` |
| `404` | no such report (or parameter) for this company | — |
| `409` | the report exists but cannot be run — switched off, deleted in SAP, or not read-only | `{"detail": "This report is switched off."}` |
| `502` | SAP rejected the query | `{"detail": "SAP rejected this report: invalid column name…"}` |
| `503` | SAP unreachable | `{"detail": "SAP system is currently unavailable…"}` |

A `502` is worth surfacing verbatim. These queries are authored in SAP by people
outside this app, so a broken one is a routine, fixable event — and SAP's own
complaint is what tells the person who can fix it where to look.

---

## Setup

1. Migrate: `python manage.py migrate sap_reports`.
2. Grant `sap_reports.can_view_sap_reports` to the people who read reports, and
   `sap_reports.can_manage_sap_reports` to whoever looks after the catalogue.
   The frontend sidebar gates on the `sap_reports` permission prefix, so a user
   with neither never sees the module.
3. Run the first sync — `python manage.py sync_sap_reports`, or the **Sync from
   SAP** button on the reports page.
4. Optionally give each report a friendly name and a description. The SAP names
   (`WAREHOUSE WISE DETAIL BY MANVI JI`, `DOLLY MAM BST`) are how the SAP authors
   know them, not how a new user will.
