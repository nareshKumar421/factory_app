# Notifications — Backend (Django) README

> Repo root: `C:/Users/gurpa/dev/factory_app` · Django app: `notifications`
> Paired frontend doc: [`FactoryFlow/docs/modules/notifications.md`](../../../FactoryFlow/docs/modules/notifications.md)
> (absolute: `C:/Users/gurpa/dev/FactoryFlow/docs/modules/notifications.md`)

This README is the current, code-grounded reference. The older per-topic files in this
folder (`services.md`, `signals.md`, `api.md`, `models.md`, `permissions.md`,
`commands.md`, `frontend_setup.md`) are **partly stale** — see
[Stale-doc corrections](#stale-doc-corrections) before trusting them.

---

## Overview — what it does & who uses it

The `notifications` app is the platform's **single delivery channel** for operational
alerts. It does two things:

1. **In-app notification center** — every alert is persisted as a `Notification` row so a
   user can open the bell / notifications page and see history, unread counts, and deep
   links, even if the push never arrived.
2. **Firebase Cloud Messaging (FCM) web push** — the same alert is pushed to the user's
   registered browser/PWA devices so they get a banner without polling.

It is a **service used by every other module**, not a workflow operators visit directly.
Producers (gate-in, QC, GRPO, warehouse, production, dispatch, barcode, returnable items,
stock) call `NotificationService` when something happens; the app fans the message out to
the right **Django auth group** or **permission holders**, records it, and pushes it.

Direct human users:
- **Every authenticated user** — receives notifications, manages read state, sets
  per-type preferences (API exists; see gaps), registers/unregisters devices.
- **"Notification Sender" role** (Django group, created by migration `0002`) — can compose
  and broadcast manual notifications via the send endpoints.
- **Ops/admins** — run `cleanup_stale_fcm_tokens` and inspect delivery in Django admin.

---

## Key concepts & entities

| Term | Meaning |
|---|---|
| **`UserDevice`** | One FCM token for one browser/PWA/app install. A user may have many (multi-device). `fcm_token` is **globally unique**; `is_active` gates delivery; `last_used_at` drives stale cleanup. |
| **`Notification`** | A stored, per-recipient alert (the in-app record). Carries `title`, `body`, `notification_type`, deep-link (`click_action_url`), soft reference (`reference_type` + `reference_id`), `is_read`/`read_at`, freeform `extra_data`, and `company`. |
| **`NotificationType`** | `TextChoices` enum of ~55 event codes (`GATE_ENTRY_CREATED`, `GRPO_POSTED`, `GRPO_FAILED`, `SERVICE_GRPO_FAILED`, `QC_QAM_APPROVED`, `RETURNABLE_OVERDUE`, `GENERAL_ANNOUNCEMENT`, …). Used for filtering, per-type preferences, and the SW notification `tag`. |
| **`NotificationPreference`** | Per-user opt-out for one `notification_type`. **Missing row = enabled** (opt-out model). `unique_together (user, notification_type)`. |
| **Auth group** | The primary **audience selector**. Producers target Django auth groups (`raw_material_gatein`, `grpo`, `qc_store`, `qc_chemist`, `qc_manager`, `warehouse`, `production`, `dispatch`, `barcode`, `returnable_admin`, …), not individuals. |
| **Permission audience** | Some producers (returnable items) target holders of a codename (`can_approve_returnable_gatepass`, `can_gate_out_returnable`, …) instead of a group. |
| **Data-only push** | FCM messages carry **only a `data` dict** (no FCM `notification`/`webpush` block). The frontend service worker decides how to render. |

### Data model (fields that matter)

`UserDevice` (`notifications/models.py:7`)
- `user` FK → `related_name="fcm_devices"`, `fcm_token` (`TextField`, **unique**),
  `device_type` (`WEB`/`ANDROID`/`IOS`), `device_info`, `is_active`, `created_at`,
  `last_used_at` (`auto_now`).

`Notification` (`notifications/models.py:106`)
- `recipient` FK, `company` FK (**nullable**), `title`, `body`, `notification_type`,
  `click_action_url`, `reference_type`, `reference_id` (int, nullable), `is_read`,
  `read_at`, `extra_data` (JSON), `created_at`, `created_by` FK (`SET_NULL`).
- Indexes on `(recipient, -created_at)`, `(recipient, is_read)`, `(notification_type)`.
- **Custom permissions**: `can_send_notification`, `can_send_bulk_notification`.

`NotificationPreference` (`notifications/models.py:182`)
- `user`, `notification_type`, `is_enabled` (default `True`), timestamps.

---

## End-to-end flows

### 1. Device registration (happy path)
1. Frontend obtains an FCM token after the user grants browser notification permission.
2. `POST /api/v1/notifications/devices/register/` `{fcm_token, device_type, device_info}`.
3. `NotificationService.register_device` (`services.py:41`) **deletes the token if it
   belongs to another user** (`.exclude(user=user).delete()`), then `update_or_create`s
   the row for this user with `is_active=True`.
4. Returns `{message, device_id}` (`201`).

### 2. A workflow event produces a notification (the common case)
Example: raw-material gate entry created (`raw_material_gatein/notifications.py:57`).
1. Business code saves the domain record (e.g. `VehicleEntry`).
2. The producer calls a `notify_*` helper, which wraps delivery in
   **`transaction.on_commit(...)`** so nothing sends until the DB commit succeeds.
3. On commit, `NotificationService.send_notification_by_auth_group("raw_material_gatein", …)`
   (`services.py:345`) resolves the audience: active users in that group, optionally
   filtered to the event's company.
4. For **each** user, `send_notification_to_user` (`services.py:158`):
   - checks `is_notification_enabled` — if the user disabled this type, **skips entirely**
     (no DB row, no push, returns `None`);
   - otherwise creates a `Notification` row;
   - loads the user's `is_active` device tokens and calls `_send_to_tokens`.
5. `_send_to_tokens` (`services.py:112`) builds a **data-only** `messaging.Message`
   (`_build_fcm_message`, `services.py:80`) and calls `messaging.send()` **per token,
   sequentially**. Failures are collected; `UnregisteredError` deactivates the token.

### 3. QC workflow fan-out (with mandatory gate CC)
`quality_control/signals.py` uses `post_save`/`pre_save` on `MaterialArrivalSlip` and
`RawMaterialInspection`. Each transition targets the next actor's group **and always adds
`raw_material_gatein`** via `send_notification_by_auth_groups` (`signals.py:47`), so gate
users track their material through QC. Routing:
- slip submitted → `qc_store` (+ gate)
- slip sent back → `raw_material_gatein`
- inspection submitted → `qc_chemist` (+ gate)
- chemist approved → `qc_manager` (+ gate)
- QAM accepted → `grpo` (+ gate); HOLD → `qc_store`; REJECTED → `qc_store` (+ gate)

### 4. SAP result notifications (posted / failed)
GRPO, Service GRPO, FG receipt, production-run, and intercompany-transfer postings notify
the relevant group on **both success and failure**. On SAP rejection the producer passes
the SAP error into the body — e.g. `notify_material_grpo_failed` / `notify_service_grpo_failed`
(`grpo/notifications.py:72,122`) send `GRPO_FAILED` / `SERVICE_GRPO_FAILED` to the `grpo`
group with a deep link back to the preview/retry screen.

### 5. Manual send (admin)
1. `POST /api/v1/notifications/send/` (needs `can_send_notification` + company context).
2. `SendNotificationAPI` (`views.py:321`): if `recipient_user_ids` given → send to those
   users; else `send_bulk_notification` to all active `UserCompany` users in the caller's
   company, optionally filtered by `role_filter` (a `UserRole.name`).
3. `send-by-permission/` and `send-by-group/` (both need `can_send_bulk_notification`) fan
   out to permission holders / an auth group.

### 6. Read + list lifecycle
- `GET /api/v1/notifications/` filters by `recipient=request.user`, then by the
  `Company-Code` header (`company.code == X` **OR** `company IS NULL`), `is_read`, `type`.
  Supports **both** `page`/`page_size` and `limit`/`offset` pagination and returns
  `results, count, total_count, unread_count, page, page_size, limit, offset`.
- `GET /api/v1/notifications/<id>/` returns one and **marks it read** as a side effect.
- `POST /api/v1/notifications/mark-read/` marks listed ids, or **all unread** if body empty.

### 7. Cleanup
`python manage.py cleanup_stale_fcm_tokens [--days 30]` →
`NotificationService.cleanup_stale_tokens` deletes `UserDevice` rows with
`last_used_at` older than N days. Intended as a scheduled job.

---

## Critical business rules & invariants

- **In-app record survives push failure.** The `Notification` row is created before FCM is
  attempted, and FCM errors are swallowed inside `_send_to_tokens`. If Firebase is down the
  row still commits — the bell works, the banner doesn't. (See failure modes.)
- **Preference is a hard gate, not just push.** A disabled `notification_type` skips the
  whole `send_notification_to_user` — **no in-app row either**. Missing preference = enabled.
- **Notifications never break business flows.** Producers wrap sends in
  `transaction.on_commit` and/or `try/except` (`returnable_items/notifications.py:40`,
  `quality_control/signals.py:130`). A notification failure must never roll back the gate
  entry / GRPO / transition that triggered it.
- **Audience = group/permission, resolved from the record's company.** Sends are scoped to
  the producing record's `company`; the caller's company context is used for manual sends.
  This is the cross-company boundary: reads use the `Company-Code` header, writes resolve
  company from the source record.
- **Company-null notifications are visible in every company context** (the list filter is
  `company__code == X OR company IS NULL`). Producers should set `company` to avoid leaking
  an alert across a user's sibling companies.
- **Token uniqueness / reassignment.** `fcm_token` is globally unique; registering a token
  already owned by another user **moves** it (shared kiosk / device handoff safety).
- **Dedup across groups.** `send_notification_by_auth_groups` uses `.distinct()`, so a user
  in several targeted groups is notified once per event.
- **Send permissions.** `send/` requires `can_send_notification`; `send-by-permission/` and
  `send-by-group/` require `can_send_bulk_notification`. All three also require
  `HasCompanyContext`.

---

## Integrations & cross-module boundaries

- **Firebase Admin SDK** — `get_firebase_app()` (`services.py:21`) lazily initializes a
  singleton from `settings.FCM_CREDENTIALS_PATH` (`config/settings.py:427`, default
  `firebase-service-account.json`, resolved against `BASE_DIR`). Missing file →
  `FileNotFoundError`, caught by `_send_to_tokens` and reported as an all-failed result.
- **`company` app** — `Notification.company` FK; `send_bulk_notification` reads
  `UserCompany`; company scoping on `by_permission`/`by_group`.
- **`accounts` / auth** — recipients are `AUTH_USER_MODEL`; audiences are Django auth
  groups and permissions.
- **SAP (indirect)** — the app carries SAP outcomes but never calls SAP. Relevant types:
  `GRPO_POSTED/FAILED`, `SERVICE_GRPO_POSTED/FAILED`, `FG_RECEIPT_POSTED/FAILED`,
  `PRODUCTION_RUN_SAP_POSTED/FAILED`, `INTERCOMPANY_TRANSFER_COMPLETED/FAILED`. A `(2000xx)`
  SAP transaction-notification rejection surfaces to operators **only** through the
  corresponding `*_FAILED` notification body.
- **Producer apps (upstream)** — each owns its `notifications.py` (or `signals.py`):
  `raw_material_gatein`, `quality_control`, `grpo`, `warehouse`, `production_execution`,
  `dispatch_plans`, `barcode`, `daily_needs_gatein`, `maintenance_gatein`,
  `construction_gatein`, `person_gatein`, `returnable_items`, `stock_dashboard`,
  `docking_admin`.
- **Frontend (downstream)** — consumes `/api/v1/notifications/*`; FCM plumbing lives in
  `FactoryFlow/src/core/notifications` and `public/firebase-messaging-sw.js`. See the
  paired frontend doc.

---

## Real-world edge cases

| # | Trigger | Current behaviour | Operator-visible symptom | Risk / gap |
|---|---|---|---|---|
| 1 | **FCM credentials missing / Firebase unreachable** during a send | `get_firebase_app()` raises → `_send_to_tokens` returns `{success:0, failure:len, error}`; the `Notification` **row still commits** | Bell/list keep working; **no push banners** arrive on any device | Silent for push; only logs show it. No alerting that push is globally down. |
| 2 | **Token expired / app uninstalled** (`messaging.UnregisteredError`) | That `UserDevice` is set `is_active=False`; other devices still get the push | User on that one device stops getting banners | Row lingers `is_active=False` until 30-day cleanup; re-register on next login fixes it. |
| 3 | **User disabled a notification type** | `send_notification_to_user` returns `None` early — **no row, no push** | User never sees the event even in the in-app center | Opt-out silently drops in-app history too; a user may miss a critical `*_FAILED` alert they muted. |
| 4 | **Bulk send to a large group** | `_send_to_tokens` calls `messaging.send()` **once per token, sequentially, inside the request** | Slow `send/` response; long-held DB transaction on `send_notification_to_user` | No batching (`send_each`/multicast) and no async queue → O(devices) round-trips. Perf hotspot. |
| 5 | **Producer forgets `company=`** | Row saved with `company=NULL` | Alert shows up for the recipient in **every** company they can switch to | Cross-company leak of an alert; matches the "blank/leak in sibling company" class of bugs. |
| 6 | **Shared device, second user logs in & registers same token** | `register_device` deletes the first user's `UserDevice` and reassigns | First user stops receiving push on that device | Correct handoff, but the first user isn't told their device was unregistered. |
| 7 | **SAP rejects a GRPO/Service GRPO** | Producer calls `notify_*_grpo_failed` with the SAP error in the body; `grpo` group gets a `*_FAILED` with a retry deep link | GRPO team sees "GRPO Posting Failed: <SAP error>" and can retry | Delivery depends on users being in the `grpo` group and not having muted the type. |
| 8 | **Duplicate save signals** (QC slip/inspection re-saved) | `pre_save` snapshots prior state; handlers no-op if the relevant status/flag didn't actually change (`signals.py:104,144,217`) | No duplicate QC notifications | Guard is per-field; an unrelated field change that coincides with a status change can still re-fire in rare paths. |
| 9 | **`on_commit` never fires** (outer transaction rolled back) | The queued send is discarded | No notification for an event that also didn't persist | Correct-by-design, but if the business save is retried the notify must be re-queued by the retry path. |

---

## Failure modes / what can break

- **Global push outage** (bad/rotated service-account file, Firebase project disabled):
  every send logs `FCM is not configured or unavailable` (`services.py:126`); operators
  keep seeing in-app notifications but **no banners** — easy to miss because nothing errors
  in the UI.
- **Serial-send latency**: a broadcast to a big company/group can make `send/` (or the
  triggering request, since QC/gate producers fan out on commit) noticeably slow. On a
  shared PG box this can compound with other load.
- **Muted critical alerts**: because preferences gate the in-app row too, a user who muted
  `GRPO_FAILED` / `RETURNABLE_OVERDUE` gets **no trace** of it.
- **Stale/duplicated device tokens**: without the scheduled `cleanup_stale_fcm_tokens`,
  `UserDevice` accumulates dead tokens; each still costs one `messaging.send()` attempt per
  notification until it errors.
- **Send-permission misconfig**: `send-by-group/` accepts any group name; a wrong or empty
  group name silently notifies zero users and returns `recipients_count: 0` (no error).

---

## Improvement opportunities & known gaps

- **Batch/parallelize FCM** — use `messaging.send_each`/multicast or an async worker
  (Celery) instead of a per-token loop inside the request/transaction (edge cases 4 & fail-2).
- **Move FCM out of `@transaction.atomic`** on `send_notification_to_user` — network I/O
  inside a DB transaction holds locks longer than needed.
- **Decouple in-app from push in preferences** — let a user silence *push* for a type while
  still keeping the in-app record (today muting drops both — edge 3).
- **Push-health signal** — surface a system alert (or admin badge) when
  `get_firebase_app()` fails, so a global outage isn't invisible (fail-1).
- **Default `company` guard** — assert/scope `company` on create to prevent null-company
  cross-company leaks (edge 5).
- **Delete this folder's stale docs** or fold them in — see next section.

### Stale-doc corrections
- `signals.md` describes a `notifications/signals.py` with a `_get_company_users` helper
  that notifies **all company users**. **That file does not exist.** Triggers now live in
  each producer app's `notifications.py`/`signals.py` and target **auth groups /
  permissions**, delivered via `transaction.on_commit`.
- `services.md` shows FCM messages built with `notification=` and `webpush=` blocks. The
  current `_build_fcm_message` (`services.py:80`) sends a **data-only** message; the browser
  service worker renders the banner.

---

## Permissions & roles

| Capability | Gate | Endpoint(s) |
|---|---|---|
| Receive / list / read own notifications | `IsAuthenticated` | `GET /`, `GET /<id>/`, `mark-read/`, `unread-count/` |
| Manage own preferences | `IsAuthenticated` | `GET/POST preferences/` |
| Register / unregister own device | `IsAuthenticated` | `devices/register/`, `devices/unregister/` |
| Send test push to a token | `IsAuthenticated` | `test/` |
| Send manual (specific users or company broadcast) | `can_send_notification` + `HasCompanyContext` | `send/` |
| Send by permission / by group | `can_send_bulk_notification` + `HasCompanyContext` | `send-by-permission/`, `send-by-group/` |

- Migration `0002_create_notification_groups` seeds the **"Notification Sender"** auth group
  with both custom permissions.
- Frontend nav/route gating mirrors these codenames
  (`NOTIFICATION_PERMISSIONS.SEND` / `SEND_BULK`) — see the paired frontend doc.

### API surface (all under `/api/v1/notifications/`, `notifications/urls.py`)
```
POST devices/register/      register FCM token
POST devices/unregister/    remove FCM token (logout)
GET  /                       list (filters: is_read, type, Company-Code header; page|limit)
GET  <id>/                   retrieve + mark read
POST mark-read/              {notification_ids:[…]} or {} for all
GET  unread-count/           {unread_count}
GET/POST preferences/        list all types / update one (by notification_type or *_id)
POST test/                   push-only to one token (503 on failure)
POST send/                   manual send (can_send_notification)
POST send-by-permission/     fan-out to permission holders (can_send_bulk_notification)
POST send-by-group/          fan-out to an auth group  (can_send_bulk_notification)
```

---

## Developer file map

**Backend (`C:/Users/gurpa/dev/factory_app`)**
- `notifications/models.py` — `UserDevice`, `Notification`, `NotificationType`, `NotificationPreference`.
- `notifications/services.py` — `NotificationService` (register/cleanup/send*), `get_firebase_app`, `_build_fcm_message` (data-only), `_send_to_tokens`.
- `notifications/views.py` — all `APIView`s (device, list/detail/mark-read/unread-count, preferences, send/send-by-permission/send-by-group, test).
- `notifications/serializers.py` — request/response serializers.
- `notifications/permissions.py` — `CanSendNotification`, `CanSendBulkNotification`.
- `notifications/urls.py` — route table.
- `notifications/admin.py` — read-only admin for `UserDevice` and `Notification`.
- `notifications/management/commands/cleanup_stale_fcm_tokens.py` — stale-token purge.
- `notifications/migrations/0002_create_notification_groups.py` — seeds "Notification Sender".
- `notifications/tests.py` — API + workflow-fan-out tests (good source of expected routing).
- `config/settings.py:427` — `FCM_CREDENTIALS_PATH`.
- **Producers**: `raw_material_gatein/notifications.py`, `quality_control/signals.py`,
  `grpo/notifications.py`, `warehouse/notifications.py`, `production_execution/notifications.py`,
  `dispatch_plans/notifications.py`, `barcode/notifications.py`, `daily_needs_gatein/notifications.py`,
  `maintenance_gatein/notifications.py`, `construction_gatein/notifications.py`,
  `person_gatein/notifications.py`, `returnable_items/notifications.py`, `stock_dashboard/jobs.py`,
  `docking_admin/services.py`.

**Frontend (`C:/Users/gurpa/dev/FactoryFlow`)** — consumers of this API
- `src/core/notifications/*` — FCM service, notification API service, hooks, bell/gate/prompt.
- `src/core/store/slices/notification.slice.ts` — Redux thunks/state.
- `src/app/providers/NotificationProvider.tsx` — login→setup→register / foreground handling.
- `src/config/firebase.config.ts`, `public/firebase-messaging-sw.js` — Firebase init + SW.
- `src/modules/notifications/*` — the two user-facing pages.

---

## Related docs
- **Paired frontend doc**: [`FactoryFlow/docs/modules/notifications.md`](../../../FactoryFlow/docs/modules/notifications.md)
  (`C:/Users/gurpa/dev/FactoryFlow/docs/modules/notifications.md`)
- Legacy (partly stale, read with the corrections above): `services.md`, `signals.md`,
  `api.md`, `models.md`, `permissions.md`, `commands.md`, `frontend_setup.md` in this folder.
- Related memory: SAP transaction-notification validations (`(2000xx)` GRPO rejections);
  cross-company flow boundary (reads need company context, writes resolve company from the record).
