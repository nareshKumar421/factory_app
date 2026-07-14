# Docking Admin — Scan-Skip & Partial-Scan Approvals (backend)

Django app: `docking_admin` · Mounted at `/api/v1/docking-admin/` (`config/urls.py`).

Paired frontend doc: [FactoryFlow `docs/modules/admin.md`](../../../FactoryFlow/docs/modules/admin.md)
(absolute: `C:/Users/gurpa/dev/FactoryFlow/docs/modules/admin.md`).

> This doc is grounded in the code as of this writing. Where an older description
> conflicts with the code, the code wins.

---

## Overview — what it does & who uses it

When a sales-dispatch truck is being loaded ("Docking"), the operator scans a barcode on
every box so the system can prove the full invoiced quantity physically left the factory.
Sometimes that can't happen — the scanner is broken, a bill's boxes are pre-sealed, or only
part of the load is going now and the rest follows later. Rather than let the operator wave
the truck through unchecked, `docking_admin` provides a **request → admin approval** gate:

- **Scan-skip request** (`DockingScanSkipRequest`) — the operator scanned **zero** boxes and
  wants to leave the scanning step without scanning at all.
- **Partial-scan request** (`DockingPartialScanRequest`) — the operator scanned **some but not
  all** boxes and wants to dispatch the partial load.

An admin (the "Docking Approver" role) reviews each request from the Admin module and
approves or rejects it. An **approval** is what lets the load slip past the box-scan half of
the gatepass-readiness gate in `gate_core`; while a request is **pending**, the operator is
hard-gated and cannot advance.

**Actors**

| Actor | Does |
|---|---|
| Docking scan **operator** | Raises scan-skip / partial-scan requests from the scan page; waits for approval. |
| Docking **approver** (admin/manager) | Reviews the queue, approves or rejects with an optional/required note. |

This app owns only the **request + review records and notifications**. It does **not** print
gatepasses, dispatch trucks, or write to SAP. The gate that *consumes* an approval lives in
`gate_core` (`services/sales_dispatch_gatepass.py::get_gatepass_readiness`).

---

## Key concepts & entities

- **Docking entry / load** = `gate_core.SalesDispatchGateOut`. One row per truck's
  sales-dispatch gate-out. May carry **multiple bills** (`SalesDispatchGateOutDocument`), each
  with invoiced line items (`SalesDispatchGateOutItem`). Both request models FK this via
  `sales_dispatch`.
- **Box scan** = `gate_core.SalesDispatchBoxScan`. One row per physically scanned box,
  optionally attributed to a bill + item with a scanned quantity.
- **`DockingScanSkipRequest`** (`models.py`) — the **zero-scan** request. Fields: `company`,
  `sales_dispatch`, `reason`, `status`, `requested_by/at`, `reviewed_by/at`, `review_notes`.
- **`DockingPartialScanRequest`** (`models.py`) — the **some-but-not-all** request. Same shape
  plus `scanned_boxes` and `expected_boxes` snapshots captured when the request was raised.
- **`DockingScanSkipStatus`** — shared `PENDING → APPROVED | REJECTED` lifecycle for both
  models (the partial model deliberately reuses this enum).
- **Approval is per docking entry (load-wide), NOT per bill.** Both models key on
  `sales_dispatch` (the whole truck) and enforce a partial unique constraint
  (`unique_pending_scan_skip_per_dispatch` / `unique_pending_partial_scan_per_dispatch`) so at
  most **one PENDING request per docking entry** exists at a time. One approval covers **every
  bill on that truck**. (The separate per-bill, credit-note "short ship" concept is
  `gate_core.PartialDispatchApproval` — see *Integrations* below. Do not confuse them.)
- **Expected box count** — computed live by `resolved_expected_box_count` (SAP/doc/item stored
  totals, else a per-item `quantity ÷ pack-size` estimate, else 1 box per unit for weight/KGS
  lines). This is the number the scan page shows, so operator view, the readiness lock, and the
  approval queue all agree.
- **Box-scan-optional company** — companies listed in
  `settings.DOCKING_BOX_SCAN_OPTIONAL_COMPANY_CODES` (e.g. Jivo Beverages) don't scan at the
  factory at all; their loads never need a scan and never need one of these approvals.

---

## End-to-end flows

### Flow A — Scan-skip (operator scanned zero boxes)

1. Operator opens the docking barcode-scan step but can't scan. Frontend shows the
   `ScanSkipPanel`. They submit a **reason**.
2. `POST /api/v1/docking-admin/scan-skip-requests/` `{sales_dispatch, reason}`.
   `DockingScanSkipRequestListCreateView.post`:
   - Resolves the docking with `get_sales_dispatch_or_404(request, id)` — across **all the
     user's companies**, not the active `Company-Code` header — then acts on the docking's
     **own** `entry.company`.
   - **400** if `entry.status` is already scan-closed (`GATEPASS_PRINTED`, `PRINT_COMMITTED`,
     `DISPATCHED`, `REJECTED`, `CANCELLED`).
   - If a PENDING request already exists → returns it with **200** (idempotent; no duplicate).
   - Else creates the request (`PENDING`) → **201**, then `notify_approvers_of_new_request`.
3. Approvers with `can_approve_docking_scan_skip` get a push/in-app notification
   (`DOCKING_SCAN_SKIP_REQUESTED`) scoped to the docking's company; a live badge count appears
   in their sidebar.
4. Approver opens **Admin → Docking Approvals**, clicks Approve or Reject.
   - `POST .../scan-skip-requests/<pk>/approve/` or `.../reject/` (`{notes?}`).
   - Guard: **400** if the request is not still `PENDING` ("already approved/rejected").
   - Reject requires a non-empty `notes` (**400** `{"notes": [...]}` otherwise). Approve notes
     are optional.
   - `mark_reviewed(status, reviewer, notes)` stamps `reviewed_by/at`, `review_notes`,
     `updated_by`, then `notify_requester_of_review` tells the operator (`DOCKING_SCAN_SKIP_REVIEWED`).
5. On **approve**, the operator's scan page unlocks: `get_gatepass_readiness` now sees
   `scan_skip_approved = True` and, because there are zero scans, treats box-scans as satisfied.
   On **reject**, the operator must scan boxes to proceed.

### Flow B — Partial-scan (operator scanned some, not all)

1. Operator has scanned ≥1 box but the load still carries unscanned invoiced goods. Frontend
   shows the `PartialScanPanel`; they submit a **reason**.
2. `POST /api/v1/docking-admin/partial-scan-requests/` `{sales_dispatch, reason}`.
   `DockingPartialScanRequestListCreateView.post`:
   - Same cross-company resolution + scan-closed guard as Flow A.
   - Recomputes partial-ness with **`load_scan_status(entry)`** — the *same* function the
     readiness gate uses, so this endpoint can never refuse an approval the gate is demanding
     (that would deadlock the operator). Then:
     - **400 "No boxes are scanned — request a scan skip instead."** if `has_scans` is false.
     - **400 "All boxes are scanned — no partial-dispatch approval is needed."** if not partial.
   - Idempotent existing-PENDING short-circuit → **200**.
   - Else creates the request with server-computed `scanned_boxes`/`expected_boxes` snapshots →
     **201**, then `notify_approvers_of_new_partial_request`.
3–4. Approver review is identical to Flow A but uses `can_approve_docking_partial_scan` and the
   **Partial Dispatch Approvals** queue/page.
5. On **approve**, `get_gatepass_readiness` sees `partial_scan_approved = True` and treats the
   partial box-scan as satisfied. On **reject**, the operator must scan the remaining boxes.

### Flow C — How an approval is consumed (the gate, in `gate_core`)

`get_gatepass_readiness(entry)` (`gate_core/services/sales_dispatch_gatepass.py`) decides
`box_scans_ok`:

```
if box_scan_optional:        box_scans_ok = True          # company doesn't scan at all
elif not has_box_scans:      box_scans_ok = scan_skip_approved     # zero-scan  -> needs scan-skip
elif is_partial_scan:        box_scans_ok = partial_scan_approved  # some/not-all -> needs partial-scan
else:                        box_scans_ok = True          # fully scanned / expected unknown
```

`scan_skip_approved` / `partial_scan_approved` are read via the reverse relations
`entry.scan_skip_requests` / `entry.partial_scan_requests` (`any(status == "APPROVED")`) — the
gate deliberately does **not** import `docking_admin` (would be a circular import). The **print**
endpoint (`SalesDispatchGatepassPrintView`, `views_sales_dispatch.py`) re-runs
`ensure_gatepass_ready` under a `select_for_update` lock, so the same box-scan rule is enforced
server-side at print time, not just in the UI.

---

## Critical business rules & invariants

1. **One PENDING request per docking entry per type.** DB-enforced partial unique constraints.
   Re-POSTing returns the existing PENDING request (200) instead of creating a duplicate.
2. **Approval is load-wide (per `sales_dispatch`).** Covers all bills on the truck; there is no
   per-bill scan approval in this app.
3. **Zero-scan uses scan-skip; some-but-not-all uses partial-scan.** The partial endpoint
   refuses the zero-scan case (400) and the fully-scanned case (400); scan-skip has no such
   scan-count check (see edge cases).
4. **Partial-ness is judged by the SAME rule as the gate** (`load_scan_status`): partial when
   there are scans AND either the load-wide box total is short OR any `(bill, item)` still has
   unscanned invoiced quantity (`has_unscanned_bill_lines`). This prevents a surplus on one bill
   from masking a shortfall on another, and prevents a request/gate deadlock.
5. **Reject requires a note; approve doesn't.** Enforced in both review base views and mirrored
   in the frontend dialog.
6. **Only PENDING requests are reviewable.** Re-reviewing a decided request → 400.
7. **Scan-closed lock.** No new request may be raised once the docking is
   `GATEPASS_PRINTED / PRINT_COMMITTED / DISPATCHED / REJECTED / CANCELLED` (`SCAN_CLOSED_STATUSES`).
8. **Company resolution differs by direction (cross-company boundary):**
   - **Operator reads/writes** (`by-sales-dispatch`, create) resolve the docking across **all
     the user's companies** and act on the record's own company. Correct for aggregated UIs.
   - **Admin queue + review** (`list`, `approve`, `reject`) are scoped to the **active**
     `request.company.company` header. An approver must have the docking's company active to see
     or act on its requests (see edge cases / failure modes).
9. **Permission gating is Django-permission based** (`HasRequiredDjangoPermission`), per HTTP
   method, plus `IsAuthenticated` + `HasCompanyContext`. The `by-sales-dispatch` reads allow any
   of request/view/approve perms (so an operator can poll their own request's status).

---

## Integrations & cross-module boundaries

- **`gate_core` (downstream consumer).** `get_gatepass_readiness` / `ensure_gatepass_ready`
  read this app's approvals to unlock the box-scan requirement. `load_scan_status` and
  `resolved_expected_box_count` are imported *from* `gate_core` *into* this app (views +
  serializer) so partial-ness and box totals are computed identically on both sides.
- **`notifications`.** `services.py` calls `NotificationService.send_notification_by_permission`
  (fan-out to approvers by permission codename, company-scoped) and
  `send_notification_to_user` (back to the requester). Both scan-skip and partial-scan reuse the
  **same** two notification types: `DOCKING_SCAN_SKIP_REQUESTED` and `DOCKING_SCAN_SKIP_REVIEWED`
  (no dedicated partial types). Notification failures are swallowed (best-effort; never block the
  request/review).
- **`company`.** `HasCompanyContext` supplies `request.company`; every request row is stamped
  with a `company` FK.
- **SAP:** none. This app never posts to SAP. SAP doc numbers (`sap_doc_num`) only ride along as
  read-only display context on the docking entry.
- **Do NOT confuse with `gate_core.PartialDispatchApproval`.** That is a **separate** model for
  authorising a **bill** to ship **short** (items physically held back and credited). It keys on
  `document` (one bill), is unique per active document (**per-bill**), and requires a **credit
  note** before print (`ensure_partial_dispatch_cleared`). `docking_admin`'s partial-scan request
  is about **scanning completeness of the whole load**, is **per docking entry**, and needs no
  credit note. Two different gates, two different tables.

---

## Real-world edge cases

Each: **trigger → current behaviour → operator-visible symptom → risk/gap**.

1. **Scanner offline, nothing scanned.**
   → Operator raises a scan-skip with a reason; load hard-locked until approved.
   → Symptom: "Locked — box-scan skip is awaiting admin approval." Approval unlocks Continue.
   → Risk: if no approver has the docking's company active, the request is invisible to them
   (queue is active-company scoped) — approval can stall.

2. **Short load — rest to follow (partial scan).**
   → Some boxes scanned; `load_scan_status` flags partial; operator raises a partial-scan
   request; snapshot `scanned_boxes/expected_boxes` stored.
   → Symptom: yellow partial panel, `3 / 10` count; Continue locked until approved.
   → Risk: the approval is **load-wide** — it waves through *all* remaining unscanned goods on
   every bill, not just the intended short bill. There's no per-bill scoping here.

3. **Scan-skip approved, then operator scans a box.**
   → Approval persists, but the gate now sees `has_box_scans = True` and (if short)
   `is_partial_scan = True`, so it switches to requiring `partial_scan_approved`; the scan-skip
   approval no longer satisfies the gate.
   → Symptom: operator re-locked with "request partial dispatch approval to continue" despite an
   earlier skip approval. → Gap: the skip approval silently stops helping; a *new* partial
   request is needed.

4. **Bill added to the truck after a partial-scan approval (late-booked bill).**
   → The gate only checks `any(status == APPROVED)`; it does **not** re-evaluate against the
   new, larger expected count. The stale approval still returns `partial_scan_approved = True`.
   → Symptom: the newly-added bill's boxes are never required — the load prints/dispatches with
   the extra bill unscanned. → Risk: real; a genuine shortfall can leave the factory unnoticed.

5. **Approver on the wrong company.**
   → A request raised for company B while the approver's active `Company-Code` is A: the list
   queue and both sidebar badges (scoped to `request.company.company`) show nothing for B, and
   `approve/reject` 404 (the request isn't in the active-company queryset).
   → Symptom: approver "sees no pending requests" though the operator is blocked. → Gap: unlike
   the operator reads, the admin queue is **not** cross-company; the approver must switch company.

6. **Fully scanned but operator still asks for partial approval.**
   → `load_scan_status` reports not-partial → **400 "All boxes are scanned — no partial-dispatch
   approval is needed."**
   → Symptom: request refused with a clear message; nothing to approve. → Correct by design.

7. **Over-scan on one bill hides a shortfall on another.**
   → Load-wide box total nets to "complete" (e.g. 20/20) but bill A is short and bill B
   over-scanned. `has_unscanned_bill_lines` still flags partial.
   → Symptom: gate stays locked and the partial request is *accepted* (201) — the two agree, no
   deadlock. → This is the fix for the old blind spot; covered by `PerBillScanCompletenessTests`.

8. **Weight-priced (KGS) line with no PCS pack size.**
   → `_expected_item_boxes` counts 1 box per invoiced unit instead of 0, so the line is visible
   to the total and a zero-scan on it flags the load partial.
   → Symptom: a KGS bill left unscanned correctly blocks the load even if PCS bills are full.

9. **Request raised, then docking reaches gatepass/dispatch before review.**
   → New requests are blocked once scan-closed; but a request raised earlier can still be
   sitting PENDING. Reviewing it succeeds (review has no status guard on the docking), yet the
   approval is moot because the load already advanced past scanning.
   → Symptom: approver approves a request that no longer matters. → Minor housekeeping gap.

10. **Duplicate submit / double-click.**
    → Idempotent: existing PENDING returns 200 with the same row; DB unique constraint backstops
    a race. → Symptom: no duplicate rows, no error.

---

## Failure modes / what can break

- **Notification service down / misconfigured.** Requests and reviews still succeed (best-effort,
  errors logged as `Failed to notify …`). → Symptom: approver never gets a push and must notice
  the sidebar badge or open the queue manually; operator may not learn the decision until they
  reload the scan page.
- **Approver stuck on the wrong company** (edge case 5). → Symptom: "no pending requests" while
  operators are blocked; looks like the feature is broken.
- **Stale load-wide approval** (edge cases 3, 4). → Symptom: a truck dispatches with unscanned
  goods, or an operator is unexpectedly re-locked after scanning post-skip-approval.
- **Missing `entry_no`/context.** Serializer `getattr`-guards every docking field, so a partial
  or unusual docking still serialises (blank strings) rather than 500. Notifications fall back to
  `#<id>` labels.
- **`entry.company` unresolved on the scan page.** `_scan_page_url` falls back to the approvals
  URL if the docking has no `vehicle_entry_id`, so the requester's "review" notification still
  links somewhere sensible.
- **Regression class — passing the wrong object to the resolver.** `get_sales_dispatch_or_404`
  takes the **request** (resolves via `user_company_ids(request)`); passing a `Company` object
  raised `AttributeError → 500`. Locked down by `ScanSkipCompanyResolutionTests`.

---

## Improvement opportunities & known gaps

- **Approval doesn't re-validate against later changes.** A load-wide approval should arguably be
  invalidated (or re-checked) when a bill is added or the expected box count grows after approval
  (edge case 4).
- **Admin queue is active-company only.** Consider resolving the approver's queue/badges across
  all their companies (like the operator reads) so requests can't hide behind the active
  `Company-Code` (edge case 5).
- **Scan-skip has no scan-count guard.** Unlike the partial endpoint, the scan-skip create path
  doesn't verify zero scans; a skip approval on a since-scanned load is simply ignored by the
  gate rather than rejected up front (edge case 3) — a clearer 400 would help.
- **Shared notification types.** Partial-scan reuses the scan-skip notification enum; a dedicated
  `DOCKING_PARTIAL_SCAN_*` type would make approver inboxes clearer.
- **No expiry / audit rollup.** Approved requests live forever; there's no "supersede on
  re-scan" or TTL.

---

## Permissions & roles

Django permissions on the two models (created by migrations `0001`, `0003`; granted to groups by
`0002_create_docking_admin_groups`, `0004_partial_scan_group_permissions`):

| Codename (`docking_admin.…`) | Grants |
|---|---|
| `can_request_docking_scan_skip` | Raise a scan-skip request (operator). |
| `can_view_docking_scan_skip` | See the scan-skip admin queue. |
| `can_approve_docking_scan_skip` | Approve/reject scan-skip requests. |
| `can_request_docking_partial_scan` | Raise a partial-scan request (operator). |
| `can_view_docking_partial_scan` | See the partial-scan admin queue. |
| `can_approve_docking_partial_scan` | Approve/reject partial-scan requests. |

**Seeded groups:**
- **Docking Approver** → view + approve, for **both** scan-skip and partial-scan.
- **Docking Scan Operator** → request (raise) both types.

**Enforcement:** `HasRequiredDjangoPermission` maps permissions per HTTP method
(`required_permissions = {"GET": …VIEW, "POST": …REQUEST/APPROVE}`). `by-sales-dispatch` reads
allow any of request/view/approve (so operators can poll their own request). Notification fan-out
targets the **approve** codename. Frontend nav/route gating mirrors these (see the paired doc).

---

## Developer file map

**Backend (`C:/Users/gurpa/dev/factory_app/docking_admin/`)**
- `models.py` — `DockingScanSkipRequest`, `DockingPartialScanRequest`, `DockingScanSkipStatus`,
  `mark_reviewed`, unique constraints, model permissions.
- `views.py` — 8 `APIView`s: list/create, by-sales-dispatch, approve, reject (× scan-skip +
  partial). `SCAN_CLOSED_STATUSES`; partial-ness check via `load_scan_status`.
- `serializers.py` — read serializers (docking context via `getattr`; partial serializer computes
  live `expected_boxes` with `resolved_expected_box_count`); create + review serializers.
- `services.py` — the four notification helpers + approvals URLs + approve permission codenames.
- `urls.py` — the 8 routes under `/api/v1/docking-admin/`.
- `admin.py` — Django admin for `DockingScanSkipRequest`.
- `migrations/0001…0004` — models, groups, partial model, partial-scan group perms.
- `tests.py` — `ScanSkipCompanyResolutionTests`, `PartialScanApprovalTests`,
  `PerBillScanCompletenessTests`.

**Consumed from `gate_core`**
- `services/sales_dispatch_gatepass.py` — `get_gatepass_readiness`, `ensure_gatepass_ready`,
  `load_scan_status`, `resolved_expected_box_count`, `is_box_scan_optional`,
  `has_unscanned_bill_lines`.
- `views_sales_dispatch.py` — `get_sales_dispatch_or_404`, `SalesDispatchGatepassPrintView`
  (re-check at print), `ensure_partial_dispatch_cleared` (the *other*, per-bill approval).
- `models/sales_dispatch.py` — `SalesDispatchGateOut(Status)`, `…Document`, `…Item`,
  `SalesDispatchBoxScan`, and the separate `PartialDispatchApproval`.

**Key frontend files** (see paired doc for detail)
- `src/modules/admin/pages/DockingScanApprovalsPage.tsx`, `DockingPartialScanApprovalsPage.tsx`
- `src/modules/admin/api/dockingApproval.*`, `partialScanApproval.*`
- `src/modules/admin/components/DockingApprovalsBadge.tsx`, `PartialApprovalsBadge.tsx`
- `src/modules/admin/module.config.tsx`
- `src/modules/gate/pages/customerSalesFlow/SalesDispatchBarcodeScanPage.tsx` (operator side)

---

## Related docs

- Paired frontend doc: `C:/Users/gurpa/dev/FactoryFlow/docs/modules/admin.md`
- `C:/Users/gurpa/dev/factory_app/gate_core/docs/sales_dispatch.md` — the full docking →
  scan → gatepass → dispatch lifecycle this app plugs into (readiness gate, print re-check).
- `C:/Users/gurpa/dev/FactoryFlow/docs/modules/sales-dispatch-docking.md`,
  `docs/modules/dispatch.md`, `docs/modules/gate.md` — operator-side docking journey.
