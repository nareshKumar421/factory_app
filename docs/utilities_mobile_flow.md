# Utilities Mobile — Application Flow Document

Complete flow documentation for all modules being integrated into Sampooran.

---

## Table of Contents

- [1. System Architecture Flow](#1-system-architecture-flow)
- [2. Authentication Flow](#2-authentication-flow)
- [3. Docking Flow](#3-docking-flow)
- [4. Dynamic Forms & Approval Flow](#4-dynamic-forms--approval-flow)
- [5. Material Tracking Flow](#5-material-tracking-flow)
- [6. Gate Pass Flow](#6-gate-pass-flow)
- [7. Reporting Flow](#7-reporting-flow)
- [8. Notification Flow (FCM + WhatsApp)](#8-notification-flow-fcm--whatsapp)
- [9. File Upload Flow](#9-file-upload-flow)
- [10. Scheduled Tasks Flow](#10-scheduled-tasks-flow)
- [11. Cross-Module Interactions](#11-cross-module-interactions)
- [12. Error Handling & Edge Cases](#12-error-handling--edge-cases)
- [13. Status Reference](#13-status-reference)

---

## 1. System Architecture Flow

```
┌──────────────────────────────────────────────────────────────┐
│                      Mobile / Web Client                      │
│          (PWA / Browser / Android / iOS)                      │
└──────────────────────┬───────────────────────────────────────┘
                       │  HTTPS + JWT Token
                       │  Header: Authorization: Bearer <token>
                       │  Header: Company-Code: JIVO_OIL
                       ▼
┌──────────────────────────────────────────────────────────────┐
│                     Django REST Framework                      │
│                                                               │
│  ┌─────────┐ ┌──────────┐ ┌───────────┐ ┌─────────────────┐ │
│  │ accounts│ │ docking  │ │ dynamic   │ │ material        │ │
│  │ (auth)  │ │          │ │ _forms    │ │ _tracking       │ │
│  └────┬────┘ └────┬─────┘ └─────┬─────┘ └───────┬─────────┘ │
│       │           │             │               │            │
│  ┌────┴───────────┴─────────────┴───────────────┴─────────┐  │
│  │              Shared Services Layer                       │  │
│  │  NotificationService │ WhatsAppService │ SAPClient      │  │
│  └──────────┬──────────────────┬─────────────────┬─────────┘  │
└─────────────┼──────────────────┼─────────────────┼────────────┘
              │                  │                 │
     ┌────────┴───┐    ┌────────┴───┐    ┌────────┴───┐
     │ PostgreSQL │    │  Firebase   │    │  SAP HANA  │
     │ (primary)  │    │  (FCM)     │    │  (ERP)     │
     └────────────┘    └────────────┘    └────────────┘
              │                │
         ┌────┴─────┐   ┌─────┴──────┐
         │  Redis   │   │  AiSensy   │
         │ (cache)  │   │ (WhatsApp) │
         └──────────┘   └────────────┘
```

### Request Lifecycle (Every API Call)

```
Client Request
    │
    ▼
[1] Django Middleware
    ├── CORS validation (django-cors-headers)
    ├── Security middleware (CSRF, XFrame, etc.)
    └── Rate limiting (50/hr anon, 500/hr auth)
    │
    ▼
[2] JWT Authentication (Simple JWT)
    ├── Extract Bearer token from Authorization header
    ├── Validate token expiry
    ├── Check blacklist (Redis-backed)
    └── Attach request.user = User instance
    │
    ▼
[3] Permission: IsAuthenticated
    └── Reject 401 if no valid user
    │
    ▼
[4] Permission: HasCompanyContext
    ├── Extract Company-Code from request header
    ├── Validate UserCompany exists (user + company)
    ├── Attach request.company = Company instance
    └── Reject 403 if user doesn't belong to company
    │
    ▼
[5] APIView.get() / .post() / .patch()
    ├── Filter querysets by company=request.company
    ├── Validate with write serializer (Serializer)
    ├── Execute business logic (services.py)
    ├── Set created_by / updated_by = request.user
    ├── Trigger notifications (if applicable)
    └── Return Response with read serializer (ModelSerializer)
    │
    ▼
[6] JSON Response → Client
```

---

## 2. Authentication Flow

### Login

```
┌────────┐         ┌──────────────┐         ┌────────────┐
│ Client │         │ accounts app │         │ PostgreSQL │
└───┬────┘         └──────┬───────┘         └─────┬──────┘
    │                     │                       │
    │  POST /api/v1/accounts/login/               │
    │  {email, password}  │                       │
    │────────────────────►│                       │
    │                     │  Query User by email  │
    │                     │──────────────────────►│
    │                     │  User record          │
    │                     │◄──────────────────────│
    │                     │                       │
    │                     │  check_password()     │
    │                     │  (bcrypt verify)      │
    │                     │                       │
    │                     │  Generate JWT pair:   │
    │                     │  access (1500 min)    │
    │                     │  refresh (7 days)     │
    │                     │                       │
    │  {access, refresh,  │                       │
    │   user: {id, email, │                       │
    │   full_name,        │                       │
    │   companies: [...]}}│                       │
    │◄────────────────────│                       │
    │                     │                       │
    │  Store tokens in    │                       │
    │  localStorage/      │                       │
    │  SecureStorage      │                       │
```

### Token Refresh

```
Client                          accounts
  │                                │
  │  POST /api/v1/accounts/token/refresh/
  │  {refresh: "<token>"}          │
  │───────────────────────────────►│
  │                                │  Validate refresh token
  │                                │  Check blacklist (Redis)
  │                                │  Rotate: old refresh → blacklisted
  │                                │  Generate new access + refresh
  │  {access, refresh}             │
  │◄───────────────────────────────│
```

### Protected Request Pattern

```
Client                    HasCompanyContext           APIView
  │                          │                          │
  │  GET /api/v1/docking/entries/                       │
  │  Authorization: Bearer <jwt>                        │
  │  Company-Code: JIVO_OIL  │                          │
  │─────────────────────────►│                          │
  │                          │  1. Decode JWT → user    │
  │                          │  2. Lookup Company by    │
  │                          │     code "JIVO_OIL"      │
  │                          │  3. Verify UserCompany   │
  │                          │     (user, company)      │
  │                          │  4. Set request.user     │
  │                          │     Set request.company  │
  │                          │─────────────────────────►│
  │                          │                          │  QuerySet.filter(
  │                          │                          │    company=request.company
  │                          │                          │  )
  │  JSON Response           │                          │
  │◄─────────────────────────┼──────────────────────────│
```

---

## 3. Docking Flow

### Status Lifecycle

```
┌─────────────────────────────────────────────────────────────────────┐
│                        DOCKING STATUS FLOW                           │
│                                                                      │
│  ┌─────────┐    ┌──────────┐    ┌────────────────┐    ┌──────────┐  │
│  │  DRAFT  │───►│  DOCKED  │───►│INVOICE_VERIFIED│───►│COMPLETED │  │
│  └─────────┘    └──────────┘    └────────────────┘    └──────────┘  │
│       │              │                  │                    │        │
│   Create entry   dock-in API       verify API           complete API │
│   Auto-generate  dock_in_time=now  All invoices         dock_out=now │
│   entry_number   + photos          verified via SAP     is_locked=T  │
│                                                                      │
│  Any status ───────────────────────────────────► ┌──────────┐        │
│  (except COMPLETED)                              │CANCELLED │        │
│                                                  └──────────┘        │
└─────────────────────────────────────────────────────────────────────┘
```

### Step-by-Step: Inward Docking (with Invoice)

```
Security Guard                     System                          SAP HANA
        │                             │                              │
   [1]  │  POST /api/v1/docking/entries/                             │
        │  {vehicle_number: "UP32AT1234",                            │
        │   driver_name: "Ramesh",                                   │
        │   entry_type: "INWARD"}      │                              │
        │─────────────────────────────►│                              │
        │                              │  DockingService              │
        │                              │  .generate_entry_number()    │
        │                              │  → "DOCK-JIVO_OIL-20260323-001"
        │                              │                              │
        │                              │  DockingEntry.objects.create(│
        │                              │    company=request.company,  │
        │                              │    created_by=request.user,  │
        │                              │    status=DRAFT,             │
        │                              │    ...)                      │
        │                              │                              │
        │  ◄── 201 {id: 42,            │                              │
        │       entry_number: "DOCK-..."│                              │
        │       status: "DRAFT"}       │                              │
        │                              │                              │
   [2]  │  POST /entries/42/dock-in/   │                              │
        │─────────────────────────────►│                              │
        │                              │  Validate: not locked        │
        │                              │  Validate: dock_in_time null │
        │                              │  dock_in_time = now()        │
        │                              │  status → DOCKED             │
        │                              │  updated_by = request.user   │
        │  ◄── 200 {status: "DOCKED"}  │                              │
        │                              │                              │
   [3]  │  POST /entries/42/photos/    │                              │
        │  Content-Type: multipart OR: │                              │
        │  {image_base64: "data:image/ │                              │
        │   jpeg;base64,/9j/...",      │                              │
        │   photo_type: "VEHICLE"}     │                              │
        │─────────────────────────────►│                              │
        │                              │  If base64:                  │
        │                              │    decode_base64_image()     │
        │                              │  Validate: ≤15 MB            │
        │                              │  Save → media/docking/       │
        │                              │         photos/2026/03/23/   │
        │                              │  DockingPhoto.objects.create │
        │  ◄── 201 {id, url}           │                              │
        │                              │                              │
   [4]  │  GET /docking/sap/invoice-lookup/                           │
        │  ?invoice_number=INV-2026-001│                              │
        │─────────────────────────────►│                              │
        │                              │  sap_client                  │
        │                              │  .get_invoice_by_number() ──►│
        │                              │  HANA query: OPCH table      │
        │                              │  ◄── row data                │
        │  ◄── {doc_entry: 1001,        │                              │
        │       supplier_code: "V001",  │                              │
        │       supplier_name: "ABC",   │                              │
        │       total_amount: 50000}    │                              │
        │                              │                              │
   [5]  │  POST /entries/42/invoices/  │                              │
        │  {invoice_number: "INV-2026-001",                           │
        │   invoice_date: "2026-03-23",│                              │
        │   supplier_name: "ABC",      │                              │
        │   total_amount: 50000}       │                              │
        │─────────────────────────────►│                              │
        │                              │  DockingInvoice.objects      │
        │                              │  .create(...)                │
        │  ◄── 201 {id: 1, ...}        │                              │
        │                              │                              │
   [6]  │  POST /entries/42/invoices/1/verify/                        │
        │─────────────────────────────►│                              │
        │                              │  get_invoice_by_number() ───►│
        │                              │  ◄── match found             │
        │                              │  invoice.is_verified = True  │
        │                              │  invoice.verified_by = user  │
        │                              │  invoice.verified_at = now() │
        │                              │  invoice.sap_doc_entry = 1001│
        │                              │                              │
        │                              │  Check: all invoices verified?
        │                              │  Yes → entry.status =        │
        │                              │        INVOICE_VERIFIED       │
        │  ◄── 200 {is_verified: true}  │                              │
        │                              │                              │
   [7]  │  POST /entries/42/complete/  │                              │
        │─────────────────────────────►│                              │
        │                              │  DockingService              │
        │                              │  .validate_completion():     │
        │                              │  ✓ dock_in_time set          │
        │                              │  ✓ ≥1 invoice verified       │
        │                              │  ✓ ≥1 photo attached         │
        │                              │                              │
        │                              │  dock_out_time = now()       │
        │                              │  status → COMPLETED          │
        │                              │  is_locked = True            │
        │                              │                              │
        │                              │  NotificationService         │
        │                              │  .send_notification_to_user( │
        │                              │    user=entry.created_by,    │
        │                              │    title="Docking Completed",│
        │                              │    reference_type=           │
        │                              │      "docking_entry",        │
        │                              │    reference_id=42,          │
        │                              │    company=entry.company)    │
        │                              │                              │
        │  ◄── 200 {status: "COMPLETED",│                              │
        │       is_locked: true}       │                              │
```

### Empty Truck Entry (Simplified)

```
Guard                           System
  │                                │
  │  POST /docking/entries/        │
  │  {entry_type: "EMPTY_TRUCK",   │
  │   vehicle_number: "MH04AB5678"}│
  │───────────────────────────────►│  Create → DRAFT
  │                                │
  │  POST /entries/{id}/dock-in/   │
  │───────────────────────────────►│  status → DOCKED
  │                                │
  │  POST /entries/{id}/photos/    │
  │  {photo_type: "VEHICLE"}       │
  │───────────────────────────────►│  Save photo
  │                                │
  │  POST /entries/{id}/complete/  │
  │───────────────────────────────►│  Validate:
  │                                │  ✓ dock_in_time set
  │                                │  ✓ ≥1 photo
  │                                │  (no invoice needed for EMPTY_TRUCK)
  │                                │  status → COMPLETED, is_locked=True
```

### Completion Validation Rules

| Entry Type | dock_in_time | Invoices | All Verified | Photos |
|---|---|---|---|---|
| INWARD | Required | ≥1 | All | ≥1 |
| OUTWARD | Required | ≥1 | All | ≥1 |
| EMPTY_TRUCK | Required | Not required | N/A | ≥1 |

---

## 4. Dynamic Forms & Approval Flow

### Form Lifecycle

```
┌─────────────────────────────────────────────────────────────────────┐
│                         FORM LIFECYCLE                               │
│                                                                      │
│  ADMIN CREATES FORM                    USER SUBMITS FORM             │
│  ──────────────────                    ──────────────────             │
│  ┌──────────────────┐                  ┌────────────────┐            │
│  │ Create Form      │                  │ Fill & Submit  │            │
│  │ POST /forms/     │                  │ POST /forms/   │            │
│  └────────┬─────────┘                  │ {id}/submit/   │            │
│           │                            └───────┬────────┘            │
│  ┌────────▼─────────┐                          │                     │
│  │ Add Fields       │                  ┌───────▼────────┐            │
│  │ POST /forms/     │                  │    PENDING     │            │
│  │ {id}/fields/     │                  └───────┬────────┘            │
│  └────────┬─────────┘                          │                     │
│           │                          ┌─────────┴──────────┐          │
│  ┌────────▼─────────┐                │                    │          │
│  │ Set Permissions  │         ┌──────▼──────┐   ┌────────▼────────┐ │
│  │ (FormPermission) │         │  APPROVED   │   │   REJECTED      │ │
│  └────────┬─────────┘         └──────┬──────┘   │ (with reason)   │ │
│           │                          │          └─────────────────┘ │
│  ┌────────▼─────────┐         ┌──────▼──────┐                       │
│  │ Publish          │         │   CLOSED    │                       │
│  │ is_published=True│         └─────────────┘                       │
│  └──────────────────┘                                               │
└─────────────────────────────────────────────────────────────────────┘
```

### Form Submission & Approval (Full Detail)

```
User                      System                    Approver            Notifications
  │                          │                          │                    │
  │  GET /dynamic-forms/forms/                          │                    │
  │─────────────────────────►│                          │                    │
  │                          │  Check FormPermission    │                    │
  │                          │  for request.user        │                    │
  │                          │  Filter: company,        │                    │
  │                          │  is_published, is_active │                    │
  │  ◄── [{id:10, title:     │                          │                    │
  │       "Material Request",│                          │                    │
  │       field_count: 4}]   │                          │                    │
  │                          │                          │                    │
  │  GET /forms/10/          │                          │                    │
  │─────────────────────────►│                          │                    │
  │  ◄── {fields: [          │                          │                    │
  │    {id:1, label:"Item",  │                          │                    │
  │     field_type:"TEXT",   │                          │                    │
  │     is_required:true},   │                          │                    │
  │    {id:2, label:"Qty",   │                          │                    │
  │     field_type:"INTEGER",│                          │                    │
  │     validation_rules:    │                          │                    │
  │     {min:1, max:999}},   │                          │                    │
  │    {id:3, label:"Photo", │                          │                    │
  │     field_type:"IMAGE"}, │                          │                    │
  │    {id:4, label:"Priority",                         │                    │
  │     field_type:"SELECT", │                          │                    │
  │     options:["Low",      │                          │                    │
  │              "Medium",   │                          │                    │
  │              "High"]}    │                          │                    │
  │  ]}                      │                          │                    │
  │                          │                          │                    │
  │  POST /forms/10/submit/  │                          │                    │
  │  {responses: [           │                          │                    │
  │    {field_id:1,          │                          │                    │
  │     value:"Steel Pipe"}, │                          │                    │
  │    {field_id:2,          │                          │                    │
  │     value:"50"},         │                          │                    │
  │    {field_id:3,          │                          │                    │
  │     image_base64:        │                          │                    │
  │     "data:image/..."},   │                          │                    │
  │    {field_id:4,          │                          │                    │
  │     value:"High"}        │                          │                    │
  │  ]}                      │                          │                    │
  │─────────────────────────►│                          │                    │
  │                          │                          │                    │
  │                          │  @transaction.atomic:    │                    │
  │                          │                          │                    │
  │                          │  1. Validate required    │                    │
  │                          │     fields present       │                    │
  │                          │  2. Validate types       │                    │
  │                          │     (int for INTEGER,    │                    │
  │                          │      option in list      │                    │
  │                          │      for SELECT)         │                    │
  │                          │  3. Validate rules       │                    │
  │                          │     (min/max for Qty)    │                    │
  │                          │  4. Decode base64 image  │                    │
  │                          │  5. Create FormSubmission│                    │
  │                          │     status=PENDING       │                    │
  │                          │  6. Create FieldResponse │                    │
  │                          │     for each answer      │                    │
  │                          │                          │                    │
  │                          │  7. Query FormPermission │                    │
  │                          │     where can_approve=T  │                    │
  │                          │                          │                    │
  │                          │  8. For each approver: ──────────────────────►│
  │                          │     NotificationService  │     FCM Push       │
  │                          │     .send_notification   │     to all devices │
  │                          │     _to_user()           │                    │
  │                          │                          │                    │
  │                          │  9. WhatsAppService ─────────────────────────►│
  │                          │     .send_pending_       │     WhatsApp to    │
  │                          │     approval_alert()     │     approvers      │
  │                          │                          │                    │
  │  ◄── 201 {id: 55,        │                          │                    │
  │       status: "PENDING"} │                          │                    │
  │                          │                          │                    │
  ─ ─ ─ ─ ─ ─ ─ ─ (approver reviews) ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─
  │                          │                          │                    │
  │                          │  GET /pending-approvals/ │                    │
  │                          │◄─────────────────────────│                    │
  │                          │  Filter: company,        │                    │
  │                          │  approvable form IDs,    │                    │
  │                          │  status=PENDING          │                    │
  │                          │──────► [{id:55, ...}]    │                    │
  │                          │                          │                    │
  │                          │  GET /submissions/55/    │                    │
  │                          │◄─────────────────────────│                    │
  │                          │──────► {responses: [...],│                    │
  │                          │        images, values}   │                    │
  │                          │                          │                    │
  │                          │ ┌─── APPROVE PATH ──────┐│                    │
  │                          │ │                        ││                    │
  │                          │ │ POST /submissions/55/  ││                    │
  │                          │ │      /approve/         ││                    │
  │                          │ │◄───────────────────────││                    │
  │                          │ │                        ││                    │
  │                          │ │ status → APPROVED      ││                    │
  │                          │ │ approved_by = approver ││                    │
  │                          │ │ approved_at = now()    ││                    │
  │                          │ │                        ││                    │
  │  ◄── FCM: "Submission    │ │ Notify submitter ─────────────────────────►│
  │       approved"          │ │ WhatsApp alert ───────────────────────────►│
  │                          │ └────────────────────────┘│                    │
  │                          │                          │                    │
  │                          │ ┌─── REJECT PATH ───────┐│                    │
  │                          │ │                        ││                    │
  │                          │ │ POST /submissions/55/  ││                    │
  │                          │ │      /reject/          ││                    │
  │                          │ │ {rejection_reason:     ││                    │
  │                          │ │  "Qty exceeds budget"} ││                    │
  │                          │ │◄───────────────────────││                    │
  │                          │ │                        ││                    │
  │                          │ │ status → REJECTED      ││                    │
  │                          │ │ rejection_reason saved ││                    │
  │                          │ │                        ││                    │
  │  ◄── FCM: "Submission    │ │ Notify with reason ───────────────────────►│
  │       rejected: Qty..."  │ │                        ││                    │
  │                          │ └────────────────────────┘│                    │
```

### Field Validation Rules

| Field Type | Validation | Example |
|---|---|---|
| TEXT | max_length (optional) | `{"max_length": 500}` |
| INTEGER | min, max | `{"min": 1, "max": 999}` |
| DECIMAL | min, max, decimal_places | `{"min": 0.0, "max": 100.0}` |
| DATE | min_date, max_date | `{"min_date": "today"}` |
| SELECT | value must be in options list | options: ["Low", "Medium", "High"] |
| RADIO | value must be in options list | same as SELECT |
| CHECKBOX | values must be subset of options | multiple selection allowed |
| IMAGE | File size ≤ 15 MB, image MIME type | jpeg, png, gif, webp |
| FILE | File size ≤ 15 MB | any file type |

---

## 5. Material Tracking Flow

### Material Movement Status Flow

```
┌──────────────────────────────────────────────────────────────────────┐
│                  MATERIAL MOVEMENT STATUS FLOW                        │
│                                                                       │
│  ┌───────────┐   ┌───────────┐   ┌──────────────────┐  ┌─────────┐  │
│  │DISPATCHED │──►│IN_TRANSIT │──►│    RECEIVED       │─►│ CLOSED  │  │
│  └───────────┘   └───────────┘   └──────────────────┘  └─────────┘  │
│       │               │                                   is_locked  │
│       │               │          ┌──────────────────┐      = True   │
│       │               └─────────►│PARTIALLY_RECEIVED│──┐             │
│       │                          └──────────────────┘  │             │
│       │                                │               │             │
│       │                                └───(receive    │             │
│       │                                     more)──────┘             │
│       │                                                              │
│       │   ┌─────────────────────────────────────────┐                │
│       └──►│  OVERDUE                                │                │
│           │  (auto: expected_return_date < today)    │                │
│           │  Triggered by check_overdue_movements    │                │
│           └─────────────────────────────────────────┘                │
└──────────────────────────────────────────────────────────────────────┘
```

### Outward Movement (Full Flow)

```
Store Operator            System                  Receiver            Notifications
  │                          │                       │                     │
  │  POST /material-tracking/│                       │                     │
  │       /movements/        │                       │                     │
  │  {movement_type: "OUTWARD",                      │                     │
  │   from_location: "Factory A",                    │                     │
  │   to_location: "Warehouse B",                    │                     │
  │   vehicle_number: "UP32AT1234",                  │                     │
  │   expected_return_date: "2026-04-05",            │                     │
  │   items: [               │                       │                     │
  │     {material_name:      │                       │                     │
  │      "Steel Rod",        │                       │                     │
  │      material_code: "SR001",                     │                     │
  │      quantity: 100,      │                       │                     │
  │      uom: "KG"},         │                       │                     │
  │     {material_name:      │                       │                     │
  │      "Copper Wire",      │                       │                     │
  │      quantity: 50,       │                       │                     │
  │      uom: "MTR"}         │                       │                     │
  │   ]}                     │                       │                     │
  │─────────────────────────►│                       │                     │
  │                          │  Generate:             │                     │
  │                          │  "MOV-JIVO_OIL-        │                     │
  │                          │   20260323-001"        │                     │
  │                          │  status = DISPATCHED    │                     │
  │                          │  dispatched_by = user   │                     │
  │                          │  dispatched_at = now()  │                     │
  │                          │                        │                     │
  │                          │  Create MovementItems  │                     │
  │                          │  (100 KG, 50 MTR)      │                     │
  │                          │                        │                     │
  │                          │  WhatsApp alert ───────────────────────────►│
  │                          │  "Material dispatched   │                     │
  │                          │   MOV-...-001:          │                     │
  │                          │   100 KG Steel Rod,     │                     │
  │                          │   50 MTR Copper Wire    │                     │
  │                          │   to Warehouse B"       │                     │
  │                          │                        │                     │
  │  ◄── 201 {id: 42,        │                        │                     │
  │       movement_number:   │                        │                     │
  │       "MOV-...-001",     │                        │                     │
  │       status: "DISPATCHED"}                       │                     │
  │                          │                        │                     │
  ─ ─ ─ ─ ─ ─ ─ (material in transit) ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─
  │                          │                        │                     │
  │                          │  POST /movements/42/   │                     │
  │                          │       /receive/         │                     │
  │                          │  {items: [              │                     │
  │                          │    {item_id: 1,         │                     │
  │                          │     received_qty: 95},  │                     │
  │                          │    {item_id: 2,         │                     │
  │                          │     received_qty: 50}   │                     │
  │                          │  ]}                     │                     │
  │                          │◄────────────────────────│                     │
  │                          │                        │                     │
  │                          │  Update each item's    │                     │
  │                          │  received_quantity     │                     │
  │                          │                        │                     │
  │                          │  Check: all items fully │                     │
  │                          │  received?              │                     │
  │                          │  item 1: 95/100 ✗      │                     │
  │                          │  item 2: 50/50 ✓       │                     │
  │                          │  → PARTIALLY_RECEIVED   │                     │
  │                          │                        │                     │
  │  ◄── FCM Push:           │  Notify dispatcher ────────────────────────►│
  │      "95/100 KG received"│                        │                     │
  │                          │                        │                     │
  ─ ─ ─ ─ ─ (remaining arrives) ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─
  │                          │                        │                     │
  │                          │  POST /movements/42/receive/                 │
  │                          │  {items: [{item_id:1,  │                     │
  │                          │   received_qty: 100}]} │                     │
  │                          │◄────────────────────────│                     │
  │                          │                        │                     │
  │                          │  item 1: 100/100 ✓     │                     │
  │                          │  item 2: 50/50 ✓       │                     │
  │                          │  → status = RECEIVED    │                     │
  │                          │  received_by = user     │                     │
  │                          │  received_at = now()    │                     │
  │                          │  received_back = True   │                     │
  │                          │                        │                     │
  │  POST /movements/42/close/                        │                     │
  │─────────────────────────►│                        │                     │
  │                          │  status → CLOSED        │                     │
  │                          │  is_locked = True       │                     │
```

### Receive Quantity Logic

```python
# How status is determined after each receive call:

for each item in movement.items:
    if item.received_quantity < item.quantity:
        any_partial = True
    if item.received_quantity >= item.quantity:
        any_complete = True

if all items fully received:
    movement.status = RECEIVED
elif any item has received_quantity > 0:
    movement.status = PARTIALLY_RECEIVED
```

---

## 6. Gate Pass Flow

### Gate Pass Status Flow

```
┌──────────────────────────────────────────────────────────────────┐
│                      GATE PASS STATUS FLOW                        │
│                                                                    │
│  ┌───────┐    ┌──────────────────┐    ┌──────────┐   ┌────────┐  │
│  │ DRAFT │───►│ PENDING_APPROVAL │───►│ APPROVED │──►│ CLOSED │  │
│  └───────┘    └──────────────────┘    └──────────┘   └────────┘  │
│                        │                                          │
│                        ▼                                          │
│                  ┌──────────┐                                     │
│                  │ REJECTED │                                     │
│                  └──────────┘                                     │
└──────────────────────────────────────────────────────────────────┘
```

### Returnable Gate Pass (Linked to Movement)

```
Requester               System                  Approver             Gate Guard
  │                        │                        │                     │
  │  POST /gate-passes/    │                        │                     │
  │  {pass_type: "RETURNABLE",                      │                     │
  │   party_name: "ABC Ltd",                        │                     │
  │   vehicle_number: "...",│                        │                     │
  │   movement_id: 42,     │  ◄── link to movement  │                     │
  │   items: [              │                        │                     │
  │     {material_name:    │                        │                     │
  │      "Steel Rod",      │                        │                     │
  │      quantity: 100,    │                        │                     │
  │      uom: "KG",        │                        │                     │
  │      value: 25000}     │                        │                     │
  │   ]}                   │                        │                     │
  │───────────────────────►│                        │                     │
  │                        │  Generate:              │                     │
  │                        │  "GP-JIVO_OIL-          │                     │
  │                        │   20260323-001"         │                     │
  │                        │  Create GatePass +      │                     │
  │                        │  GatePassItems           │                     │
  │                        │  status → PENDING_APPROVAL                   │
  │                        │                        │                     │
  │                        │  FCM → approvers ──────►│                     │
  │                        │  WhatsApp → approvers   │                     │
  │                        │                        │                     │
  │  ◄── 201 {id: 5,       │                        │                     │
  │       status: "PENDING_│                        │                     │
  │       APPROVAL"}       │                        │                     │
  │                        │                        │                     │
  │                        │  POST /gate-passes/5/   │                     │
  │                        │       /approve/         │                     │
  │                        │◄───────────────────────│                     │
  │                        │  status → APPROVED      │                     │
  │                        │  approved_by = approver │                     │
  │                        │  approved_at = now()    │                     │
  │                        │                        │                     │
  │  ◄── FCM: "Gate pass   │                        │                     │
  │       approved"        │                        │                     │
  │                        │                        │                     │
  │                        │  GET /gate-passes/5/print/                   │
  │                        │◄────────────────────────────────────────────│
  │                        │  Generate printable HTML/PDF                 │
  │                        │  with: pass number, items,                   │
  │                        │  party, vehicle, approval                    │
  │                        │─────────────────────────────────────────────►│
  │                        │                        │          Print pass │
  │                        │                        │          Allow exit │
  │                        │                        │                     │
  ─ ─ ─ (material returns, movement receives) ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─
  │                        │                        │                     │
  │                        │  When linked movement   │                     │
  │                        │  status → RECEIVED:     │                     │
  │                        │  gate_pass.status →     │                     │
  │                        │    CLOSED (auto)        │                     │
```

### Non-Returnable Gate Pass

Same flow as returnable, but:
- No `movement_id` link
- Auto-closes immediately after guard allows exit
- No return tracking

---

## 7. Reporting Flow

### On-Demand Report Execution

```
User                          System                        DB
  │                              │                            │
  │  GET /reporting/reports/     │                            │
  │─────────────────────────────►│                            │
  │                              │  Filter by user roles:     │
  │                              │  reports where user's role │
  │                              │  is in allowed_roles       │
  │  ◄── [{id:5, name: "Daily   │                            │
  │       Material Out",         │                            │
  │       category: "MATERIAL",  │                            │
  │       parameters: [          │                            │
  │         {name: "start_date", │                            │
  │          type: "date",       │                            │
  │          required: true,     │                            │
  │          label: "Start Date"},                            │
  │         {name: "end_date",   │                            │
  │          type: "date",       │                            │
  │          required: true,     │                            │
  │          label: "End Date"}  │                            │
  │       ]}]                    │                            │
  │                              │                            │
  │  POST /reports/5/execute/    │                            │
  │  {start_date: "2026-03-01", │                            │
  │   end_date: "2026-03-23"}   │                            │
  │─────────────────────────────►│                            │
  │                              │  ReportService             │
  │                              │  .execute_report():        │
  │                              │                            │
  │                              │  1. Validate required      │
  │                              │     params present         │
  │                              │  2. cursor.execute(         │
  │                              │     query_template,        │
  │                              │     parameters)  ────────►│
  │                              │     (parameterized!)       │
  │                              │  3. ◄── result rows        │
  │                              │  4. Create                 │
  │                              │     ReportExecutionLog     │
  │                              │                            │
  │  ◄── {columns: [{key: "date",│                            │
  │       label: "Date"}, ...],  │                            │
  │       rows: [{date: "...",   │                            │
  │       material: "...",       │                            │
  │       qty: 100}, ...],       │                            │
  │       total_count: 150,      │                            │
  │       execution_time_ms: 45} │                            │
  │                              │                            │
  │  POST /reports/5/export/     │                            │
  │  {format: "excel",           │                            │
  │   start_date, end_date}      │                            │
  │─────────────────────────────►│                            │
  │                              │  Execute query             │
  │                              │  ReportService             │
  │                              │  .export_to_excel()        │
  │                              │  → openpyxl workbook       │
  │                              │  → BytesIO                 │
  │  ◄── File download            │                            │
  │      (Content-Disposition:   │                            │
  │       attachment)            │                            │
```

### Scheduled Report Flow (Management Command)

```
┌──────────────────────────────────────────────────────────────────────┐
│  Cron → python manage.py run_scheduled_reports (hourly)               │
│                                                                       │
│  ┌─────────────────────────┐                                          │
│  │ Query ScheduledReport   │  WHERE is_active=True                    │
│  │ WHERE next_run_at ≤ now │  AND next_run_at ≤ now()                │
│  └───────────┬─────────────┘                                          │
│              │                                                        │
│              ▼  For each due schedule:                                 │
│  ┌─────────────────────────┐                                          │
│  │ ReportService           │                                          │
│  │ .execute_report(        │                                          │
│  │   report,               │                                          │
│  │   schedule.parameters,  │                                          │
│  │   is_scheduled=True)    │                                          │
│  └───────────┬─────────────┘                                          │
│              │                                                        │
│              ▼                                                        │
│  ┌─────────────────────────┐                                          │
│  │ ReportService           │                                          │
│  │ .export_to_excel()      │  → BytesIO with .xlsx                    │
│  └───────────┬─────────────┘                                          │
│              │                                                        │
│              ▼                                                        │
│  ┌─────────────────────────┐                                          │
│  │ Django EmailMessage     │                                          │
│  │ subject: "Scheduled     │                                          │
│  │   Report: {name}"       │                                          │
│  │ to: schedule.recipients │                                          │
│  │ attach: {name}.xlsx     │                                          │
│  │ .send()                 │                                          │
│  └───────────┬─────────────┘                                          │
│              │                                                        │
│              ▼                                                        │
│  ┌─────────────────────────┐                                          │
│  │ Update schedule:        │                                          │
│  │ last_run_at = now()     │                                          │
│  │ next_run_at =           │                                          │
│  │   calculate_next(       │                                          │
│  │     schedule_type)      │                                          │
│  └─────────────────────────┘                                          │
│                                                                       │
│  Schedule Types:                                                      │
│  ├── DAILY        → next day, same time                               │
│  ├── WEEKLY       → +7 days                                           │
│  ├── HALF_MONTHLY → 1st ↔ 16th of month                              │
│  └── MONTHLY      → same day, next month                              │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 8. Notification Flow (FCM + WhatsApp)

### Dual Channel Architecture

```
Any Module Event                NotificationService         WhatsAppService
  │                                │                              │
  │  e.g. form approved            │                              │
  │                                │                              │
  │  [A] Push Notification:        │                              │
  │  NotificationService           │                              │
  │  .send_notification_to_user(   │                              │
  │    user=submitter,             │                              │
  │    title="Approved",           │                              │
  │    body="Your form...",        │                              │
  │    notification_type=          │                              │
  │      GENERAL_ANNOUNCEMENT,     │                              │
  │    reference_type=             │                              │
  │      "form_submission",        │                              │
  │    reference_id=55,            │                              │
  │    company=submission.company, │                              │
  │    created_by=approver)        │                              │
  │───────────────────────────────►│                              │
  │                                │  1. Notification.objects     │
  │                                │     .create() → DB record    │
  │                                │                              │
  │                                │  2. Query UserDevice for     │
  │                                │     user's FCM tokens        │
  │                                │                              │
  │                                │  3. firebase_admin            │
  │                                │     .messaging.send_each()   │
  │                                │     → Push to all devices    │
  │                                │                              │
  │  [B] WhatsApp:                 │                              │
  │  WhatsAppService               │                              │
  │  .send_approval_notification(  │                              │
  │    submission, 'APPROVED')     │                              │
  │────────────────────────────────────────────────────────────►  │
  │                                │                              │
  │                                │  1. Get user phone number    │
  │                                │  2. _format_phone()          │
  │                                │     → "+91XXXXXXXXXX"        │
  │                                │  3. WhatsAppLog.objects      │
  │                                │     .create(status=SENDING)  │
  │                                │  4. requests.post(           │
  │                                │     AISENSY_API_URL,         │
  │                                │     {apiKey, campaignName,   │
  │                                │      destination,            │
  │                                │      templateParams})        │
  │                                │  5. On success:              │
  │                                │     log.status = SENT        │
  │                                │  6. On failure:              │
  │                                │     log.status = FAILED      │
  │                                │     log.error_message = str  │
```

### Notification Trigger Matrix

| Module | Event | FCM Push | WhatsApp | Email |
|---|---|---|---|---|
| `dynamic_forms` | New submission pending | Approvers | Approvers | - |
| `dynamic_forms` | Submission approved | Submitter | Submitter | - |
| `dynamic_forms` | Submission rejected | Submitter | Submitter | - |
| `docking` | Docking completed | Creator | - | - |
| `material_tracking` | Material dispatched | - | Dispatcher | - |
| `material_tracking` | Material received | Dispatcher | - | - |
| `material_tracking` | Movement overdue | Dispatcher | Dispatcher | - |
| `material_tracking` | Gate pass approved | Requester | Requester | - |
| `material_tracking` | Gate pass rejected | Requester | Requester | - |
| `reporting` | Scheduled report ready | - | - | Recipients |

### Polling/Reminder Flow (Management Command)

```
┌───────────────────────────────────────────────────────────────────┐
│  Cron → python manage.py send_whatsapp_reminders                   │
│  Schedule: every 2 hrs during work hours (8 AM – 8 PM)            │
│                                                                    │
│  [1] Pending form approvals older than 4 hours:                    │
│      FormSubmission.filter(status=PENDING, created_at < -4hrs)     │
│      → WhatsApp reminder to each approver                          │
│                                                                    │
│  [2] Overdue material movements:                                   │
│      MaterialMovement.filter(status=OVERDUE)                       │
│      → WhatsApp reminder to dispatcher                             │
│                                                                    │
│  [3] Open gate passes older than 24 hours:                         │
│      GatePass.filter(status=PENDING_APPROVAL,                      │
│                       created_at < -24hrs)                         │
│      → WhatsApp reminder to approvers                              │
└───────────────────────────────────────────────────────────────────┘
```

---

## 9. File Upload Flow

### Mobile Camera Capture (Base64)

```
Mobile Device                  APIView                        Storage
  │                               │                              │
  │  User taps camera icon        │                              │
  │  ┌────────────────────┐       │                              │
  │  │ <input type="file" │       │                              │
  │  │  accept="image/*"  │       │                              │
  │  │  capture="camera"> │       │                              │
  │  │                    │       │                              │
  │  │ JS: FileReader     │       │                              │
  │  │ .readAsDataURL()   │       │                              │
  │  │ → base64 string    │       │                              │
  │  └────────────────────┘       │                              │
  │                               │                              │
  │  POST (JSON body)             │                              │
  │  {image_base64:               │                              │
  │   "data:image/jpeg;base64,    │                              │
  │    /9j/4AAQSkZJRg..."}       │                              │
  │──────────────────────────────►│                              │
  │                               │                              │
  │                               │  decode_base64_image():      │
  │                               │  1. Split at ";base64,"      │
  │                               │  2. Extract ext from header  │
  │                               │     "data:image/jpeg" → jpeg │
  │                               │  3. Validate ext in          │
  │                               │     (jpeg,png,gif,webp)      │
  │                               │  4. base64.b64decode()       │
  │                               │  5. Check size ≤ 15 MB       │
  │                               │  6. Return ContentFile(      │
  │                               │     data, name=              │
  │                               │     "dock_a1b2c3d4.jpeg")    │
  │                               │                              │
  │                               │  model.image = content_file  │
  │                               │  model.save()   ────────────►│
  │                               │                    media/     │
  │                               │                    docking/   │
  │                               │                    photos/    │
  │                               │                    2026/03/23/│
  │                               │                    dock_a1b2..│
  │  ◄── {id, url:                │                              │
  │   "/media/docking/photos/     │                              │
  │    2026/03/23/dock_a1b2.."}   │                              │
```

### Multipart Upload (Alternative)

```
Mobile Device                  APIView                        Storage
  │                               │                              │
  │  POST (multipart/form-data)   │                              │
  │  Content-Type: multipart/...  │                              │
  │  image: <binary file>         │                              │
  │  photo_type: "VEHICLE"        │                              │
  │──────────────────────────────►│                              │
  │                               │                              │
  │                               │  request.FILES['image']      │
  │                               │  → InMemoryUploadedFile      │
  │                               │  Assign directly to          │
  │                               │  ImageField                  │
  │                               │  model.save() ──────────────►│
  │                               │                              │
  │  ◄── {id, url}                │                              │
```

### Upload Path Convention

```
media/
├── docking/
│   └── photos/
│       └── 2026/03/23/
│           ├── dock_a1b2c3d4.jpeg
│           └── dock_e5f6g7h8.jpeg
│
├── forms/
│   ├── responses/
│   │   └── 2026/03/23/
│   │       └── form_i9j0k1l2.jpeg
│   └── files/
│       └── 2026/03/23/
│           └── doc_m3n4o5p6.pdf
│
└── gate_core/                        ← already exists
    └── attachments/
```

---

## 10. Scheduled Tasks Flow

### Overview (No Celery — Cron + Management Commands)

```
┌──────────────────────────────────────────────────────────────────┐
│                    CRON SCHEDULE (Server)                          │
│                                                                    │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │ 0 8 * * *    check_overdue_movements                         │ │
│  │              Daily at 8 AM                                    │ │
│  │              Mark overdue, send FCM + WhatsApp                │ │
│  └──────────────────────────────────────────────────────────────┘ │
│                                                                    │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │ 0 * * * *    run_scheduled_reports                            │ │
│  │              Hourly                                            │ │
│  │              Execute due reports, email Excel to recipients    │ │
│  └──────────────────────────────────────────────────────────────┘ │
│                                                                    │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │ 0 8,10,12,14,16,18,20 * * *    send_whatsapp_reminders      │ │
│  │              Every 2 hrs (8 AM – 8 PM)                        │ │
│  │              Remind: pending approvals, overdue movements     │ │
│  └──────────────────────────────────────────────────────────────┘ │
│                                                                    │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │ Existing:    setup_utility_groups                             │ │
│  │              Run once after deployment                         │ │
│  │              Creates Django auth groups + permissions          │ │
│  └──────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

---

## 11. Cross-Module Interactions

### Full Outward Cycle: Movement → Gate Pass → Docking

```
┌─────────────────────────────────────────────────────────────────────────┐
│                    CROSS-MODULE: OUTWARD MATERIAL CYCLE                  │
│                                                                          │
│  material_tracking              material_tracking              docking   │
│  ┌─────────────────┐           ┌─────────────────┐      ┌────────────┐  │
│  │ 1. Create       │           │ 2. Create        │      │ 3. Truck   │  │
│  │ MaterialMovement│──────────►│ GatePass         │      │ docks for  │  │
│  │ (OUTWARD, items)│ link via  │ (RETURNABLE,     │      │ loading    │  │
│  │ status=DISPATCHED│movement_id│ movement_id=42) │      │            │  │
│  └────────┬────────┘           └────────┬─────────┘      └─────┬──────┘  │
│           │                             │                      │         │
│           │                             ▼                      │         │
│           │                    ┌─────────────────┐             │         │
│           │                    │ Approver approves│             │         │
│           │                    │ gate pass         │             │         │
│           │                    └────────┬─────────┘             │         │
│           │                             │                      │         │
│           │                             ▼                      ▼         │
│           │                    ┌─────────────────┐      ┌────────────┐  │
│           │                    │ Guard prints pass│      │ Photos     │  │
│           │                    │ allows vehicle   │─────►│ captured   │  │
│           │                    │ to exit          │      │ Dock-out   │  │
│           │                    └─────────────────┘      └────────────┘  │
│           │                                                              │
│  ─ ─ ─ ─ ─ ─ ─ (material at destination) ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─   │
│           │                                                              │
│           ▼                                                              │
│  ┌─────────────────┐                                                     │
│  │ 4. Receiver      │                                                    │
│  │ confirms receipt │                                                    │
│  │ status=RECEIVED  │                                                    │
│  └────────┬────────┘                                                     │
│           │                                                              │
│           ▼                                                              │
│  ┌─────────────────┐           ┌─────────────────┐                       │
│  │ 5. Close         │──────────►│ Gate pass auto- │                       │
│  │ movement         │  trigger  │ closed (CLOSED)  │                       │
│  │ status=CLOSED    │           └─────────────────┘                       │
│  │ is_locked=True   │                                                    │
│  └─────────────────┘                                                     │
└─────────────────────────────────────────────────────────────────────────┘
```

### Form Submission → Material Movement (Optional Hook)

```
┌───────────────────────────────────────────────────────────────────┐
│  Example: "Material Request Form" triggers material dispatch      │
│                                                                    │
│  dynamic_forms                          material_tracking          │
│  ┌──────────────┐                       ┌──────────────────┐      │
│  │ User submits │                       │                  │      │
│  │ "Material    │                       │                  │      │
│  │  Request"    │                       │                  │      │
│  │ form         │                       │                  │      │
│  └──────┬───────┘                       │                  │      │
│         │                               │                  │      │
│         ▼                               │                  │      │
│  ┌──────────────┐                       │                  │      │
│  │ Approver     │  ──(post-approve     │                  │      │
│  │ APPROVES     │    service hook)─────►│ Auto-create     │      │
│  │ submission   │                       │ MaterialMovement │      │
│  └──────────────┘                       │ from form data   │      │
│                                          └──────────────────┘      │
│                                                                    │
│  This linkage is optional — implemented in                         │
│  FormSubmissionService.approve() as a hook.                        │
└───────────────────────────────────────────────────────────────────┘
```

### Inward Flow: Docking → SAP → Existing Gate Entry

```
┌───────────────────────────────────────────────────────────────────┐
│  INWARD FLOW: Supplier truck arrives                              │
│                                                                    │
│  docking                    sap_client              gate_core      │
│  ┌──────────────┐           ┌──────────┐           ┌───────────┐  │
│  │ Truck docks  │──lookup──►│ Verify   │           │           │  │
│  │ Invoice      │           │ invoice  │           │           │  │
│  │ captured &   │◄──data────│ in SAP   │           │           │  │
│  │ verified     │           └──────────┘           │           │  │
│  └──────┬───────┘                                   │           │  │
│         │                                           │           │  │
│         │  If raw material with PO:                 │           │  │
│         │  create VehicleEntry ─────────────────────►│ Existing │  │
│         │  (links to existing                       │ gate flow │  │
│         │   gate management flow:                   │ security, │  │
│         │   security → weighment → QC)              │ weighment,│  │
│         │                                           │ QC        │  │
│         │  If non-PO material:                      └───────────┘  │
│         │  complete docking independently                          │
└───────────────────────────────────────────────────────────────────┘
```

---

## 12. Error Handling & Edge Cases

### Locked Entry Protection

All modules with `is_locked` field follow this pattern:

```python
# In every PATCH/POST action view:
if entry.is_locked:
    return Response(
        {"error": "Entry is locked and cannot be modified."},
        status=status.HTTP_400_BAD_REQUEST
    )
```

**When entries get locked:**
- `DockingEntry` → on COMPLETED
- `MaterialMovement` → on CLOSED
- `GatePass` → inherits from movement

### Status Transition Validation

```python
# Invalid transitions are rejected:

VALID_TRANSITIONS = {
    'DRAFT': ['DOCKED', 'CANCELLED'],
    'DOCKED': ['INVOICE_VERIFIED', 'CANCELLED'],
    'INVOICE_VERIFIED': ['COMPLETED', 'CANCELLED'],
    'COMPLETED': [],        # terminal state
    'CANCELLED': [],        # terminal state
}

if new_status not in VALID_TRANSITIONS.get(current_status, []):
    return Response({"error": f"Cannot transition from {current_status} to {new_status}."})
```

### Concurrent Modification

- `updated_at` auto-updates on every save (optimistic concurrency possible)
- `is_locked` prevents post-completion edits
- `@transaction.atomic` on multi-model operations (e.g., form submission)

### WhatsApp Failures

- Failures are logged to `WhatsAppLog` with `status=FAILED` and `error_message`
- Never blocks the main operation — WhatsApp is fire-and-forget
- Failed messages can be retried via admin or management command

### SAP Connection Failures

- Invoice lookup returns `404 Not Found` if SAP is unreachable
- Docking can continue without SAP verification (manual override by admin)
- All SAP queries have timeout protection

---

## 13. Status Reference

### All Status Flows (Quick Reference)

| Module | Entity | Statuses |
|---|---|---|
| `docking` | DockingEntry | DRAFT → DOCKED → INVOICE_VERIFIED → COMPLETED / CANCELLED |
| `dynamic_forms` | FormSubmission | PENDING → APPROVED / REJECTED → CLOSED |
| `material_tracking` | MaterialMovement | DISPATCHED → IN_TRANSIT → RECEIVED / PARTIALLY_RECEIVED → CLOSED (+ OVERDUE auto) |
| `material_tracking` | GatePass | DRAFT → PENDING_APPROVAL → APPROVED / REJECTED → CLOSED |
| `reporting` | ScheduledReport | Active / Inactive (runs on cron schedule) |

### Lock Behavior

| Entity | Locked When | Effect |
|---|---|---|
| DockingEntry | status = COMPLETED | No edits, no new invoices/photos |
| MaterialMovement | status = CLOSED | No edits, no more receives |
| GatePass | Linked movement CLOSED | Auto-closed |
| FormSubmission | is_closed = True | No status changes |
