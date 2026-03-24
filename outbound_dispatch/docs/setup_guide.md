# Outbound Dispatch - Setup & Deployment Guide

## Prerequisites

- Python 3.10+
- PostgreSQL database (already configured)
- SAP HANA connection (via hdbcli)
- SAP B1 Service Layer access

---

## Installation

The app is already added to `INSTALLED_APPS` in `config/settings.py` and URLs are registered in `config/urls.py`.

### 1. Run Migrations

```bash
python manage.py migrate outbound_dispatch
```

### 2. Set Up Permissions

Add the outbound permissions to appropriate Django groups. You can do this via the admin panel or a management command.

**Required Permissions:**

| Permission | Roles |
|------------|-------|
| `view_shipmentorder` | All dock roles |
| `can_sync_shipments` | Admin, System |
| `can_assign_dock_bay` | Dock Planner |
| `can_execute_pick_task` | Warehouse Team |
| `can_inspect_trailer` | Dock Team |
| `can_load_truck` | Dock Team |
| `can_confirm_load` | Dock Supervisor |
| `can_dispatch_shipment` | Dock Operator |
| `can_post_goods_issue` | Admin |
| `can_view_outbound_dashboard` | Logistics Manager |

### 3. Verify SAP Connectivity

The app uses the existing `sap_client` infrastructure. Verify HANA and Service Layer are accessible:

```bash
python manage.py shell -c "
from sap_client.client import SAPClient
client = SAPClient('JIVO_OIL')
orders = client.get_open_sales_orders()
print(f'Found {len(orders)} open Sales Orders')
"
```

---

## SAP Tables Used

### HANA Read (Sales Orders)
- `ORDR` — Sales Order headers
- `RDR1` — Sales Order line items

### Service Layer Write (Goods Issue)
- `POST /b1s/v2/InventoryGenExits` — Create Goods Issue document

---

## Environment Variables

No new environment variables required. The app uses existing SAP credentials from `.env`:

```
HANA_HOST=103.89.45.192
HANA_PORT=30015
HANA_USER=DSR
HANA_PASSWORD=****
SL_URL=https://103.89.45.192:50000
SL_USER=B1i
SL_PASSWORD=****
```

---

## App Structure

```
outbound_dispatch/
├── models/
│   ├── shipment_order.py         # ShipmentOrder, ShipmentStatus
│   ├── shipment_order_item.py    # ShipmentOrderItem, PickStatus
│   ├── pick_task.py              # PickTask, PickTaskStatus
│   ├── outbound_load_record.py   # OutboundLoadRecord, TrailerCondition
│   └── goods_issue_posting.py    # GoodsIssuePosting, GoodsIssueStatus
├── services/
│   ├── outbound_service.py       # Core business logic
│   └── sap_sync_service.py       # SAP Sales Order sync
├── docs/
│   ├── api_docs.md               # API documentation
│   ├── frontend_guide.md         # Frontend integration guide
│   └── setup_guide.md            # This file
├── views.py                      # API endpoints
├── serializers.py                # Request/response serializers
├── permissions.py                # Permission classes
├── urls.py                       # URL routing
├── admin.py                      # Django admin configuration
└── tests.py                      # Test suite
```

---

## Monitoring

### Key Logs

All outbound operations are logged under `outbound_dispatch.services`:

```python
# In settings.py LOGGING config:
'outbound_dispatch.services': {
    'level': 'INFO',
    'handlers': ['console', 'file'],
},
```

### Admin Panel

All models are registered in Django admin at `/admin/outbound_dispatch/`. Use the admin panel to:
- View/edit shipment orders
- Check Goods Issue posting status
- Monitor pick tasks
- Review load records
