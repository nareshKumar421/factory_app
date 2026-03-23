# Sampooran — Project Structure

Complete directory structure of the Sampooran factory management system with all existing apps and the planned Utilities Mobile integration apps.

---

## Root Directory

```
factory_app_v2/
├── manage.py                       # Django management script
├── requirement.txt                 # Python dependencies
├── README.md                       # Project overview
├── .env                            # Environment variables (secret — not committed)
├── .env.local                      # Local environment overrides
├── .gitignore                      # Git ignore rules
├── test_rate_limit.py              # Rate limit test script
│
├── config/                         # Django project configuration
├── docs/                           # Documentation
├── media/                          # User-uploaded files (runtime)
│
├── ── Core ──────────────────────────────────────────────────────
├── accounts/                       # Custom user model, JWT auth
├── company/                        # Multi-company, roles, permissions
├── gate_core/                      # Base models, enums, shared gate logic
├── notifications/                  # FCM push + WhatsApp notifications
├── sap_client/                     # SAP HANA & Service Layer integration
│
├── ── Gate Management ───────────────────────────────────────────
├── driver_management/              # Drivers, VehicleEntry (core gate model)
├── vehicle_management/             # Vehicles, transporters
├── security_checks/                # Vehicle inspection, alcohol test
├── weighment/                      # Gross/tare/net weight
├── raw_material_gatein/            # Purchase order receipts
├── quality_control/                # Multi-parameter QC, approval workflow
├── daily_needs_gatein/             # Canteen/daily supplies
├── maintenance_gatein/             # Maintenance materials
├── construction_gatein/            # Construction materials
├── person_gatein/                  # Employee entry/exit tracking
├── grpo/                           # Goods Receipt PO (SAP posting)
│
├── ── Production ────────────────────────────────────────────────
├── production_execution/           # Runs, hourly logs, breakdowns, waste
├── production_planning/            # SAP production order planning (scaffold)
├── sap_plan_dashboard/             # SAP production plan dashboard
│
├── ── Utilities Mobile (NEW) ────────────────────────────────────
├── docking/                        # Truck docking, invoice verification
├── dynamic_forms/                  # Database-driven forms, approval workflow
├── material_tracking/              # Outward movement, returns, gate passes
└── reporting/                      # SQL-based reports, scheduled email
```

---

## Config

```
config/
├── __init__.py
├── settings.py                     # All configuration (DB, JWT, CORS, media, etc.)
├── urls.py                         # Root URL routing → all app URLs
├── admin.py                        # Admin site customization (Sampooran branding)
├── view.py                         # Root view (health check)
├── asgi.py                         # ASGI entry point
└── wsgi.py                         # WSGI entry point
```

---

## Core Apps

### accounts — Authentication & Users

```
accounts/
├── __init__.py
├── apps.py
├── admin.py                        # User admin with search, filters
├── managers.py                     # Custom UserManager (email-based)
├── models.py                       # User (email, full_name, employee_code), Department
├── serializers.py                  # Login, user, department serializers
├── views.py                        # LoginAPI, TokenRefreshAPI, MeAPI, ChangePasswordAPI
├── urls.py                         # /api/v1/accounts/
├── tests.py
└── migrations/
```

### company — Multi-Company & Roles

```
company/
├── __init__.py
├── apps.py
├── admin.py
├── models.py                       # Company, UserRole, UserCompany
├── permissions.py                  # HasCompanyContext (extracts Company-Code header)
├── serializers.py
├── views.py                        # CompanyListAPI, UserCompanyAPI
├── urls.py                         # /api/v1/company/
├── tests.py
└── migrations/
```

### gate_core — Shared Gate Infrastructure

```
gate_core/
├── __init__.py
├── apps.py
├── admin.py
├── enums.py                        # GateEntryStatus, EntryType enums
├── permissions.py                  # Gate-specific permissions
├── serializers.py                  # Full-view gate entry serializers
├── views.py                        # Combined gate entry list/detail APIs
├── urls.py                         # /api/v1/gate-core/
├── tests.py
├── models/
│   ├── __init__.py
│   ├── base.py                     # BaseModel (created_at, updated_at, created_by, updated_by, is_active)
│   ├── gate_entry.py               # GateEntryBase abstract model
│   ├── gate_attachments.py         # GateAttachment model
│   └── unit_choice.py              # UnitChoice model
├── services/
│   ├── __init__.py
│   ├── lock_manager.py             # Entry lock/unlock logic
│   └── status_guard.py             # Status transition validation
└── migrations/
```

### notifications — Push & WhatsApp

```
notifications/
├── __init__.py
├── apps.py
├── admin.py
├── models.py                       # UserDevice, Notification, NotificationType
│                                   #   + WhatsAppTemplate, WhatsAppLog (NEW)
├── permissions.py
├── serializers.py
├── services.py                     # NotificationService (FCM push)
│                                   #   send_notification_to_user()
│                                   #   send_notification_to_group()
│                                   #   send_bulk_notification()
│                                   #   send_notification_by_permission()
│                                   #   send_notification_by_auth_group()
├── views.py                        # DeviceRegisterAPI, NotificationListAPI, MarkReadAPI
├── urls.py                         # /api/v1/notifications/
├── tests.py
├── management/
│   └── commands/
│       ├── cleanup_stale_fcm_tokens.py
│       └── send_whatsapp_reminders.py  # (NEW) Cron: every 2hrs
├── services/                           # (NEW) subfolder
│   └── whatsapp_service.py             # WhatsAppService (AiSensy API)
└── migrations/
```

### sap_client — SAP HANA & Service Layer

```
sap_client/
├── __init__.py
├── apps.py
├── admin.py
├── client.py                       # Main SAP client interface
├── context.py                      # Company → SAP DB mapping
├── dtos.py                         # PO, POItem, Vendor, Warehouse DTOs
├── exceptions.py                   # SAP-specific exceptions
├── models.py
├── registry.py                     # SAP connection registry
├── serializers.py
├── views.py                        # OpenPOsAPI, VendorsAPI, WarehousesAPI
├── urls.py                         # /api/v1/po/
├── tests.py
├── hana/
│   ├── __init__.py
│   ├── connection.py               # HANA database connection manager
│   ├── po_reader.py                # Purchase order queries
│   ├── vendor_reader.py            # Vendor master queries
│   └── warehouse_reader.py         # Warehouse master queries
├── service_layer/
│   ├── __init__.py
│   ├── auth.py                     # Service Layer authentication
│   ├── grpo_writer.py              # GRPO posting to SAP
│   ├── attachment_writer.py        # Attachment posting to SAP
│   └── production_order_writer.py  # Production order posting
└── migrations/
```

---

## Gate Management Apps

### driver_management — Drivers & Gate Entries

```
driver_management/
├── __init__.py
├── apps.py
├── admin.py
├── serializers.py
├── views.py                        # DriverListCreateAPI, VehicleEntryListCreateAPI
├── urls.py                         # /api/v1/driver-management/
├── tests.py
├── models/
│   ├── __init__.py
│   ├── driver.py                   # Driver (name, license, contact)
│   └── vehicle_entry.py            # VehicleEntry (core gate model — all entries start here)
└── migrations/
```

### vehicle_management — Vehicles & Transporters

```
vehicle_management/
├── __init__.py
├── apps.py
├── admin.py
├── serializers.py
├── views.py
├── urls.py                         # /api/v1/vehicle-management/
├── tests.py
├── models/
│   ├── __init__.py
│   ├── vehicle.py                  # Vehicle (number, type)
│   └── transporter.py              # Transporter (name, contact)
└── migrations/
```

### security_checks — Vehicle Inspection

```
security_checks/
├── __init__.py
├── apps.py
├── admin.py
├── serializers.py
├── views.py
├── urls.py                         # /api/v1/security-checks/
├── tests.py
├── models/
│   ├── __init__.py
│   └── security_check.py           # SecurityCheck (alcohol_test, vehicle_condition, seals)
└── migrations/
```

### weighment — Weighbridge

```
weighment/
├── __init__.py
├── apps.py
├── admin.py
├── serializers.py
├── views.py
├── urls.py                         # /api/v1/weighment/
├── tests.py
├── models/
│   ├── __init__.py
│   └── weighment.py                # Weighment (gross_weight, tare_weight, net_weight)
├── services/
│   ├── __init__.py
│   └── calculator.py               # Net weight calculation
└── migrations/
```

### raw_material_gatein — Purchase Order Receipts

```
raw_material_gatein/
├── __init__.py
├── apps.py
├── admin.py
├── permissions.py
├── serializers.py
├── signals.py                      # Auto-trigger on PO receipt events
├── views.py
├── urls.py                         # /api/v1/raw-material-gatein/
├── tests.py
├── models/
│   ├── __init__.py
│   ├── po_receipt.py               # POReceipt (linked to VehicleEntry + SAP PO)
│   └── po_item_receipt.py          # POItemReceipt (line items with quantities)
├── services/
│   ├── __init__.py
│   ├── gate_completion.py          # Gate entry completion rules
│   └── validations.py              # PO validation logic
└── migrations/
```

### quality_control — QC Inspection & Approval

```
quality_control/
├── __init__.py
├── apps.py
├── admin.py
├── enums.py                        # InspectionStatus, FinalStatus enums
├── permissions.py
├── serializers.py
├── signals.py                      # Auto-trigger on inspection events
├── views.py                        # Raw material QC views
├── views_production_qc.py          # Production QC views
├── urls.py                         # /api/v1/quality-control/
├── tests.py
├── models/
│   ├── __init__.py
│   ├── material_type.py            # MaterialType classification
│   ├── qc_parameter_master.py      # QCParameterMaster (per material)
│   ├── material_arrival_slip.py    # MaterialArrivalSlip (DRAFT → SUBMITTED)
│   ├── raw_material_inspection.py  # RawMaterialInspection (multi-level approval)
│   ├── inspection_parameter_result.py  # Individual test results
│   ├── arrival_slip_attachment.py  # Attachments for arrival slips
│   ├── production_qc_session.py    # Production QC sessions
│   └── production_qc_result.py     # Production QC results
├── services/
│   └── rules.py                    # QC business rules
└── migrations/
```

### daily_needs_gatein / maintenance_gatein / construction_gatein / person_gatein

All gate entry type apps follow the same pattern:

```
{app_name}/
├── __init__.py
├── apps.py
├── admin.py
├── models.py                       # {Type}GateEntry model
├── permissions.py                  # App-specific permissions
├── serializers.py
├── views.py
├── urls.py                         # /api/v1/{app-name}/
├── tests.py
├── services/
│   ├── __init__.py
│   └── {type}_completion.py        # Completion validation rules
└── migrations/
```

### grpo — Goods Receipt PO (SAP Posting)

```
grpo/
├── __init__.py
├── apps.py
├── admin.py
├── models.py                       # GRPOPosting, GRPOLinePosting, GRPOAttachment
├── permissions.py
├── serializers.py
├── services.py                     # GRPO posting logic → SAP
├── views.py
├── urls.py                         # /api/v1/grpo/
├── tests.py
└── migrations/
```

---

## Production Apps

### production_execution — Factory Floor Operations

```
production_execution/
├── __init__.py
├── apps.py
├── admin.py
├── models.py                       # 23 models:
│                                   #   ProductionLine, Machine, ProductionRun,
│                                   #   ProductionSegment, ProductionLog,
│                                   #   MachineBreakdown, BreakdownCategory,
│                                   #   ProductionMaterialUsage, MachineRuntime,
│                                   #   ProductionManpower, LineClearance,
│                                   #   MachineChecklistTemplate, MachineChecklistEntry,
│                                   #   WasteLog, WasteCategory,
│                                   #   ResourceElectricity, ResourceWater,
│                                   #   ResourceGas, ResourceCompressedAir,
│                                   #   ResourceLabour, MachineCost, OverheadCost,
│                                   #   ProductionRunCost
├── permissions.py
├── serializers.py                  # Read + Write serializer pairs for all models
├── views.py                        # APIView classes for all endpoints
├── urls.py                         # /api/v1/production-execution/
├── tests.py                        # 48 test cases
├── management/
│   └── commands/
│       └── setup_production_groups.py  # Creates auth groups + permissions
├── services/
│   ├── __init__.py
│   ├── production_service.py       # Production run business logic
│   ├── cost_calculator.py          # Per-unit cost calculation
│   ├── sap_reader.py               # Read SAP production orders
│   └── sap_writer.py               # Write production data to SAP
└── migrations/
```

### production_planning — SAP Order Planning (Scaffold)

```
production_planning/
├── __init__.py
└── migrations/
    └── __init__.py
```

### sap_plan_dashboard — Production Plan Analytics

```
sap_plan_dashboard/
├── __init__.py
├── apps.py
├── hana_reader.py                  # HANA queries for production orders
├── models.py
├── permissions.py
├── serializers.py
├── services.py                     # Dashboard aggregation logic
├── views.py
├── urls.py                         # /api/v1/sap-plan-dashboard/
└── tests.py
```

---

## Utilities Mobile Apps (NEW — To Be Created)

### docking — Truck Docking & Invoice Verification

```
docking/
├── __init__.py
├── apps.py
├── admin.py                        # DockingEntryAdmin with inlines
├── models.py                       # DockingEntry, DockingInvoice, DockingPhoto
├── serializers.py                  # Read: List + Detail, Write: Create serializers
├── views.py                        # APIView: ListCreate, Detail, DockIn, Complete,
│                                   #   InvoiceListCreate, InvoiceVerify,
│                                   #   PhotoListCreate, SAPInvoiceLookup
├── urls.py                         # /api/v1/docking/
├── services.py                     # DockingService: number generation, completion rules
├── permissions.py                  # can_view_docking, can_manage_docking, can_verify_invoice
└── migrations/
```

### dynamic_forms — Database-Driven Forms & Approvals

```
dynamic_forms/
├── __init__.py
├── apps.py
├── admin.py                        # FormAdmin with field inlines
├── models.py                       # Form, FormField, FormPermission,
│                                   #   FormSubmission, FieldResponse
├── serializers.py                  # Read: FormList, FormDetail, SubmissionList,
│                                   #   SubmissionDetail, Write: Create, Submit, Approve
├── views.py                        # APIView: FormListCreate, FormDetail,
│                                   #   FieldListCreate, FormSubmit,
│                                   #   SubmissionList, SubmissionDetail,
│                                   #   SubmissionApprove, SubmissionReject,
│                                   #   PendingApprovals
├── urls.py                         # /api/v1/dynamic-forms/
├── services.py                     # FormSubmissionService: create, approve, reject
│                                   #   (triggers FCM + WhatsApp notifications)
├── permissions.py                  # can_manage_forms, can_submit_forms, can_approve_submissions
└── migrations/
```

### material_tracking — Outward Movement, Returns & Gate Passes

```
material_tracking/
├── __init__.py
├── apps.py
├── admin.py                        # MovementAdmin, GatePassAdmin with item inlines
├── models.py                       # MaterialMovement, MovementItem,
│                                   #   GatePass, GatePassItem
├── serializers.py                  # Read + Write serializers for movements and gate passes
├── views.py                        # APIView: MovementListCreate, MovementDetail,
│                                   #   MovementDispatch, MovementReceive, MovementClose,
│                                   #   OverdueMovements, GatePassListCreate,
│                                   #   GatePassDetail, GatePassApprove, GatePassReject,
│                                   #   GatePassPrint
├── urls.py                         # /api/v1/material-tracking/
├── services.py                     # MovementService: number gen, receive logic, overdue
├── permissions.py                  # can_view_movements, can_manage_movements,
│                                   #   can_receive_materials, can_manage_gate_passes,
│                                   #   can_approve_gate_passes
├── management/
│   └── commands/
│       └── check_overdue_movements.py  # Cron: daily at 8 AM
└── migrations/
```

### reporting — SQL Reports & Scheduled Email

```
reporting/
├── __init__.py
├── apps.py
├── admin.py                        # ReportDefinitionAdmin, ScheduledReportAdmin
├── models.py                       # ReportDefinition, ScheduledReport, ReportExecutionLog
├── serializers.py
├── views.py                        # APIView: ReportList, ReportDetail,
│                                   #   ReportExecute, ReportExport,
│                                   #   ScheduleListCreate, ScheduleDetail
├── urls.py                         # /api/v1/reporting/
├── services.py                     # ReportService: execute (parameterized query),
│                                   #   export_to_excel (openpyxl), calculate_next_run
├── permissions.py                  # can_view_reports, can_manage_reports, can_manage_schedules
├── management/
│   └── commands/
│       └── run_scheduled_reports.py    # Cron: hourly
└── migrations/
```

---

## Shared Utilities (NEW)

```
core/
└── utils/
    └── image_utils.py              # decode_base64_image() — base64 → ContentFile
```

---

## Media Directory (Runtime)

```
media/
├── gate_core/
│   └── attachments/YYYY/MM/DD/     # Existing gate attachments
├── docking/                         # NEW
│   └── photos/YYYY/MM/DD/          # Docking operation photos
├── forms/                           # NEW
│   ├── responses/YYYY/MM/DD/       # Form image responses
│   └── files/YYYY/MM/DD/           # Form file uploads
└── grpo/
    └── attachments/                 # GRPO attachments
```

---

## Documentation

```
docs/
├── ── Existing ──────────────────────────────────────────
├── permissions_and_groups.md           # Permission matrix for all apps
├── production_execution_flow.md        # Production module flow diagrams
├── production_execution_frontend_guide.md  # Frontend integration guide
├── sap_plan_flow.md                    # SAP planning dashboard flow
├── GRPO_ATTACHMENT_ERROR_ANALYSIS.md   # GRPO debugging notes
├── GRPO_ATTACHMENT_FIX_CHANGELOG.md    # GRPO fix changelog
├── production_execution/               # Detailed PE docs
│   ├── README.md
│   ├── 01_models.md
│   ├── 02_api_endpoints.md
│   ├── 03_permissions.md
│   ├── 04_business_rules.md
│   ├── 05_implementation_phases.md
│   └── 06_validation_checks.md
│
├── ── Utilities Mobile (NEW) ────────────────────────────
├── utilities_mobile.md                 # PHP app documentation (source reference)
├── utilities_mobile_integration.md     # Django integration blueprint (models, views, URLs)
├── utilities_mobile_flow.md            # All module flow diagrams
├── utilities_mobile_data_migration.md  # PHP → Django data migration guide
├── utilities_mobile_api_contract.md    # API reference for frontend team
└── project_structure.md                # This file
```

---

## URL Map

| Prefix | App | Description |
|---|---|---|
| `/api/v1/accounts/` | accounts | Auth, users, departments |
| `/api/v1/company/` | company | Companies, roles, user-company |
| `/api/v1/notifications/` | notifications | FCM devices, notifications |
| `/api/v1/gate-core/` | gate_core | Full-view gate entry APIs |
| `/api/v1/driver-management/` | driver_management | Drivers, vehicle entries |
| `/api/v1/vehicle-management/` | vehicle_management | Vehicles, transporters |
| `/api/v1/security-checks/` | security_checks | Vehicle inspections |
| `/api/v1/weighment/` | weighment | Weight recording |
| `/api/v1/raw-material-gatein/` | raw_material_gatein | PO receipts |
| `/api/v1/quality-control/` | quality_control | QC inspections |
| `/api/v1/daily-needs-gatein/` | daily_needs_gatein | Daily supplies |
| `/api/v1/maintenance-gatein/` | maintenance_gatein | Maintenance materials |
| `/api/v1/construction-gatein/` | construction_gatein | Construction materials |
| `/api/v1/person-gatein/` | person_gatein | Employee entry/exit |
| `/api/v1/grpo/` | grpo | Goods receipt posting |
| `/api/v1/production-execution/` | production_execution | Production runs, logs |
| `/api/v1/sap-plan-dashboard/` | sap_plan_dashboard | SAP plan analytics |
| `/api/v1/po/` | sap_client | SAP POs, vendors, warehouses |
| `/api/v1/docking/` | docking | **NEW** — Truck docking |
| `/api/v1/dynamic-forms/` | dynamic_forms | **NEW** — Forms & approvals |
| `/api/v1/material-tracking/` | material_tracking | **NEW** — Material movement |
| `/api/v1/reporting/` | reporting | **NEW** — Reports & schedules |

---

## App Pattern Reference

Every app in this project follows these conventions:

| File | Purpose | Pattern |
|---|---|---|
| `models.py` | Data models | Inherit from `gate_core.models.base.BaseModel` |
| `serializers.py` | Read/write serializers | `ModelSerializer` (read) + `Serializer` (write) |
| `views.py` | API endpoints | `APIView` with `[IsAuthenticated, HasCompanyContext]` |
| `urls.py` | URL routing | `path()` with `.as_view()`, no DRF routers |
| `services.py` | Business logic | Static/classmethod service classes |
| `permissions.py` | Custom permissions | Defined in model `Meta.permissions` |
| `admin.py` | Django admin | `ModelAdmin` with list_display, filters, inlines |
| `management/commands/` | Scheduled tasks | Django management commands run via cron |

### Naming Convention

| Type | Pattern | Example |
|---|---|---|
| List + Create view | `{Entity}ListCreateAPI` | `DockingEntryListCreateAPI` |
| Detail view | `{Entity}DetailAPI` | `DockingEntryDetailAPI` |
| Action view | `{Action}API` | `DockInAPI`, `DockOutCompleteAPI` |
| Read serializer (list) | `{Entity}ListSerializer` | `DockingEntryListSerializer` |
| Read serializer (detail) | `{Entity}DetailSerializer` | `DockingEntryDetailSerializer` |
| Write serializer | `{Entity}CreateSerializer` | `DockingEntryCreateSerializer` |
| URL name | `{app}-{entity}-{action}` | `docking-entry-list-create` |
| Service class | `{Entity}Service` | `DockingService` |
| Management command | `{verb}_{noun}` | `check_overdue_movements` |

---

## Statistics

| Metric | Existing | New (Utilities Mobile) | Total |
|---|---|---|---|
| Django Apps | 20 | 4 | 24 |
| Models | ~50 | 16 | ~66 |
| API Endpoints | ~60 | 39 | ~99 |
| Management Commands | 2 | 4 | 6 |
| Auth Groups | ~10 | 9 | ~19 |
| Custom Permissions | ~30 | 14 | ~44 |
