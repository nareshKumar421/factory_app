# Utilities Mobile - Docking & Factory Operations PWA

A Progressive Web App (PWA) for mobile factory and warehouse operations at Jivo Wellness Pvt Ltd. Handles docking, invoice processing, material tracking, dynamic form submissions, and multi-level approval workflows with WhatsApp notifications.

## Tech Stack

- **Backend:** PHP (vanilla, no framework)
- **Primary Database:** PostgreSQL (`utilities`)
- **Secondary Databases:** SQL Server (DSRLive, JSAP, Jivo_All_Branches)
- **Frontend:** HTML5, Bootstrap 5.3, jQuery 3.6
- **PWA:** Service Worker + manifest.json (offline support)
- **Email:** PHPMailer (SMTP via 2cent.me)
- **Notifications:** AiSensy WhatsApp API
- **ERP Integration:** SAP HANA via SQL Server linked server (`HANA112`)

## Project Structure

```
mobile/
├── config.php                      # Database credentials & shared config
├── manifest.json                   # PWA manifest
├── sw.js                           # Service Worker for offline caching
├── uploads/                        # User-uploaded images (base64 → JPG)
│
├── ── Authentication ──
│   ├── login_html.php              # Login page UI
│   ├── check_login.php             # API: validate credentials, create session
│   ├── validate_session.php        # API: verify session token
│   └── logout.php                  # Destroy session
│
├── ── Entry Points ──
│   ├── um.php                      # Main PWA entry (section-based nav)
│   ├── um_power.php                # Power user entry (gallery upload)
│   └── main_html.php               # Dashboard with permission-based menu
│
├── ── Form System ──
│   ├── list_forms.php              # API: list accessible forms
│   ├── get_questions.php           # API: fetch form fields by formid
│   ├── submit_response.php         # API: submit answers + images
│   ├── show_submission.php         # View submitted responses
│   └── close_submission.php        # API: close a submission
│
├── ── Approval Workflow ──
│   ├── approve_submission.php      # API: approve/reject + WhatsApp alerts
│   ├── utility_approval.php        # Approval dashboard UI
│   └── confirm_received.php        # Token-based confirmation links
│
├── ── Docking & Invoicing ──
│   ├── dock_invoice.php            # Docking invoice with SAP HANA queries
│   ├── dock_photo.php              # Photo capture for docking
│   ├── gate_invoice2.php           # Gate pass/invoice generation
│   ├── gate_finalprint.php         # Final gate printing
│   ├── gupta_invoice.php           # Variant invoice handler
│   ├── mart_print.php              # Mart reporting/printing
│   └── empty_truck_in.php          # Empty truck entry tracking
│
├── ── Reporting ──
│   ├── reporting.php               # Main dashboard (SQL queries + date filters)
│   ├── reporting_android.php       # Android-optimized layout
│   ├── material_out_report.php     # Half-monthly material report (emailed)
│   └── planningreport.php          # Planning/capacity analysis
│
├── ── Notifications (Polling) ──
│   ├── polling.php – polling11.php # WhatsApp reminders for pending items
│   └── close_responses.php         # Close returned material responses
│
├── ── Admin Tools ──
│   ├── pg_client.php               # PostgreSQL query executor
│   ├── sql_client.php              # SQL Server query executor
│   ├── lock.php                    # System lock/unlock with audit
│   └── set_out_movement.php        # Material movement tracking
│
└── ── Shared Components ──
    ├── menu_include.php            # Navigation menu (standard)
    ├── menu_include_android.php    # Navigation menu (Android variant)
    └── sqlsrv_retry.php            # SQL Server connection retry handler
```

## Key Features

### Authentication & Authorization

- Credential validation against `tbl_login` in PostgreSQL
- Base64-encoded random session tokens stored in `tbl_sessions` (7-day expiry)
- Page-level access via `tbl_pageperm` — dashboard menu generated dynamically per user
- Form-level access via `tbl_permissions`

### Dynamic Form System

Forms are entirely database-driven — no hardcoded HTML:

- **Questions** defined in `tbl_questions` (formid, question text, type, order)
- **Responses** stored in `tbl_responses` (formid, questionid, answer, images)
- Supported field types: text, integer, image capture
- Images captured via device camera, converted to base64, saved as JPG

### Approval Workflow

```
Submit Form → Pending Review → Approved / Rejected → WhatsApp Notification
```

- Approvers use `utility_approval.php` dashboard
- Approval/rejection triggers WhatsApp message to submitter via AiSensy API
- Status tracked in `tbl_responses.approved`

### Docking & Invoice Processing

- `dock_invoice.php` queries SAP HANA via SQL Server linked server (`OPENQUERY(HANA112, ...)`)
- Real-time invoice data from SAP
- `dock_photo.php` captures photos during docking operations
- `empty_truck_in.php` tracks empty truck arrivals
- `gate_invoice2.php` / `gate_finalprint.php` for gate pass generation and printing

### Material Tracking

- **Outward movement:** `set_out_movement.php` marks materials as dispatched
- **Return confirmation:** `confirm_received.php` uses token-based links
- **Gate passes:** `gate_invoice2.php` generates gate entry/exit documents
- **Closure:** `close_submission.php` and `close_responses.php` finalize records

### Reporting

| Page | Description |
|---|---|
| `reporting.php` | Main dashboard with predefined SQL queries and date filters |
| `reporting_android.php` | Android-optimized layout |
| `material_out_report.php` | Generates and emails half-monthly material reports |
| `planningreport.php` | Planning and capacity analysis |

### WhatsApp Polling / Reminders

Multiple polling scripts (`polling.php` through `polling11.php`) send periodic WhatsApp reminders via AiSensy API for:
- Pending approvals
- Outstanding material returns
- Overdue submissions

## Database Schema

### PostgreSQL — `utilities` Database

| Table | Key Columns | Purpose |
|---|---|---|
| `tbl_login` | `login`, `password`, `name` | User credentials |
| `tbl_sessions` | `login`, `session_token`, `expiry` | Active sessions |
| `tbl_questions` | `id`, `formid`, `question`, `type`, `order` | Form field definitions |
| `tbl_responses` | `id`, `formid`, `questionid`, `answer`, `imagepath`, `login`, `timestamp` | Form submissions |
| `tbl_permissions` | `formid`, `login`, `access` | Form-level access control |
| `tbl_pageperm` | `login`, `page`, `link` | Page-level access control |
| `tbl_lock` | — | System lock status |
| `tbl_lock_email` | — | Lock status audit trail |
| `tbl_invoice_printing` | — | Invoice print tracking |

**Extended `tbl_responses` columns:** `approved`, `closed`, `received_back`, `out_movement`, `gate_return_receiver`

### SQL Server Databases (Read-Only)

| Database | Host | Purpose |
|---|---|---|
| `DSRLive` | dsr.jivocanola.com | Docking and invoice data |
| `JSAP` | dsr.jivocanola.com | SAP HANA integration queries |
| `Jivo_All_Branches_Live` | 103.89.45.75 | Branch-wide reporting |

## API Endpoints

All endpoints accept GET or POST and return JSON.

### Authentication

| Endpoint | Method | Parameters | Description |
|---|---|---|---|
| `check_login.php` | POST | `login`, `password` | Authenticate and get token |
| `validate_session.php` | POST | `session_token`, `login` | Verify active session |
| `logout.php` | GET | — | Destroy session |

### Forms

| Endpoint | Method | Parameters | Description |
|---|---|---|---|
| `list_forms.php` | GET | `password`, `login` | List accessible forms |
| `get_questions.php` | GET | `formid`, `password` | Get form fields |
| `submit_response.php` | POST | `formid`, answers, images (base64) | Submit form with attachments |
| `close_submission.php` | POST | `response_id` | Close a submission |

### Approvals

| Endpoint | Method | Parameters | Description |
|---|---|---|---|
| `approve_submission.php` | POST | `response_id`, `status`, `login` | Approve or reject |
| `confirm_received.php` | GET | `token` | Confirm material received |

## External Integrations

### AiSensy WhatsApp API

- **Endpoint:** `https://backend.aisensy.com/campaign/t1/api/v2`
- **Auth:** JWT bearer token
- **Templates:** `material_approve2` (approval notifications), `material_out` (dispatch alerts)
- **Phone format:** `+91XXXXXXXXXX`

### SAP HANA (via SQL Server Linked Server)

- **Linked server:** `HANA112`
- **Query method:** `OPENQUERY(HANA112, 'SELECT ...')`
- Used in `dock_invoice.php` for real-time ERP data

### PHPMailer (Email)

- **SMTP host:** `2cent.me`
- **From:** `jivo@2cent.me`
- **Usage:** Half-monthly material out reports

## PWA & Offline Support

### Manifest

- **App name:** Utilities Mobile
- **Start URL:** `/mobile/um.php`
- **Display:** standalone
- **Theme:** `#6200EE`

### Service Worker

- **Cache name:** `utilities-mobile-v1`
- **Strategy:** Network-first with cache fallback
- **Cached resources:** App pages, Bootstrap 5.3 CSS/JS (CDN), jQuery 3.6 (CDN)

## File Upload System

1. User captures image via mobile camera (or gallery in `um_power.php`)
2. JavaScript converts image to base64
3. Base64 sent via POST to `submit_response.php`
4. Server decodes and saves as `img_{UNIQID}.jpg` in `uploads/`
5. Path stored in `tbl_responses.imagepath`

## Setup

### Prerequisites

- PHP 7.4+ with extensions: `pgsql`, `sqlsrv`, `mbstring`, `json`
- PostgreSQL server with `utilities` database
- SQL Server (for DSR/SAP integration)
- Apache/Nginx web server
- PHPMailer library at `/home/phpmailer/`

### Deployment

```bash
# Place under web server document root
cp -r mobile/ /var/www/site2/mobile/

# Set upload directory permissions
mkdir -p /var/www/site2/mobile/uploads/
chmod 755 /var/www/site2/mobile/uploads/

# Configure database credentials in config.php

# Access the app
# https://yourdomain.com/mobile/um.php
```

## Navigation & Routing

No client-side router — navigation is server-rendered:

1. **Login** → `login_html.php`
2. **Dashboard** → `main_html.php` (menu built from `tbl_pageperm`)
3. **Feature pages** → Direct links to PHP files
4. **PWA entry** → `um.php` uses section-based show/hide via JavaScript

## Security Considerations

| Area | Current State | Recommendation |
|---|---|---|
| Database credentials | Hardcoded in PHP files | Move to `.env` or environment variables |
| API keys | Hardcoded JWT tokens in source | Use environment variables or secrets vault |
| SQL queries | Mix of parameterized and string interpolation | Use parameterized queries consistently |
| File uploads | No file type validation | Validate MIME type and file size |
| Upload directory | Web-accessible | Restrict direct access via server config |
| App password | Single shared password for API calls | Implement per-user API authentication |
| Session expiry | 7-day token lifetime | Consider shorter expiry with refresh tokens |
