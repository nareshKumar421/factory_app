# Sampooran - Factory Management System

A comprehensive Django REST Framework-based manufacturing and logistics management system designed for multi-company factory operations. Integrates with SAP Business One for real-time data synchronization and handles end-to-end factory workflows — from raw material intake through production execution and quality control.

## Tech Stack

- **Backend:** Django 6.0.1, Django REST Framework 3.16.1
- **Database:** PostgreSQL
- **Cache:** Redis
- **ERP Integration:** SAP HANA + SAP Business One Service Layer
- **Authentication:** JWT (Simple JWT)
- **Notifications:** Firebase Cloud Messaging

## Project Structure

```
factory_app_v2/
├── config/                     # Django settings, root URLs, WSGI
├── accounts/                   # Custom user model (email-based), JWT auth
├── company/                    # Multi-company support, user-company roles
│
├── gate_core/                  # Base models & full-view APIs for gate entries
├── driver_management/          # Driver registration, VehicleEntry (core gate model)
├── vehicle_management/         # Vehicles & transporters
├── security_checks/            # Vehicle inspection, alcohol tests, seal validation
├── weighment/                  # Weighbridge integration (gross/tare weight)
├── raw_material_gatein/        # Purchase Order receipt processing
├── quality_control/            # Multi-parameter QC with approval workflow
├── daily_needs_gatein/         # Canteen/daily supplies gate entry
├── maintenance_gatein/         # Maintenance materials gate entry
├── construction_gatein/        # Construction materials gate entry
├── person_gatein/              # Employee entry/exit tracking
│
├── production_execution/       # Production runs, hourly logs, breakdowns, waste
├── production_planning/        # SAP production order planning
├── sap_plan_dashboard/         # SAP production plan dashboard & analytics
│
├── sap_client/                 # SAP HANA queries & Service Layer integration
├── grpo/                       # Goods Receipt PO — posts receipts to SAP
├── notifications/              # Firebase push notifications
└── docs/                       # Internal documentation
```

## Key Features

### Authentication & Multi-Company

- Email-based custom user model with JWT authentication
- Multi-company support with role-based access (Admin, QC, Store, etc.)
- `Company-Code` header required for all operations
- Rate limiting: 50 req/hr (anonymous), 500 req/hr (authenticated)

### Gate Management System

Handles all incoming/outgoing materials through a unified flow:

```
VehicleEntry → Security Check → Weighment → Module Entry → QC (if applicable)
```

**Entry types:** Raw Material, Daily Need, Maintenance, Construction, Person

**Status flow:** `DRAFT → IN_PROGRESS → QC_PENDING → QC_COMPLETED → COMPLETED`

### Quality Control

- Dynamic multi-parameter QC inspections per material type
- Multi-level approval: Security Guard → QA Chemist → QA Manager
- Final status: ACCEPTED / REJECTED / HOLD

### Production Execution

- Production runs linked to SAP production orders
- Hourly production logging (12 slots: 07:00–19:00)
- Machine breakdown & runtime tracking
- Line clearance checklists
- Waste management with 4-level approval (Engineer → AM → Store → HOD)
- Material usage and manpower tracking

### SAP Integration

- Real-time HANA queries for POs, vendors, warehouses
- GRPO posting via SAP Service Layer
- Production order sync from SAP OWOR
- Per-company SAP database mapping

### Notifications

- Firebase Cloud Messaging for real-time push notifications
- Event-driven notification system

## API Overview

**Base URL:** `/api/v1/`

| Module | Endpoint Prefix | Description |
|---|---|---|
| Auth | `/accounts/` | Login, token refresh, user management |
| Company | `/company/` | Company & role management |
| Drivers | `/driver-management/drivers/` | Driver CRUD |
| Vehicles | `/vehicle-management/vehicles/` | Vehicle & transporter CRUD |
| Security | `/security-checks/` | Vehicle security inspections |
| Weighment | `/weighment/` | Gross/tare weight recording |
| Raw Material | `/raw-material-gatein/` | PO receipt processing |
| Quality Control | `/quality-control/` | QC inspections & approvals |
| Gate Core | `/gate-core/` | Full-view gate entry APIs |
| Production | `/production-execution/` | Runs, logs, breakdowns, waste |
| SAP PO | `/po/` | Open POs, vendors, warehouses |
| Notifications | `/notifications/` | Push notifications |

**Auth header:** `Authorization: Bearer <jwt_token>`
**Company header:** `Company-Code: <company_code>`

## Setup

### Prerequisites

- Python 3.11+
- PostgreSQL
- Redis

### Installation

```bash
# Clone the repository
git clone <repository-url>
cd factory_app_v2

# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows

# Install dependencies
pip install -r requirement.txt

# Configure environment variables (see below)
cp .env.example .env

# Run migrations
python manage.py migrate

# Setup permission groups
python manage.py setup_production_groups

# Create superuser
python manage.py createsuperuser

# Run development server
python manage.py runserver
```

### Environment Variables

```env
# Django
SECRET_KEY=your-secret-key
DEBUG=True
ALLOWED_HOSTS=*

# PostgreSQL
DB_ENGINE=django.db.backends.postgresql
DB_NAME=factory_db
DB_USER=postgres
DB_PASSWORD=your-password
DB_HOST=localhost
DB_PORT=5432

# SAP HANA
HANA_HOST=your-hana-host
HANA_PORT=30015
HANA_USER=your-user
HANA_PASSWORD=your-password

# SAP Service Layer
SL_URL=https://your-sap-server:50000/b1s/v1
SL_USER=your-user
SL_PASSWORD=your-password

# Company SAP Databases
COMPANY_DB_JIVO_OIL=your-db
COMPANY_DB_JIVO_MART=your-db
COMPANY_DB_JIVO_BEVERAGES=your-db

# Redis
REDIS_URL=redis://127.0.0.1:6379/1

# Firebase
FCM_CREDENTIALS_PATH=path/to/firebase-credentials.json

# CORS
CORS_ALLOWED_ORIGINS=http://localhost:3000
```

## Testing

```bash
# Run production execution tests
python manage.py test production_execution -v2 --keepdb
```

## Companies

The system is configured for three companies under Jivo Wellness:

| Company | Code |
|---|---|
| Jivo Oil | JIVO_OIL |
| Jivo Mart | JIVO_MART |
| Jivo Beverages | JIVO_BEVERAGES |
