# Non-Moving Raw Material Dashboard — Architecture

## App Structure

```
non_moving_rm/
├── __init__.py
├── apps.py                 # Django app config
├── models.py               # Permission model (no DB table)
├── permissions.py          # DRF permission classes
├── serializers.py          # Input validation & response shape
├── views.py                # API endpoints
├── services.py             # Business logic & aggregation
├── hana_reader.py          # SAP HANA query execution
├── urls.py                 # URL routing
├── tests.py                # Unit & integration tests
├── management/commands/
│   └── check_non_moving_report.py   # Print the report live from SAP
└── docs/
    ├── FRONTEND_GUIDE.md   # Frontend integration guide
    ├── API_REFERENCE.md    # API endpoint reference
    └── ARCHITECTURE.md     # This file
```

## Request Flow

```
HTTP Request
    │
    ▼
views.py (NonMovingRMReportAPI / ItemGroupDropdownAPI)
    ├── Validate query params (serializers.py)
    ├── Check auth & permissions (permissions.py)
    ├── Extract company code from request context
    │
    ▼
services.py (NonMovingRMService)
    ├── Create CompanyContext (from sap_client)
    ├── Call HanaNonMovingRMReader
    ├── Aggregate results (branch summary, totals)
    │
    ▼
hana_reader.py (HanaNonMovingRMReader)
    ├── Connect to SAP HANA via HanaConnection
    ├── Execute company-scoped SAP B1 table query
    ├── Map raw rows to dicts
    ├── Close connection
    │
    ▼
Response serialized and returned
```

## Layer Responsibilities

| Layer          | File            | Responsibility                                       |
|----------------|-----------------|------------------------------------------------------|
| **View**       | views.py        | HTTP handling, auth, input validation, error mapping  |
| **Service**    | services.py     | Business logic, aggregation, response shaping         |
| **Reader**     | hana_reader.py  | SAP HANA connection, SQL execution, row mapping       |
| **Serializer** | serializers.py  | Input validation, response structure definition       |
| **Permission** | permissions.py  | Access control via Django permission system           |

## Where the numbers come from

The report is computed here, by one query per company schema at `(item, warehouse)`
grain — `OITW` for stock, `OITM`/`OITB` for the item, `OWHS` for the warehouse and
`OINM` for movement. SAP's `REPORT_BP_NON_MOVING_RM` procedure is no longer called:
it lived in the Beverages schema, answered for all three companies at once, carried
no warehouse (so the service had to pro-rate quantities across warehouses to guess
one), and eventually stopped answering, which the dashboard surfaced as a 502
"SAP data error". API_REFERENCE.md lists the source of every field.

Because rows now carry a real warehouse, `warehouse_summary` is a plain roll-up of
the same rows rather than an estimate, and `services.py` does no second HANA read.

## Dependencies

- `sap_client.hana.connection.HanaConnection` — HANA connection management
- `sap_client.context.CompanyContext` — Multi-company configuration
- `sap_client.exceptions` — Custom SAP error classes
- `company.permissions.HasCompanyContext` — Company header enforcement
- `hdbcli` — SAP HANA Python driver

## Error Handling

| Exception            | HTTP Status | Meaning                          |
|---------------------|-------------|----------------------------------|
| `SAPConnectionError` | 503         | Cannot connect to SAP HANA       |
| `SAPDataError`       | 502         | Query execution failed            |
| Validation errors    | 400         | Invalid query parameters          |
| Auth errors          | 401/403     | Missing token or permissions      |

## Configuration

All SAP HANA credentials are read from `.env` via Django settings:

```
HANA_HOST=103.89.45.192
HANA_PORT=30015
HANA_USER=DSR
HANA_PASSWORD=***
```

Company-specific schemas are mapped in `sap_client/registry.py`.
