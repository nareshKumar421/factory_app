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
    ├── Execute stored procedure / SQL query
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
