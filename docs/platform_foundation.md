# Platform / Foundation (Backend)

> Apps: `accounts`, `company`, `sap_client`, `ai_assistant`
> Audience: new developers + technical managers.
> This doc is grounded in the code as of this revision. Where older docs disagree, trust this / the code.
> Paired frontend doc: `C:/Users/gurpa/dev/FactoryFlow/docs/modules/platform.md`

---

## Overview — what it does & who uses it

The platform/foundation apps are the cross-cutting plumbing every other module sits on:

- **`accounts`** — the custom email-based `User` model, JWT login / refresh / change-password, and the `/me` identity + permission feed. Owns *who you are* and *what you can do*.
- **`company`** — multi-company support. A user belongs to one or more `Company` rows (JIVO_OIL, JIVO_MART, JIVO_BEVERAGES…) each with a descriptive `UserRole`. The `HasCompanyContext` permission turns the `Company-Code` request header into a bound company for the request. Owns *which company you are acting as*.
- **`sap_client`** — the single gateway to SAP Business One. Given a company code it resolves that company's SAP HANA schema + Service Layer credentials, then reads (HANA SQL) or writes (Service Layer REST) documents. Owns *the SAP boundary*.
- **`ai_assistant`** — the global read-only "Factory AI" assistant (Google Gemini). Answers operator questions from ORM context, deterministic counts, or guarded read-only SQL. Owns *natural-language querying*.

Who uses it: **every authenticated user** hits `accounts` + `company` on every request. **Gate / GRPO / dispatch / production / warehouse** operators indirectly drive `sap_client`. The **AI widget** is available to all authenticated users; its SQL power is gated by a dedicated permission.

URL prefixes (see `config/urls.py`):

| App | Prefix | Notes |
|---|---|---|
| accounts | `/api/v1/accounts/` | login, token refresh, me, change-password, users, departments |
| company | `/api/v1/company/` | `companies/` CRUD viewset |
| sap_client | **`/api/v1/po/`** | open POs, PO items, create GRPO, warehouses, vendors (note the `po` prefix, not `sap`) |
| ai_assistant | `/api/v1/ai/` | `assistant/chat/` |

---

## Key concepts & entities

### accounts
- **`User`** (`accounts/models.py`) — `AbstractBaseUser` + `PermissionsMixin`. `USERNAME_FIELD = "email"` (no username field). Unique `email` and `employee_code`. `is_active`, `is_staff`, plus inherited `is_superuser`, `groups`, `user_permissions`. Created via `UserManager` (`accounts/managers.py`).
- **`Department`** — flat name/description list (`accounts/urls.py` → `departments/`). Not tied to permissions.
- **Permissions come from `get_all_permissions()`** — direct user perms **+** group perms **+** (for superusers) **every** permission in the system. This is the single source of truth the frontend gates on.

### company
- **`Company`** — `name`, unique `code` (the code is the join key to SAP; e.g. `JIVO_OIL`), `is_active`.
- **`UserRole`** — free-text role label (`Admin`, `QC`, `Store`…). **Descriptive metadata only** — it is *not* the access-control mechanism (Django permissions are). Do not confuse `UserCompany.role` with authorization.
- **`UserCompany`** — the membership row: `user` × `company` × `role`, plus `is_default` and `is_active`. `unique_together = (user, company)`.
- **`HasCompanyContext`** (`company/permissions.py`) — the DRF permission that reads the `Company-Code` header, looks up the caller's active `UserCompany` for that code, and attaches it as `request.company`. Downstream code reads `request.company.company.code`.

### sap_client
- **`SAPClient(company_code)`** (`sap_client/client.py`) — the one public entry point. Every method builds a fresh reader/writer bound to a `CompanyContext`.
- **`CompanyContext`** (`context.py`) → **`COMPANY_SAP_REGISTRY`** (`registry.py`) — maps a company code to two credential blocks: `hana` (host/port/user/password/**schema**) and `service_layer` (base_url/**company_db**/username/password). Schemas/DBs come from `settings.COMPANY_DB` (env `COMPANY_DB_JIVO_OIL` etc.).
- **Two physical channels:**
  - **HANA (reads)** — `sap_client/hana/*` via `hdbcli`. Direct SQL against the company schema. `HanaConnection` sets `connectTimeout=15s`, `communicationTimeout=60s` so a dead SAP fails fast instead of hanging a worker.
  - **Service Layer (writes)** — `sap_client/service_layer/*` via REST `…/b1s/v2/…`. `ServiceLayerSession.login()` posts `CompanyDB/UserName/Password` and returns session cookies. **`verify=False`** (TLS not verified).
- **DTOs** (`dtos.py`) — typed carriers (`PODTO`, `GRPORequestDTO`, `WarehouseDTO`, `ProductionOrderDTO`…).
- **Exceptions** (`exceptions.py`) — `SAPValidationError`, `SAPConnectionError`, `SAPDataError`. Views map these to HTTP 400 / 503 / 502.

### ai_assistant
- **`FactoryAssistantService`** (`ai_assistant/services.py`, ~2000 lines) — the whole assistant. Company-scoped, read-only.
- **`AIAssistantAccess`** — unmanaged model whose only purpose is to carry the permission **`ai_assistant.can_query_factory_database`** (gates the free-form SQL path).
- **`AIAssistantInteraction`** — audit row for every question: user, company, question, page, mode, provider, model, status (`SUCCESS`/`ERROR`/`BLOCKED`), generated SQL, validation status, blocked reason, row count, latency.

---

## End-to-end flows

### 1. Login (happy path)
1. `POST /api/v1/accounts/login/` → `LoginView` (`TokenObtainPairView`) → `LoginSerializer`.
2. SimpleJWT validates email+password, issues `access` + `refresh`.
3. `LoginSerializer.validate()` enriches the response with the user's **active** companies (`UserCompany.filter(is_active=True)` → id, name, code, role, is_default), plus `token.access_expires_in` / `refresh_expires_in` (seconds) and a `user` block.
4. Client stores tokens + companies and moves to company selection.

### 2. Establish company context (every subsequent request)
1. Client sends `Authorization: Bearer <access>` **and** `Company-Code: JIVO_OIL`.
2. `IsAuthenticated` validates the JWT; `HasCompanyContext` reads the header, fetches the matching active `UserCompany`, sets `request.company`.
3. View logic reads `request.company.company.code` to scope data / pick the SAP schema.

### 3. Fetch identity + permissions
1. `GET /api/v1/accounts/me/` → `MeView` → `MeSerializer`.
2. Returns the full user, active companies, and the sorted union of `get_all_permissions()`.
3. Client caches this; nav + route guards are computed from `permissions`.

### 4. Token refresh
1. `POST /api/v1/accounts/token/refresh/` → `CustomTokenRefreshView`.
2. SimpleJWT rotates the token (`ROTATE_REFRESH_TOKENS=True`, `BLACKLIST_AFTER_ROTATION=True` — the old refresh is blacklisted via the installed `token_blacklist` app).
3. The view re-appends `token.access_expires_in` / `refresh_expires_in` so the client can recompute expiry.

### 5. SAP read (e.g. open POs for a supplier)
1. `GET /api/v1/po/open-pos/?supplier_code=…` → `OpenPOListAPI` (`IsAuthenticated` + `HasCompanyContext`).
2. `SAPClient(company_code=request.company.company.code)` → `HanaPOReader` → SQL against that company's HANA schema.
3. Success → serialized DTOs. `SAPConnectionError` → **503**; `SAPDataError` → **502**.

### 6. SAP write (create GRPO / Purchase Delivery Note)
1. `POST /api/v1/po/grpo/` → `CreateGRPOAPI`. Payload validated by `GRPORequestSerializer` (CardCode + DocumentLines required).
2. `SAPClient.create_grpo()` → `GRPOWriter` logs into the Service Layer, POSTs `…/b1s/v2/PurchaseDeliveryNotes`.
3. `201` → serialized response. SAP `400` → `SAPValidationError` → **400** with the SAP message. Connection issues → `SAPConnectionError` → **503**. Other → `SAPDataError` → **502**.

### 7. AI assistant answer (`POST /api/v1/ai/assistant/chat/`)
`AssistantChatAPI` (`IsAuthenticated` + `HasCompanyContext`) → `FactoryAssistantService.answer()` runs a **3-tier cascade**, all company-scoped, and audits the result:
1. **Build context** — token-search ~12 operational sections (gate entries, security checks, raw-material POs, daily needs, maintenance, construction, GRPO postings, QC, weighment, production, warehouse, barcode inventory) + barcode boxes/pallets/print logs + optional pending-GRPO + optional `*.md` docs. Every queryset is filtered to the current company.
2. **Direct answer** — deterministic "how many / count" answers straight from the ORM aggregates (no LLM). Pending-GRPO counts require `grpo.can_view_pending_grpo`.
3. **Database SQL path** — only if the question looks analytical **and** the user has `ai_assistant.can_query_factory_database`. Gemini generates read-only SQL against a whitelisted schema; the SQL is validated + wrapped with `LIMIT 50` + executed on the read-only alias; a second Gemini call summarizes the rows.
4. **General Gemini** — falls back to a short, read-only, context-grounded answer.
Config errors → **503** `ai_not_configured`; provider errors → **502** `ai_provider_error`.

---

## Critical business rules & invariants

1. **Two gates on company-scoped endpoints.** A request must pass **both** `IsAuthenticated` (valid JWT) **and** `HasCompanyContext` (valid `Company-Code` → active `UserCompany`). Missing header → `PermissionDenied("Company-Code header is missing.")`; no membership → `PermissionDenied("You do not have access to any companies.")`. Both surface as **403**.
2. **The company code is the SAP routing key.** `request.company.company.code` selects the HANA schema + Service Layer `CompanyDB` via `COMPANY_SAP_REGISTRY`. A code not in the registry raises `SAPValidationError` — a `Company` can exist in Django yet be un-routable to SAP.
3. **SAP writes are single-company and synchronous.** Each writer logs into exactly one company's Service Layer per call. There is no cross-company posting.
4. **`get_all_permissions()` is the authorization source.** Group perms + direct perms are merged; **superusers implicitly hold every permission** (so they see every module/data set). Changing a group's perms changes effective access for all its members.
5. **`UserCompany.role` is not authorization.** It is a label. Do not gate features on it server-side; gate on Django permissions.
6. **The AI assistant is strictly read-only.** The SQL validator (`_validate_read_only_sql`) rejects anything but a single `SELECT`/`WITH`: forbidden-keyword regex (insert/update/delete/drop/alter/grant/…), no comments, no multiple statements, sensitive-column block (password/token/secret/session/…), allowed-table whitelist, and — for company-scoped tables — a mandatory current-company filter. Execution runs inside `SET TRANSACTION READ ONLY` + `statement_timeout = 10000ms` on `AI_ASSISTANT_SQL_DATABASE_ALIAS`, wrapped in `LIMIT 50`.
7. **AI free-form SQL requires an explicit permission.** Without `ai_assistant.can_query_factory_database`, the SQL tier is skipped silently and the assistant answers from context only.
8. **Login only exposes active memberships.** `is_active=False` `UserCompany` rows never appear in the login/`me` company list.
9. **JWT lifetimes** (`config/settings.py`): access = **1500 minutes (25 h)**, refresh = **7 days**, rotate + blacklist on refresh.

---

## Integrations & cross-module boundaries

- **SAP Business One** — the only integration `sap_client` exists for. HANA (read) and Service Layer (write) are separate hosts/credentials per company. Callers across the codebase (gate_core dispatch, grpo, marketplace, production_execution, warehouse, dashboards) all funnel through `SAPClient`.
- **Cross-company boundary** — `HasCompanyContext` binds a request to **one** company via the header. This is the mechanism behind the "reads need the right company, writes resolve company from the record" rule in team memory: if the caller sends the wrong/stale `Company-Code`, company-scoped reads return another company's data or blank. Writes into SAP always target the header's company.
- **Permissions ↔ frontend nav** — `accounts` `/me` feeds the permission list the React sidebar gates on. **Changing a Django group's permissions alone can hide or show whole frontend modules** — the sidebar keys off permission *prefixes*, not group membership. (See team memory: "Group perms vs frontend nav gating".)
- **AI ↔ everything** — `FactoryAssistantService` imports models from a dozen apps (barcode, grpo, quality_control, production_execution, warehouse, driver_management, weighment…). Its read-only SQL path can read almost any managed, company-scoped table (auth/admin/session/token tables are excluded).
- **Notifications** — logout on the client also unregisters FCM devices (frontend concern; see paired doc).

---

## Real-world edge cases

Each: trigger → current behaviour → operator-visible symptom → risk/gap.

1. **Missing / stale `Company-Code` header** — trigger: client lost its cached current company. → Backend raises 403 "Company-Code header is missing." (The frontend interceptor normally auto-restores the default active company first, so this is rare.) → Symptom: a red error toast, or — worse — data for the *default* company when the user meant another. → Risk: silent wrong-company reads ("blank in sibling company").
2. **Company exists in Django but not in `COMPANY_SAP_REGISTRY`** — trigger: a new `Company` row added without wiring env + registry. → `get_company_config` raises `SAPValidationError`. → Any SAP screen (POs, GRPO, warehouses) errors for that company. → Risk: onboarding a 4th company needs code changes, not just data.
3. **SAP HANA unreachable during a read** — trigger: SAP box down / network. → `HanaConnection` times out (15 s connect) → `SAPConnectionError` → **503** "SAP system is currently unavailable." → Operator sees "try again later." → Risk: none data-wise; the fail-fast timeout protects workers.
4. **Service Layer rejects a GRPO post** — trigger: SAP business validation (e.g. item-group 105 gross-weight mandatory, error `(200032)`). → `GRPOWriter` catches SAP `400` → `SAPValidationError` → **400** with the raw SAP message. → Operator sees the SAP rejection text. → Risk: message is SAP-speak; these come from SAP's `SBO_SP_TransactionNotification`, not our code (see team memory).
5. **Partial / duplicate SAP write on flaky network** — trigger: connection drops after SAP created the document but before the 201 is read. → We raise `SAPConnectionError`/`SAPDataError`; the caller may retry. → Symptom: operator retries and may create a duplicate GRPO. → Risk: no idempotency key at this layer; dedupe lives in calling modules (grpo).
6. **AI question needs another company's data** — trigger: "compare JIVO_OIL vs JIVO_MART". → SQL validator raises "Company-scoped queries must filter the current company." → status `BLOCKED`; assistant falls back / says it can't. → Risk: expected; cross-company analytics is intentionally impossible here.
7. **AI user lacks `can_query_factory_database`** — trigger: normal operator asks an analytical question. → SQL tier skipped; audit `validation_status='permission_denied'`. → Assistant still answers from ORM context/counts. → Risk: user may not realize the deep query was skipped.
8. **Gemini key missing / quota exhausted / timeout** — trigger: `GEMINI_API_KEY` empty, or 429, or slow. → `AssistantConfigError` → **503** `ai_not_configured`; provider issues → **502** `ai_provider_error` with a human message (rate limit, DNS failure, key rejected). Model fallback list is tried first on 429/5xx/UNAVAILABLE. → Operator sees a friendly assistant bubble. → Risk: assistant depends on outbound internet + Google.
9. **Refresh token expired (7 days idle)** — trigger: user away a week. → Refresh returns 401. → Client clears session, redirects to login. → Symptom: re-login required. → Risk: none.
10. **Group permission changed while user is logged in** — trigger: admin edits a Django group. → `/me` reflects it on next fetch; frontend refreshes permissions periodically. → Symptom: modules appear/disappear minutes later or on reload. → Risk: a *revoked* permission stays effective in the cached client for up to the refresh interval.

---

## Failure modes / what can break

- **SAP down / slow** → 502/503 on any SAP screen; operators blocked from posting. Notice: "SAP system is currently unavailable."
- **Wrong company env mapping** (`COMPANY_DB_*` points at the wrong schema) → reads/writes hit the wrong SAP company **silently** — most dangerous class; no error, just wrong data.
- **`verify=False` on Service Layer login/post** → TLS is not verified; a MITM on the SAP path is undetected. Notice: none (by design, silent).
- **No HANA connection pooling** → each read opens a fresh `hdbcli` connection; under load this adds latency (bounded by the timeouts).
- **AI SQL alias falls back to `default`** → if `AI_DB_NAME` is unset, `AI_ASSISTANT_SQL_DATABASE_ALIAS` defaults to `default`, so AI queries run on the **primary** DB (still read-only + 10 s timeout). A pathological query adds load to production. Notice: possible slow-query blips.
- **Gemini/internet outage** → assistant returns 502/503; rest of the app is unaffected.
- **`/me` permission fetch failing repeatedly** → client keeps a stale (possibly empty) permission set; modules silently missing. Notice: "my menu disappeared."

---

## Improvement opportunities & known gaps

- **No server-side logout / refresh revocation.** There is no `/accounts/logout/` route (only login, token/refresh, me, change-password, users, departments). The frontend even defines `AUTH.LOGOUT: '/accounts/logout/'` but nothing serves it — logout is client-side only. A stolen refresh token stays valid until it expires or is rotated. Consider a blacklist-on-logout endpoint (the `token_blacklist` app is already installed).
- **Broad list endpoints.** `UserListView` (`/accounts/users/`) and `DepartmentListView` (`/accounts/departments/`) require only `IsAuthenticated` — any logged-in user can enumerate all users, and `POST` a new department. No `HasCompanyContext`, no model perms.
- **`CompanyViewSet` is full CRUD.** `create/update/delete` of companies is exposed to anyone passing `DjangoModelPermissionsOrAnonReadOnly` + a valid company context. Company master data is sensitive; consider tightening.
- **Hardcoded 3-company registry.** Adding a company requires editing `registry.py` + `settings.COMPANY_DB` + env. No DB-driven SAP config.
- **`company/permissions.py` is marked "Not Final".** Works (relies on `unique_together`), but has a stray broad `except`/messaging mismatch ("You do not have access to any companies" for a single-code miss).
- **AI document search cost.** For "how-to/guide" questions the service walks up to 250 `*.md` files under `BASE_DIR` on each call. Fine now; would benefit from an index if docs grow.
- **AI SQL on primary DB by default.** Wire a dedicated read-replica (`AI_DB_*`) in every environment so heavy AI queries never touch the write primary.

---

## Permissions & roles (who sees / does what)

- **Authorization = Django permissions** (`user_permissions` + `groups`), surfaced via `/me` `get_all_permissions()`. This — not `UserCompany.role` — is what the app enforces and what the sidebar gates on.
- **Superuser** → every permission → every module + full AI SQL.
- **`ai_assistant.can_query_factory_database`** → unlocks the assistant's free-form read-only SQL tier. Without it, the assistant is limited to context + deterministic counts.
- **`grpo.can_view_pending_grpo`** → lets the assistant include pending-GRPO context.
- **`UserCompany.role`** (`Admin`/`QC`/`Store`/…) → shown in the UI (profile, company picker) and available to the frontend's `hasCompanyRole` helper, but the backend does not authorize on it.
- **Nav gating consequence (CRITICAL):** because the frontend sidebar shows a module when the user has **any** permission under that module's app prefix, editing a group's permission set can add/remove entire modules from a user's menu without touching the user directly.

---

## Developer file map

### Backend (this repo)
- `accounts/models.py` — `User`, `Department`.
- `accounts/managers.py` — `UserManager` (email-based create_user/superuser).
- `accounts/serializers.py` — `LoginSerializer` (companies + token expiry), `MeSerializer` (`get_all_permissions`), `ChangePasswordSerializer`, `UserSerializer`, `DepartmentSerializer`.
- `accounts/views.py` — `LoginView`, `CustomTokenRefreshView`, `MeView`, `ChangePasswordView`, `UserListView`, `DepartmentListView`.
- `accounts/urls.py` — routes under `/api/v1/accounts/`. `accounts/admin.py` — rich User/Department admin.
- `company/models.py` — `Company`, `UserRole`, `UserCompany`.
- `company/permissions.py` — **`HasCompanyContext`** (the Company-Code gate).
- `company/views.py` / `serializers.py` / `urls.py` — `CompanyViewSet`. `company/admin.py` — company/role/membership admin. `company/README.md`.
- `sap_client/client.py` — **`SAPClient`** facade (all read/write methods).
- `sap_client/context.py` + `registry.py` — company → SAP config (`COMPANY_SAP_REGISTRY`).
- `sap_client/hana/connection.py` — `HanaConnection` (timeouts). `sap_client/hana/*_reader.py` — PO/GRPO/warehouse/vendor/stock-transfer/service-GRPO-options readers.
- `sap_client/service_layer/auth.py` — `ServiceLayerSession.login()`. `…/grpo_writer.py`, `ap_invoice_writer.py`, `delivery_note_writer.py`, `production_order_writer.py`, `attachment_writer.py`.
- `sap_client/dtos.py`, `exceptions.py`, `serializers.py`, `views.py`, `urls.py` (under `/api/v1/po/`), `sap_client/README.md`, `sap_client/docs/`.
- `ai_assistant/services.py` — `FactoryAssistantService` (context, direct answers, guarded SQL, Gemini calls, audit).
- `ai_assistant/models.py` — `AIAssistantAccess` (permission carrier), `AIAssistantInteraction` (audit).
- `ai_assistant/views.py` / `serializers.py` / `urls.py` (`/api/v1/ai/assistant/chat/`).
- `config/settings.py` — `AUTH_USER_MODEL`, `SIMPLE_JWT`, CORS (`Company-Code` header allowed), `COMPANY_DB`, HANA/SL creds, `GEMINI_*`, `AI_ASSISTANT_*`, optional `ai_readonly` DB.
- `config/urls.py` — mounts all app URL prefixes.

### Frontend (paired repo — see the frontend doc for detail)
- `C:/Users/gurpa/dev/FactoryFlow/src/modules/auth/*` — login / company-select / loading / profile screens.
- `C:/Users/gurpa/dev/FactoryFlow/src/core/auth/*` — token store (IndexedDB), Redux slice, permission hooks, route guards.
- `C:/Users/gurpa/dev/FactoryFlow/src/core/api/client.ts` — axios interceptors that attach `Authorization` + `Company-Code`.
- `C:/Users/gurpa/dev/FactoryFlow/src/modules/ai/*` — the global AI assistant widget.

---

## Related docs
- **Paired frontend doc:** `C:/Users/gurpa/dev/FactoryFlow/docs/modules/platform.md`
- `C:/Users/gurpa/dev/factory_app/docs/permissions_and_groups.md` — full custom-permission inventory + recommended groups.
- `C:/Users/gurpa/dev/factory_app/company/README.md` — company API reference.
- `C:/Users/gurpa/dev/factory_app/sap_client/README.md` + `sap_client/docs/*` — SAP read/write API details.
- `C:/Users/gurpa/dev/factory_app/docs/AI_ASSISTANT_DEEP_ANALYSIS_AND_IMPROVEMENT_PLAN.md` — assistant design notes.
- Team memory: "Group perms vs frontend nav gating", "Cross-company flow boundary", "SAP transaction-notification validations", "Prod server & observability".
