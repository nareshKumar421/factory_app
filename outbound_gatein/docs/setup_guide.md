# Outbound Gate Entry — Setup & Deployment Guide

## 1. App Registration

The app is registered in `config/settings.py`:

```python
INSTALLED_APPS = [
    ...
    'outbound_gatein',
]
```

URL routing in `config/urls.py`:

```python
path("api/v1/outbound-gatein/", include("outbound_gatein.urls")),
```

## 2. Run Migrations

```bash
python manage.py makemigrations outbound_gatein
python manage.py makemigrations driver_management   # for OUTBOUND entry_type
python manage.py migrate
```

## 3. Seed Outbound Purposes

```bash
python manage.py shell -c "
from outbound_gatein.models import OutboundPurpose
purposes = [
    'Finished Goods Dispatch',
    'Sample Delivery',
    'Inter-Plant Transfer',
    'Customer Return Pickup',
    'Scrap / Waste Disposal',
]
for p in purposes:
    OutboundPurpose.objects.get_or_create(name=p)
print('Outbound purposes seeded.')
"
```

## 4. Assign Permissions

The following permissions are auto-created by Django:

**OutboundGateEntry:**
- `outbound_gatein.add_outboundgateentry`
- `outbound_gatein.view_outboundgateentry`
- `outbound_gatein.change_outboundgateentry`
- `outbound_gatein.delete_outboundgateentry`
- `outbound_gatein.can_complete_outbound_entry`
- `outbound_gatein.can_release_for_loading`

**OutboundPurpose:**
- `outbound_gatein.add_outboundpurpose`
- `outbound_gatein.view_outboundpurpose`
- `outbound_gatein.change_outboundpurpose`
- `outbound_gatein.delete_outboundpurpose`
- `outbound_gatein.can_manage_outbound_purpose`

**Gate Core Full View:**
- `gate_core.can_view_outbound_full_entry`

## 5. App Structure

```
outbound_gatein/
├── __init__.py
├── apps.py
├── models.py              # OutboundGateEntry, OutboundPurpose
├── serializers.py          # Entry + List serializers
├── views.py                # Create, Update, Complete, Release, List, Purposes
├── urls.py                 # 6 URL patterns
├── permissions.py          # 7 permission classes
├── admin.py                # Admin registration
├── services/
│   ├── __init__.py
│   └── outbound_gate_completion.py   # complete_outbound_gate_entry()
├── tests.py                # Model, Service, API tests
├── migrations/
│   └── __init__.py
└── docs/
    ├── api_docs.md
    ├── frontend_guide.md
    └── setup_guide.md
```

## 6. Integration with outbound_dispatch

The `outbound_gatein` app feeds into `outbound_dispatch` at the **Link Vehicle** step:

1. Gate officer creates VehicleEntry with `entry_type="OUTBOUND"`
2. Fills outbound details, confirms vehicle is empty
3. Security check is submitted, gate entry completed
4. Vehicle appears in `GET /api/v1/outbound-gatein/available-vehicles/`
5. Warehouse user links the vehicle to a shipment via `POST /api/v1/outbound/shipments/{id}/link-vehicle/`
6. `link_vehicle()` validates:
   - VehicleEntry.entry_type is `OUTBOUND`
   - `outbound_entry.vehicle_empty_confirmed` is `True`

## 7. VehicleEntry Changes

A new entry type `OUTBOUND` was added to `VehicleEntry.ENTRY_TYPE_CHOICES`:

```python
ENTRY_TYPE_CHOICES = (
    ("RAW_MATERIAL", "Raw Material"),
    ("DAILY_NEED", "Daily Need / Canteen"),
    ("MAINTENANCE", "Maintenance"),
    ("CONSTRUCTION", "Construction"),
    ("OUTBOUND", "Outbound Dispatch"),    # NEW
)
```

## 8. API Endpoints Summary

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/outbound-gatein/gate-entries/{id}/outbound/` | Create outbound entry |
| GET | `/api/v1/outbound-gatein/gate-entries/{id}/outbound/` | Read outbound entry |
| PUT | `/api/v1/outbound-gatein/gate-entries/{id}/outbound/update/` | Update outbound entry |
| POST | `/api/v1/outbound-gatein/gate-entries/{id}/complete/` | Complete & lock gate entry |
| POST | `/api/v1/outbound-gatein/gate-entries/{id}/release-for-loading/` | Release vehicle for loading |
| GET | `/api/v1/outbound-gatein/available-vehicles/` | List vehicles for link dropdown |
| GET | `/api/v1/outbound-gatein/purposes/` | List outbound purposes |
| GET | `/api/v1/gate-core/outbound-gate-entry/{id}/` | Full view (gate_core) |
